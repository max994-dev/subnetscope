"""Resolve configured watch-hotkeys vs a subnet (registered + UID).

Uses read-only chain queries (`get_uid_for_hotkey_on_subnet`). Public SS58
only — same policy as the coldkey modal.

When miner-rewards is available, attaches emission rank / incentive / share
for that UID (miner or validator pool), same basis as the miner table.
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import HotkeyEntry
from .coldkey import is_valid_ss58

log = logging.getLogger(__name__)


def _short_ss58(s: str, head: int = 6, tail: int = 4) -> str:
    if not s or len(s) <= head + tail + 1:
        return s
    return f"{s[:head]}…{s[-tail:]}"


def _rank_percentile(rank_pos: int, pool_size: int) -> float:
    """100 = top of pool by emission, 0 = bottom (linear by rank position)."""
    if pool_size <= 1:
        return 100.0 if rank_pos <= 1 else 0.0
    return round(100.0 * (1.0 - (float(rank_pos) - 1.0) / float(pool_size - 1)), 1)


def registration_status_for_subnet(
    sdk_client: Any,
    netuid: int,
    entries: list[HotkeyEntry],
    miner_rewards_svc: Any = None,
    coldkey_dir: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return one row per configured hotkey with UID if registered on `netuid`.

    Also resolves each hotkey's owner coldkey from chain (``get_hotkey_owner``)
    and, when that coldkey is in ``coldkey_dir`` (ss58 -> name), surfaces its
    friendly name so the register command can be pre-filled with the real
    wallet name instead of a ``<coldkey>`` placeholder.
    """
    out: list[dict[str, Any]] = []
    if not entries or sdk_client is None:
        return out
    ck_dir = coldkey_dir or {}

    try:
        tls_fn = getattr(sdk_client, "_thread_subtensor", None)
        sub = tls_fn() if tls_fn is not None else getattr(sdk_client, "subtensor", None)
    except Exception as e:  # noqa: BLE001
        log.warning("watch_hotkeys: no subtensor (%s)", e)
        return out

    if sub is None:
        return out

    for e in entries:
        hk = (e.ss58 or "").strip()
        row: dict[str, Any] = {
            "name": (e.name or "").strip(),
            "note": (e.note or "").strip(),
            "ss58": hk,
            "hotkey_short": _short_ss58(hk) if hk else "—",
            "uid": None,
            "registered": False,
            "error": None,
        }
        if not hk:
            row["error"] = "missing SS58"
            out.append(row)
            continue
        if not is_valid_ss58(hk):
            row["error"] = "invalid SS58"
            out.append(row)
            continue
        try:
            uid = sub.get_uid_for_hotkey_on_subnet(hotkey_ss58=hk, netuid=netuid)
        except Exception as ex:  # noqa: BLE001
            log.debug("watch_hotkeys uid lookup failed: %s", ex, exc_info=True)
            row["error"] = f"{type(ex).__name__}: {ex}"
            out.append(row)
            continue
        row["uid"] = uid
        row["registered"] = uid is not None
        # Owner coldkey (works whether or not the hotkey is on THIS subnet, as
        # long as it's been registered somewhere); match to the configured
        # coldkey directory for a friendly wallet name.
        try:
            owner = sub.get_hotkey_owner(hk)
        except Exception:  # noqa: BLE001
            owner = None
        owner = (str(owner).strip() if owner else "") or None
        row["coldkey_ss58"] = owner
        row["coldkey_short"] = _short_ss58(owner) if owner else None
        row["coldkey_name"] = ck_dir.get(owner) if owner else None
        out.append(row)

    uids = [int(r["uid"]) for r in out if r.get("registered") and r.get("uid") is not None]
    by_uid: dict[int, dict[str, Any]] = {}
    batch_err: str | None = None
    if uids and miner_rewards_svc is not None:
        try:
            by_uid, batch_err = miner_rewards_svc.lookup_uid_rows(
                netuid, uids, timeout_s=None)
        except Exception as ex:  # noqa: BLE001
            log.debug("watch_hotkeys metrics failed: %s", ex, exc_info=True)
            by_uid, batch_err = {}, f"{type(ex).__name__}: {ex}"

    for r in out:
        r["metrics"] = None
        r["metrics_error"] = None
        if not r.get("registered") or r.get("uid") is None:
            continue
        if miner_rewards_svc is None:
            r["metrics_error"] = "emission metrics unavailable"
            continue
        if batch_err:
            r["metrics_error"] = batch_err
            continue
        uid_i = int(r["uid"])
        m = by_uid.get(uid_i)
        if not m:
            r["metrics_error"] = "no metagraph row for uid"
            continue
        pool = int(m.get("pool_size") or 0)
        rp = int(m.get("rank_pos") or 0)
        m2 = dict(m)
        m2["rank_percentile"] = _rank_percentile(rp, pool)
        r["metrics"] = m2

    return out


def any_watch_hotkey_registered(
    sdk_client: Any,
    netuid: int,
    entries: list[HotkeyEntry],
) -> bool:
    """True if any ``hotkeys.entries`` SS58 has a UID on ``netuid`` (read-only)."""
    if not entries or sdk_client is None:
        return False
    try:
        tls_fn = getattr(sdk_client, "_thread_subtensor", None)
        sub = tls_fn() if tls_fn is not None else getattr(sdk_client, "subtensor", None)
    except Exception:  # noqa: BLE001
        return False
    if sub is None:
        return False
    for e in entries:
        hk = (e.ss58 or "").strip()
        if not hk or not is_valid_ss58(hk):
            continue
        try:
            uid = sub.get_uid_for_hotkey_on_subnet(hotkey_ss58=hk, netuid=netuid)
        except Exception:  # noqa: BLE001
            continue
        if uid is not None:
            return True
    return False
