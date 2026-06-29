"""Per-subnet miner reward distribution (ranking by paid emission).

The detail page's *Emission split* card answers "what % of subnet emission
goes to owner / validators / miners". This module answers the next
question — "*within the miner bucket*, who actually gets paid?"

For one subnet we fetch the metagraph, drop validator UIDs (anyone with a
validator permit), sort the rest by per-block alpha emission descending, and
return:

  * `miners` — ranked list, each entry has uid, hotkey, incentive, emission
    rank (`rank_pos`), share of miner-bucket (0-100%), α/day, τ/day, and when
    subnet tempo is known: **α/tempo** and **τ/tempo** (paid α/block × tempo
    blocks per epoch × pool price).
  * `summary` — totals (active miners, miner-bucket total τ/day) plus the
    cumulative share captured by the top {1,5,10,50} miners and ``tempo_blocks``.

Caching: per-netuid in-memory snapshot, default 5 min TTL (~25 blocks at
12 s, much shorter than tempo for any subnet). Coalesces concurrent
requests; first lookup is blocking with a generous timeout to absorb the
cold websocket handshake.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# A coldkey or IP shared by more than this many miners flags a sybil-ish cluster.
SYBIL_THRESHOLD = 3

log = logging.getLogger(__name__)

DEFAULT_TTL = 300.0
# Page-render path: short — let HTML come back fast and show a "loading" stub
# in the card. The background metagraph fetch keeps running and populates the
# cache in time for the next page load (~30-45 s on the first cold call).
LOOKUP_TIMEOUT_S = 8.0
# JSON API path: longer, since clients are likely to wait for fresh data.
LOOKUP_TIMEOUT_API_S = 45.0
BLOCKS_PER_DAY = 7200
BLOCK_SECONDS = 12.0
DEFAULT_TOP_LIMIT = 30


# ─── data shapes ────────────────────────────────────────────────────────────


@dataclass
class _Snapshot:
    netuid: int = 0
    owner: dict[str, Any] | None = None
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
    tempo_blocks: int = 0  # subnet epoch length; scales paid α/block → α per tempo
    ref_block: int = 0     # chain block the metagraph was synced at
    churn: dict[str, Any] | None = None  # registration/deregistration turnover


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
        reg_blocks  = _to_int_list(getattr(meta, "block_at_registration", None))
        axons       = getattr(meta, "axons", None) or []

        # Block the metagraph was synced at — reference for tenure / churn.
        try:
            ref_block = int(getattr(meta, "block"))
        except Exception:  # noqa: BLE001
            ref_block = self._head_block()

        def _ip(uid: int) -> str:
            try:
                ip = str(axons[uid].ip)
                return "" if ip in ("0.0.0.0", "") else ip
            except Exception:  # noqa: BLE001
                return ""

        n = max(len(emissions), len(incentives), len(permits), len(hotkeys))
        if n == 0:
            return _Snapshot(netuid=netuid, fetched_at=time.time(),
                             error="empty metagraph")

        price = self._subnet_price(netuid)
        tempo = self._subnet_tempo(netuid)
        # IMPORTANT: metagraph `.emission` is denominated PER TEMPO (per epoch),
        # NOT per block — sum(emission)/tempo ≈ (1 - owner_cut) for every subnet.
        # Convert to a per-block rate with the subnet's tempo so α/day and τ/day
        # are real. Fall back to 360 (the common tempo) if the directory scan
        # hasn't recorded one for this subnet yet.
        eff_tempo = tempo if tempo and tempo > 0 else 360

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
            rb    = int(_at(reg_blocks, uid, 0))
            tenure_days = (max(0, ref_block - rb) * BLOCK_SECONDS / 86400.0
                           if rb > 0 and ref_block > 0 else None)
            row: dict[str, Any] = {
                "uid": uid,
                "hotkey": hk,
                "hotkey_short": _short(hk),
                "coldkey": ck,
                "coldkey_short": _short(ck),
                "ip": _ip(uid),
                "incentive": inc,
                "rank": rk,
                "trust": tr,
                "reg_block": rb,
                "tenure_days": tenure_days,
                "alpha_per_block": emi / eff_tempo,
                "alpha_per_day": emi / eff_tempo * BLOCKS_PER_DAY,
                "tao_per_day": emi / eff_tempo * BLOCKS_PER_DAY * price,
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

        # Compute share-of-miner-pool + assign emission rank + per-tempo rewards.
        for idx, m in enumerate(miners_raw, start=1):
            m["rank_pos"] = idx
            m["share_of_miners"] = (
                (m["alpha_per_block"] / total_alpha_per_block)
                if total_alpha_per_block > 0 else 0.0
            )
            m["share_pct"] = m["share_of_miners"] * 100.0
            if tempo > 0:
                apt = m["alpha_per_block"] * float(tempo)
                m["alpha_per_tempo"] = apt
                m["tao_per_tempo"] = apt * price
            else:
                m["alpha_per_tempo"] = None
                m["tao_per_tempo"] = None

        total_val_alpha = sum(v["alpha_per_block"] for v in validators_raw)
        active_val = sum(1 for v in validators_raw if v["alpha_per_block"] > 0)
        for idx, v in enumerate(validators_raw, start=1):
            v["rank_pos"] = idx
            v["share_of_validators"] = (
                (v["alpha_per_block"] / total_val_alpha)
                if total_val_alpha > 0 else 0.0
            )
            v["share_pct"] = v["share_of_validators"] * 100.0
            if tempo > 0:
                apt = v["alpha_per_block"] * float(tempo)
                v["alpha_per_tempo"] = apt
                v["tao_per_tempo"] = apt * price
            else:
                v["alpha_per_tempo"] = None
                v["tao_per_tempo"] = None

        # Flag clustering: rows whose coldkey or (non-empty) IP is shared by
        # more than SYBIL_THRESHOLD miners. Counted across ALL miners so a
        # cluster is detected even if some members fall below the display cutoff.
        ck_counts = Counter(m["coldkey"] for m in miners_raw if m.get("coldkey"))
        ip_counts = Counter(m["ip"] for m in miners_raw if m.get("ip"))
        for m in miners_raw:
            m["coldkey_count"] = ck_counts.get(m["coldkey"], 0)
            m["ip_count"] = ip_counts.get(m["ip"], 0) if m.get("ip") else 0
            m["sybil"] = (m["coldkey_count"] > SYBIL_THRESHOLD
                          or m["ip_count"] > SYBIL_THRESHOLD)

        # Build the subnet-owner row (shown on top of the table).
        owner: dict[str, Any] | None = None
        try:
            sinfo = sub.subnet(netuid)
            owner_hk = str(getattr(sinfo, "owner_hotkey", "") or "")
            owner_ck = str(getattr(sinfo, "owner_coldkey", "") or "")
            if owner_hk and owner_hk in hotkeys:
                ou = hotkeys.index(owner_hk)
                emi = float(_at(emissions, ou, 0.0))
                ck = str(_at(coldkeys, ou, "") or "") or owner_ck
                owner = {
                    "uid": ou,
                    "hotkey": owner_hk,
                    "hotkey_short": _short(owner_hk),
                    "coldkey": ck,
                    "coldkey_short": _short(ck),
                    "ip": _ip(ou),
                    "incentive": float(_at(incentives, ou, 0.0)),
                    "validator_permit": bool(_at(permits, ou, False)),
                    "alpha_per_block": emi / eff_tempo,
                    "tao_per_day": emi / eff_tempo * BLOCKS_PER_DAY * price,
                    "tao_per_tempo": (emi / eff_tempo * float(tempo) * price)
                                     if tempo > 0 else None,
                }
        except Exception as e:  # noqa: BLE001
            log.debug("miner_rewards sn%d: owner lookup failed: %s", netuid, e)

        churn = self._compute_churn(netuid, reg_blocks, ref_block)

        return _Snapshot(
            netuid=netuid,
            owner=owner,
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
            tempo_blocks=int(tempo),
            ref_block=ref_block,
            churn=churn,
        )

    def _head_block(self) -> int:
        try:
            from .cache import get_scanner
            return int(get_scanner().get().head_block or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _subnet_row(self, netuid: int):
        """Return the cached directory row for ``netuid`` (or None)."""
        try:
            from .cache import get_scanner
            scan = get_scanner().get()
        except Exception:  # noqa: BLE001
            return None
        for r in scan.rows:
            if int(r.netuid) == int(netuid):
                return r
        return None

    def _compute_churn(
        self, netuid: int, reg_blocks: list[int], ref_block: int,
    ) -> dict[str, Any] | None:
        """Registration / deregistration turnover from on-chain registration blocks.

        ``block_at_registration`` is the exact block each *currently* registered
        UID registered at. The count of UIDs registered within a window is real
        on-chain data — and on a *full* subnet each registration evicts an
        existing UID, so registrations-in-window == deregistrations-in-window.
        """
        regs = [int(b) for b in reg_blocks if int(b) > 0]
        if not regs or ref_block <= 0:
            return None

        def _within(blocks: int) -> int:
            cut = ref_block - blocks
            return sum(1 for b in regs if b >= cut)

        reg_24h = _within(BLOCKS_PER_DAY)
        reg_7d = _within(BLOCKS_PER_DAY * 7)
        reg_30d = _within(BLOCKS_PER_DAY * 30)

        tenures_s = sorted(max(0, ref_block - b) * BLOCK_SECONDS for b in regs)
        total = len(tenures_s)
        median_s = tenures_s[total // 2]
        oldest_s = tenures_s[-1]
        newest_s = tenures_s[0]

        row = self._subnet_row(netuid)
        subnetwork_n = int(getattr(row, "subnetwork_n", 0) or 0) if row else total
        max_n = int(getattr(row, "max_n", 0) or 0) if row else 0
        slots_free = max(0, max_n - subnetwork_n) if max_n else 0
        is_full = max_n > 0 and subnetwork_n >= max_n
        immunity = getattr(row, "immunity_period", None) if row else None

        return {
            "ref_block": ref_block,
            "total_uids": total,
            "is_full": bool(is_full),
            "slots_free": slots_free,
            "reg_24h": reg_24h,
            "reg_7d": reg_7d,
            "reg_30d": reg_30d,
            "reg_per_day": reg_7d / 7.0,
            "turnover_pct_30d": (reg_30d / total * 100.0) if total else 0.0,
            "median_tenure_days": median_s / 86400.0,
            "median_tenure_label": _dur_label(median_s),
            "oldest_label": _dur_label(oldest_s),
            "newest_label": _dur_label(newest_s) + " ago",
            "halflife_label": _dur_label(median_s),
            "immunity_label": (
                f"{int(immunity)} blocks (~{_dur_label(int(immunity) * BLOCK_SECONDS)})"
                if immunity else None),
        }

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

    def _subnet_tempo(self, netuid: int) -> int:
        """Subnet emission tempo (blocks per epoch) from the last directory scan."""
        try:
            from .cache import get_scanner
            scan = get_scanner().get()
        except Exception:  # noqa: BLE001
            return 0
        for r in scan.rows:
            if int(r.netuid) == int(netuid):
                t = getattr(r, "tempo", None)
                try:
                    ti = int(t) if t is not None else 0
                except (TypeError, ValueError):
                    ti = 0
                return ti if ti > 0 else 0
        return 0

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
        highlight_hotkeys: set[str] | None = None,
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
                       (time.time() - snap.fetched_at) >= self.ttl,
                       highlight_hotkeys=highlight_hotkeys)

    def fetch_snapshot_raw(self, netuid: int) -> _Snapshot:
        """Fetch a fresh processed snapshot synchronously on the *calling*
        thread (reusing that thread's subtensor). Used by the network sweep,
        which manages its own bounded, thread-reusing pool — so it must NOT go
        through ``_bg_refresh`` (which spawns a fresh thread + leaks a
        per-thread websocket each call)."""
        return self._fetch_from_chain(int(netuid))

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


def _to_int_list(x) -> list[int]:
    if x is None:
        return []
    out: list[int] = []
    for v in x:
        try:
            out.append(int(v))
        except Exception:  # noqa: BLE001
            out.append(0)
    return out


def _dur_label(seconds: float) -> str:
    """Human-friendly duration: '12 min', '7.3 h', '4.1 d'."""
    s = max(0.0, float(seconds))
    if s < 90 * 60:
        return f"{int(round(s / 60))} min"
    if s < 48 * 3600:
        return f"{s / 3600.0:.1f} h"
    return f"{s / 86400.0:.1f} d"


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


def _result(snap: _Snapshot, limit: int, stale: bool = False,
            highlight_hotkeys: set[str] | None = None) -> dict[str, Any]:
    mine = {h for h in (highlight_hotkeys or ()) if h}
    full = snap.miners or []
    lim = max(0, int(limit))
    miners = [dict(m) for m in full[:lim]]
    for m in miners:
        m["is_mine"] = m.get("hotkey") in mine
    # Always include the configured ("my") hotkeys: if one ranks below the
    # display cutoff, append its row at the bottom so it's still visible.
    if mine:
        shown = {int(m["uid"]) for m in miners}
        for m in full[lim:]:
            if m.get("hotkey") in mine and int(m["uid"]) not in shown:
                row = dict(m)
                row["is_mine"] = True
                row["below_cutoff"] = True
                miners.append(row)
    return {
        "netuid": snap.netuid,
        "owner": snap.owner,
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
            "tempo_blocks": int(getattr(snap, "tempo_blocks", 0) or 0),
        },
        "ref_block": int(getattr(snap, "ref_block", 0) or 0),
        "churn": getattr(snap, "churn", None),
        "fetched_at": snap.fetched_at,
        "fetched_at_iso": (
            datetime.fromtimestamp(snap.fetched_at, tz=timezone.utc)
            .astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            if snap.fetched_at else None),
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
