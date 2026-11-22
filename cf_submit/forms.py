import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from .cf_client import CFClient, ORIGIN, website_url
from .exceptions import CSRFTokenError, PageStructureChanged, VerificationRequired
from .language import enabled_option


def require_form(form: Tag, page_url: str) -> str:
    if form.get("method", "").lower() != "post":
        raise PageStructureChanged("Codeforces form does not declare a POST method.")
    return website_url(page_url, form.get("action") or page_url)


def _enabled(control: Tag) -> bool:
    return not (control.has_attr("disabled") or control.find_parent("fieldset", disabled=True))


def form_control(form: Tag, name: str, tag: str | None = None) -> Tag:
    matches = [
        control for control in form.find_all(tag or ["input", "select", "textarea"], attrs={"name": name})
        if _enabled(control)
    ]
    if len(matches) != 1:
        raise PageStructureChanged(f"Codeforces form is missing an unambiguous {name} field.")
    return matches[0]


def require_csrf(form: Tag) -> None:
    tokens = [field.get("value", "").strip() for field in form.find_all(
        "input", attrs={"type": "hidden", "name": "csrf_token"}
    ) if _enabled(field)]
    if len(tokens) != 1 or not tokens[0]:
        raise CSRFTokenError("Codeforces form is missing an unambiguous CSRF token.")


def require_no_challenge(form: Tag) -> None:
    if form.select_one(
        ".g-recaptcha, .h-captcha, .cf-turnstile, iframe[src*='captcha'], "
        "[name*='captcha' i], [name*='otp' i], [autocomplete='one-time-code'], "
        "[name='twoFactorCode'], form[action*='twoFactor' i]"
    ):
        raise VerificationRequired("Codeforces requires additional verification. Complete it in your browser.")


@dataclass(frozen=True, repr=False)
class FormPost:
    action: str
    referer: str
    fields: list[tuple[str, str | bytes]]
    multipart: bool
    empty_files: list[str]

    def send(self, client: CFClient):
        headers = {"Origin": ORIGIN, "Referer": self.referer}
        if self.multipart:
            parts = [(name, (None, value)) for name, value in self.fields]
            parts.extend((name, ("", b"", "application/octet-stream")) for name in self.empty_files)
            return client.request("POST", self.action, files=parts, headers=headers)
        return client.request("POST", self.action, data=self.fields, headers=headers)


def prepare_form(form: Tag, page_url: str, overrides: Mapping[str, str | bytes]) -> FormPost:
    require_no_challenge(form)
    action = require_form(form, page_url)
    require_csrf(form)
    encoding = form.get("enctype", "application/x-www-form-urlencoded").lower()
    if encoding not in {"application/x-www-form-urlencoded", "multipart/form-data"}:
        raise PageStructureChanged("Codeforces form declares an unsupported encoding.")
    fields = []
    empty_files = []
    used_overrides = set()
    submitters = []
    for control in form.find_all(["input", "select", "textarea", "button"]):
        if not _enabled(control):
            continue
        if control.has_attr("form"):
            raise PageStructureChanged("Codeforces form contains an unsupported form association.")
        name = control.get("name")
        kind = control.get("type", "submit" if control.name == "button" else "text").lower()
        if (control.name in ("input", "button") and kind == "submit"):
            submitters.append(control)
            continue
        if control.name == "button" or kind in {"reset", "button", "image"}:
            continue
        if not name:
            continue
        required = control.has_attr("required")
        if name in overrides:
            if name in used_overrides or control.has_attr("readonly") or kind == "file":
                raise PageStructureChanged(f"Cannot fill the Codeforces {name} field.")
            used_overrides.add(name)
            fields.append((name, overrides[name]))
        elif kind == "file":
            if required:
                raise PageStructureChanged("Codeforces form requires a file upload.")
            if encoding == "multipart/form-data":
                empty_files.append(name)
            else:
                fields.append((name, ""))
        elif kind in ("checkbox", "radio"):
            if control.has_attr("checked"):
                fields.append((name, control.get("value", "on")))
            elif required:
                raise PageStructureChanged("A required Codeforces form control is unchecked.")
        elif control.name == "select":
            options = [opt for opt in control.find_all("option") if enabled_option(opt)]
            selected = [opt for opt in options if opt.has_attr("selected")]
            if not control.has_attr("multiple"):
                selected = selected[-1:] or options[:1]
            if required and (not selected or any(not opt.get("value", opt.text) for opt in selected)):
                raise PageStructureChanged("A required Codeforces form option is missing.")
            fields.extend((name, opt.get("value", opt.text)) for opt in selected)
        else:
            value = control.get_text() if control.name == "textarea" else control.get("value", "")
            if required and not value:
                raise PageStructureChanged("A required Codeforces form value is missing.")
            fields.append((name, value))
    if used_overrides != set(overrides):
        raise PageStructureChanged("Codeforces form is missing required input fields.")
    if any(control.has_attr("name") or "formaction" in control.attrs or "formmethod" in control.attrs
           or "formenctype" in control.attrs for control in submitters):
        if len(submitters) != 1:
            raise PageStructureChanged("Codeforces form has ambiguous submit buttons.")
        button = submitters[0]
        if any(button.has_attr(attr) for attr in ("formaction", "formmethod", "formenctype")):
            raise PageStructureChanged("Codeforces submit button overrides the form method, action or encoding.")
        if button.has_attr("name"):
            fields.append((button["name"], button.get("value", "")))
    return FormPost(action, page_url, fields, encoding == "multipart/form-data", empty_files)


def login_form(page: BeautifulSoup) -> Tag | None:
    forms = [
        form for form in page.find_all("form")
        if form.select_one("input[name='handleOrEmail']") and form.select_one("input[type='password']")
    ]
    if len(forms) > 1:
        raise PageStructureChanged("Codeforces displays multiple login forms.")
    return forms[0] if forms else None


def signed_in_handle(page: BeautifulSoup) -> str | None:
    for script in page.find_all("script", src=False):
        match = re.search(r'''\bCodeforces\.handle\s*=\s*["']([A-Za-z0-9_.-]+)["']''', script.string or "")
        if match:
            return match[1]
    header = page.select_one("#header")
    if header and header.find("a", href=lambda href: href and urlsplit(href).path == "/logout"):
        for anchor in header.find_all("a", href=True):
            match = re.fullmatch(r"/profile/([A-Za-z0-9_.-]+)/?", urlsplit(anchor["href"]).path)
            if match:
                return match[1]
    return None
