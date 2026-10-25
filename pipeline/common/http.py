"""Polite, cached HTTP for public exchange and regulator sites.

Each download is saved to a known path and never fetched again, which makes
every stage idempotent. A 404 leaves a `.404` marker so it is not retried.
Requests to one host are spaced by `min_interval_s`.
"""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 "
              "Safari/537.36")
_RETRY_STATUS = {429, 500, 502, 503, 504, 530}


class FetchError(RuntimeError):
    pass


class Fetcher:
    def __init__(self, min_interval_s: float = 0.6, timeout_s: float = 30,
                 retries: int = 3,
                 transport: httpx.BaseTransport | None = None):
        self.min_interval_s = min_interval_s
        self.retries = retries
        self._last: dict[str, float] = {}
        self._client = httpx.Client(
            timeout=timeout_s, follow_redirects=True, transport=transport,
            headers={"User-Agent": USER_AGENT,
                     "Accept-Language": "en-US,en;q=0.9"})
        self.requests = 0

    def close(self) -> None:
        self._client.close()

    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        gap = time.monotonic() - self._last.get(host, 0.0)
        if gap < self.min_interval_s:
            time.sleep(self.min_interval_s - gap)
        self._last[host] = time.monotonic()

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait(url)
            try:
                resp = self._client.request(method, url, **kw)
                self.requests += 1
                if resp.status_code not in _RETRY_STATUS:
                    return resp
                last = FetchError(f"HTTP {resp.status_code} for {url}")
            except httpx.HTTPError as e:
                last = e
            time.sleep(min(2 ** attempt, 10))
        raise FetchError(f"{url}: {last}")

    def get(self, url: str, dest: Path | None = None, *,
            referer: str | None = None,
            remember_missing: bool = True) -> bytes | None:
        """GET `url`, caching the body at `dest`. Returns None on a 404.

        Pass `remember_missing=False` for files that may simply not be
        published yet (today's bhavcopy), so a later run tries again.
        """
        if dest is not None:
            if dest.exists():
                return dest.read_bytes()
            if _missing(dest).exists():
                return None
        headers = {"Referer": referer} if referer else {}
        resp = self.request("GET", url, headers=headers)
        if resp.status_code == 404:
            if dest is not None and remember_missing:
                _missing(dest).parent.mkdir(parents=True, exist_ok=True)
                _missing(dest).touch()
            return None
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} for {url}")
        if dest is not None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            tmp.write_bytes(resp.content)
            tmp.replace(dest)
        return resp.content

    def post_form(self, url: str, data: dict, *, referer: str,
                  dest: Path | None = None) -> bytes:
        if dest is not None and dest.exists():
            return dest.read_bytes()
        resp = self.request("POST", url, data=data, headers={
            "Referer": referer, "X-Requested-With": "XMLHttpRequest"})
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} for POST {url}")
        if dest is not None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.content)
        return resp.content


def _missing(dest: Path) -> Path:
    return dest.with_name(dest.name + ".404")
