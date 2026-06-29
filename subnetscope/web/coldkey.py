"""Read-only coldkey balance + stake-position cache for the wallet modal.

Strict policy: this module *only* uses the public SS58 address to query
the chain. It never reads, requests, or stores private keys, mnemonics,
or wallet-file passwords.

Per-address it returns:
  * Free TAO balance (from `subtensor.get_balance(ss58)`)
  * Per-(hotkey, netuid) alpha stake positions, joined to the cached
    subnet rows so each position carries a name + TAO-equivalent value
    (`alpha * pool.price_tao_per_alpha`).
  * Aggregate totals: free TAO, staked TAO value, total portfolio value.

A short-TTL in-memory cache (default 60 s) coalesces concurrent requests
and keeps repeated modal opens cheap.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# SS58 addresses on Substrate are base58-encoded, 47-48 chars, start with
# digits/letters (mainnet TAO addresses begin with '5'). This is a quick
# sanity gate so we don't pass garbage straight to the chain RPC.
_SS58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")

DEFAULT_TTL = 60.0
# Cold-start of a per-thread bittensor websocket + metadata fetch can take
# ~30-40s on the first call. After that, calls hit the cache in <50 ms.
LOOKUP_TIMEOUT_S = 45.0


def is_valid_ss58(s: str) -> bool:
    return bool(s) and bool(_SS58_RE.match(s.strip()))


# --------------------------------------------------------------- data shapes


@dataclass
class _Snapshot:
    free_tao: float = 0.0
    positions: list[dict[str, Any]] = field(default_factory=list)
    total_stake_value_tao: float = 0.0
    total_value_tao: float = 0.0
    fetched_at: float = 0.0
    error: str | None = None


# ------------------------------------------------------------------- service


class ColdkeyService:
    def __init__(self, sdk_client, ttl: float = DEFAULT_TTL):
        self._sdk_client = sdk_client
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._cache: dict[str, _Snapshot] = {}
        self._fetching: set[str] = set()
        self._wallet_hist_last: dict[str, float] = {}

    # ------------------------------------------------------------------ chain

    def _get_subtensor(self):
        """Return a Subtensor safe to use from the calling thread.

        Bittensor's websocket is not thread-safe, and our coldkey lookups
        run from background daemon threads that may overlap with the
        scanner. Prefer a per-thread connection (created on first use)
        so we never collide with the scanner's main connection.
        """
        if self._sdk_client is None:
            return None
        try:
            tls_fn = getattr(self._sdk_client, "_thread_subtensor", None)
            if tls_fn is not None:
                return tls_fn()
            return self._sdk_client.subtensor
        except Exception:  # noqa: BLE001
            return None

    def _fetch_from_chain(self, ss58: str) -> _Snapshot:
        """One round-trip pair against the chain to build a fresh snapshot."""
        sub = self._get_subtensor()
        if sub is None:
            return _Snapshot(fetched_at=time.time(),
                             error="subtensor not initialised")

        try:
            bal = sub.get_balance(ss58)
            free_tao = float(getattr(bal, "tao", 0.0)) if bal is not None else 0.0
        except Exception as e:  # noqa: BLE001
            log.warning("coldkey %s: get_balance failed: %s", ss58, e)
            return _Snapshot(fetched_at=time.time(),
                             error=f"get_balance failed: {e}")

        try:
            stakes = sub.get_stake_info_for_coldkey(ss58) or []
        except Exception as e:  # noqa: BLE001
            log.warning("coldkey %s: get_stake_info_for_coldkey failed: %s",
                        ss58, e)
            stakes = []

        # Build a price/name lookup from the cached scanner rows so we can
        # convert per-subnet alpha into a TAO-equivalent value.
        subnet_lookup = self._subnet_lookup()
        hotkey_names = self._hotkey_names()
        positions: list[dict[str, Any]] = []
        total_stake_value_tao = 0.0
        for s in stakes:
            try:
                netuid = int(getattr(s, "netuid"))
                hotkey_ss58 = str(getattr(s, "hotkey_ss58", "") or "")
                stake_alpha = _bal_to_float(getattr(s, "stake", None))
                emission_alpha = _bal_to_float(getattr(s, "emission", None))
            except Exception:  # noqa: BLE001
                continue
            if stake_alpha <= 0 and emission_alpha <= 0:
                continue
            info = subnet_lookup.get(netuid) or {}
            # Root subnet (netuid 0) is denominated in TAO directly.
            price = 1.0 if netuid == 0 else float(info.get("price", 0.0))
            stake_value_tao = stake_alpha * price
            emission_value_tao = emission_alpha * price
            total_stake_value_tao += stake_value_tao
            positions.append({
                "netuid": netuid,
                "name": info.get("name") or (
                    "root" if netuid == 0 else f"sn{netuid}"),
                "hotkey": hotkey_ss58,
                "hotkey_short": _short(hotkey_ss58),
                "hotkey_name": hotkey_names.get(hotkey_ss58) or None,
                "stake_alpha": stake_alpha,
                "stake_value_tao": stake_value_tao,
                "price_tao_per_alpha": price,
                "pool_tao_in": float(info.get("tao_in", 0.0)),
                "pool_alpha_in": float(info.get("alpha_in", 0.0)),
                "emission_alpha": emission_alpha,
                "emission_value_tao": emission_value_tao,
            })

        # Also surface hotkeys this coldkey has *registered* on a subnet, even
        # when they hold no alpha stake (e.g. a freshly registered miner). The
        # stake loop above only yields staked positions, so registered-but-
        # unstaked hotkeys would otherwise be invisible in the wallet modal.
        seen = {(p["hotkey"], int(p["netuid"])) for p in positions}
        registered_pairs: set[tuple[str, int]] = set()
        try:
            owned = sub.get_owned_hotkeys(ss58) or []
            for hk in owned:
                hk_s = str(hk)
                try:
                    nets = sub.get_netuids_for_hotkey(hk_s) or []
                except Exception:  # noqa: BLE001
                    nets = []
                for nu in nets:
                    registered_pairs.add((hk_s, int(nu)))
        except Exception as e:  # noqa: BLE001
            log.debug("coldkey %s: registered-hotkey lookup failed: %s", ss58, e)

        # Flag staked positions that are also registered, then append the
        # registered-only (zero-stake) hotkeys.
        for p in positions:
            p["registered"] = (p["hotkey"], int(p["netuid"])) in registered_pairs
        for hk_s, nu in registered_pairs:
            if (hk_s, nu) in seen:
                continue
            info = subnet_lookup.get(nu) or {}
            price = 1.0 if nu == 0 else float(info.get("price", 0.0))
            positions.append({
                "netuid": nu,
                "name": info.get("name") or ("root" if nu == 0 else f"sn{nu}"),
                "hotkey": hk_s,
                "hotkey_short": _short(hk_s),
                "hotkey_name": hotkey_names.get(hk_s) or None,
                "stake_alpha": 0.0,
                "stake_value_tao": 0.0,
                "price_tao_per_alpha": price,
                "pool_tao_in": float(info.get("tao_in", 0.0)),
                "pool_alpha_in": float(info.get("alpha_in", 0.0)),
                "emission_alpha": 0.0,
                "emission_value_tao": 0.0,
                "registered": True,
            })

        # Staked positions first (by value), registered-only ones at the bottom.
        positions.sort(key=lambda p: p["stake_value_tao"], reverse=True)
        total_value_tao = free_tao + total_stake_value_tao

        return _Snapshot(
            free_tao=free_tao,
            positions=positions,
            total_stake_value_tao=total_stake_value_tao,
            total_value_tao=total_value_tao,
            fetched_at=time.time(),
            error=None,
        )

    def _hotkey_names(self) -> dict[str, str]:
        """SS58 -> friendly name from the configured ``hotkeys.entries``."""
        try:
            from .cache import get_scanner
            cfg = get_scanner().cfg
        except Exception:  # noqa: BLE001
            return {}
        return {(e.ss58 or "").strip(): (e.name or "").strip()
                for e in (cfg.hotkeys.entries or []) if e.ss58 and e.name}

    def _subnet_lookup(self) -> dict[int, dict[str, Any]]:
        try:
            from .cache import get_scanner
            scanner = get_scanner()
            scan = scanner.get()
        except Exception:  # noqa: BLE001
            return {}
        out: dict[int, dict[str, Any]] = {}
        for r in scan.rows:
            out[int(r.netuid)] = {
                "name": r.name or f"sn{r.netuid}",
                "price": float(r.price_tao_per_alpha or 0.0),
                "category": r.category,
                "tao_in": float(r.tao_in or 0.0),
                "alpha_in": float(r.alpha_in or 0.0),
            }
        return out

    # ----------------------------------------------------------------- public

    def lookup(self, ss58: str, force: bool = False) -> dict[str, Any]:
        """Return a fresh-ish snapshot dict for `ss58`.

        Always returns quickly. If a cached value exists, it's returned
        immediately and a background refresh kicks off when stale.
        """
        ss58 = (ss58 or "").strip()
        if not is_valid_ss58(ss58):
            return _result(ss58, _Snapshot(
                fetched_at=time.time(),
                error="invalid SS58 address",
            ))

        now = time.time()
        with self._lock:
            snap = self._cache.get(ss58)
            fresh = (snap is not None
                     and snap.error is None
                     and (now - snap.fetched_at) < self.ttl)

            if fresh and not force:
                return _result(ss58, snap)

            if ss58 not in self._fetching:
                self._fetching.add(ss58)
                threading.Thread(
                    target=self._bg_refresh, args=(ss58, force),
                    name=f"coldkey-{ss58[:8]}", daemon=True,
                ).start()

        # If we have nothing cached, block briefly waiting for first result.
        if snap is None:
            deadline = time.time() + LOOKUP_TIMEOUT_S
            while time.time() < deadline:
                time.sleep(0.2)
                with self._lock:
                    snap = self._cache.get(ss58)
                if snap is not None:
                    break

        if snap is None:
            return _result(ss58, _Snapshot(
                fetched_at=time.time(),
                error="lookup timed out",
            ))
        return _result(ss58, snap, stale=force or
                       (time.time() - snap.fetched_at) >= self.ttl)

    def _maybe_record_wallet_history(
        self, ss58: str, snap: _Snapshot, *, force_refresh: bool,
    ) -> None:
        now = time.time()
        gap = 0.0 if force_refresh else 300.0
        with self._lock:
            last = self._wallet_hist_last.get(ss58, 0.0)
            if now - last < gap:
                return
            self._wallet_hist_last[ss58] = now
        try:
            from .cache import get_scanner

            get_scanner().db.append_wallet_stake_snapshot(
                ss58,
                free_tao=snap.free_tao,
                positions=snap.positions,
            )
        except Exception:  # noqa: BLE001
            log.debug("wallet history append failed", exc_info=True)

    def _bg_refresh(self, ss58: str, force_refresh: bool = False) -> None:
        try:
            snap = self._fetch_from_chain(ss58)
            with self._lock:
                # Don't overwrite a good snapshot with an error from a
                # transient failure — keep the last good one and just mark
                # the cache as stale on the next read.
                prior = self._cache.get(ss58)
                if snap.error is None or prior is None:
                    self._cache[ss58] = snap
                else:
                    log.debug("coldkey %s: refresh failed (%s); keeping prior",
                              ss58, snap.error)
            if snap.error is None:
                self._maybe_record_wallet_history(
                    ss58, snap, force_refresh=force_refresh)
        finally:
            with self._lock:
                self._fetching.discard(ss58)

    def prewarm(self) -> None:
        """Open a per-thread subtensor on a dedicated background thread so
        the *first* real user query doesn't pay the ~30 s cold-start cost
        of the websocket handshake + bittensor metadata fetch.
        """
        def _open() -> None:
            try:
                sub = self._get_subtensor()
                if sub is not None:
                    log.info("coldkey service: prewarm subtensor ready")
            except Exception:  # noqa: BLE001
                log.debug("coldkey service: prewarm failed", exc_info=True)
        threading.Thread(target=_open, name="coldkey-prewarm",
                         daemon=True).start()


# ------------------------------------------------------------------ helpers


def _bal_to_float(b: Any) -> float:
    if b is None:
        return 0.0
    v = getattr(b, "tao", None)
    if v is not None:
        try:
            return float(v)
        except Exception:  # noqa: BLE001
            pass
    try:
        return float(b)
    except Exception:  # noqa: BLE001
        return 0.0


def _short(s: str, head: int = 6, tail: int = 4) -> str:
    if not s or len(s) <= head + tail + 1:
        return s
    return f"{s[:head]}…{s[-tail:]}"


def _result(ss58: str, snap: _Snapshot, *, stale: bool = False) -> dict[str, Any]:
    return {
        "ss58": ss58,
        "free_tao": round(snap.free_tao, 9),
        "total_stake_value_tao": round(snap.total_stake_value_tao, 9),
        "total_value_tao": round(snap.total_value_tao, 9),
        "positions": snap.positions,
        "position_count": len(snap.positions),
        "ts": snap.fetched_at or None,
        "ts_iso": (datetime.fromtimestamp(snap.fetched_at, tz=timezone.utc)
                   .astimezone().isoformat()) if snap.fetched_at else None,
        "stale": bool(stale),
        "error": snap.error,
    }


# ---------------------------------------------------------------- singleton


_service: ColdkeyService | None = None


def init_coldkey_service(sdk_client, ttl: float = DEFAULT_TTL,
                         prewarm: bool = True) -> ColdkeyService:
    global _service
    _service = ColdkeyService(sdk_client=sdk_client, ttl=ttl)
    if prewarm:
        _service.prewarm()
    return _service


def get_coldkey_service() -> ColdkeyService | None:
    return _service
