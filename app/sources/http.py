"""Polite HTTP for scrapers: obeys robots.txt and crawl-delay, throttles, backs off on 429/5xx.

We never try to get around a block. A 403, a robots disallow, or repeated 429s raise an error;
the source is then marked blocked/failed and Ivan sees it on the Sources page.
"""

import logging
import time
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.core.settings import get_settings

log = logging.getLogger(__name__)

MAX_RETRY_AFTER_S = 120
MAX_REDIRECTS = 3
REDIRECT_CODES = (301, 302, 303, 307, 308)


class SourceError(Exception):
    """Temporary problem (network, 5xx). Worth retrying next run."""


class SourceNotFound(SourceError):
    """The page is gone (404). Normal for an ad that was removed; not a source failure."""


class SourceParseError(SourceError):
    """The page loaded but does not look like we expect: the site layout probably changed."""


class RobotsDisallowed(SourceError):
    """robots.txt forbids this URL for us. Do not fetch."""


class SourceBlocked(SourceError):
    """The site refused us (403 / repeated 429). Back off; never evade."""


class PoliteClient:
    def __init__(
        self,
        min_interval_s: float = 10.0,
        user_agent: str | None = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_retries: int = 3,
    ):
        self.user_agent = user_agent or get_settings().geo_user_agent
        self.http = http or httpx.Client(
            timeout=20, headers={"User-Agent": self.user_agent}, follow_redirects=False
        )
        self._owns_http = http is None
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self._sleep, self._clock = sleep, clock
        self._last: float | None = None
        self._robots: dict[str, RobotFileParser] = {}

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- robots.txt
    def _robots_for(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rp = RobotFileParser()
            try:
                r = self._get_robots(origin)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                elif r.status_code in (401, 403):
                    rp.disallow_all = True
                else:
                    rp.allow_all = True  # no robots.txt = no restrictions
            except httpx.HTTPError:
                raise SourceError(f"could not read robots.txt for {origin}") from None
            self._robots[origin] = rp
        return self._robots[origin]

    def _get_robots(self, origin: str) -> httpx.Response:
        """robots.txt, following up to 5 redirects (the final document applies to origin)."""
        url = f"{origin}/robots.txt"
        for _ in range(5):
            r = self.http.get(url)
            if r.status_code in REDIRECT_CODES and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            return r
        return r

    def allowed(self, url: str) -> bool:
        return self._robots_for(url).can_fetch(self.user_agent, url)

    def _interval_for(self, url: str) -> float:
        delay = self._robots_for(url).crawl_delay(self.user_agent)
        return max(self.min_interval_s, float(delay)) if delay else self.min_interval_s

    def _throttle(self, interval: float) -> None:
        if self._last is not None:
            wait = interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    # ---- fetch
    def get(self, url: str) -> httpx.Response:
        """GET with robots/throttle/backoff. Redirects are followed by hand so that every hop
        (including one to another host) is checked against that host's robots.txt first."""
        for _ in range(MAX_REDIRECTS + 1):
            r = self._get_once(url)
            if r.status_code in REDIRECT_CODES:
                target = r.headers.get("location")
                if not target:
                    raise SourceError(f"redirect without Location from {url}")
                url = urljoin(url, target)
                continue
            return r
        raise SourceError(f"too many redirects (> {MAX_REDIRECTS})")

    def _get_once(self, url: str) -> httpx.Response:
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        interval = self._interval_for(url)
        for attempt in range(1, self.max_retries + 1):
            self._throttle(interval)
            try:
                r = self.http.get(url)
            except httpx.HTTPError as e:
                log.warning("GET %s failed (%s), attempt %d", url, e, attempt)
                if attempt == self.max_retries:
                    raise SourceError(str(e)) from e
                continue
            if r.status_code == 403:
                raise SourceBlocked(f"403 for {url}")
            if r.status_code in (429, 503):
                wait = _retry_after(r)
                log.warning("GET %s -> %d, waiting %ss", url, r.status_code, wait)
                if attempt == self.max_retries:
                    raise SourceBlocked(f"{r.status_code} for {url} after {attempt} tries")
                self._sleep(wait)
                continue
            if r.status_code >= 500:
                if attempt == self.max_retries:
                    raise SourceError(f"{r.status_code} for {url}")
                continue
            if r.status_code == 404:
                raise SourceNotFound(f"404 for {url}")
            if r.status_code in REDIRECT_CODES:
                return r  # get() follows it after the robots check of the new URL
            r.raise_for_status()
            return r
        raise SourceError(f"gave up on {url}")  # pragma: no cover


def _retry_after(r: httpx.Response) -> float:
    try:
        return min(float(r.headers.get("Retry-After", "30")), MAX_RETRY_AFTER_S)
    except ValueError:
        return 30.0
