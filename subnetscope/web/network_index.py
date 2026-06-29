"""Cross-subnet account index + miner rank-history recorder.

Two jobs, both fed by one periodic sweep of every subnet's metagraph:

  1. **Account lookup** for the dashboard: given a coldkey SS58 or an axon IP,
     return which subnets that account's hotkeys are registered on and how
     many. Coldkey lookups go straight to the chain (fast, always fresh); IP
     lookups read an in-memory index built by the sweep (there is no
     chain query from IP → hotkey, so the whole network must be scanned).

  2. **Rank-hold history**: each sweep records the ordered top-K miners per
     subnet into ``subnet_rank_snapshots``. Over time this lets the detail
     page show how long the current leaders have actually kept their rank —
     real recorded data, not an estimate.

The sweep uses a small, **thread-reusing** pool so it never spawns a fresh
websocket per subnet (which would leak connections). It reuses
``MinerRewardsService.fetch_snapshot_raw`` for the metagraph → ranked-rows
processing so the rank basis matches the live miner table exactly.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from .miner_rewards import _dur_label, _short

log = logging.getLogger(__name__)

_IPV4_RE = re.compile(r"^(\d{1,3})(\.\d{1,3}){3}$")
_SS58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")

SWEEP_INTERVAL_S = 900.0       # full network sweep cadence (15 min)
RANK_TOP_K = 20                # leaders recorded per subnet per snapshot
SWEEP_WORKERS = 4              # bounded, thread-reusing pool
RANK_RETENTION_DAYS = 45


def classify_query(q: str) -> str:
    """Return 'ip', 'coldkey', or 'invalid' for a lookup string."""
    s = (q or "").strip()
    if _IPV4_RE.match(s) and all(0 <= int(p) <= 255 for p in s.split(".")):
        return "ip"
    if _SS58_RE.match(s):
        return "coldkey"
    return "invalid"


class NetworkIndexService:
    def __init__(self, sdk_client, miner_rewards_svc, db, *,
                 sweep_interval: float = SWEEP_INTERVAL_S,
                 top_k: int = RANK_TOP_K, workers: int = SWEEP_WORKERS):
        self._sdk = sdk_client
        self._mr = miner_rewards_svc
        self._db = db
        self.sweep_interval = float(sweep_interval)
        self.top_k = int(top_k)
        self.workers = max(1, int(workers))
        self.interval_label = _dur_label(self.sweep_interval)

        self._lock = threading.Lock()
        self._ip_index: dict[str, list[dict]] = {}
        self._ck_index: dict[str, list[dict]] = {}
        self._built_at: float = 0.0
        self._sweeping = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None

    # ---------------------------------------------------------------- chain
    def _get_subtensor(self):
        if self._sdk is None:
            return None
        try:
            tls_fn = getattr(self._sdk, "_thread_subtensor", None)
            return tls_fn() if tls_fn is not None else self._sdk.subtensor
        except Exception:  # noqa: BLE001
            return None

    def _netuid_names(self) -> dict[int, str]:
        try:
            from .cache import get_scanner
            scan = get_scanner().get()
        except Exception:  # noqa: BLE001
            return {}
        return {int(r.netuid): (r.name or f"sn{r.netuid}") for r in scan.rows}

    # ---------------------------------------------------------------- sweep
    def start(self) -> None:
        """Launch the background sweep loop (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._pool = ThreadPoolExecutor(
                max_workers=self.workers, thread_name_prefix="netidx")
            self._thread = threading.Thread(
                target=self._loop, name="network-index", daemon=True)
            self._thread.start()
        log.info("network index: sweep loop started (every %s, top-%d)",
                 self.interval_label, self.top_k)

    def stop(self) -> None:
        self._stop.set()
        if self._pool is not None:
            self._pool.shutdown(wait=False)

    def _loop(self) -> None:
        # Small initial delay so the first directory scan can populate the
        # netuid list before we sweep.
        self._stop.wait(8.0)
        while not self._stop.is_set():
            try:
                self.sweep_once()
            except Exception:  # noqa: BLE001
                log.exception("network index: sweep failed")
            self._stop.wait(self.sweep_interval)

    def _fetch_one(self, netuid: int):
        """Worker: fetch + process one subnet's metagraph (reused thread)."""
        try:
            return netuid, self._mr.fetch_snapshot_raw(netuid)
        except Exception as e:  # noqa: BLE001
            log.debug("network index sn%d: fetch failed: %s", netuid, e)
            return netuid, None

    def sweep_once(self) -> dict[str, int]:
        """One full pass: rebuild IP/coldkey index + record rank snapshots."""
        names = self._netuid_names()
        netuids = sorted(names.keys())
        if not netuids or self._pool is None:
            return {"subnets": 0}

        with self._lock:
            if self._sweeping:
                return {"subnets": 0, "skipped": 1}
            self._sweeping = True

        ip_index: dict[str, list[dict]] = {}
        ck_index: dict[str, list[dict]] = {}
        ts = int(time.time())
        done = recorded = 0
        try:
            futs = {self._pool.submit(self._fetch_one, n): n for n in netuids}
            for fut in as_completed(futs):
                netuid, snap = fut.result()
                if snap is None or getattr(snap, "error", None):
                    continue
                name = names.get(netuid, f"sn{netuid}")
                done += 1

                # Index every UID (miners + validators) by IP and coldkey.
                rows = list(getattr(snap, "miners", []) or [])
                vals = list(getattr(snap, "validators", []) or [])
                for role, group in (("miner", rows), ("validator", vals)):
                    for m in group:
                        entry = {
                            "netuid": netuid, "name": name,
                            "uid": int(m.get("uid", 0)),
                            "hotkey": m.get("hotkey", ""),
                            "hotkey_short": m.get("hotkey_short", ""),
                            "coldkey": m.get("coldkey", ""),
                            "ip": m.get("ip", ""),
                            "incentive": float(m.get("incentive", 0.0)),
                            "tao_per_day": float(m.get("tao_per_day", 0.0)),
                            "tenure_days": m.get("tenure_days"),
                            "role": role,
                        }
                        ip = (entry["ip"] or "").strip()
                        if ip and ip not in ("0.0.0.0",):
                            ip_index.setdefault(ip, []).append(entry)
                        ck = (entry["coldkey"] or "").strip()
                        if ck:
                            ck_index.setdefault(ck, []).append(entry)

                # Record the ordered top-K miners for rank-hold history.
                top = [{
                    "uid": int(m.get("uid", 0)),
                    "hk": m.get("hotkey", ""),
                    "r": int(m.get("rank_pos", 0) or 0),
                    "inc": round(float(m.get("incentive", 0.0)), 6),
                } for m in rows[:self.top_k] if m.get("hotkey")]
                if top:
                    try:
                        self._db.record_rank_snapshot(
                            netuid, ts, top,
                            retention_days=RANK_RETENTION_DAYS)
                        recorded += 1
                    except Exception:  # noqa: BLE001
                        log.debug("rank snapshot sn%d failed", netuid,
                                  exc_info=True)
        finally:
            with self._lock:
                self._ip_index = ip_index
                self._ck_index = ck_index
                self._built_at = time.time()
                self._sweeping = False

        log.info("network index sweep: %d/%d subnets, %d rank snapshots, "
                 "%d ips, %d coldkeys",
                 done, len(netuids), recorded, len(ip_index), len(ck_index))
        return {"subnets": done, "rank_snapshots": recorded,
                "ips": len(ip_index), "coldkeys": len(ck_index)}

    # --------------------------------------------------------------- lookup
    def lookup(self, query: str, *, force: bool = False) -> dict[str, Any]:
        kind = classify_query(query)
        if kind == "ip":
            return self.lookup_ip(query.strip())
        if kind == "coldkey":
            return self.lookup_coldkey(query.strip(), force=force)
        return {"query": query, "kind": "invalid",
                "error": "Enter a coldkey SS58 (5…) or an IPv4 address."}

    def _index_age(self) -> float | None:
        with self._lock:
            return (time.time() - self._built_at) if self._built_at else None

    def lookup_ip(self, ip: str) -> dict[str, Any]:
        with self._lock:
            built = self._built_at > 0
            entries = list(self._ip_index.get(ip, []))
        if not built:
            # First sweep hasn't finished yet — tell the UI to poll.
            return {"query": ip, "kind": "ip", "building": True,
                    "subnets": [], "total_registrations": 0,
                    "total_hotkeys": 0, "total_subnets": 0}
        return _group_result(ip, "ip", entries, self._index_age(),
                             self.interval_label)

    def lookup_coldkey(self, ss58: str, *, force: bool = False) -> dict[str, Any]:
        """Authoritative, live coldkey → registrations via direct chain calls,
        enriched with uid/incentive from the sweep index when available."""
        sub = self._get_subtensor()
        if sub is None:
            return {"query": ss58, "kind": "coldkey",
                    "error": "subtensor not initialised"}
        names = self._netuid_names()
        with self._lock:
            idx_entries = list(self._ck_index.get(ss58, []))
        # (hotkey, netuid) -> enrichment from the index.
        enrich = {(e["hotkey"], e["netuid"]): e for e in idx_entries}

        entries: list[dict] = []
        try:
            owned = sub.get_owned_hotkeys(ss58) or []
        except Exception as e:  # noqa: BLE001
            log.debug("coldkey %s owned-hotkeys failed: %s", ss58, e)
            owned = []
        for hk in owned:
            hk_s = str(hk)
            try:
                nets = sub.get_netuids_for_hotkey(hk_s) or []
            except Exception:  # noqa: BLE001
                nets = []
            for nu in nets:
                nu = int(nu)
                e = enrich.get((hk_s, nu), {})
                entries.append({
                    "netuid": nu,
                    "name": names.get(nu, f"sn{nu}"),
                    "uid": e.get("uid"),
                    "hotkey": hk_s,
                    "hotkey_short": _short(hk_s),
                    "coldkey": ss58,
                    "ip": e.get("ip", ""),
                    "incentive": e.get("incentive"),
                    "tao_per_day": e.get("tao_per_day"),
                    "tenure_days": e.get("tenure_days"),
                    "role": e.get("role", ""),
                })

        # If the chain calls came back empty (e.g. coldkey owns hotkeys only via
        # delegation), fall back to whatever the sweep index recorded.
        if not entries and idx_entries:
            entries = idx_entries
        return _group_result(ss58, "coldkey", entries, self._index_age(),
                             self.interval_label)


# ─── result shaping ──────────────────────────────────────────────────────────


def _group_result(query: str, kind: str, entries: list[dict],
                  index_age: float | None, interval_label: str) -> dict[str, Any]:
    by_subnet: dict[int, dict] = {}
    hotkeys: set[str] = set()
    for e in entries:
        nu = int(e["netuid"])
        g = by_subnet.setdefault(nu, {
            "netuid": nu, "name": e.get("name") or f"sn{nu}", "hotkeys": [],
        })
        g["hotkeys"].append({
            "hotkey": e.get("hotkey", ""),
            "hotkey_short": e.get("hotkey_short") or _short(e.get("hotkey", "")),
            "uid": e.get("uid"),
            "ip": e.get("ip", ""),
            "incentive": e.get("incentive"),
            "tao_per_day": e.get("tao_per_day"),
            "role": e.get("role", ""),
            "tenure_label": (_dur_label(e["tenure_days"] * 86400)
                             if e.get("tenure_days") is not None else None),
        })
        if e.get("hotkey"):
            hotkeys.add(e["hotkey"])
    subnets = sorted(by_subnet.values(), key=lambda s: s["netuid"])
    for s in subnets:
        s["count"] = len(s["hotkeys"])
        s["hotkeys"].sort(key=lambda h: (h.get("uid") is None, h.get("uid") or 0))
    return {
        "query": query,
        "kind": kind,
        "building": False,
        "subnets": subnets,
        "total_subnets": len(subnets),
        "total_hotkeys": len(hotkeys),
        "total_registrations": sum(s["count"] for s in subnets),
        "index_age_seconds": index_age,
        "index_age_label": (_dur_label(index_age) + " ago"
                            if index_age is not None else None),
        "interval_label": interval_label,
    }


# ─── rank-hold computation (detail page) ─────────────────────────────────────


def compute_rank_tenure(db, netuid: int, current_miners: list[dict], *,
                        top_n: int = 10, interval_label: str = "15 min") -> dict | None:
    """How long the *current* top-``top_n`` miners have held their rank.

    Reads recorded ``subnet_rank_snapshots``. "Held rank" = the length of the
    most-recent unbroken run of snapshots in which a hotkey sat in the top-N.
    Registration tenure (already live on-chain) is surfaced alongside so the
    card is useful even before enough history accumulates.
    """
    miners = [m for m in (current_miners or []) if m.get("hotkey")]
    if not miners:
        return None
    now = time.time()
    history = db.rank_history(netuid, hours=RANK_RETENTION_DAYS * 24)
    since = db.rank_tracking_since(netuid)
    hist_desc = list(reversed(history))  # newest first

    def in_top(snap_top, hk, n):
        for e in snap_top:
            if e.get("hk") == hk and int(e.get("r", 1 << 30)) <= n:
                return True
        return False

    def hold_seconds(hk, n):
        start = now
        for snap in hist_desc:
            if in_top(snap["top"], hk, n):
                start = snap["ts"]
            else:
                break
        return now - start

    top_members = miners[:top_n]
    members = []
    for m in top_members:
        hk = m["hotkey"]
        members.append({
            "rank": int(m.get("rank_pos", 0) or 0),
            "hotkey": hk,
            "hotkey_short": m.get("hotkey_short") or _short(hk),
            "coldkey": m.get("coldkey", ""),
            "coldkey_short": m.get("coldkey_short") or _short(m.get("coldkey", "")),
            "ip": m.get("ip", ""),
            # carry shared-counts so the template can flag a sybil-style cluster
            # among the leaders (same coldkey/IP across several top ranks).
            "coldkey_count": int(m.get("coldkey_count", 0) or 0),
            "ip_count": int(m.get("ip_count", 0) or 0),
            "hold_label": _dur_label(hold_seconds(hk, top_n)) if hist_desc else "—",
            "reg_label": (_dur_label(m["tenure_days"] * 86400)
                          if m.get("tenure_days") is not None else "—"),
        })

    enough = len(history) >= 2
    leader = top_members[0]
    leader_hk = leader["hotkey"]

    # 24h turnover: current top-N hotkeys not present in the ~24h-ago snapshot.
    turnover_24h = None
    target = now - 86400
    past = min(history, key=lambda s: abs(s["ts"] - target)) if history else None
    if past is not None and abs(past["ts"] - target) <= 6 * 3600:
        past_set = {e["hk"] for e in past["top"] if int(e.get("r", 1 << 30)) <= top_n}
        turnover_24h = sum(1 for m in top_members if m["hotkey"] not in past_set)

    holds = [hold_seconds(m["hotkey"], top_n) for m in top_members]
    median_hold = sorted(holds)[len(holds) // 2] if holds else 0.0

    return {
        "top_n": top_n,
        "enough": enough,
        "samples": len(history),
        "members": members,
        "leader_hotkey_short": leader.get("hotkey_short") or _short(leader_hk),
        "leader_top1_label": _dur_label(hold_seconds(leader_hk, 1)) if enough else "—",
        "median_hold_label": _dur_label(median_hold) if enough else "—",
        "turnover_24h": turnover_24h if turnover_24h is not None else "—",
        "tracking_since_label": (
            datetime.fromtimestamp(since, tz=timezone.utc).astimezone()
            .strftime("%Y-%m-%d %H:%M") if since else None),
        "tracking_label": _dur_label(now - since) if since else "—",
        "interval_label": interval_label,
    }


# ─── singleton ───────────────────────────────────────────────────────────────
_service: NetworkIndexService | None = None


def init_network_index(sdk_client, miner_rewards_svc, db, *,
                       start: bool = True, **kw) -> NetworkIndexService:
    global _service
    _service = NetworkIndexService(sdk_client, miner_rewards_svc, db, **kw)
    if start:
        _service.start()
    return _service


def get_network_index() -> NetworkIndexService | None:
    return _service
