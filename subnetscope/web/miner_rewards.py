"""Per-subnet miner reward distribution (ranking by paid emission).

The detail page's *Emission split* card answers "what % of subnet emission
goes to owner / validators / miners". This module answers the next
question — "*within the miner bucket*, who actually gets paid?"

For one subnet we fetch the metagraph, drop validator UIDs (anyone with a
validator permit), sort the rest by per-block alpha emission descending, and
return:

  * `miners` — ranked list, each entry has uid, hotkey, incentive, share of
    miner-bucket (0-1), alpha/day, τ/day (alpha × current pool price).
  * `summary` — totals (active miners, miner-bucket total τ/day) plus the
    cumulative share captured by the top {1,5,10,50} miners.

Caching: per-netuid in-memory snapshot, default 5 min TTL (~25 blocks at
12 s, much shorter than tempo for any subnet). Coalesces concurrent
requests; first lookup is blocking with a generous timeout to absorb the
cold websocket handshake.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_TTL = 300.0
# Page-render path: short — let HTML come back fast and show a "loading" stub
# in the card. The background metagraph fetch keeps running and populates the
# cache in time for the next page load (~30-45 s on the first cold call).
LOOKUP_TIMEOUT_S = 8.0
# JSON API path: longer, since clients are likely to wait for fresh data.
LOOKUP_TIMEOUT_API_S = 45.0
BLOCKS_PER_DAY = 7200
DEFAULT_TOP_LIMIT = 30


# ─── data shapes ────────────────────────────────────────────────────────────


@dataclass
class _Snapshot:
    netuid: int = 0
    miners: list[dict[str, Any]] = field(default_factory=list)
    validators: list[dict[str, Any]] = field(default_factory=list)
    total_miner_uids: int = 0          # miners (validator_permit=False)
    active_miner_uids: int = 0         # miners with non-zero emission
    total_validator_uids: int = 0
    active_validator_uids: int = 0
    total_miner_alpha_per_block: float = 0.0
    total_miner_alpha_per_day: float = 0.0
    total_miner_tao_per_day: float = 0.0
    total_validator_alpha_per_block: float = 0.0
    price_tao_per_alpha: float = 0.0
    top1_pct: float = 0.0
    top5_pct: float = 0.0
    top10_pct: float = 0.0
    top50_pct: float = 0.0
    fetched_at: float = 0.0
    error: str | None = None


# ─── service ────────────────────────────────────────────────────────────────


class MinerRewardsService:
    def __init__(self, sdk_client, ttl: float = DEFAULT_TTL):
        self._sdk_client = sdk_client
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._cache: dict[int, _Snapshot] = {}
        self._fetching: set[int] = set()
        # One-shot Events per netuid the foreground waits on. The bg fetch
        # thread sets+pops them when it writes a snapshot. Using Event.wait
        # is safe under GIL contention (uses pthread_cond_wait under the
        # hood) — a busy `time.sleep` loop would starve here because the
        # bittensor SDK holds the GIL solid during metagraph decode.
        self._ready: dict[int, threading.Event] = {}

    # ------------------------------------------------------------------ chain

    def _get_subtensor(self):
        if self._sdk_client is None:
            return None
        try:
            tls_fn = getattr(self._sdk_client, "_thread_subtensor", None)
            if tls_fn is not None:
                return tls_fn()
            return self._sdk_client.subtensor
        except Exception:  # noqa: BLE001
            return None

    def _fetch_from_chain(self, netuid: int) -> _Snapshot:
        sub = self._get_subtensor()
        if sub is None:
            return _Snapshot(netuid=netuid, fetched_at=time.time(),
                             error="subtensor not initialised")
        try:
            meta = sub.metagraph(netuid=netuid, lite=True)
        except Exception as e:  # noqa: BLE001
            log.warning("miner_rewards sn%d: metagraph fetch failed: %s",
                        netuid, e)
            return _Snapshot(netuid=netuid, fetched_at=time.time(),
                             error=f"metagraph fetch failed: {e}")

        emissions   = _to_float_list(getattr(meta, "emission", None))
        incentives  = _to_float_list(getattr(meta, "incentive", None))
        permits     = _to_bool_list(getattr(meta, "validator_permit", None))
        hotkeys     = _to_str_list(getattr(meta, "hotkeys", None))
        coldkeys    = _to_str_list(getattr(meta, "coldkeys", None))
        ranks       = _to_float_list(getattr(meta, "rank", None))
        trusts      = _to_float_list(getattr(meta, "trust", None))

        n = max(len(emissions), len(incentives), len(permits), len(hotkeys))
        if n == 0:
            return _Snapshot(netuid=netuid, fetched_at=time.time(),
                             error="empty metagraph")

        price = self._subnet_price(netuid)

        miners_raw: list[dict[str, Any]] = []
        validators_raw: list[dict[str, Any]] = []
        for uid in range(n):
            is_val = bool(_at(permits, uid, False))
            emi   = float(_at(emissions, uid, 0.0))
            inc   = float(_at(incentives, uid, 0.0))
            hk    = str(_at(hotkeys, uid, "") or "")
            ck    = str(_at(coldkeys, uid, "") or "")
            rk    = float(_at(ranks, uid, 0.0))
            tr    = float(_at(trusts, uid, 0.0))
            row: dict[str, Any] = {
                "uid": uid,
                "hotkey": hk,
                "hotkey_short": _short(hk),
                "coldkey": ck,
                "coldkey_short": _short(ck),
                "incentive": inc,
                "rank": rk,
                "trust": tr,
                "alpha_per_block": emi,
                "alpha_per_day": emi * BLOCKS_PER_DAY,
                "tao_per_day": emi * BLOCKS_PER_DAY * price,
            }
            if is_val:
                validators_raw.append(row)
            else:
                miners_raw.append(row)

        miners_raw.sort(key=lambda m: m["alpha_per_block"], reverse=True)
        validators_raw.sort(key=lambda v: v["alpha_per_block"], reverse=True)

        total_alpha_per_block = sum(m["alpha_per_block"] for m in miners_raw)
        total_alpha_per_day   = total_alpha_per_block * BLOCKS_PER_DAY
        total_tao_per_day     = total_alpha_per_day * price
        active = sum(1 for m in miners_raw if m["alpha_per_block"] > 0)

        # Compute share-of-miner-pool + assign rank.
        for idx, m in enumerate(miners_raw, start=1):
            m["rank_pos"] = idx
            m["share_of_miners"] = (
                (m["alpha_per_block"] / total_alpha_per_block)
                if total_alpha_per_block > 0 else 0.0
            )
            m["share_pct"] = m["share_of_miners"] * 100.0

        total_val_alpha = sum(v["alpha_per_block"] for v in validators_raw)
        active_val = sum(1 for v in validators_raw if v["alpha_per_block"] > 0)
        for idx, v in enumerate(validators_raw, start=1):
            v["rank_pos"] = idx
            v["share_of_validators"] = (
                (v["alpha_per_block"] / total_val_alpha)
                if total_val_alpha > 0 else 0.0
            )
            v["share_pct"] = v["share_of_validators"] * 100.0

        return _Snapshot(
            netuid=netuid,
            miners=miners_raw,
            validators=validators_raw,
            total_miner_uids=len(miners_raw),
            active_miner_uids=active,
            total_validator_uids=len(validators_raw),
            active_validator_uids=active_val,
            total_miner_alpha_per_block=total_alpha_per_block,
            total_miner_alpha_per_day=total_alpha_per_day,
            total_miner_tao_per_day=total_tao_per_day,
            total_validator_alpha_per_block=total_val_alpha,
            price_tao_per_alpha=price,
            top1_pct=_cum_pct(miners_raw, 1),
            top5_pct=_cum_pct(miners_raw, 5),
            top10_pct=_cum_pct(miners_raw, 10),
            top50_pct=_cum_pct(miners_raw, 50),
            fetched_at=time.time(),
            error=None,
        )

    def _subnet_price(self, netuid: int) -> float:
        try:
            from .cache import get_scanner
            scan = get_scanner().get()
        except Exception:  # noqa: BLE001
            return 0.0
        for r in scan.rows:
            if int(r.netuid) == int(netuid):
                return float(r.price_tao_per_alpha or 0.0)
        return 0.0

    # ----------------------------------------------------------------- public

    def _get_snapshot_blocking(
        self,
        netuid: int,
        *,
        force: bool,
        wait_s: float,
    ) -> _Snapshot | None:
        """Return cached metagraph snapshot, or None if still loading after wait."""
        now = time.time()
        ready: threading.Event | None = None
        with self._lock:
            snap = self._cache.get(netuid)
            fresh = (snap is not None
                     and snap.error is None
                     and (now - snap.fetched_at) < self.ttl)

            if fresh and not force:
                return snap

            ready = self._ready.get(netuid)
            if ready is None:
                ready = threading.Event()
                self._ready[netuid] = ready

            if netuid not in self._fetching:
                self._fetching.add(netuid)
                threading.Thread(
                    target=self._bg_refresh, args=(netuid,),
                    name=f"miner-rewards-{netuid}", daemon=True,
                ).start()

        if snap is None and wait_s > 0 and ready is not None:
            ready.wait(timeout=wait_s)
            with self._lock:
                snap = self._cache.get(netuid)

        return snap

    def lookup_uid_rows(
        self,
        netuid: int,
        uids: list[int],
        *,
        force: bool = False,
        timeout_s: float | None = None,
    ) -> tuple[dict[int, dict[str, Any]], str | None]:
        """UID → metagraph emission row plus ``role`` (miner | validator).

        Used by watch-hotkey cards. Rank and share are within the miner pool
        or validator pool respectively (same basis as the miner table).
        """
        try:
            netuid_i = int(netuid)
        except (TypeError, ValueError):
            return {}, "invalid netuid"
        if not uids:
            return {}, None

        wait_s = float(timeout_s) if timeout_s is not None else LOOKUP_TIMEOUT_S
        snap = self._get_snapshot_blocking(netuid_i, force=force, wait_s=wait_s)
        if snap is None:
            return {}, "loading"
        if snap.error:
            return {}, snap.error

        by_uid_m = {int(m["uid"]): m for m in snap.miners}
        by_uid_v = {int(v["uid"]): v for v in (snap.validators or [])}
        out: dict[int, dict[str, Any]] = {}
        for uid in uids:
            try:
                ui = int(uid)
            except (TypeError, ValueError):
                continue
            if ui in by_uid_m:
                row = dict(by_uid_m[ui])
                row["role"] = "miner"
                row["pool_size"] = snap.total_miner_uids
                out[ui] = row
            elif ui in by_uid_v:
                row = dict(by_uid_v[ui])
                row["role"] = "validator"
                row["pool_size"] = snap.total_validator_uids
                out[ui] = row
        return out, None

    def get(
        self,
        netuid: int,
        force: bool = False,
        limit: int = DEFAULT_TOP_LIMIT,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Return a fresh-ish ranked-miner snapshot for `netuid`.

        `limit` truncates the `miners` list in the returned dict; totals
        and percentile cumulants are always computed against the full set.
        `timeout_s` overrides how long to block waiting for the *first*
        snapshot (defaults to a short page-render-friendly value).
        """
        try:
            netuid = int(netuid)
        except (TypeError, ValueError):
            return _result(_Snapshot(fetched_at=time.time(),
                                     error="invalid netuid"), limit)

        wait_s = float(timeout_s) if timeout_s is not None else LOOKUP_TIMEOUT_S
        snap = self._get_snapshot_blocking(netuid, force=force, wait_s=wait_s)

        if snap is None:
            return _result(_Snapshot(netuid=netuid, fetched_at=time.time(),
                                     error="loading"), limit)
        return _result(snap, limit, stale=force or
                       (time.time() - snap.fetched_at) >= self.ttl)

    def _bg_refresh(self, netuid: int) -> None:
        ev: threading.Event | None = None
        try:
            snap = self._fetch_from_chain(netuid)
            with self._lock:
                prior = self._cache.get(netuid)
                if snap.error is None or prior is None:
                    self._cache[netuid] = snap
                else:
                    log.debug("miner_rewards sn%d: refresh failed (%s); "
                              "keeping prior", netuid, snap.error)
        finally:
            with self._lock:
                self._fetching.discard(netuid)
                ev = self._ready.pop(netuid, None)
            if ev is not None:
                ev.set()


