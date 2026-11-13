"""Polite public-web fetcher: robots.txt respected, LinkedIn hard-blocked, cached, rate-limited."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from . import config

BLOCKED_HOSTS = ("linkedin.com", "lnkd.in", "licdn.com")
CACHE_TTL_S = 24 * 3600
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_last_hit: dict[str, float] = {}


class Blocked(RuntimeError):
    pass


@dataclass
class Page:
    url: str
    status: int
    html: str
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.html)


def is_blocked(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


def _refuse_blocked(request: httpx.Request) -> None:
    """Runs before EVERY request, including each redirect hop, so LinkedIn is never contacted at all."""
    if is_blocked(str(request.url)):
        raise Blocked(f"blocked host (LinkedIn is never fetched): {request.url}")


def _client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": config.USER_AGENT, "Accept-Language": "en"},
                        follow_redirects=True, timeout=httpx.Timeout(45, connect=10),
                        event_hooks={"request": [_refuse_blocked]}, transport=transport)


def robots_allows(url: str) -> bool:
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            with _client() as c:
                r = c.get(base + "/robots.txt")
            if r.status_code >= 400:
                rp = None  # no robots.txt -> allowed
            else:
                rp.parse(r.text.splitlines())
        except httpx.HTTPError:
            rp = None
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(config.USER_AGENT, url)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    return normalize(soup.get_text(" "))


def normalize(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def get(url: str, *, use_cache: bool = True, accept_json: bool = False) -> Page:
    if is_blocked(url):
        raise Blocked(f"blocked host (LinkedIn is never fetched): {url}")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    config.ensure_data_dir()
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    cpath = config.CACHE / f"{key}.json"
    if use_cache and cpath.exists() and time.time() - cpath.stat().st_mtime < CACHE_TTL_S:
        d = json.loads(cpath.read_text())
        return Page(d["url"], d["status"], d["html"], d["text"])
    if not robots_allows(url):
        return Page(url, 999, "", "")  # disallowed by robots.txt
    host = urlparse(url).netloc
    wait = 1.0 - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    try:
        with _client() as c:
            hdrs = {"Accept": "application/json"} if accept_json else {}
            if host == "api.github.com" and os.environ.get("GITHUB_TOKEN"):
                hdrs["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"  # optional, free; raises rate limit
            r = c.get(url, headers=hdrs)
        _last_hit[host] = time.time()
        final = str(r.url)
        if is_blocked(final):
            raise Blocked(f"redirected to blocked host: {final}")
        ctype = r.headers.get("content-type", "")
        html = r.text if ("html" in ctype or "json" in ctype or "text" in ctype) else ""
        text = html if "json" in ctype else (html_to_text(html) if html else "")
        page = Page(final, r.status_code, html, text)
    except httpx.HTTPError:
        page = Page(url, 0, "", "")
    if page.status and page.status < 500:  # never cache network failures / 5xx
        cpath.write_text(json.dumps(page.__dict__))
    return page


def get_json(url: str):
    p = get(url, accept_json=True)
    if not p.ok:
        return None
    try:
        return json.loads(p.html)
    except json.JSONDecodeError:
        return None
