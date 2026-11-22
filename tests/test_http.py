"""Integration tests with real requests.Session preparation and a mocked HTTP transport."""
import io
import json
import secrets
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from cf_submit.browser import BrowserReceiptUnknown, BrowserSettings, save_settings
from cf_submit.cf_client import CFClient
from cf_submit.cli import login_main, set_main, submit_main
from cf_submit.config import Competition, Problemset, Store


class MockTransport(BaseAdapter):
    def __init__(self):
        self.queue = []
        self.requests = []

    def queue_response(self, method, url, *, status=200, body="", headers=None):
        self.queue.append((method, url, status, body, headers or {}))

    def send(self, request, **kwargs):
        self.requests.append(request)
        if not self.queue:
            raise AssertionError("Unexpected HTTP request")
        method, url, status, body, headers = self.queue.pop(0)
        parsed = urlsplit(request.url)
        self.assert_request((request.method, parsed.path + ("?" + parsed.query if parsed.query else "")), (method, url))
        self.assert_request(parsed.netloc, "codeforces.com")
        if isinstance(body, Exception):
            raise body
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = status
        response._content = body.encode("utf-8")
        response._content_consumed = True
        response.headers = CaseInsensitiveDict(headers)
        message = Message()
        for name, value in headers.items():
            message[name] = value
        response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=message))
        return response

    @staticmethod
    def assert_request(actual, expected):
        if actual != expected:
            raise AssertionError(f"Expected {expected!r}, got {actual!r}")

    def close(self):
        pass


HANDLE = '<script>Codeforces.handle = "testuser";</script>'
LOGIN = """
<form action="/account/login?flow=one" method="POST">
  <input type="hidden" name="csrf_token" value="secret-login-token">
  <input type="hidden" name="action" value="login_from_form">
  <input name="handleOrEmail">
  <input type="password" name="password">
  <input type="checkbox" name="remember" value="yes">
  <button type="submit">Log in</button>
</form>
"""
CONTEST_JSON = json.dumps({"status": "OK", "result": {
    "contest": {"id": 4, "name": "Round 4"},
    "problems": [{"index": "A", "name": "Watermelon"}, {"index": "B2", "name": "Example"}],
}})
GYM_DASHBOARD = """
<title>Dashboard - Hong Kong - Codeforces</title>
<table class="problems"><tr><th>Index</th><th>Title</th></tr>
<tr><td><a href="/gym/104172/problem/A">A</a></td>
<td><div><a href="/gym/104172/problem/A">TreeScript</a></div>2 s, 512 MB</td></tr></table>
"""


def submission_form(path, competition_id, *, language="C++23 (GCC 14-64, msys2)", multipart=False):
    encoding = 'enctype="multipart/form-data"' if multipart else ""
    return f"""
{HANDLE}
<form method="post" action="{path}/do-submit?csrf_token=browser-token" {encoding}>
    <input type="hidden" name="csrf_token" value="browser-token">
    <input type="hidden" name="contestId" value="{competition_id}">
    <input type="hidden" name="action" value="actualFormAction">
    <input type="hidden" name="dynamicField" value="actualFieldValue">
    <input type="hidden" name="ftaa" value="">
    <select name="submittedProblemIndex"><option value="A">A</option><option value="B2">B2</option></select>
    <select name="programTypeId"><option value="73">GNU G++20</option>
    <option value="999">{language}</option></select>
    <select name="tabSize"><option value="8" selected>8</option></select>
    <textarea name="source"></textarea>
    <input name="sourceFile" type="file">
    <button type="submit">Submit</button>
</form>
"""


