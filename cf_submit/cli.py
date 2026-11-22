import argparse
import getpass
import logging
import re
import sys
import traceback
import warnings
from pathlib import Path

from .adapters import ContestAdapter, adapter_for
from .auth import login, restore_cookies
from .browser import (
    browser_login,
    browser_preflight,
    browser_problemset,
    browser_submit,
    settings as browser_settings,
    use_http_login,
)
from .cf_client import CFClient
from .config import Competition, Store
from .exceptions import AuthenticationError, CFError, ConfigError, ProblemNotFound
from .problem import Source, problem_index
from .result import safe_text
from .submit import get_submission_form, send_submission


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"[ERROR] Invalid arguments. See {self.prog} --help.\n")


def _parser(prog: str, description: str) -> _Parser:
    parser = _Parser(prog=prog, description=description)
    parser.add_argument("--debug", action="store_true", help="show request status and traceback")
    return parser


def _run(handler, args) -> int:
    logger = logging.getLogger("cf_submit")
    old_level = logger.level
    debug = logging.StreamHandler()
    if args.debug:
        debug.setFormatter(logging.Formatter("[INFO] %(message)s"))
        logger.addHandler(debug)
        logger.setLevel(logging.DEBUG)
    try:
        return handler(args)
    except CFError as exc:
        print(f"[ERROR] {safe_text(str(exc))}", file=sys.stderr)
        if args.debug:
            traceback.print_tb(exc.__traceback__)
            print(f"[INFO] {type(exc).__name__}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"[ERROR] Local I/O failed ({type(exc).__name__}). Check file permissions.", file=sys.stderr)
        if args.debug:
            traceback.print_tb(exc.__traceback__)
        return 1
    except KeyboardInterrupt:
        print("\n[WARNING] Cancelled.", file=sys.stderr)
        return 130
    finally:
        if args.debug:
            logger.removeHandler(debug)
            logger.setLevel(old_level)


def _set(args) -> int:
    store = Store()
    refresh = args.kind == "refresh"
    if refresh:
        if args.id is not None:
            raise ConfigError("Use cfset refresh without a competition ID.")
        competition = store.current()
    else:
        if args.id is None or not re.fullmatch(r"[1-9][0-9]{0,9}", args.id):
            raise ConfigError("Provide a positive competition ID using cfset contest ID or cfset gym ID.")
        competition = Competition(args.kind, int(args.id))
    adapter = adapter_for(competition)
    profile = browser_settings(store)
    if profile is not None and profile.enabled:
        problemset = None
        if isinstance(adapter, ContestAdapter):
            try:
                with CFClient() as client:
                    problemset = adapter.fetch_public_problemset(client)
            except CFError as exc:
                logging.getLogger(__name__).debug("Public contest API unavailable (%s)", type(exc).__name__)
        if problemset is None:
            print("[INFO] Waiting for the paired browser to load the competition problems.", flush=True)
            problemset = browser_problemset(profile, adapter)
    else:
        with CFClient() as client:
            try:
                restore_cookies(client, store, required=False)
            except AuthenticationError:
                print("[WARNING] Saved login is unavailable. Run cflogin for restricted competitions.", file=sys.stderr)
            problemset = adapter.fetch_problemset(client)
    store.save_competition(competition, problemset, refresh=refresh)
    print(f"[OK] {adapter.platform} {competition.id} · {safe_text(problemset.name)} · "
          f"{len(problemset.problems)} cached problems")
    for index, title in problemset.problems.items():
        print(f"  {index}  {safe_text(title)}")
    return 0


def set_main(argv=None) -> int:
    parser = _parser("cfset", "Set a Codeforces competition and cache its problem list.")
    parser.add_argument("kind", choices=("contest", "gym", "refresh"))
    parser.add_argument("id", nargs="?")
    return _run(_set, parser.parse_args(argv))


def _login(args) -> int:
    store = Store()
    if args.browser:
        if args.password is not None or (args.account is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.account)):
            raise AuthenticationError("Use cflogin --browser with an optional Codeforces handle.")
        if args.port is not None and not 1 <= args.port <= 65535:
            raise ConfigError("Browser port must be between 1 and 65535.")
        handle = browser_login(store, args.account, port=args.port, open_browser=args.open_browser)
        print(f"[OK] Signed in as {handle}. Session stays in the local browser.")
        return 0
    if args.port is not None or args.open_browser:
        raise ConfigError("--port and --open-browser require --browser.")
    if not args.account or not args.account.strip():
        raise AuthenticationError("Provide a Codeforces handle or email, or use cflogin --browser.")
    password = args.password
    if password is None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("Codeforces password: ")
        except (EOFError, getpass.GetPassWarning):
            raise AuthenticationError("Cannot read a hidden password. Run cflogin in a terminal.") from None
    else:
        print("[WARNING] Password arguments can appear in shell history and process listings. Use cflogin HANDLE.",
              file=sys.stderr)
    if not password:
        raise AuthenticationError("Account and password are required.")
    with CFClient() as client:
        handle = login(client, store, args.account.strip(), password)
    use_http_login(store)
    print(f"[OK] Signed in as {handle}. Session saved.")
    return 0


