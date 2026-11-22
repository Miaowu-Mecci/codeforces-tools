import re
from dataclasses import dataclass
from time import monotonic, sleep, time
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup
from requests import Response

from .adapters import CompetitionAdapter
from .auth import verify_account
from .cf_client import CFClient, html, website_url
from .exceptions import (
    AuthenticationError,
    CFError,
    PageStructureChanged,
    SubmissionRejected,
    SubmissionUnknown,
    VerificationRequired,
)
from .language import is_target_language

_POSITIVE_ID = re.compile(r"[1-9][0-9]*")
_SUCCESS = re.compile(r"\b(?:your\s+)?(?:solution|code)\s+(?:has\s+been\s+|was\s+)?submitted successfully\b", re.I)
_NOTICE = re.compile(r'''\bCodeforces\.showMessage\(\s*(["'])(.*?)\1\s*\)''', re.S)


@dataclass(frozen=True)
class PublicSubmission:
    id: int
    created_at: int
    index: str
    language: str


def public_submissions(client: CFClient, adapter: CompetitionAdapter, handle: str) -> list[PublicSubmission]:
    query = urlencode({"contestId": adapter.competition.id, "handle": handle, "from": 1, "count": 20})
    response = client.get(f"/api/contest.status?{query}", website=False, timeout=(2, 3))
    try:
        data = response.json()
        if response.status_code != 200 or data["status"] != "OK" or not isinstance(data["result"], list):
            raise ValueError
        records = []
        for item in data["result"]:
            submission = PublicSubmission(
                item["id"], item["creationTimeSeconds"], item["problem"]["index"], item["programmingLanguage"],
            )
            members = item["author"]["members"]
            if (
                type(submission.id) is not int or submission.id <= 0
                or type(submission.created_at) is not int
                or not isinstance(submission.index, str) or not isinstance(submission.language, str)
                or item["contestId"] != adapter.competition.id
                or not any(member["handle"].casefold() == handle.casefold() for member in members)
            ):
                raise ValueError
            records.append(submission)
        return records
    except (AttributeError, KeyError, TypeError, ValueError):
        raise PageStructureChanged("Codeforces status API did not return verifiable submission records.") from None


def recover_public_submission(
    client: CFClient, adapter: CompetitionAdapter, handle: str, index: str, *,
    baseline: list[PublicSubmission], since: int, last_check: float,
) -> int | None:
    highest = max((record.id for record in baseline), default=0)
    for _ in range(3):
        # The anonymous API requires at least two seconds between requests.
        delay = last_check + 2.1 - monotonic()
        if delay > 0:
            sleep(delay)
        try:
            current = public_submissions(client, adapter, handle)
        except CFError:
            return None
        last_check = monotonic()
        additions = [record for record in current if record.id > highest]
        if not additions:
            continue
        if len(additions) != 1:
            return None
        candidate = additions[0]
        if (
            since <= candidate.created_at <= int(time()) + 2
            and candidate.index == index
            and is_target_language(candidate.language)
        ):
            return candidate.id
        return None
    return None


def safe_text(value: str) -> str:
    return " ".join("".join(char for char in value if char.isprintable() or char.isspace()).split())


def server_errors(page: BeautifulSoup) -> list[str]:
    return list(dict.fromkeys(
        message for tag in page.select(".error, .error__for-form, .error-message, .alert-danger, [role='alert']")
        if not tag.has_attr("hidden")
        and not re.search(r"display\s*:\s*none", tag.get("style", ""), re.I)
        and (message := safe_text(tag.get_text(" ", strip=True)))
        and not _SUCCESS.search(message)
    ))


def reject_with_errors(page: BeautifulSoup, sensitive_values: list[str]) -> None:
    errors = server_errors(page)
    if not errors:
        return
    message = " / ".join(errors)
    for value in sorted(set(sensitive_values), key=len, reverse=True):
        if value:
            message = message.replace(value, "[redacted]")
    raise SubmissionRejected(message[:450])


