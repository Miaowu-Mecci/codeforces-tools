from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from .adapters import CompetitionAdapter
from .auth import verify_account
from .cf_client import CFClient, ORIGIN, html
from .exceptions import (
    AuthenticationError,
    NetworkError,
    PageStructureChanged,
    ProblemNotFound,
    SubmissionUnknown,
    VerificationRequired,
)
from .forms import FormPost, form_control, login_form, prepare_form, require_no_challenge
from .language import choose_language, enabled_option
from .problem import Source
from .result import read_result, recent_rows, reject_with_errors


@dataclass(frozen=True)
class SubmissionForm:
    post: FormPost
    language_label: str


def get_submission_form(
    client: CFClient, adapter: CompetitionAdapter, handle: str, index: str, source: Source
) -> SubmissionForm:
    response = client.get(f"{adapter.submit_path}?locale=en")
    page = html(response)
    require_no_challenge(page)
    if response.status_code in (401, 403) or login_form(page) is not None:
        raise AuthenticationError(f"{adapter.platform} denied access to the submission page. Check login and permissions.")
    if response.status_code == 404:
        raise adapter.unavailable(f"{adapter.platform} submission page was not found.")
    if response.status_code != 200:
        raise NetworkError(f"{adapter.platform} submission page returned HTTP {response.status_code}.")
    verify_account(page, handle)
    if urlsplit(response.url).path.rstrip("/") != adapter.submit_path:
        raise PageStructureChanged(f"Codeforces redirected away from the {adapter.platform} submission page.")

    forms = [
        form for form in page.find_all("form")
        if form.find(["select", "textarea", "input"], attrs={"name": ["programTypeId", "source"]})
    ]
    if len(forms) != 1:
        if not forms:
            reject_with_errors(page, [cookie.value for cookie in client.session.cookies if cookie.value])
        raise PageStructureChanged(f"Cannot find an unambiguous {adapter.platform} submission form.")

    form = forms[0]
    problem = form_control(form, "submittedProblemIndex", "select")
    matches = {
        option["value"] for option in problem.find_all("option", value=True)
        if enabled_option(option) and option["value"].upper() == index
    }
    if len(matches) != 1:
        raise ProblemNotFound(f"Problem {index} is unavailable or ambiguous in the submission form. Check cfset and access.")
    language = form_control(form, "programTypeId", "select")
    lang_id, lang_label = choose_language(language)
    form_control(form, "source", "textarea")
    if contest_field := form.find("input", attrs={"name": "contestId"}):
        if contest_field.get("value") != str(adapter.competition.id):
            raise PageStructureChanged("The submission form's competition ID differs from cfset.")
    post = prepare_form(form, response.url, {
        "submittedProblemIndex": next(iter(matches)),
        "programTypeId": lang_id,
        "source": source.content,
    })
    return SubmissionForm(post, lang_label)


def send_submission(
    client: CFClient, adapter: CompetitionAdapter, submission: SubmissionForm,
    source: Source, handle: str, index: str,
) -> int:
    recent_url = f"{ORIGIN}{adapter.recent_path}"
    response = client.get(f"{recent_url}?locale=en")
    baseline = recent_rows(adapter, response, html(response), handle, index)
    source.verify_unchanged()
    sensitive = [cookie.value for cookie in client.session.cookies if cookie.value]
    sensitive.extend(value for _, value in parse_qsl(urlsplit(submission.post.action).query) if value)
    sensitive.extend(
        value for name, value in submission.post.fields
        if name not in {"action", "source", "submittedProblemIndex", "programTypeId", "contestId"}
        and isinstance(value, str) and (name == "csrf_token" or len(value) >= 6)
    )
    sensitive.extend(
        line.strip() for line in source.content.decode("utf-8").splitlines() if len(line.strip()) >= 8
    )
    try:
        response = submission.post.send(client)
    except VerificationRequired:
        raise
    except (NetworkError, KeyboardInterrupt):
        raise SubmissionUnknown(recent_url) from None
    try:
        return read_result(client, adapter, response, handle, index, baseline, sensitive)
    except KeyboardInterrupt:
        raise SubmissionUnknown(recent_url) from None
