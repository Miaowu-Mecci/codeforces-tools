"""Opt-in, read-only probes of production Codeforces. No credentials or POSTs."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cf_submit.adapters import ContestAdapter, GymAdapter
from cf_submit.cf_client import CFClient
from cf_submit.cli import set_main
from cf_submit.config import Competition, Store
from cf_submit.exceptions import VerificationRequired


@unittest.skipUnless(os.getenv("CF_SUBMIT_LIVE") == "1", "set CF_SUBMIT_LIVE=1 for real GET requests")
class LiveReadOnlyTests(unittest.TestCase):
    def test_public_contest_api_and_local_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "cf-submit")
            with patch("cf_submit.cli.Store", return_value=store):
                self.assertEqual(set_main(["contest", "4"]), 0)
            self.assertEqual(store.current(), Competition("contest", 4))
            self.assertEqual(store.problemset(store.current()).problems["A"], "Watermelon")

    def test_public_gym_problem_list(self):
        with CFClient() as client:
            try:
                problemset = GymAdapter(Competition("gym", 104172)).fetch_problemset(client)
            except VerificationRequired as exc:
                self.skipTest(str(exc))
        self.assertEqual(problemset.problems["A"], "TreeScript")

    def test_anonymous_gym_api_requires_separate_authentication(self):
        with CFClient() as client:
            response = client.get("/api/contest.standings?contestId=104172", website=False)
        self.assertEqual(response.json().get("status"), "FAILED")
        self.assertIn("authenticated", response.json().get("comment", ""))

    def _require_nonchallenge_page(self, path):
        with CFClient() as client:
            try:
                response = client.get(path)
            except VerificationRequired as exc:
                self.skipTest(str(exc))
        self.assertEqual(response.status_code, 200)

    def test_login_page_reachable(self):
        self._require_nonchallenge_page("/enter?locale=en")

    def test_contest_submit_route_reachable(self):
        self._require_nonchallenge_page(ContestAdapter(Competition("contest", 4)).submit_path)

    def test_gym_submit_route_reachable(self):
        self._require_nonchallenge_page(GymAdapter(Competition("gym", 104172)).submit_path)

    def test_existing_contest_submission_url(self):
        adapter = ContestAdapter(Competition("contest", 4))
        with CFClient() as client:
            response = client.get("/api/contest.status?contestId=4&from=1&count=1", website=False)
        submission_id = response.json()["result"][0]["id"]
        self.assertGreater(submission_id, 0)
        self._require_nonchallenge_page(adapter.submission_url(submission_id))

    def test_existing_gym_submission_url(self):
        # Historical ID listed on the public Gym 104172 status page.
        adapter = GymAdapter(Competition("gym", 104172))
        self._require_nonchallenge_page(adapter.submission_url(395719754))