def success_notice(page: BeautifulSoup) -> bool:
    if any(_SUCCESS.search(tag.get_text(" ", strip=True)) for tag in page.select(
        ".success, .alert-success, .notice, [role='status'], [role='alert']"
    )):
        return True
    return any(
        _SUCCESS.search(match[2])
        for script in page.find_all("script", src=False)
        for match in _NOTICE.finditer(script.string or "")
    )


def _receipt_id(adapter: CompetitionAdapter, url: str) -> int | None:
    path = urlsplit(url).path.rstrip("/")
    prefix = f"{adapter.path}/submission/"
    suffix = path.removeprefix(prefix)
    return int(suffix) if path.startswith(prefix) and _POSITIVE_ID.fullmatch(suffix) else None


def recent_rows(
    adapter: CompetitionAdapter, response: Response, page: BeautifulSoup, handle: str, index: str,
) -> dict[int, bool]:
    if response.status_code != 200 or urlsplit(response.url).path.rstrip("/") != adapter.recent_path:
        raise PageStructureChanged("Codeforces My Submissions page is unavailable.")
    verify_account(page, handle)
    table = page.select_one("table.status-frame-datatable")
    rows = page.select("tr[data-submission-id]")
    if table is None and not rows and not re.search(
        r"\b(?:no submissions|nothing to show)\b", page.get_text(" ", strip=True), re.I
    ):
        raise PageStructureChanged("Codeforces My Submissions table is missing.")

    parsed = {}
    for row in rows:
        raw_id = row["data-submission-id"]
        if not _POSITIVE_ID.fullmatch(raw_id):
            raise PageStructureChanged("Invalid Submission ID in My Submissions.")
        links = row.find_all("a", href=True)
        indices = {
            found.upper() for link in links
            if (found := adapter.index_from_link(response.url, link["href"]))
        }
        user_links = {
            urlsplit(link["href"]).path.rstrip("/").lower() for link in links
            if urlsplit(link["href"]).path.startswith("/profile/")
        }
        matches = index in indices and any(
            is_target_language(cell.get_text(" ", strip=True)) for cell in row.find_all("td")
        ) and (f"/profile/{handle.lower()}" in user_links or not user_links)
        parsed[int(raw_id)] = matches
    if table is not None and not rows and table.select_one("td.id-cell, tr .view-source"):
        raise PageStructureChanged("Codeforces changed the My Submissions row format.")
    return parsed


def read_result(
    client: CFClient, adapter: CompetitionAdapter, response: Response, handle: str,
    index: str, baseline: dict[int, bool], sensitive_values: list[str],
) -> int:
    recent_url = f"https://codeforces.com{adapter.recent_path}"
    try:
        receipt = None
        if response.status_code in (301, 302, 303) and response.headers.get("Location"):
            receipt = _receipt_id(adapter, website_url(response.url, response.headers["Location"]))
        original = response
        response = client.follow_post(response)
        page = html(response)
        if response.status_code in (401, 403) or urlsplit(response.url).path == "/enter":
            raise AuthenticationError("Codeforces denied this submission. Check login and competition permissions.")
        if response.status_code < 500:
            reject_with_errors(page, sensitive_values)
        if response is original and 400 <= response.status_code < 500 and response.status_code != 408:
            raise SubmissionRejected(f"Codeforces rejected the submission (HTTP {response.status_code}).")
        if response.status_code >= 300:
            raise SubmissionUnknown(recent_url)

        if receipt and receipt == _receipt_id(adapter, response.url) and receipt > max(baseline, default=0):
            verify_account(page, handle)
            return receipt

        if success_notice(page):
            if urlsplit(response.url).path.rstrip("/") != adapter.recent_path:
                response = client.get(recent_url)
                page = html(response)
            current = recent_rows(adapter, response, page, handle, index)
            floor = max(baseline, default=0)
            new = [sid for sid in current if sid > floor]
            if len(new) == 1 and current[new[0]]:
                return new[0]
    except VerificationRequired as exc:
        raise SubmissionUnknown(recent_url, str(exc)) from None
    except (AuthenticationError, SubmissionRejected, SubmissionUnknown):
        raise
    except CFError:
        pass
    raise SubmissionUnknown(recent_url)
