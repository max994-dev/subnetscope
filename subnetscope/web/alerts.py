"""Alert evaluator. Runs after every fresh chain scan.

Triggers:
  slot-open      previously-full subnet now has UID slots free
  tempo-near     <= N blocks until the next epoch boundary, i.e. when validators
                   start a fresh task/scoring round. Only fires if a configured
                   watch hotkey (``hotkeys.entries`` in config) is registered on
                   that subnet (requires chain lookup per candidate subnet).
                   (Internal kind stays "tempo-near"; surfaced as "validator
                   tasks" in the UI.)
  new-subnet     a netuid we have never seen before

These alerts appear in the web **Alerts** panel (GET ``/api/alerts``) and
in the 🔔 dropdown; each row links to ``/subnet/{netuid}``.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..config import HotkeyEntry
from ..types import SubnetRow
from .state_db import StateDB
from .watch_hotkeys import any_watch_hotkey_registered

log = logging.getLogger(__name__)

SNAPSHOT_LOOKBACK_SECONDS = 3600  # ~1h snapshot for slot-open compare
DEDUPE_WINDOW_SECONDS = 6 * 3600
TEMPO_NEAR_BLOCKS = 5
TEMPO_BLOCK_SECONDS = 12  # ~Finney block time; used in alert copy / UI hints


def tempo_blocks_to_tick(r: SubnetRow, block: int) -> int | None:
    """Blocks until the next emission tick, or ``None`` if tempo is unknown."""
    if not r.tempo or r.tempo <= 0:
        return None
    blocks_into_cycle = block % r.tempo
    return r.tempo - blocks_into_cycle


def is_tempo_near(r: SubnetRow, block: int) -> bool:
    """True when within ``TEMPO_NEAR_BLOCKS`` of the next emission tick."""
    btt = tempo_blocks_to_tick(r, block)
    return btt is not None and 0 < btt <= TEMPO_NEAR_BLOCKS


def _format_burn(x: float) -> str:
    if x >= 1:
        return f"{x:.4f} t"
    if x >= 0.01:
        return f"{x:.5f} t"
    return f"{x:.6f} t"


def evaluate(
    db: StateDB,
    rows: list[SubnetRow],
    block: int,
    scan_ts: int,
    *,
    sdk_client: Any | None = None,
    watch_hotkeys: list[HotkeyEntry] | None = None,
) -> int:
    """Run all alert rules. Returns number of new alerts inserted."""
    new_count = 0
    hk_entries = list(watch_hotkeys or [])

    for r in rows:
        prev = db.snapshot_at_or_before(
            r.netuid, scan_ts - SNAPSHOT_LOOKBACK_SECONDS)

        if prev and prev["max_n"] and prev["subnetwork_n"] is not None:
            was_full = prev["subnetwork_n"] >= prev["max_n"]
            now_open = r.slots_free > 0
            if was_full and now_open:
                if not db.alert_exists_recently(
                        "slot-open", r.netuid, DEDUPE_WINDOW_SECONDS):
                    msg = (f"Slot opened - was {prev['subnetwork_n']}/"
                           f"{prev['max_n']}, now {r.subnetwork_n}/{r.max_n} "
                           f"({r.slots_free} free)")
                    if db.insert_alert(scan_ts, "slot-open", r.netuid,
                                       r.name, msg,
                                       json.dumps({"slots_free": r.slots_free})):
                        new_count += 1

        blocks_to_tick = tempo_blocks_to_tick(r, block)
        if blocks_to_tick is None or not (0 < blocks_to_tick <= TEMPO_NEAR_BLOCKS):
            continue
        if not hk_entries:
            continue
        if not any_watch_hotkey_registered(sdk_client, r.netuid, hk_entries):
            continue
        window = max(60, (r.tempo or 1) * TEMPO_BLOCK_SECONDS)
        if not db.alert_exists_recently("tempo-near", r.netuid, window):
            eta_s = blocks_to_tick * TEMPO_BLOCK_SECONDS
            msg = (f"Validators will start sending tasks in ~{blocks_to_tick} "
                   f"blocks (~{eta_s}s) — the next epoch begins, so validators "
                   f"start a fresh scoring round. A watch hotkey from config is "
                   f"registered here; keep your miner up and answering.")
            if db.insert_alert(scan_ts, "tempo-near", r.netuid,
                               r.name, msg, json.dumps({
                                   "blocks_to_tick": blocks_to_tick,
                                   "tempo": r.tempo,
                                   "watch_hotkeys": True,
                               })):
                new_count += 1

    return new_count


def emit_new_subnet_alerts(db: StateDB, new_netuids: list[int],
                           rows: list[SubnetRow], scan_ts: int) -> int:
    """Insert one new-subnet alert per truly-new netuid. Suppress on the very
    first scan (when every netuid looks "new" because the DB is empty)."""
    if not new_netuids:
        return 0
    n = 0
    by_id = {r.netuid: r for r in rows}
    for nid in new_netuids:
        r = by_id.get(nid)
        if not r:
            continue
        msg = (f"New subnet appeared: {r.name or 'unnamed'} "
               f"(category={r.category}, "
               f"burn={_format_burn(r.recycle_tao)}, "
               f"slots {r.subnetwork_n}/{r.max_n})")
        if db.insert_alert(scan_ts, "new-subnet", nid, r.name, msg,
                           json.dumps({"netuid": nid,
                                       "category": r.category})):
            n += 1
    return n