# ─── helpers ────────────────────────────────────────────────────────────────


def _at(seq, i, default):
    try:
        return seq[i]
    except (IndexError, KeyError, TypeError):
        return default


def _to_float_list(x) -> list[float]:
    if x is None:
        return []
    out: list[float] = []
    for v in x:
        try:
            tao_attr = getattr(v, "tao", None)
            out.append(float(tao_attr) if tao_attr is not None else float(v))
        except Exception:  # noqa: BLE001
            out.append(0.0)
    return out


def _to_bool_list(x) -> list[bool]:
    if x is None:
        return []
    out: list[bool] = []
    for v in x:
        try:
            out.append(bool(v))
        except Exception:  # noqa: BLE001
            out.append(False)
    return out


def _to_str_list(x) -> list[str]:
    if x is None:
        return []
    return [str(v) if v is not None else "" for v in x]


def _short(ss58: str) -> str:
    s = (ss58 or "").strip()
    return f"{s[:6]}…{s[-4:]}" if len(s) > 12 else s


def _cum_pct(miners: list[dict[str, Any]], n: int) -> float:
    """Cumulative emission share of the top-`n` miners (0-100)."""
    if not miners:
        return 0.0
    total = sum(m["alpha_per_block"] for m in miners)
    if total <= 0:
        return 0.0
    head = sum(m["alpha_per_block"] for m in miners[:n])
    return (head / total) * 100.0


