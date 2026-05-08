"""Real-time TAO price + 24h chart cache for the dashboard.

Source: CoinGecko's free public API (no key required).
  * Spot     -> /simple/price?ids=bittensor&vs_currencies=usd&include_24hr_change=true
                 ...&include_market_cap=true&include_24hr_vol=true
  * History  -> /coins/bittensor/market_chart?vs_currency=usd&days=1

Design mirrors burn_live.py:
  * In-memory singleton.
  * Per-resource TTL with a "fetching" flag to coalesce concurrent callers.
  * Stale values returned immediately while a background thread refreshes.
  * Graceful degradation: failed fetches keep last known value and mark it stale.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

CG_BASE = "https://api.coingecko.com/api/v3"
COIN_ID = "bittensor"
HTTP_TIMEOUT_S = 8.0
USER_AGENT = "subnetscope/0.1 (+https://github.com)"

# TTLs chosen to stay well under CoinGecko's free-tier rate limits
# (~10-30 req/min). Spot: 1 req/min; chart: 1 req per 5 min.
SPOT_TTL = 60.0
CHART_TTL = 300.0


@dataclass
class _Spot:
    usd: float | None = None
    usd_24h_change: float | None = None
    usd_market_cap: float | None = None
    usd_24h_vol: float | None = None
    fetched_at: float = 0.0


@dataclass
class _Chart:
    # Each point: {"t": unix_seconds, "v": price_usd}
    points: list[dict[str, float]] = field(default_factory=list)
    fetched_at: float = 0.0


class TaoPriceCache:
    def __init__(self, spot_ttl: float = SPOT_TTL,
                 chart_ttl: float = CHART_TTL):
        self.spot_ttl = spot_ttl
        self.chart_ttl = chart_ttl
        self._lock = threading.Lock()
        self._spot = _Spot()
        self._chart = _Chart()
        self._spot_fetching = False
        self._chart_fetching = False
        self._client: httpx.Client | None = None

    # --------------------------------------------------------------- internals

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=HTTP_TIMEOUT_S,
                headers={"accept": "application/json",
                         "user-agent": USER_AGENT},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _fetch_spot(self) -> _Spot | None:
        try:
            r = self._http().get(
                f"{CG_BASE}/simple/price",
                params={
                    "ids": COIN_ID,
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                    "include_24hr_vol": "true",
                    "include_market_cap": "true",
                },
            )
            r.raise_for_status()
            data: dict[str, Any] = r.json()
            row = data.get(COIN_ID) or {}
            usd = _to_float(row.get("usd"))
            if usd is None:
                return None
            return _Spot(
                usd=usd,
                usd_24h_change=_to_float(row.get("usd_24h_change")),
                usd_market_cap=_to_float(row.get("usd_market_cap")),
                usd_24h_vol=_to_float(row.get("usd_24h_vol")),
                fetched_at=time.time(),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("tao_price spot fetch failed: %s", e)
            return None

    def _fetch_chart(self, days: int = 1) -> _Chart | None:
        try:
            r = self._http().get(
                f"{CG_BASE}/coins/{COIN_ID}/market_chart",
                params={"vs_currency": "usd", "days": str(days)},
            )
            r.raise_for_status()
            data: dict[str, Any] = r.json()
            raw = data.get("prices") or []
            pts: list[dict[str, float]] = []
            for entry in raw:
                if not (isinstance(entry, (list, tuple)) and len(entry) >= 2):
                    continue
                t_ms = _to_float(entry[0])
                v = _to_float(entry[1])
                if t_ms is None or v is None:
                    continue
                pts.append({"t": t_ms / 1000.0, "v": v})
            if not pts:
                return None
            return _Chart(points=pts, fetched_at=time.time())
        except Exception as e:  # noqa: BLE001
            log.warning("tao_price chart fetch failed: %s", e)
            return None

    # ---------------------------------------------------------------- spot api

    def _bg_refresh_spot(self) -> None:
        try:
            new = self._fetch_spot()
            if new is not None:
                with self._lock:
                    self._spot = new
        finally:
            with self._lock:
                self._spot_fetching = False

    def get_spot(self) -> dict[str, Any]:
        """Always returns quickly. Triggers a background refresh if stale."""
        now = time.time()
        with self._lock:
            spot = self._spot
            fresh = spot.usd is not None \
                and (now - spot.fetched_at) < self.spot_ttl
            if not fresh and not self._spot_fetching:
                self._spot_fetching = True
                threading.Thread(target=self._bg_refresh_spot,
                                 name="tao-spot", daemon=True).start()

        # If we have nothing at all, block briefly for the first fetch.
        if spot.usd is None:
            for _ in range(15):  # up to ~3 s
                time.sleep(0.2)
                with self._lock:
                    spot = self._spot
                if spot.usd is not None:
                    break

        stale = spot.usd is None \
            or (time.time() - spot.fetched_at) >= self.spot_ttl
        return {
            "usd": spot.usd,
            "usd_24h_change": spot.usd_24h_change,
            "usd_market_cap": spot.usd_market_cap,
            "usd_24h_vol": spot.usd_24h_vol,
            "ts": spot.fetched_at or None,
            "ts_iso": _ts_iso(spot.fetched_at),
            "stale": stale,
            "ttl": self.spot_ttl,
        }

    # --------------------------------------------------------------- chart api

    def _bg_refresh_chart(self, days: int) -> None:
        try:
            new = self._fetch_chart(days=days)
            if new is not None:
                with self._lock:
                    self._chart = new
        finally:
            with self._lock:
                self._chart_fetching = False

    def get_chart(self, hours: float = 24.0) -> dict[str, Any]:
        """Return (cached) 24h price chart points trimmed to `hours`."""
        now = time.time()
        with self._lock:
            chart = self._chart
            fresh = bool(chart.points) \
                and (now - chart.fetched_at) < self.chart_ttl
            if not fresh and not self._chart_fetching:
                self._chart_fetching = True
                # CoinGecko's `days=1` endpoint returns auto-granularity points
                # (~5 min). For larger windows we'd ask days=7/30/etc.
                days = 1 if hours <= 24 else max(2, int((hours / 24) + 0.5))
                threading.Thread(target=self._bg_refresh_chart, args=(days,),
                                 name="tao-chart", daemon=True).start()

        if not chart.points:
            for _ in range(20):  # up to ~4 s
                time.sleep(0.2)
                with self._lock:
                    chart = self._chart
                if chart.points:
                    break

        cutoff = time.time() - (hours * 3600.0)
        pts = [p for p in chart.points if p["t"] >= cutoff]
        stale = (time.time() - chart.fetched_at) >= self.chart_ttl \
            if chart.points else True
        return {
            "points": pts,
            "count": len(pts),
            "ts": chart.fetched_at or None,
            "ts_iso": _ts_iso(chart.fetched_at),
            "stale": stale,
            "ttl": self.chart_ttl,
            "hours": hours,
        }

    def prewarm(self) -> None:
        """Kick off background fetches for spot + chart without blocking."""
        with self._lock:
            need_spot = not self._spot_fetching
            need_chart = not self._chart_fetching
            if need_spot:
                self._spot_fetching = True
            if need_chart:
                self._chart_fetching = True
        if need_spot:
            threading.Thread(target=self._bg_refresh_spot,
                             name="tao-spot-prewarm", daemon=True).start()
        if need_chart:
            threading.Thread(target=self._bg_refresh_chart, args=(1,),
                             name="tao-chart-prewarm", daemon=True).start()


# ------------------------------------------------------------------- helpers


def _to_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ts_iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc) \
        .astimezone().isoformat()


# ---------------------------------------------------------------- singleton


_cache: TaoPriceCache | None = None


def init_tao_price(spot_ttl: float = SPOT_TTL,
                   chart_ttl: float = CHART_TTL,
                   prewarm: bool = True) -> TaoPriceCache:
    global _cache
    _cache = TaoPriceCache(spot_ttl=spot_ttl, chart_ttl=chart_ttl)
    if prewarm:
        _cache.prewarm()
    return _cache


def get_tao_price_cache() -> TaoPriceCache:
    global _cache
    if _cache is None:
        _cache = TaoPriceCache()
    return _cache
