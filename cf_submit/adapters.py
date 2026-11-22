import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .cf_client import CFClient, ORIGIN, html, website_url
from .config import Competition, Problemset
from .exceptions import (
    AuthenticationError,
    ContestNotFound,
    GymNotFound,
    NetworkError,
    PageStructureChanged,
)
from .forms import login_form
from .problem import valid_index


class CompetitionAdapter:
    def __init__(self, competition: Competition):
        self.competition = competition

    @property
    def submit_path(self) -> str:
        return f"{self.path}/submit"

    @property
    def recent_path(self) -> str:
        return f"{self.path}/my"

    def submission_url(self, submission_id: int) -> str:
        return f"{ORIGIN}{self.path}/submission/{submission_id}"

    def index_from_link(self, page_url: str, target: str) -> str | None:
        try:
            path = urlsplit(website_url(page_url, target)).path
        except PageStructureChanged:
            return None
        prefix = f"{self.path}/problem/"
        index = path.removeprefix(prefix).rstrip("/")
        return index if path.startswith(prefix) and valid_index(index) else None

    def _dashboard(self, client: CFClient) -> tuple[BeautifulSoup, str]:
        response = client.get(f"{self.path}?locale=en")
        page = html(response)
        path = urlsplit(response.url).path.rstrip("/")
        if path == "/enter" or login_form(page) is not None:
            raise AuthenticationError(f"{self.platform} requires access. Run cflogin and check your permissions.")
        if response.status_code in (401, 403, 404) or path == "":
            raise self.unavailable(
                f"{self.platform} {self.competition.id} was not found or you do not have access."
            )
        if response.status_code != 200:
            raise NetworkError(f"{self.platform} dashboard returned HTTP {response.status_code}.")
        if path != self.path:
            raise PageStructureChanged(f"Unexpected {self.platform} dashboard URL.")
        return page, response.url

    def _dashboard_problems(self, page: BeautifulSoup, url: str) -> Problemset:
        table = page.select_one("table.problems")
        if table is None:
            raise PageStructureChanged(f"{self.platform} problems table is missing or hidden.")
        problems = {}
        for row in table.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if not cells:
                continue
            if len(cells) < 2:
                raise PageStructureChanged(f"{self.platform} problem row has an unexpected layout.")
            index_link = cells[0].find("a", href=True)
            index = self.index_from_link(url, index_link["href"]) if index_link else None
            title = next(
                (link.get_text(" ", strip=True) for link in cells[1].find_all("a", href=True)
                 if self.index_from_link(url, link["href"]) == index),
                "",
            ) if index else ""
            if not index or not title or index.upper() in problems:
                raise PageStructureChanged(f"Cannot parse a complete {self.platform} problem row.")
            problems[index.upper()] = title

        title = page.title.get_text(" ", strip=True) if page.title else ""
        name = re.sub(r"^Dashboard\s+-\s+|\s+-\s+Codeforces$", "", title)
        if not name or name == "Codeforces":
            raise PageStructureChanged(f"{self.platform} dashboard is missing the competition name.")
        try:
            return Problemset(name, problems)
        except ValueError:
            raise PageStructureChanged(f"{self.platform} dashboard did not expose a complete problem list.") from None

    def fetch_problemset(self, client: CFClient) -> Problemset:
        raise NotImplementedError


class ContestAdapter(CompetitionAdapter):
    platform = "Codeforces Contest"
    unavailable = ContestNotFound

    @property
    def path(self) -> str:
        return f"/contest/{self.competition.id}"

    def fetch_problemset(self, client: CFClient) -> Problemset:
        public = self.fetch_public_problemset(client)
        if public is not None:
            return public
        return self._dashboard_problems(*self._dashboard(client))

    def fetch_public_problemset(self, client: CFClient) -> Problemset | None:
        response = client.get(
            f"/api/contest.standings?contestId={self.competition.id}", website=False
        )
        if response.status_code not in (200, 400, 401, 403, 404):
            raise NetworkError(f"Codeforces standings API returned HTTP {response.status_code}.")
        try:
            data = response.json()
        except ValueError:
            if response.status_code == 200:
                raise PageStructureChanged("Codeforces standings API did not return JSON.") from None
            return None
        if not isinstance(data, dict):
            raise PageStructureChanged("Codeforces standings API returned invalid data.")
        if data.get("status") == "FAILED":
            return None
        try:
            if response.status_code != 200 or data.get("status") != "OK":
                raise ValueError
            result = data["result"]
            contest = result["contest"]
            if contest["id"] != self.competition.id:
                raise ValueError
            entries = result["problems"]
            if not isinstance(entries, list):
                raise ValueError
            problems = {entry["index"]: entry["name"] for entry in entries}
            if len(problems) != len(entries):
                raise ValueError
            if not problems:
                return None
            return Problemset(contest["name"], problems)
        except (KeyError, TypeError, ValueError):
            raise PageStructureChanged("Codeforces standings API returned an invalid problem list.") from None


class GymAdapter(CompetitionAdapter):
    platform = "Codeforces Gym"
    unavailable = GymNotFound

    @property
    def path(self) -> str:
        return f"/gym/{self.competition.id}"

    def fetch_problemset(self, client: CFClient) -> Problemset:
        # Gym standings require separately signed API credentials. The dashboard uses web cookies.
        return self._dashboard_problems(*self._dashboard(client))


def adapter_for(competition: Competition) -> CompetitionAdapter:
    if competition.kind == "contest":
        return ContestAdapter(competition)
    return GymAdapter(competition)
