"""Per-subnet emission split (owner / validators / miners).

In dTAO/Yuma the per-block alpha emission going to UIDs is split into
three buckets:

  owner_pct      = SubnetOwnerCut / u16::MAX                 (chain-global, ~18%)
  validators_pct = (1 - owner_pct) * (kappa / u16::MAX)      (per-subnet)
  miners_pct     = (1 - owner_pct) * (1 - kappa / u16::MAX)  (per-subnet)

`SubnetOwnerCut` is a single u16 stored under `SubtensorModule` and applies to
every subnet (defaults to ~18%). `kappa` is per-subnet and we already collect
it during the bulk scan, so we don't need extra RPC calls per detail page —
just a *single* cached read for the global owner cut.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

U16_MAX = 65535
DEFAULT_OWNER_CUT_U16 = 11796   # ≈ 18.0 %  (Bittensor protocol default)
DEFAULT_KAPPA_U16     = 32767   # ≈ 50.0 %  (yuma consensus default)
OWNER_CUT_TTL_S       = 600     # 10 min — this knob essentially never changes


@dataclass
class _OwnerCutCache:
    value_u16: int = DEFAULT_OWNER_CUT_U16
    fetched_at: float = 0.0
    source: str = "default"   # "chain" once we've successfully read it


class EmissionSplitService:
    """Caches the global SubnetOwnerCut and computes per-subnet splits."""

    def __init__(self, sdk_client: Any | None, ttl: float = OWNER_CUT_TTL_S):
        self._sdk = sdk_client
        self._ttl = max(60.0, float(ttl))
        self._cache = _OwnerCutCache()
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    def _query_owner_cut(self) -> int | None:
        """Read `SubtensorModule.SubnetOwnerCut` from chain. Returns u16 or None."""
        if self._sdk is None:
            return None
        try:
            sub = self._sdk.subtensor
            substrate = getattr(sub, "substrate", None)
            if substrate is None:
                return None
            res = substrate.query(
                module="SubtensorModule",
                storage_function="SubnetOwnerCut",
            )
            v = getattr(res, "value", res)
            if v is None:
                return None
            iv = int(v)
            if iv < 0 or iv > U16_MAX:
                return None
            return iv
        except Exception as e:  # noqa: BLE001
            log.debug("SubnetOwnerCut query failed: %s", e)
            return None

    def _refresh_if_stale(self) -> None:
        now = time.time()
        with self._lock:
            age = now - self._cache.fetched_at
            if age < self._ttl and self._cache.fetched_at > 0:
                return
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            v = self._query_owner_cut()
            with self._lock:
                if v is not None:
                    self._cache = _OwnerCutCache(
                        value_u16=v, fetched_at=time.time(), source="chain")
                else:
                    # Keep prior value but bump timestamp so we don't hammer
                    # the chain on every page load when the RPC is sad.
                    self._cache.fetched_at = time.time()
        finally:
            self._refresh_lock.release()

    def owner_cut(self) -> tuple[int, str, float]:
        """Return (owner_cut_u16, source, age_seconds)."""
        self._refresh_if_stale()
        with self._lock:
            c = self._cache
            age = max(0.0, time.time() - c.fetched_at) if c.fetched_at else 0.0
            return c.value_u16, c.source, age

    def split(
        self,
        kappa_u16: int | None,
        emission_per_day_tao: float | None,
    ) -> dict[str, Any]:
        """Compute the 3-way emission split for one subnet.

        Returns percentages (0-100), per-day TAO amounts, and the raw
        kappa/owner-cut values used. Safe with `None` inputs (uses defaults).
        """
        owner_u16, source, age = self.owner_cut()
        kappa = int(kappa_u16) if kappa_u16 is not None else DEFAULT_KAPPA_U16
        if kappa < 0 or kappa > U16_MAX:
            kappa = DEFAULT_KAPPA_U16

        owner_frac = owner_u16 / U16_MAX
        kappa_frac = kappa / U16_MAX
        validators_frac = (1.0 - owner_frac) * kappa_frac
        miners_frac     = (1.0 - owner_frac) * (1.0 - kappa_frac)

        emi = float(emission_per_day_tao or 0.0)
        return {
            "owner_pct":      round(owner_frac * 100.0, 2),
            "validators_pct": round(validators_frac * 100.0, 2),
            "miners_pct":     round(miners_frac * 100.0, 2),
            "owner_tao_day":      emi * owner_frac,
            "validators_tao_day": emi * validators_frac,
            "miners_tao_day":     emi * miners_frac,
            "emission_per_day_tao": emi,
            "kappa_u16":     kappa,
            "kappa_pct":     round(kappa_frac * 100.0, 2),
            "owner_cut_u16": owner_u16,
            "owner_cut_pct": round(owner_frac * 100.0, 2),
            "owner_cut_source": source,
            "owner_cut_age_s": round(age, 1),
        }

    def prewarm(self) -> None:
        """Kick a background refresh so the first page load is instant."""
        t = threading.Thread(
            target=self._refresh_if_stale,
            name="emission-split-prewarm", daemon=True)
        t.start()


# ─── module-level singleton ──────────────────────────────────────────────────
_service: EmissionSplitService | None = None


def init_emission_split(
    sdk_client: Any | None,
    ttl: float = OWNER_CUT_TTL_S,
    prewarm: bool = True,
) -> EmissionSplitService:
    global _service
    _service = EmissionSplitService(sdk_client=sdk_client, ttl=ttl)
    if prewarm:
        _service.prewarm()
    return _service


def get_emission_split_service() -> EmissionSplitService:
    """Return the configured service, or a no-SDK fallback that uses defaults."""
    global _service
    if _service is None:
        _service = EmissionSplitService(sdk_client=None)
    return _service
