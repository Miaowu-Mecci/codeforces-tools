import re
import stat
from http.cookiejar import Cookie

from .cf_client import CFClient, html
from .config import Store
from .exceptions import AuthenticationError, PageStructureChanged, VerificationRequired
from .forms import form_control, login_form, prepare_form, require_no_challenge, signed_in_handle


def _codeforces_cookie(domain: str) -> bool:
    return domain.lower().lstrip(".") == "codeforces.com"


def _cookie_data(cookie: Cookie) -> dict:
    data = vars(cookie).copy()
    data["rest"] = data.pop("_rest")
    return data


def restore_cookies(client: CFClient, store: Store, *, required: bool = True) -> str | None:
    try:
        if stat.S_IMODE(store.cookie_path.stat().st_mode) != 0o600:
            raise AuthenticationError("Cookie permissions must be 600. Run cflogin again.")
        data = store.read_json(store.cookie_path)
        handle = data["handle"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", handle) or data["version"] != 1:
            raise ValueError("Invalid session metadata.")
        cookies = [Cookie(**entry) for entry in data["cookies"]]
        active = [cookie for cookie in cookies if _codeforces_cookie(cookie.domain) and not cookie.is_expired()]
        if not active:
            raise AuthenticationError("Codeforces cookies have expired. Run cflogin again.")
        for cookie in active:
            client.session.cookies.set_cookie(cookie)
        return handle
    except FileNotFoundError:
        if not required:
            return None
        raise AuthenticationError("No saved login. Run cflogin HANDLE.") from None
    except (KeyError, OSError, TypeError, ValueError):
        raise AuthenticationError("Cannot read saved login. Run cflogin again.") from None


def login(client: CFClient, store: Store, account: str, password: str) -> str:
    response = client.get("/enter?locale=en")
    if response.status_code != 200:
        raise AuthenticationError(f"Codeforces login page returned HTTP {response.status_code}.")
    page = html(response)
    require_no_challenge(page)
    form = login_form(page)
    if form is None:
        raise PageStructureChanged("Codeforces login form could not be found.")
    account_field = form_control(form, "handleOrEmail", "input")
    password_field = form_control(form, "password", "input")
    if password_field.get("type", "").lower() != "password" or account_field.get("type", "text") not in {"text", "email"}:
        raise PageStructureChanged("Codeforces changed the login fields.")
    overrides = {"handleOrEmail": account, "password": password}
    remember = form.find("input", attrs={"name": "remember", "type": "checkbox"})
    if remember is not None and not remember.has_attr("disabled"):
        overrides["remember"] = remember.get("value", "on")
    post = prepare_form(form, response.url, overrides)
    response = client.follow_post(post.send(client))
    require_no_challenge(html(response))
    if response.status_code != 200:
        raise AuthenticationError(f"Codeforces login returned HTTP {response.status_code}.")

    check = client.get("/?locale=en")
    if check.status_code != 200:
        raise AuthenticationError(f"Codeforces session check returned HTTP {check.status_code}.")
    page = html(check)
    require_no_challenge(page)
    handle = signed_in_handle(page)
    if handle is None or ("@" not in account and handle.lower() != account.lower()):
        raise AuthenticationError("Codeforces did not confirm login. Check your credentials or the website.")
    cookies = [
        _cookie_data(cookie) for cookie in client.session.cookies
        if _codeforces_cookie(cookie.domain) and not cookie.is_expired()
    ]
    if not cookies:
        raise AuthenticationError("Codeforces did not provide reusable login cookies.")
    store.write_json(store.cookie_path, {"version": 1, "handle": handle, "cookies": cookies})
    return handle


def verify_account(page, handle: str) -> None:
    current = signed_in_handle(page)
    if current and current.lower() == handle.lower():
        return
    if page.select_one("input[autocomplete='one-time-code'], form[action*='twoFactor' i]"):
        raise VerificationRequired("Codeforces requires additional verification. Complete it in your browser.")
    raise AuthenticationError("Codeforces did not confirm the saved account. Run cflogin again or check website access.")
