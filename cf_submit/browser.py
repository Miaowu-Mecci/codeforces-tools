import base64
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .adapters import CompetitionAdapter
from .cf_client import CFClient, ORIGIN
from .config import Problemset, Store
from .exceptions import (
    AuthenticationError,
    CFError,
    CSRFTokenError,
    LanguageNotFound,
    NetworkError,
    PageStructureChanged,
    ProblemNotFound,
    SubmissionRejected,
    SubmissionUnknown,
    VerificationRequired,
)
from .language import is_target_language
from .problem import Source
from .result import public_submissions, recover_public_submission

PORT = 12450
_PAIRING = re.compile(r"[A-Za-z0-9_-]{43}")
_HANDLE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_RESULT_LIMIT = 131_072


class BrowserUnavailable(CFError):
    pass


class BrowserReceiptUnknown(SubmissionUnknown):
    """The browser returned an incomplete result for a claimed submission."""


@dataclass(frozen=True, repr=False)
class BrowserSettings:
    code: str
    handle: str | None = None
    enabled: bool = False
    port: int = PORT


def settings(store: Store) -> BrowserSettings | None:
    try:
        data = store.read_json(store.root / "browser.json")
        code, handle, enabled = data["code"], data["handle"], data["enabled"]
        port = data.get("port", PORT)
        if not _PAIRING.fullmatch(code) or type(enabled) is not bool or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError
        if (handle is not None or enabled) and not _HANDLE.fullmatch(handle):
            raise ValueError
        return BrowserSettings(code, handle, enabled, port)
    except FileNotFoundError:
        return None
    except (TypeError, KeyError, ValueError, OSError):
        raise BrowserUnavailable("Cannot read browser pairing settings.") from None


def save_settings(store: Store, config: BrowserSettings) -> None:
    store.write_json(store.root / "browser.json", {
        "code": config.code, "handle": config.handle, "enabled": config.enabled,
        "port": config.port,
    })


class _Job:
    def __init__(self, kind: str, payload: dict, timeout: int):
        self.id = secrets.token_hex(16)
        self.kind = kind
        self.payload = payload
        self.expires = time.monotonic() + timeout
        self.claimed = False
        self.result: dict | None = None
        self.changed = threading.Condition()

    def claim(self) -> dict | None:
        with self.changed:
            remaining = self.expires - time.monotonic()
            if self.claimed or remaining < 2:
                return None
            self.claimed = True
            self.changed.notify_all()
            return {
                "id": self.id,
                "kind": self.kind,
                "payload": self.payload,
                "remainingMs": int((remaining - 1) * 1000),
            }

    def complete(self, body: object) -> bool:
        with self.changed:
            if (
                not self.claimed or self.result is not None or time.monotonic() >= self.expires
                or not isinstance(body, dict) or body.get("id") != self.id
                or not isinstance(body.get("result"), dict)
            ):
                return False
            self.result = body["result"]
            self.changed.notify_all()
            return True

    def wait(self, on_claim=None) -> tuple[dict | None, bool]:
        notified = False
        with self.changed:
            while self.result is None:
                if self.claimed and not notified and on_claim is not None:
                    on_claim()
                    notified = True
                remaining = self.expires - time.monotonic()
                if remaining <= 0:
                    break
                self.changed.wait(remaining)
            return self.result, self.claimed

    def cancel(self) -> bool:
        with self.changed:
            self.expires = time.monotonic()
            return self.claimed