def login_main(argv=None) -> int:
    parser = _parser("cflogin", "Sign in to Codeforces with an HTTP or browser session.")
    parser.add_argument("--browser", action="store_true", help="sign in manually in the paired local browser")
    parser.add_argument("--port", type=int, metavar="PORT", help="browser relay port (default: 12450 or saved port)")
    parser.add_argument("--open-browser", action="store_true", help="open the login page in this desktop's default browser")
    parser.add_argument("account", nargs="?", metavar="HANDLE_OR_EMAIL")
    parser.add_argument("password", nargs="?", help="omit to enter the password without echo")
    return _run(_login, parser.parse_args(argv))


def confirm() -> bool:
    while True:
        try:
            answer = input("Submit this solution? [Y/n]: ").strip().lower()
        except EOFError:
            print()
            return False
        if answer == "y":
            return True
        if answer in {"n", ""}:
            return False


def _review_submission(args, adapter, problems, index: str, source: Source, language: str) -> bool:
    print(
        f"[INFO] {adapter.platform} {adapter.competition.id} · {safe_text(problems.name)}\n"
        f"  Problem   {index} · {safe_text(problems.problems[index])}\n"
        f"  Source    {safe_text(str(source.path))}\n"
        f"  Language  {safe_text(language)}\n"
        f"  SHA-256   {source.sha256}"
    )
    if args.dry_run:
        source.verify_unchanged()
        print("[OK] Dry run complete.")
        return False
    if not args.yes and not confirm():
        print("[INFO] Submission cancelled.")
        return False
    return True


def _submit(args) -> int:
    store = Store()
    index = problem_index(args.filename, args.problem)
    source = Source.read(args.filename)
    competition = store.current()
    problems = store.problemset(competition)
    if index not in problems.problems:
        raise ProblemNotFound(f"Problem {index} is not in the current cache. Check -p or run cfset refresh.")
    adapter = adapter_for(competition)

    profile = browser_settings(store)
    if profile is not None and profile.enabled:
        language = browser_preflight(profile, adapter, index)
        if not _review_submission(args, adapter, problems, index, source, language):
            return 0
        with store.lock("submission"):
            submission_id = browser_submit(profile, adapter, index, language, source)
    else:
        with CFClient() as client:
            handle = restore_cookies(client, store)
            submission = get_submission_form(client, adapter, handle, index, source)
            language = submission.language_label
            if not _review_submission(args, adapter, problems, index, source, language):
                return 0
            with store.lock("submission"):
                submission_id = send_submission(client, adapter, submission, source, handle, index)

    print(
        f"[OK] Submission Accepted\n"
        f"  Platform       {adapter.platform}\n"
        f"  Contest ID     {competition.id}\n"
        f"  Problem        {index}\n"
        f"  Language       {safe_text(language)}\n"
        f"  Submission ID  {submission_id}\n"
        f"  Submission URL {adapter.submission_url(submission_id)}"
    )
    return 0


def submit_main(argv=None) -> int:
    parser = _parser("submit", "Submit a C++ source file to the configured Codeforces competition.")
    parser.add_argument("filename", type=Path)
    parser.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    parser.add_argument("-p", "--problem", metavar="INDEX", help="override the problem index")
    parser.add_argument("--dry-run", action="store_true", help="check the source, session and form without submitting")
    return _run(_submit, parser.parse_args(argv))