def _result(snap: _Snapshot, limit: int, stale: bool = False) -> dict[str, Any]:
    miners = snap.miners[: max(0, int(limit))] if snap.miners else []
    return {
        "netuid": snap.netuid,
        "miners": miners,
        "miners_returned": len(miners),
        "summary": {
            "total_miner_uids": snap.total_miner_uids,
            "active_miner_uids": snap.active_miner_uids,
            "total_validator_uids": snap.total_validator_uids,
            "total_miner_alpha_per_block": snap.total_miner_alpha_per_block,
            "total_miner_alpha_per_day": snap.total_miner_alpha_per_day,
            "total_miner_tao_per_day": snap.total_miner_tao_per_day,
            "price_tao_per_alpha": snap.price_tao_per_alpha,
            "top1_pct": snap.top1_pct,
            "top5_pct": snap.top5_pct,
            "top10_pct": snap.top10_pct,
            "top50_pct": snap.top50_pct,
        },
        "fetched_at": snap.fetched_at,
        "age_seconds": max(0.0, time.time() - snap.fetched_at)
                       if snap.fetched_at else None,
        "stale": stale,
        "error": snap.error,
    }


# ─── module-level singleton ─────────────────────────────────────────────────
_service: MinerRewardsService | None = None


def init_miner_rewards(
    sdk_client,
    ttl: float = DEFAULT_TTL,
) -> MinerRewardsService:
    global _service
    _service = MinerRewardsService(sdk_client=sdk_client, ttl=ttl)
    return _service


def get_miner_rewards_service() -> MinerRewardsService | None:
    return _service
