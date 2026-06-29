"""History snapshotter: writes one DB row per subnet per chain scan,
then runs the alert engine. Called by CachedScanner after every fresh scan.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..types import ScanResult
from . import alerts as alerts_mod
from .state_db import StateDB

log = logging.getLogger(__name__)


def record_scan(
    db: StateDB,
    scan: ScanResult,
    *,
    sdk_client: Any = None,
    watch_hotkeys: list | None = None,
) -> dict:
    """Persist a scan into history and emit any new alerts. Returns a small
    dict of stats so callers can log it."""
    ts = int(scan.fetched_at.timestamp())
    block = int(scan.head_block)

    # Track if the DB was empty BEFORE we touched it; if so, suppress
    # new-subnet alerts (every netuid would otherwise be "new").
    is_first_scan = _db_is_empty(db)

    new_netuids = db.upsert_identity(ts, scan.rows)
    written = db.write_snapshot(ts, block, scan.rows)
    alert_count = alerts_mod.evaluate(
        db, scan.rows, block, ts,
        sdk_client=sdk_client,
        watch_hotkeys=watch_hotkeys,
    )
    if not is_first_scan:
        alert_count += alerts_mod.emit_new_subnet_alerts(
            db, new_netuids, scan.rows, ts)

    log.info(
        "snapshot ts=%d block=%d wrote=%d new_subnets=%d alerts=%d "
        "(first_scan=%s)",
        ts, block, written, len(new_netuids), alert_count, is_first_scan)
    return {
        "ts": ts,
        "block": block,
        "rows_written": written,
        "new_netuids": new_netuids,
        "alerts_emitted": alert_count,
        "first_scan": is_first_scan,
    }


def _db_is_empty(db: StateDB) -> bool:
    with db._cursor() as cur:
        cur.execute("SELECT 1 FROM subnet_identity LIMIT 1")
        return cur.fetchone() is None
