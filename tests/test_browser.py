import io
import json
import secrets
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import requests

from cf_submit.browser import PORT, _Job, _Relay, settings
from cf_submit.cli import login_main, set_main, submit_main
from cf_submit.config import Competition, Store


class RelayTests(unittest.TestCase):
    def test_loopback_broker_authenticates_and_claims_once(self):
        job = _Job("preflight", {"path": "/gym/104172", "index": "A"}, 20)
        try:
            relay = _Relay(0, "extension-pairing-secret", job)
        except PermissionError:
            self.skipTest("loopback sockets are restricted")
        with relay:
            thread = threading.Thread(target=relay.serve_forever)
            thread.start()
            url = f"http://127.0.0.1:{relay.server_port}"
            auth = {"Authorization": "Bearer extension-pairing-secret"}

            def claim():
                return requests.get(url + "/job", headers=auth, timeout=3)

            try:
                self.assertEqual(requests.get(url + "/job", timeout=3).status_code, 401)
                response = requests.get(url + "/job", headers=auth | {"Host": "other.invalid"}, timeout=3)
                self.assertEqual(response.status_code, 403)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first, second = sorted(pool.map(lambda _: claim(), range(2)), key=lambda r: r.status_code)
                self.assertEqual([first.status_code, second.status_code], [200, 204])
                self.assertEqual(first.json()["payload"], {"path": "/gym/104172", "index": "A"})
                result = {"id": first.json()["id"], "result": {"status": "ok", "language": "GNU G++23"}}
                wrong_id = result | {"id": "wrong"}
                self.assertEqual(requests.post(url + "/result", json=wrong_id, headers=auth, timeout=3).status_code, 409)
                self.assertEqual(requests.post(url + "/result", json=result, headers=auth, timeout=3).status_code, 204)
                self.assertEqual(requests.post(url + "/result", json=result, headers=auth, timeout=3).status_code, 409)
                self.assertEqual(job.wait(), (result["result"], True))
            finally:
                relay.shutdown()
                thread.join()

    def test_cancelled_job_cannot_be_claimed(self):
        job = _Job("submit", {}, 10)
        self.assertFalse(job.cancel())
        self.assertIsNone(job.claim())


class CLIBrowserTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = Store(self.directory / "cf-submit")
        self.source = self.directory / "b2.cpp"
        self.source.write_bytes(b"// UTF-8 \xe9\xa2\x98\r\nint main() {}\n")

    def test_login_configure_and_submit_use_browser_mode(self):
        with (
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as errors,
            patch("cf_submit.cli.Store", return_value=self.store),
            patch("cf_submit.browser._exchange") as exchange,
            patch("getpass.getpass") as prompt,
            patch("cf_submit.cli.CFClient") as http_client,
        ):
            exchange.side_effect = [
                {"status": "ok", "handle": "testuser"},
                {"status": "ok", "name": "Example Gym", "problems": {"B2": "Example problem"}},
                {"status": "ok", "language": "C++23 (GCC 14-64, msys2)"},
                {"status": "ok", "language": "C++23 (GCC 14-64, msys2)"},
                {"status": "ok", "submissionId": 412},
            ]
            self.assertEqual(login_main(["--browser", "--port", "12580", "testuser"]), 0)
            self.assertTrue(settings(self.store).enabled)
            self.assertEqual(settings(self.store).port, 12580)
            self.assertEqual(set_main(["gym", "104172"]), 0)
            self.assertEqual(self.store.current(), Competition("gym", 104172))
            self.assertEqual(submit_main([str(self.source), "--dry-run"]), 0)
            self.assertEqual(submit_main([str(self.source), "-y"]), 0, errors.getvalue())
            prompt.assert_not_called()
            http_client.assert_not_called()

        self.assertEqual([call.args[1] for call in exchange.call_args_list],
                         ["login", "problems", "preflight", "preflight", "submit"])
        self.assertEqual([call.kwargs["port"] for call in exchange.call_args_list], [12580] * 5)
        self.assertNotIn("source", exchange.call_args_list[-2].args[2])
        self.assertIn("source", exchange.call_args.args[2])
        self.assertIn("https://codeforces.com/gym/104172/submission/412", output.getvalue())
        self.assertFalse(self.store.cookie_path.exists())
        self.assertEqual((self.store.root / "browser.json").stat().st_mode & 0o777, 0o600)

    def test_legacy_pairing_uses_default_and_port_change_requires_login_confirmation(self):
        self.store.write_json(self.store.root / "browser.json", {
            "code": secrets.token_urlsafe(32), "handle": "testuser", "enabled": True,
        })
        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as errors,
            patch("cf_submit.cli.Store", return_value=self.store),
            patch("cf_submit.browser._exchange", side_effect=[
                {"status": "ok"}, {"status": "ok", "handle": "testuser"},
            ]) as exchange,
        ):
            self.assertEqual(settings(self.store).port, PORT)
            self.assertEqual(login_main(["--browser", "--port", "65536"]), 1)
            self.assertIn("between 1 and 65535", errors.getvalue())
            exchange.assert_not_called()
            self.assertEqual(login_main(["--browser", "--port", "12580"]), 1)
            self.assertEqual(settings(self.store).port, PORT)
            self.assertEqual(login_main(["--browser"]), 0)
            self.assertEqual(exchange.call_args.kwargs["port"], PORT)

    def test_desktop_mode_launches_existing_opener_and_does_not_send_credentials(self):
        def exchange(code, kind, payload, *, on_ready, **kwargs):
            self.assertEqual(kind, "login")
            self.assertEqual(payload, {"handle": None, "useOpenTab": True})
            on_ready()
            return {"status": "ok", "handle": "testuser"}

        with (
            redirect_stdout(io.StringIO()),
            patch.dict("os.environ", {"DISPLAY": ":0"}, clear=True),
            patch("cf_submit.cli.Store", return_value=self.store),
            patch("cf_submit.browser._exchange", side_effect=exchange),
            patch("cf_submit.browser.shutil.which", return_value="/usr/bin/xdg-open"),
            patch("cf_submit.browser.subprocess.Popen") as launch,
        ):
            launch.return_value.wait.return_value = 0
            self.assertEqual(login_main(["--browser", "--open-browser"]), 0)
        self.assertEqual(launch.call_args.args[0], ["/usr/bin/xdg-open", "https://codeforces.com/enter?locale=en"])

    def test_desktop_mode_explains_that_no_visible_session_is_available(self):
        with (
            redirect_stderr(io.StringIO()) as errors,
            patch.dict("os.environ", {}, clear=True),
            patch("cf_submit.cli.Store", return_value=self.store),
            patch("cf_submit.browser._exchange") as exchange,
        ):
            self.assertEqual(login_main(["--browser", "--open-browser"]), 1)
        exchange.assert_not_called()
        self.assertIn("No graphical desktop", errors.getvalue())
        self.assertIsNone(settings(self.store))

    def test_source_changed_after_confirmation_never_queued(self):
        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as errors,
            patch("cf_submit.cli.Store", return_value=self.store),
            patch("cf_submit.adapters.ContestAdapter.fetch_public_problemset", return_value=None),
            patch("cf_submit.browser._exchange") as exchange,
        ):
            exchange.side_effect = [
                {"status": "ok", "handle": "testuser"},
                {"status": "ok", "name": "Sample", "problems": {"B2": "Example"}},
                {"status": "ok", "language": "GNU G++23"},
            ]
            self.assertEqual(login_main(["--browser"]), 0)
            self.assertEqual(set_main(["contest", "4"]), 0)

            def confirm_and_edit(prompt):
                self.source.write_text("changed")
                return "y"

            with patch("builtins.input", side_effect=confirm_and_edit):
                self.assertEqual(submit_main([str(self.source)]), 1)
        self.assertEqual(exchange.call_count, 3)
        self.assertIn("Source changed", errors.getvalue())

    def test_browser_error_never_echoes_details(self):
        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as errors,
            patch("cf_submit.cli.Store", return_value=self.store),
            patch("cf_submit.browser._exchange", return_value={"handle": "bad login text", "cookie": "secret"}),
        ):
            self.assertEqual(login_main(["--browser"]), 1)
        self.assertNotIn("secret", errors.getvalue())
        data = json.loads((self.store.root / "browser.json").read_text())
        self.assertFalse(data["enabled"])
        self.assertIsNone(data["handle"])
