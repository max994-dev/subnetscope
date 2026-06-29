"""Fetch + render a subnet's GitHub README for the detail page's Readme tab.

Given a subnet's ``github_repo`` URL we resolve owner/repo, pull the raw
README markdown from ``raw.githubusercontent.com`` (no API token, generous
rate limits), render it to sanitised HTML, and rewrite relative image/link
paths to absolute GitHub URLs. Results are cached per-netuid with a 1 h TTL.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import markdown

log = logging.getLogger(__name__)

DEFAULT_TTL = 3600.0
FETCH_TIMEOUT_S = 8.0
# README lives under a handful of conventional names; try raw first (no API
# rate limit) before falling back to the GitHub API which resolves any name.
_CANDIDATES = ["README.md", "readme.md", "Readme.md", "README.MD",
               "README.markdown", "README.rst", "README.txt", "README"]

_REPO_RE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s#?]+)", re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_IFRAME_RE = re.compile(r"<iframe\b[^>]*>.*?</iframe>", re.IGNORECASE | re.DOTALL)
_ON_ATTR_RE = re.compile(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_HREF_RE = re.compile(r"(href\s*=\s*)([\"'])\s*javascript:[^\"']*\2", re.IGNORECASE)


def parse_repo(url: str) -> tuple[str, str] | None:
    m = _REPO_RE.search(url or "")
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


@dataclass
class _Entry:
    html: str | None = None
    error: str | None = None
    source_url: str | None = None
    repo: str | None = None
    fetched_at: float = 0.0


class ReadmeService:
    def __init__(self, ttl: float = DEFAULT_TTL):
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._cache: dict[int, _Entry] = {}
        # markdown.Markdown is stateful/not thread-safe; guard + reset per use.
        self._md_lock = threading.Lock()
        self._md = markdown.Markdown(extensions=[
            "extra", "sane_lists", "smarty", "codehilite", "tables", "nl2br",
        ])

    # ------------------------------------------------------------------ public
    def get(self, netuid: int, force: bool = False) -> dict[str, Any]:
        netuid = int(netuid)
        now = time.time()
        with self._lock:
            ent = self._cache.get(netuid)
            if ent and not force and (now - ent.fetched_at) < self.ttl \
                    and ent.error is None:
                return self._result(netuid, ent, stale=False)

        repo_url = self._github_repo(netuid)
        ent = self._fetch(repo_url)
        with self._lock:
            # Keep a prior good render if a refresh transiently fails.
            prior = self._cache.get(netuid)
            if ent.error and prior and prior.html:
                return self._result(netuid, prior, stale=True)
            self._cache[netuid] = ent
        return self._result(netuid, ent, stale=False)

    # ------------------------------------------------------------------ chain
    def _github_repo(self, netuid: int) -> str | None:
        try:
            from .cache import get_scanner
            scan = get_scanner().get()
        except Exception:  # noqa: BLE001
            return None
        for r in scan.rows:
            if int(r.netuid) == netuid:
                return r.github_repo
        return None

    # ------------------------------------------------------------------ fetch
    def _fetch(self, repo_url: str | None) -> _Entry:
        if not repo_url:
            return _Entry(error="no GitHub repo configured", fetched_at=time.time())
        parsed = parse_repo(repo_url)
        if not parsed:
            return _Entry(error="unrecognised GitHub URL", repo=repo_url,
                          fetched_at=time.time())
        owner, repo = parsed

        raw_md: str | None = None
        used_name: str | None = None
        for name in _CANDIDATES:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{name}"
            text = self._http_get(url)
            if text is not None:
                raw_md, used_name = text, name
                break
        if raw_md is None:
            # Last resort: GitHub API resolves any README name (rate-limited).
            api = self._http_get(
                f"https://api.github.com/repos/{owner}/{repo}/readme",
                accept="application/vnd.github.raw")
            if api is not None:
                raw_md, used_name = api, "README"
        if raw_md is None:
            return _Entry(error="README not found in repo", repo=f"{owner}/{repo}",
                          fetched_at=time.time())

        html = self._render(raw_md, owner, repo)
        src = f"https://github.com/{owner}/{repo}/blob/HEAD/{used_name}"
        return _Entry(html=html, source_url=src, repo=f"{owner}/{repo}",
                      fetched_at=time.time())

    def _http_get(self, url: str, accept: str | None = None) -> str | None:
        req = urllib.request.Request(url, headers={
            "User-Agent": "subnetscope",
            "Accept": accept or "text/plain",
        })
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
                if resp.status != 200:
                    return None
                data = resp.read()
            return data.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            log.debug("readme fetch %s failed: %s", url, e)
            return None

    # ------------------------------------------------------------------ render
    def _render(self, raw_md: str, owner: str, repo: str) -> str:
        with self._md_lock:
            try:
                html = self._md.convert(raw_md)
            finally:
                self._md.reset()
        html = self._sanitize(html)
        return self._absolutize(html, owner, repo)

    @staticmethod
    def _sanitize(html: str) -> str:
        html = _SCRIPT_RE.sub("", html)
        html = _IFRAME_RE.sub("", html)
        html = _ON_ATTR_RE.sub("", html)
        html = _JS_HREF_RE.sub(r"\1\2#\2", html)
        return html

    @staticmethod
    def _absolutize(html: str, owner: str, repo: str) -> str:
        raw_base = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/"
        blob_base = f"https://github.com/{owner}/{repo}/blob/HEAD/"

        def fix_src(m):
            val = m.group(1)
            if re.match(r"^(https?:)?//|^data:|^#", val):
                return m.group(0)
            return f'src="{raw_base}{val.lstrip("./")}"'

        def fix_href(m):
            val = m.group(1)
            if re.match(r"^(https?:)?//|^#|^mailto:|^data:", val):
                return m.group(0)
            return f'href="{blob_base}{val.lstrip("./")}"'

        html = re.sub(r'src\s*=\s*"([^"]*)"', fix_src, html)
        html = re.sub(r'href\s*=\s*"([^"]*)"', fix_href, html)
        return html

    # ------------------------------------------------------------------ result
    def _result(self, netuid: int, ent: _Entry, *, stale: bool) -> dict[str, Any]:
        return {
            "netuid": netuid,
            "repo": ent.repo,
            "html": ent.html,
            "source_url": ent.source_url,
            "error": ent.error,
            "stale": stale,
            "fetched_at": ent.fetched_at or None,
        }


# ─── singleton ───────────────────────────────────────────────────────────────
_service: ReadmeService | None = None


def init_readme(ttl: float = DEFAULT_TTL) -> ReadmeService:
    global _service
    _service = ReadmeService(ttl=ttl)
    return _service


def get_readme_service() -> ReadmeService | None:
    return _service