class _Relay(ThreadingHTTPServer):
    def __init__(self, port: int, code: str, job: _Job):
        self.code = code
        self.job = job
        super().__init__(("127.0.0.1", port), _Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def handle_error(self, request, client_address) -> None:
        # The relay handles source and pairing data. Keep request diagnostics local to each client.
        pass


class _Handler(BaseHTTPRequestHandler):
    server: _Relay

    def log_message(self, format, *args) -> None:
        pass

    def _respond(self, status: int, body: dict | None = None) -> None:
        content = json.dumps(body, ensure_ascii=True).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if content:
            self.wfile.write(content)

    def _authorized(self) -> bool:
        # Reject DNS rebinding and require a pairing secret on every request.
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self._respond(403)
            return False
        token = self.headers.get("Authorization", "").encode("utf-8")
        if not hmac.compare_digest(token, f"Bearer {self.server.code}".encode()):
            self._respond(401)
            return False
        return True

    def do_GET(self) -> None:
        if not self._authorized():
            return
        if self.path != "/job":
            self._respond(404)
            return
        job = self.server.job.claim()
        self._respond(200 if job else 204, job)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if self.path != "/result":
            self._respond(404)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 0 < length <= _RESULT_LIMIT:
                raise ValueError
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            self._respond(400)
            return
        self._respond(204 if self.server.job.complete(body) else 409)


def _exchange(
    code: str, kind: str, payload: dict, *, timeout: int, port: int = PORT, on_ready=None, on_claim=None,
) -> dict:
    recent_url = f"{ORIGIN}{payload['path']}/my" if kind == "submit" else None
    job = _Job(kind, payload, timeout)
    try:
        relay = _Relay(port, code, job)
    except OSError:
        raise BrowserUnavailable(f"Cannot listen on server port {port}. Check for another browser command.") from None
    with relay:
        worker = threading.Thread(target=relay.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        worker.start()
        try:
            try:
                if on_ready is not None:
                    on_ready()
                result, claimed = job.wait(on_claim)
            except KeyboardInterrupt:
                claimed = job.cancel()
                if recent_url and claimed:
                    raise SubmissionUnknown(recent_url) from None
                raise
        finally:
            relay.shutdown()
            worker.join()
    if result is None:
        if recent_url and claimed:
            raise SubmissionUnknown(recent_url)
        if claimed:
            raise BrowserUnavailable("Browser operation timed out. Check the Codeforces tab and try again.")
        raise BrowserUnavailable(
            f"Browser did not connect. Check the extension's local port and access to server port {port}."
        )
    if result.get("status") == "ok":
        return result
    _raise_browser_error(result.get("status"), recent_url)


def _raise_browser_error(status: object, recent_url: str | None) -> None:
    if status == "unknownVerification" and recent_url:
        raise SubmissionUnknown(recent_url, "Codeforces requested verification after the POST. Check the browser tab.")
    if status == "unknown" and recent_url:
        raise BrowserReceiptUnknown(recent_url)
    if status == "verification":
        raise VerificationRequired("Complete verification in the Codeforces browser tab, then retry the command.")
    if status == "authentication":
        raise AuthenticationError("Browser session is missing or uses another account. Run cflogin --browser.")
    if status == "language":
        raise LanguageNotFound("The browser form has no unique GNU G++23 option. No submission was sent.")
    if status == "csrf":
        raise CSRFTokenError("The browser form did not expose a valid CSRF field.")
    if status == "problem":
        raise ProblemNotFound("The requested problem is unavailable in the Codeforces form.")
    if status == "access":
        raise AuthenticationError("Codeforces denied browser access. Check this competition's permissions.")
    if status == "changed":
        raise PageStructureChanged("The browser form or language changed. Run submit again to review it.")
    if status == "rejected":
        raise SubmissionRejected("Codeforces rejected the submission. See the browser tab for the returned form.")
    if status == "network":
        raise NetworkError("The local browser could not load the Codeforces page.")
    raise PageStructureChanged("The local browser could not parse the expected Codeforces page.")


def _open_desktop_login(opener: str) -> None:
    try:
        browser = subprocess.Popen(
            [opener, f"{ORIGIN}/enter?locale=en"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if browser.wait(timeout=1) == 0:
            return
    except subprocess.TimeoutExpired:
        # Browser applications may keep the launcher process alive until they close.
        return
    except OSError:
        pass
    raise BrowserUnavailable("Could not open the desktop browser. Open it manually and run cflogin --browser.")


def browser_login(
    store: Store, account: str | None, *, port: int | None = None, open_browser: bool = False,
) -> str:
    on_ready = None
    if open_browser:
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise BrowserUnavailable("No graphical desktop found. Open the paired browser manually.")
        opener = shutil.which("xdg-open")
        if opener is None:
            raise BrowserUnavailable("xdg-open is unavailable. Open the paired browser manually.")
        on_ready = partial(_open_desktop_login, opener)

    with store.lock("browser-login"):
        current = settings(store)
        if current is None:
            current = BrowserSettings(secrets.token_urlsafe(32))
            save_settings(store, current)

        listen_port = current.port if port is None else port
        payload = {"handle": account}
        if open_browser:
            payload["useOpenTab"] = True
        print(f"[INFO] Pairing code for the local extension: {current.code}", flush=True)
        print(f"[INFO] Waiting for the browser on 127.0.0.1:{listen_port}.", flush=True)
        response = _exchange(
            current.code, "login", payload, timeout=275, port=listen_port, on_ready=on_ready,
            on_claim=lambda: print("[INFO] Finish logging in in the Codeforces browser tab.", flush=True),
        )
        handle = response.get("handle")
        if not isinstance(handle, str) or not _HANDLE.fullmatch(handle):
            raise AuthenticationError("The browser did not verify a Codeforces account.")
        if account and handle.casefold() != account.casefold():
            raise AuthenticationError("Signed-in browser account does not match the requested handle.")
        save_settings(store, BrowserSettings(current.code, handle, True, listen_port))
        return handle


def use_http_login(store: Store) -> None:
    with store.lock("browser-login"):
        current = settings(store)
        if current is not None and current.enabled:
            save_settings(store, BrowserSettings(current.code, current.handle, False, current.port))


def browser_problemset(profile: BrowserSettings, adapter: CompetitionAdapter) -> Problemset:
    response = _exchange(
        profile.code, "problems", {"path": adapter.path, "handle": profile.handle}, timeout=75, port=profile.port,
    )
    try:
        return Problemset(response["name"], response["problems"])
    except (KeyError, ValueError):
        raise PageStructureChanged("Browser did not return a complete competition problem list.") from None


def browser_preflight(profile: BrowserSettings, adapter: CompetitionAdapter, index: str) -> str:
    response = _exchange(
        profile.code, "preflight", {"path": adapter.path, "handle": profile.handle, "index": index},
        timeout=75, port=profile.port,
    )
    language = response.get("language")
    if not isinstance(language, str) or not is_target_language(language):
        raise LanguageNotFound("The local browser did not confirm a GNU G++23 option.")
    return language


def browser_submit(
    profile: BrowserSettings, adapter: CompetitionAdapter, index: str, language: str, source: Source,
) -> int:
    with CFClient() as client:
        baseline = None
        if adapter.competition.kind == "contest" and profile.handle:
            try:
                baseline = public_submissions(client, adapter, profile.handle)
            except CFError:
                pass
        last_check = time.monotonic()
        source.verify_unchanged()
        since = int(time.time())
        try:
            response = _exchange(profile.code, "submit", {
                "path": adapter.path, "handle": profile.handle, "index": index,
                "language": language, "sha256": source.sha256,
                "source": base64.b64encode(source.content).decode("ascii"),
            }, timeout=105, port=profile.port)
        except BrowserReceiptUnknown:
            if baseline is not None and profile.handle:
                print("[INFO] Checking Codeforces submission history for the new record.", flush=True)
                recovered = recover_public_submission(
                    client, adapter, profile.handle, index, baseline=baseline, since=since, last_check=last_check,
                )
                if recovered is not None:
                    return recovered
            raise
    submission_id = response.get("submissionId")
    if type(submission_id) is not int or submission_id < 1:
        raise SubmissionUnknown(f"{ORIGIN}{adapter.recent_path}")
    return submission_id
