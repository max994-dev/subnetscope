"""Alert evaluator. Runs after every fresh chain scan.

Triggers:
  burn-jump      burn fee >= 1.5x of the snapshot ~1h ago
  recommended    easy_entry_score >= threshold
  slot-open      previously-full subnet now has UID slots free
  tempo-near     <= N blocks until next emission tick
  new-subnet     a netuid we have never seen before
"""
from __future__ import annotations

import json
import logging

from ..types import SubnetRow
from .score import score_all
from .state_db import StateDB

log = logging.getLogger(__name__)

BURN_JUMP_RATIO = 1.50
BURN_LOOKBACK_SECONDS = 3600
RECOMMENDED_THRESHOLD = 60.0   # fires for top tier of easy_entry_score
DEDUPE_WINDOW_SECONDS = 6 * 3600
TEMPO_NEAR_BLOCKS = 5


def _format_burn(x: float) -> str:
    if x >= 1:
        return f"{x:.4f} t"
    if x >= 0.01:
        return f"{x:.5f} t"
    return f"{x:.6f} t"


def evaluate(db: StateDB, rows: list[SubnetRow], block: int,
             scan_ts: int) -> int:
    """Run all alert rules. Returns number of new alerts inserted."""
    new_count = 0
    scores = score_all(rows)

    for r in rows:
        prev = db.snapshot_at_or_before(
            r.netuid, scan_ts - BURN_LOOKBACK_SECONDS)

        if prev and prev["burn_tao"] and r.recycle_tao > 0:
            ratio = r.recycle_tao / prev["burn_tao"]
            if ratio >= BURN_JUMP_RATIO:
                if not db.alert_exists_recently(
                        "burn-jump", r.netuid, DEDUPE_WINDOW_SECONDS):
                    msg = (f"Burn fee jumped {ratio:.1f}x in 1h: "
                           f"{_format_burn(prev['burn_tao'])} -> "
                           f"{_format_burn(r.recycle_tao)}")
                    if db.insert_alert(scan_ts, "burn-jump", r.netuid,
                                       r.name, msg, json.dumps({
                                           "old": prev["burn_tao"],
                                           "new": r.recycle_tao,
                                           "ratio": ratio,
                                       })):
                        new_count += 1

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

        sb = scores.get(r.netuid)
        if sb and sb.score >= RECOMMENDED_THRESHOLD:
            if not db.alert_exists_recently(
                    "recommended", r.netuid, DEDUPE_WINDOW_SECONDS):
                why = "; ".join(sb.why[:3]) or "matches your criteria"
                msg = f"Recommended (score {sb.score:.0f}/100): {why}"
                if db.insert_alert(scan_ts, "recommended", r.netuid,
                                   r.name, msg, json.dumps({
                                       "score": sb.score,
                                       "why": sb.why,
                                   })):
                    new_count += 1

        if r.tempo and r.tempo > 0:
            blocks_into_cycle = block % r.tempo
            blocks_to_tick = r.tempo - blocks_into_cycle
            if 0 < blocks_to_tick <= TEMPO_NEAR_BLOCKS:
                window = max(60, r.tempo * 12)
                if not db.alert_exists_recently("tempo-near", r.netuid, window):
                    msg = (f"Emission tick in ~{blocks_to_tick} blocks "
                           f"(~{blocks_to_tick * 12}s) - "
                           f"submit weights/work now")
                    if db.insert_alert(scan_ts, "tempo-near", r.netuid,
                                       r.name, msg, json.dumps({
                                           "blocks_to_tick": blocks_to_tick,
                                           "tempo": r.tempo,
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
