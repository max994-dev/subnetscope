"""Thread-safe TTL cache for subnet scans + history snapshotter + score cache.

A full chain scan is slow (10-60s depending on RPC latency) and writes to
the chain are unnecessary when many tabs/HTMX polls hit the server in a
short window. This cache makes the dashboard feel snappy while keeping
the chain hit rate sane.
"""
from __future__ import annotations

import logging
import os
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

# substrate-interface caches block metadata/runtime info per RPC call inside the
# long-lived Subtensor objects, so RAM creeps up over hours of scans (~100MB/h
# observed). We recycle (close + lazily reconnect) the chain connections every
# RECYCLE_SECONDS to release those caches. Set SSCO_RECYCLE_SECONDS=0 to disable.
_RECYCLE_SECONDS = int(os.getenv("SSCO_RECYCLE_SECONDS", "3600"))


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
        self._initial_fetch_started: bool = False
        self._last_recycle: float = time.time()
        # (phase, done, total) — phase "fetch" during subnet rows, "finalize" post-scan
        self._scan_progress: tuple[str, int, int] = ("idle", 0, 0)

        if state_db_path is None:
            state_db_path = Path(__file__).resolve().parent.parent.parent \
                / "state.db"
        self.db = StateDB(state_db_path)

    @property
    def collector(self) -> Collector:
        return self._collector

    def peek(self) -> ScanResult | None:
        """Return the last scan if any, without blocking or starting I/O."""
        with self._lock:
            return self._cached

    def _do_scan(self) -> ScanResult:
        """Perform one chain scan + score + history snapshot. Blocking."""
        log.info("Rescanning chain (ttl=%ds)", self.ttl_seconds)
        t0 = time.time()

        def _progress(done: int, total: int) -> None:
            with self._lock:
                self._scan_progress = ("fetch", done, max(1, total))

        with self._lock:
            self._scan_progress = ("fetch", 0, 1)
        scan = self._collector.scan(progress_cb=_progress)
        with self._lock:
            self._scan_progress = ("finalize", 1, 2)
        dt = time.time() - t0
        log.info("Scan complete: %d rows in %.1fs", len(scan.rows), dt)
        try:
            record_scan(
                self.db,
                scan,
                sdk_client=self._collector.sdk,
                watch_hotkeys=list(self.cfg.hotkeys.entries or []),
            )
        except Exception:
            log.exception("record_scan failed")
        try:
            scores = score_all(scan.rows)
        except Exception:
            log.exception("score_all failed")
            scores = {}
        for r in scan.rows:
            sb = scores.get(r.netuid)
            r.easy_entry_score = sb.score if sb else None
        with self._lock:
            self._cached = scan
            self._cached_at = time.time()
            self._scores = scores
            self._scan_progress = ("idle", 0, 0)
        self._first_scan_done.set()
        # Periodically recycle chain connections to release substrate-interface's
        # per-block metadata cache (the source of the slow RAM creep). close() drops
        # every Subtensor connection; they reconnect lazily on the next scan. Safe
        # here: the scan is finished + cached and the worker pool is idle.
        if _RECYCLE_SECONDS > 0 and time.time() - self._last_recycle > _RECYCLE_SECONDS:
            log.info("Recycling chain connections to release substrate metadata cache")
            try:
                self._collector.close()
            except Exception:
                log.exception("connection recycle failed")
            self._last_recycle = time.time()
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

    def scan_progress(self) -> dict[str, int | float | str]:
        """Rough progress for the *current* blocking scan (cold start / force).

        Stale-while-revalidate background refreshes do not update this.
        """
        with self._lock:
            phase, done, total = self._scan_progress
        tot = max(1, int(total))
        d = max(0, min(int(done), tot))
        if phase == "fetch":
            pct = min(99, int(100 * d / tot))
        elif phase == "finalize":
            pct = min(99, 85 + int(14 * d / tot))
        else:
            pct = 100
        return {"phase": phase, "done": d, "total": tot, "pct": pct}

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
        with self._lock:
            if self._cached is not None:
                return
            if self._initial_fetch_started:
                return
            self._initial_fetch_started = True

        def _run():
            log.info("prewarm: starting initial scan in background")
            try:
                self.get()
                log.info("prewarm: initial scan complete")
            except Exception:
                log.exception("prewarm: scan failed")
                with self._lock:
                    self._initial_fetch_started = False

        threading.Thread(target=_run, name="prewarm", daemon=True).start()

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
