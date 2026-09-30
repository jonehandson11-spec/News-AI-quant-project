"""Restricted FT session with a mutable CookieJar and robots-aware requests."""
import os
import re
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = "NewsResearchCollector/1.0"
COOKIE_KEYS = (
    "FTSession_s", "FTSession", "ft-access-decision-policy", "FTConsent",
    "FTCookieConsentGDPR", "consentDate", "consentUUID", "usnatUUID",
)


class StopCollection(RuntimeError):
    """The reason is a fixed, safe code, never a remote exception message."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def effective_cookies(raw):
    result = {}
    for name in COOKIE_KEYS:
        match = re.search(r"(?:^|[\s;])" + re.escape(name) + r"=([^;\r\n]+)", raw)
        if not match:
            continue
        value = match.group(1).strip()
        if (not value or any(ord(c) < 32 or ord(c) > 126 for c in value)
                or value.upper().startswith("YOUR_") or value.endswith("_HERE")
                or value == "SUBSCRIPTION_POLICY"):
            continue
        result[name] = value
    return result


def validate_url(url):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname == "www.ft.com"
                 and parsed.port in (None, 443) and not parsed.username
                 and not parsed.password)
    except ValueError:
        valid = False
    if not valid:
        raise StopCollection("unsafe_destination")
    if re.match(r"^/(?:subscribe|subscription|products)(?:/|$)", parsed.path, re.I):
        raise StopCollection("subscription_required")
    if re.match(r"^/(?:login|signin|sign-in)(?:/|$)",
                parsed.path, re.I):
        raise StopCollection("auth_required")


class FTSession(requests.Session):
    def __init__(self, raw_cookie=None, delay=1.2):
        super().__init__()
        self.delay = max(1.0, delay)
        self._last_request = None
        self._robots = None
        self.headers.update({"User-Agent": USER_AGENT,
                             "Accept-Language": "en-GB,en;q=0.9"})
        raw = os.environ.get("FT_COOKIE", "") if raw_cookie is None else raw_cookie
        for name, value in effective_cookies(raw).items():
            self.cookies.set_cookie(requests.cookies.create_cookie(
                name, value, domain=".ft.com", path="/", secure=True))

    def has_login_cookie(self):
        return any(c.name in ("FTSession", "FTSession_s") and c.value
                   and not c.is_expired() for c in self.cookies)

    def send(self, request, **kwargs):
        validate_url(request.url)
        return super().send(request, **kwargs)

    def request(self, method, url, **kwargs):
        if method.upper() != "GET":
            raise StopCollection("unsupported_method")
        kwargs.pop("allow_redirects", None)
        for _ in range(6):
            validate_url(url)
            if (self._robots is not None and urlsplit(url).path != "/robots.txt"
                    and not self._robots.can_fetch(USER_AGENT, url)):
                raise StopCollection("robots_disallowed")
            if self._last_request is not None:
                time.sleep(max(0, self.delay - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            response = super().request(method, url, allow_redirects=False, **kwargs)
            if response.status_code in (401, 402, 403, 429):
                reason = {401: "auth_expired", 402: "subscription_required",
                          403: "access_denied", 429: "rate_limited"}[response.status_code]
                response.close()
                raise StopCollection(reason)
            if response.is_redirect or response.is_permanent_redirect:
                destination = urljoin(url, response.headers.get("Location", ""))
                response.close()
                if urlsplit(destination).hostname == "subs.ft.com":
                    raise StopCollection("subscription_required")
                if urlsplit(destination).hostname == "accounts.ft.com":
                    raise StopCollection("auth_required")
                validate_url(destination)
                # Session.send has already applied Set-Cookie to this CookieJar.
                # Do not reload the original credential or reuse a Cookie header.
                url = destination
                continue
            return response
        raise StopCollection("redirect_limit")

    def check_robots(self, url):
        validate_url(url)
        if self._robots is None:
            try:
                with self.get("https://www.ft.com/robots.txt", timeout=30) as response:
                    if response.status_code in (404, 410):
                        text = "User-agent: *\nAllow: /"
                    elif response.status_code >= 400:
                        raise StopCollection("robots_unavailable")
                    else:
                        text = response.text
                        if "<html" in text.lower() or len(text) > 1_000_000:
                            raise StopCollection("robots_unavailable")
                parser = RobotFileParser()
                parser.parse(text.splitlines())
                self._robots = parser
                self.delay = max(self.delay, parser.crawl_delay(USER_AGENT) or 0)
            except requests.RequestException:
                raise StopCollection("robots_unavailable") from None
        if not self._robots.can_fetch(USER_AGENT, url):
            raise StopCollection("robots_disallowed")

    def fetch(self, url):
        self.check_robots(url)
        with self.get(url, timeout=30) as response:
            response.raise_for_status()
            if len(response.content) > 8_000_000:
                raise ValueError("response_too_large")
            return response.text