def my_submissions(path, *ids, success=False, index="A"):
    rows = "".join(f"""
    <tr data-submission-id="{sid}"><td class="id-cell">{sid}</td>
    <td><a href="/profile/testuser">testuser</a></td>
    <td><a href="{path}/problem/{index}">{index} - Example</a></td>
    <td>C++23 (GCC 14-64, msys2)</td></tr>
    """ for sid in ids)
    notice = '<script>Codeforces.showMessage("Your solution has been submitted successfully");</script>' if success else ''
    return f'{HANDLE}{notice}<table class="status-frame-datatable">{rows}</table>'


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "config")
        self.transport = MockTransport()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("cf_submit.cli.Store", return_value=self.store))
        stack.enter_context(patch("cf_submit.cli.CFClient", side_effect=self.new_client))
        stack.enter_context(redirect_stdout(self.stdout))
        stack.enter_context(redirect_stderr(self.stderr))
        self.source = self.root / "A.cpp"
        self.source.write_bytes(b"// \xe4\xbd\xa0\xe5\xa5\xbd + %\r\nint main() {}\n")

    def tearDown(self):
        self.assertEqual(len(self.transport.queue), 0, "Unused mock HTTP responses")

    def new_client(self):
        session = requests.Session()
        session.mount("https://codeforces.com/", self.transport)
        return CFClient(session)

    def queue(self, *args, **kwargs):
        self.transport.queue_response(*args, **kwargs)

    def login(self):
        self.queue("GET", "/enter?locale=en", body=LOGIN, headers={
            "Set-Cookie": "initial=session-value; Path=/; Secure",
        })
        self.queue("POST", "/account/login?flow=one", status=302, headers={
            "Location": "/", "Set-Cookie": "session=private-cookie; Path=/; Secure; HttpOnly",
        })
        self.queue("GET", "/", body=HANDLE)
        self.queue("GET", "/?locale=en", body=HANDLE)
        with patch("getpass.getpass", return_value="private-password") as prompt:
            self.assertEqual(login_main(["testuser"]), 0, self.stderr.getvalue())
            prompt.assert_called_once()
        self.assertEqual(parse_qs(self.transport.requests[1].body), {
            "csrf_token": ["secret-login-token"],
            "action": ["login_from_form"],
            "handleOrEmail": ["testuser"],
            "password": ["private-password"],
            "remember": ["yes"],
        })
        self.assertIn("initial=session-value", self.transport.requests[1].headers["Cookie"])
        self.assertEqual(self.store.cookie_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("private-password", self.store.cookie_path.read_text())

    def configure(self, kind="contest"):
        cid = 4 if kind == "contest" else 104172
        self.store.save_competition(
            Competition(kind, cid), Problemset("Competition", {"A": "Manual A title", "B2": "Manual B2 title"})
        )
        return f"/{kind}/{cid}", cid

    def submit_form(self, path, cid, **kwargs):
        self.queue("GET", f"{path}/submit?locale=en", body=submission_form(path, cid, **kwargs))

    def test_contest_login_restart_submit_and_posted_source(self):
        self.login()
        path, cid = self.configure()
        self.submit_form(path, cid)
        self.queue("GET", f"{path}/my?locale=en", body=my_submissions(path, 90))
        self.queue("POST", f"{path}/do-submit?csrf_token=browser-token", status=302, headers={
            "Location": f"{path}/my",
        })
        self.queue("GET", f"{path}/my", body=my_submissions(path, 91, 90, success=True))
        with patch("builtins.input", return_value="Y"):
            self.assertEqual(submit_main([str(self.source)]), 0, self.stderr.getvalue())
        self.assertIn("Manual A title", self.stdout.getvalue())
        self.assertIn(f"https://codeforces.com{path}/submission/91", self.stdout.getvalue())
        post = self.transport.requests[-2]
        self.assertIn("session=private-cookie", post.headers["Cookie"])
        fields = parse_qs(post.body, keep_blank_values=True)
        self.assertEqual(fields["source"][0].encode(), self.source.read_bytes())
        self.assertEqual(fields["programTypeId"], ["999"])
        self.assertEqual(fields["dynamicField"], ["actualFieldValue"])
        self.assertEqual(fields["ftaa"], [""])
        self.assertEqual(fields["tabSize"], ["8"])
        self.assertEqual(fields["submittedProblemIndex"], ["A"])

    def test_gym_multipart_direct_receipt_and_explicit_index(self):
        self.login()
        path, cid = self.configure("gym")
        self.submit_form(path, cid, multipart=True)
        self.queue("GET", f"{path}/my?locale=en", body=my_submissions(path))
        self.queue("POST", f"{path}/do-submit?csrf_token=browser-token", status=303, headers={
            "Location": f"{path}/submission/254",
        })
        self.queue("GET", f"{path}/submission/254", body=HANDLE)
        overridden = self.source.rename(self.root / "main.cpp")
        with patch("builtins.input") as prompt:
            self.assertEqual(submit_main([str(overridden), "-p", "b2", "-y"]), 0, self.stderr.getvalue())
            prompt.assert_not_called()
        self.assertIn(f"https://codeforces.com{path}/submission/254", self.stdout.getvalue())
        post = self.transport.requests[-2]
        self.assertTrue(post.headers["Content-Type"].startswith("multipart/form-data;"))
        self.assertIn(overridden.read_bytes(), post.body)
        self.assertIn(b'name="submittedProblemIndex"\r\n\r\nB2', post.body)

    def test_cfset_cache_and_failed_refresh_preserves_active_competition(self):
        self.queue("GET", "/api/contest.standings?contestId=4", body=CONTEST_JSON)
        self.assertEqual(set_main(["contest", "4"]), 0, self.stderr.getvalue())
        self.assertEqual(self.transport.requests[-1].headers["Cookie"], "")
        self.assertEqual(self.store.problemset(Competition("contest", 4)).problems["A"], "Watermelon")
        self.queue("GET", "/gym/104172?locale=en", body=GYM_DASHBOARD)
        self.assertEqual(set_main(["gym", "104172"]), 0, self.stderr.getvalue())
        expected = self.store.current()
        self.assertEqual(expected, Competition("gym", 104172))
        self.queue("GET", "/gym/104172?locale=en", status=403, headers={"cf-mitigated": "challenge"})
        self.assertEqual(set_main(["refresh"]), 1)
        self.assertEqual(self.store.current(), expected)
        self.assertEqual(self.store.problemset(expected).problems["A"], "TreeScript")
        self.queue("GET", "/gym/104172?locale=en", body=GYM_DASHBOARD.replace("TreeScript", "Updated"))
        self.assertEqual(set_main(["refresh"]), 0, self.stderr.getvalue())
        self.assertEqual(self.store.problemset(expected).problems["A"], "Updated")

    def test_browser_mode_uses_public_contest_api_without_waiting_for_extension(self):
        save_settings(self.store, BrowserSettings(secrets.token_urlsafe(32), "testuser", True))
        self.queue("GET", "/api/contest.standings?contestId=4", body=CONTEST_JSON)
        with patch("cf_submit.cli.browser_problemset") as browser:
            self.assertEqual(set_main(["contest", "4"]), 0, self.stderr.getvalue())
            browser.assert_not_called()
        self.assertEqual(self.store.problemset(self.store.current()).problems["A"], "Watermelon")

    def test_browser_mode_falls_back_if_public_contest_api_is_unavailable(self):
        save_settings(self.store, BrowserSettings(secrets.token_urlsafe(32), "testuser", True))
        self.queue("GET", "/api/contest.standings?contestId=4", status=503)
        with patch("cf_submit.cli.browser_problemset", return_value=Problemset("Round 4", {"A": "Watermelon"})) as browser:
            self.assertEqual(set_main(["contest", "4"]), 0, self.stderr.getvalue())
            browser.assert_called_once()
        self.assertIn("Waiting for the paired browser", self.stdout.getvalue())

    def test_browser_receipt_recovery_reads_history_without_resending(self):
        self.configure()
        save_settings(self.store, BrowserSettings(secrets.token_urlsafe(32), "testuser", True))
        api_path = "/api/contest.status?contestId=4&handle=testuser&from=1&count=20"

        def submission(submission_id, created_at, index="A"):
            return {
                "id": submission_id, "contestId": 4, "creationTimeSeconds": created_at,
                "author": {"members": [{"handle": "testuser"}]}, "problem": {"index": index},
                "programmingLanguage": "C++23 (GCC 14-64, msys2)",
            }

        baseline = [submission(80, 975)]
        for additions, expected in (
            ([submission(81, 1000)], 81),
            ([submission(81, 999)], None),
            ([submission(81, 1000, "B2")], None),
            ([submission(82, 1000), submission(81, 1000)], None),
        ):
            with self.subTest(additions=additions):
                for records in (baseline, baseline, additions + baseline):
                    self.queue("GET", api_path, body=json.dumps({"status": "OK", "result": records}))
                self.stdout.seek(0)
                self.stdout.truncate(0)
                with (
                    patch("cf_submit.browser.CFClient", side_effect=self.new_client),
                    patch("cf_submit.browser.time.time", return_value=1000),
                    patch("cf_submit.result.time", return_value=1000),
                    patch("cf_submit.result.sleep"),
                    patch("cf_submit.browser._exchange", side_effect=[
                        {"status": "ok", "language": "C++23 (GCC 14-64, msys2)"},
                        BrowserReceiptUnknown("https://codeforces.com/contest/4/my"),
                    ]) as exchange,
                ):
                    self.assertEqual(submit_main([str(self.source), "-y"]), 0 if expected else 1)
                self.assertEqual([call.args[1] for call in exchange.call_args_list], ["preflight", "submit"])
                display = self.stdout.getvalue()
                self.assertIn("Checking Codeforces submission history", display)
                if expected:
                    self.assertIn(f"https://codeforces.com/contest/4/submission/{expected}", display)
                else:
                    self.assertNotIn("Submission ID", display)

    def test_browser_verification_and_invalid_login_stop(self):
        self.queue("GET", "/enter?locale=en", body=LOGIN.replace(
            "</form>", '<div class="g-recaptcha"></div></form>'
        ))
        self.assertEqual(login_main(["testuser", "private-password"]), 1)
        self.assertFalse(self.store.cookie_path.exists())
        self.queue("GET", "/enter?locale=en", body=LOGIN)
        self.queue("POST", "/account/login?flow=one", body=LOGIN)
        self.queue("GET", "/?locale=en", body="Signed out")
        self.assertEqual(login_main(["testuser", "private-password"]), 1)
        self.assertFalse(self.store.cookie_path.exists())
        self.assertNotIn("private-password", self.stderr.getvalue())
        self.assertIn("additional verification", self.stderr.getvalue())

    def test_dry_run_cancel_and_missing_language_never_post(self):
        self.login()
        path, cid = self.configure()
        self.submit_form(path, cid)
        self.assertEqual(submit_main([str(self.source), "--dry-run"]), 0)
        self.submit_form(path, cid)
        with patch("builtins.input", return_value=""):
            self.assertEqual(submit_main([str(self.source)]), 0)
        self.submit_form(path, cid, language="Clang++23")
        self.assertEqual(submit_main([str(self.source), "-y"]), 1)
        self.assertIn("GNU G++23 is unavailable", self.stderr.getvalue())
        self.assertFalse(any(request.method == "POST" for request in self.transport.requests[4:]))

    def test_edit_after_confirmation_stops_without_post(self):
        self.login()
        path, cid = self.configure()
        self.submit_form(path, cid)
        self.queue("GET", f"{path}/my?locale=en", body=my_submissions(path))

        def change_source(*args):
            self.source.write_text("changed", encoding="utf-8")
            return "y"

        with patch("builtins.input", side_effect=change_source):
            self.assertEqual(submit_main([str(self.source)]), 1)
        self.assertIn("changed after it was read", self.stderr.getvalue())
        self.assertEqual(self.transport.requests[-1].method, "GET")

    def test_no_retry_on_post_timeout_and_debug_excludes_secrets(self):
        self.login()
        path, cid = self.configure()
        self.submit_form(path, cid)
        self.queue("GET", f"{path}/my?locale=en", body=my_submissions(path))
        self.queue("POST", f"{path}/do-submit?csrf_token=browser-token", body=requests.ReadTimeout("private-cookie"))
        self.assertEqual(submit_main([str(self.source), "-y", "--debug"]), 1)
        output = self.stderr.getvalue()
        self.assertIn("Submission Status Unknown", output)
        self.assertNotIn("private-cookie", output)
        self.assertNotIn("browser-token", output)
        self.assertNotIn("Submission ID", self.stdout.getvalue())

    def test_rejects_unsafe_form_actions_and_missing_csrf_before_post(self):
        self.login()
        path, cid = self.configure()
        for body, message in (
            (submission_form(path, cid).replace(f'action="{path}', 'action="https://example.com'),
             "outside https://codeforces.com"),
            (submission_form(path, cid).replace('name="csrf_token"', 'name="changed"'),
             "unambiguous CSRF token"),
        ):
            self.queue("GET", f"{path}/submit?locale=en", body=body)
            self.assertEqual(submit_main([str(self.source), "-y"]), 1)
            self.assertIn(message, self.stderr.getvalue())

    def test_rejected_submission_displays_errors_without_tokens(self):
        self.login()
        path, cid = self.configure()
        self.submit_form(path, cid)
        self.queue("GET", f"{path}/my?locale=en", body=my_submissions(path))
        self.queue("POST", f"{path}/do-submit?csrf_token=browser-token", body=(
            HANDLE + '<span class="error for__source">Source rejected. browser-token private-cookie</span>'
        ))
        self.assertEqual(submit_main([str(self.source), "-y"]), 1)
        self.assertIn("Source rejected.", self.stderr.getvalue())
        self.assertNotIn("browser-token", self.stderr.getvalue())
        self.assertNotIn("private-cookie", self.stderr.getvalue())

    def test_no_guess_for_ambiguous_new_submissions(self):
        self.login()
        path, cid = self.configure()
        self.submit_form(path, cid)
        self.queue("GET", f"{path}/my?locale=en", body=my_submissions(path, 90))
        self.queue("POST", f"{path}/do-submit?csrf_token=browser-token", status=302, headers={
            "Location": f"{path}/my",
        })
        self.queue("GET", f"{path}/my", body=my_submissions(path, 92, 91, 90, success=True))
        self.assertEqual(submit_main([str(self.source), "-y"]), 1)
        self.assertNotIn("Submission ID", self.stdout.getvalue())
        self.assertIn("Submission Status Unknown", self.stderr.getvalue())

    def test_method_preserving_redirect_does_not_send_another_post(self):
        self.login()
        path, cid = self.configure()
        self.submit_form(path, cid)
        self.queue("GET", f"{path}/my?locale=en", body=my_submissions(path))
        self.queue("POST", f"{path}/do-submit?csrf_token=browser-token", status=307, headers={
            "Location": f"{path}/alternate-submit",
        })
        self.assertEqual(submit_main([str(self.source), "-y"]), 1)
        self.assertIn("Submission Status Unknown", self.stderr.getvalue())
        self.assertEqual([r.method for r in self.transport.requests[4:]], ["GET", "GET", "POST"])

    def test_verification_after_post_reports_unknown(self):
        self.login()
        path, cid = self.configure()
        self.submit_form(path, cid)
        self.queue("GET", f"{path}/my?locale=en", body=my_submissions(path))
        self.queue("POST", f"{path}/do-submit?csrf_token=browser-token", status=302, headers={
            "Location": f"{path}/my",
        })
        self.queue("GET", f"{path}/my", status=403, headers={"cf-mitigated": "challenge"})
        self.assertEqual(submit_main([str(self.source), "-y"]), 1)
        self.assertIn("Submission Status Unknown", self.stderr.getvalue())
        self.assertIn("Cloudflare verification", self.stderr.getvalue())
