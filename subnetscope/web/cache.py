"""Thread-safe TTL cache for subnet scans + history snapshotter + score cache.

A full chain scan is slow (10-60s depending on RPC latency) and writes to
the chain are unnecessary when many tabs/HTMX polls hit the server in a
short window. This cache makes the dashboard feel snappy while keeping
the chain hit rate sane.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..data.collector import Collector, build_collector
from ..types import ScanResult
from .score import ScoreBreakdown, score_all
from .snapshotter import record_scan
from .state_db import StateDB

log = logging.getLogger(__name__)


class CachedScanner:
    """Single shared collector + TTL-cached scan result + state DB.

    Multiple concurrent requests are coalesced: the first request that
    finds a stale cache triggers one rescan; everyone else waits on the
    same lock and reads the fresh result.
    """

    def __init__(self, cfg: Config, ttl_seconds: int = 30,
                 state_db_path: str | Path | None = None):
        self.cfg = cfg
        self.ttl_seconds = max(5, ttl_seconds)
        self._collector: Collector = build_collector(cfg)
        self._lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._cached: ScanResult | None = None
        self._cached_at: float = 0.0
        self._scores: dict[int, ScoreBreakdown] = {}
        self._refreshing: bool = False
        self._first_scan_done = threading.Event()

        if state_db_path is None:
            state_db_path = Path(__file__).resolve().parent.parent.parent \
                / "state.db"
        self.db = StateDB(state_db_path)

    @property
    def collector(self) -> Collector:
        return self._collector

    def _do_scan(self) -> ScanResult:
        """Perform one chain scan + score + history snapshot. Blocking."""
        log.info("Rescanning chain (ttl=%ds)", self.ttl_seconds)
        t0 = time.time()
        scan = self._collector.scan()
        dt = time.time() - t0
        log.info("Scan complete: %d rows in %.1fs", len(scan.rows), dt)
        try:
            record_scan(self.db, scan)
        except Exception:
            log.exception("record_scan failed")
        try:
            scores = score_all(scan.rows)
        except Exception:
            log.exception("score_all failed")
            scores = {}
        with self._lock:
            self._cached = scan
            self._cached_at = time.time()
            self._scores = scores
        self._first_scan_done.set()
        return scan

    def _refresh_in_background(self) -> None:
        """Spawn one and only one background refresh thread."""
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True

        def _run() -> None:
            try:
                self._do_scan()
            except Exception:
                log.exception("background refresh failed")
            finally:
                with self._lock:
                    self._refreshing = False

        threading.Thread(target=_run, name="bg-refresh", daemon=True).start()

    def get(self, force: bool = False) -> ScanResult:
        """Return cached scan; refresh in background if stale.

        Behaviour (stale-while-revalidate):

        * If we have ANY cached scan, return it immediately. If it is older
          than ``ttl_seconds`` (or force=True), spawn a background thread
          to fetch a fresh scan; the next request after it finishes will
          see the new data. This means a click never blocks waiting for
          the chain.
        * If we have NO cache yet (cold start), fall back to the old
          coalescing behaviour: one thread does the scan, others wait.
        """
        now = time.time()
        with self._lock:
            cached = self._cached
            age = now - self._cached_at if cached is not None else None

        if cached is not None:
            if force or (age is not None and age >= self.ttl_seconds):
                self._refresh_in_background()
            return cached

        # Cold start: must block. Coalesce so only one thread scans.
        with self._scan_lock:
            with self._lock:
                if self._cached is not None:
                    return self._cached
            return self._do_scan()

    # ------------------------------------------------------------ helpers

    def scores(self) -> dict[int, ScoreBreakdown]:
        return self._scores

    def cache_age_seconds(self) -> float:
        with self._lock:
            if self._cached_at == 0:
                return -1
            return time.time() - self._cached_at

    def cache_fetched_at(self) -> datetime | None:
        with self._lock:
            if self._cached is None:
                return None
            return self._cached.fetched_at.astimezone()

    # ------------------------------------------------------------ prewarm

    def prewarm_async(self) -> None:
        """Kick off a background thread that runs the first scan so the very
        first user request doesn't have to wait the full 60s+ chain scan."""
        if self._cached is not None:
            return

        def _run():
            log.info("prewarm: starting initial scan in background")
            try:
                self.get()
                log.info("prewarm: initial scan complete")
            except Exception:
                log.exception("prewarm: scan failed")

        t = threading.Thread(target=_run, name="prewarm", daemon=True)
        t.start()

    def close(self) -> None:
        try:
            self._collector.close()
        except Exception:
            log.exception("collector close failed")
        try:
            self.db.close()
        except Exception:
            log.exception("state db close failed")


# Module-level singleton.
_scanner: CachedScanner | None = None


def init_scanner(cfg: Config, ttl_seconds: int = 30,
                 state_db_path: str | Path | None = None) -> CachedScanner:
    global _scanner
    if _scanner is not None:
        _scanner.close()
    _scanner = CachedScanner(cfg, ttl_seconds=ttl_seconds,
                             state_db_path=state_db_path)
    return _scanner


def get_scanner() -> CachedScanner:
    if _scanner is None:
        raise RuntimeError(
            "scanner not initialized - call init_scanner(cfg) first")
    return _scanner
