import logging
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .exceptions import NetworkError, PageStructureChanged, VerificationRequired

ORIGIN = "https://codeforces.com"
_LOG = logging.getLogger(__name__)
_REDIRECTS = {301, 302, 303, 307, 308}


def website_url(base: str, target: str) -> str:
    try:
        parts = urlsplit(urljoin(base, target))
    except ValueError:
        raise PageStructureChanged("Codeforces returned a malformed URL.") from None
    if parts.scheme != "https" or parts.netloc != "codeforces.com":
        raise PageStructureChanged("Codeforces returned a URL outside https://codeforces.com.")
    return parts._replace(fragment="").geturl()


def html(response: requests.Response) -> BeautifulSoup:
    return BeautifulSoup(response.content, "html.parser", from_encoding="utf-8")


def require_no_verification(response: requests.Response) -> None:
    if response.headers.get("cf-mitigated", "").lower() == "challenge" or (
        response.status_code == 403
        and (b"/cdn-cgi/challenge-platform/" in response.content or b"<title>Just a moment" in response.content)
    ):
        raise VerificationRequired(
            "Codeforces requires Cloudflare verification. Complete it in your browser or use the website."
        )


class CFClient:
    def __init__(self, session: requests.Session | None = None):
        self.session = session if session is not None else requests.Session()

    def __enter__(self) -> "CFClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.session.close()

    def request(self, method: str, url: str, *, timeout=(8, 20), **kwargs) -> requests.Response:
        url = website_url(ORIGIN, url)
        try:
            response = self.session.request(
                method, url, timeout=timeout, allow_redirects=False, **kwargs
            )
        except requests.RequestException as exc:
            # Request exceptions may include a form action URL with a token.
            raise NetworkError(f"Codeforces request failed ({type(exc).__name__}).") from None
        _LOG.debug("%s Codeforces -> HTTP %d", method, response.status_code)
        require_no_verification(response)
        return response

    def get(self, url: str, *, website: bool = True, timeout=(8, 20)) -> requests.Response:
        url = website_url(ORIGIN, url)
        headers = {"Cookie": ""} if not website else {"Accept-Language": "en"}
        for _ in range(6):
            response = self.request("GET", url, headers=headers, timeout=timeout)
            if response.status_code not in _REDIRECTS:
                return response
            if not response.headers.get("Location"):
                raise PageStructureChanged("Codeforces returned a redirect without a target.")
            url = website_url(response.url, response.headers["Location"])
        raise PageStructureChanged("Codeforces returned too many redirects.")

    def follow_post(self, response: requests.Response) -> requests.Response:
        if response.status_code not in _REDIRECTS:
            return response
        if response.status_code not in (301, 302, 303) or not response.headers.get("Location"):
            raise PageStructureChanged("Codeforces returned an unsupported redirect after the POST.")
        return self.get(website_url(response.url, response.headers["Location"]))
