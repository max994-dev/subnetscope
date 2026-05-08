"""Lightweight per-netuid burn-fee fetcher with a 12-second TTL cache.

The full scanner scans *all* subnets every 120 s.  When a user is watching
a detail page in real time they want the burn fee updated every ~12 s (one
Bittensor block).  Fetching the full metagraph that frequently is too heavy;
fetching just *one* subnet's burn cost takes ~1 s via a substrate call.

This module:
  * Keeps a short-TTL cache per netuid (default 12 s).
  * Re-uses the SDKClient's main-thread subtensor connection when possible.
  * Falls back to the full-scan cached value if the lightweight call fails.
  * Coalesces concurrent requests: only one thread per netuid queries the
    chain at a time.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_TTL = 12.0  # seconds


@dataclass
class _Entry:
    tao: float
    fetched_at: float = field(default_factory=time.time)


class BurnLiveCache:
    def __init__(self, ttl: float = _TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        # Per-netuid: entry + a "fetching" flag to coalesce concurrent callers
        self._data: dict[int, _Entry] = {}
        self._fetching: set[int] = set()
        self._sdk_client = None   # set by init_burn_live

    def _get_subtensor(self):
        """Return the main-thread subtensor from the SDKClient if available."""
        if self._sdk_client is None:
            return None
        try:
            return self._sdk_client.subtensor
        except Exception:
            return None

    def _fetch(self, netuid: int) -> float | None:
        """Do a lightweight single-subnet burn-fee query."""
        sub = self._get_subtensor()
        if sub is None:
            return None
        # Import the private helper from the SDK module.
        try:
            from ..data.sdk import _safe_recycle
            v = _safe_recycle(sub, netuid)
            return v if v > 0 else None
        except Exception:
            log.debug("burn-live: _safe_recycle failed for netuid=%d", netuid,
                      exc_info=True)
        return None

    def _fallback_from_scanner(self, netuid: int) -> float | None:
        """Read the full-scan cached value as fallback."""
        try:
            from .cache import get_scanner
            scanner = get_scanner()
            scan = scanner.get()
            for r in scan.rows:
                if r.netuid == netuid:
                    return r.recycle_tao
        except Exception:
            pass
        return None

    def get(self, netuid: int) -> dict:
        """Return {tao, ts_iso, from_cache, stale} for netuid.

        Always returns quickly — triggers a background refresh if stale.
        """
        now = time.time()

        with self._lock:
            entry = self._data.get(netuid)
            if entry and (now - entry.fetched_at) < self.ttl:
                # Fresh cached value.
                return _make_result(entry.tao, entry.fetched_at, from_cache=True)

            # Stale or missing — trigger background refresh if not already running.
            if netuid not in self._fetching:
                self._fetching.add(netuid)
                threading.Thread(
                    target=self._bg_refresh, args=(netuid,),
                    name=f"burn-live-{netuid}", daemon=True,
                ).start()

            if entry:
                # Return the stale value while refresh is in flight.
                return _make_result(entry.tao, entry.fetched_at,
                                    from_cache=True, stale=True)

        # No cached value at all — block briefly (up to 4 s) waiting for the
        # background thread to complete, then fall back to the scanner.
        for _ in range(20):
            time.sleep(0.2)
            with self._lock:
                entry = self._data.get(netuid)
                if entry:
                    return _make_result(entry.tao, entry.fetched_at,
                                        from_cache=False)

        # Last resort: full-scan cache
        v = self._fallback_from_scanner(netuid) or 0.0
        return _make_result(v, now, from_cache=True, stale=True)

    def _bg_refresh(self, netuid: int) -> None:
        try:
            v = self._fetch(netuid)
            if v is None:
                v = self._fallback_from_scanner(netuid)
            if v is not None:
                with self._lock:
                    self._data[netuid] = _Entry(tao=v)
        except Exception:
            log.exception("burn-live: bg_refresh failed netuid=%d", netuid)
        finally:
            with self._lock:
                self._fetching.discard(netuid)


def _make_result(tao: float, fetched_at: float, *,
                 from_cache: bool = True, stale: bool = False) -> dict:
    return {
        "tao": tao,
        "ts": fetched_at,
        "ts_iso": datetime.fromtimestamp(fetched_at, tz=timezone.utc)
                          .astimezone().isoformat(),
        "from_cache": from_cache,
        "stale": stale,
    }


# Module singleton.
_burn_cache: BurnLiveCache | None = None


def init_burn_live(sdk_client=None, ttl: float = _TTL) -> BurnLiveCache:
    global _burn_cache
    _burn_cache = BurnLiveCache(ttl=ttl)
    _burn_cache._sdk_client = sdk_client
    return _burn_cache


def get_burn_cache() -> BurnLiveCache:
    global _burn_cache
    if _burn_cache is None:
        _burn_cache = BurnLiveCache()
    return _burn_cache
