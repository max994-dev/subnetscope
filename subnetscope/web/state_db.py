"""SQLite-backed persistence for the web layer:
  * history snapshots  - one row per netuid per scan, used for sparklines
  * subnet identity    - last-known name + first-seen timestamp (for new-subnet alerts)
  * alerts             - rolling event log
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  netuid INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  block INTEGER NOT NULL,
  burn_tao REAL,
  price_tao_per_alpha REAL,
  emission_per_day REAL,
  active_miners INTEGER,
  top1_share REAL,
  top10_share REAL,
  subnetwork_n INTEGER,
  max_n INTEGER,
  tao_in REAL,
  alpha_in REAL,
  reward_shape TEXT,
  PRIMARY KEY (netuid, ts)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_netuid_ts ON snapshots(netuid, ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);

CREATE TABLE IF NOT EXISTS subnet_identity (
  netuid INTEGER PRIMARY KEY,
  name TEXT,
  category TEXT,
  first_seen_ts INTEGER NOT NULL,
  last_seen_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL,          -- burn-jump | recommended | slot-open | tempo-near | new-subnet
  netuid INTEGER,
  name TEXT,
  message TEXT NOT NULL,
  payload TEXT                  -- json blob
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_dedup ON alerts(ts, kind, netuid);
"""


class StateDB:
    """Thin SQLite wrapper. Opens a single shared connection in WAL mode and
    serializes writes through a re-entrant lock (good enough for one
    snapshotter thread + many read-only requests)."""

    def __init__(self, path: str | Path):
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None,
            timeout=10.0,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)

    # -- helpers ---------------------------------------------------------

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- snapshots -------------------------------------------------------

    def write_snapshot(self, ts: int, block: int, rows) -> int:
        """Bulk-insert one snapshot per row. Returns rows written."""
        params = []
        for r in rows:
            params.append((
                int(r.netuid),
                int(ts),
                int(block),
                float(r.recycle_tao),
                float(r.price_tao_per_alpha),
                float(r.emission_per_day),
                int(r.active_miners) if r.active_miners is not None else None,
                float(r.top1_share) if r.top1_share is not None else None,
                float(r.top10_share) if r.top10_share is not None else None,
                int(r.subnetwork_n),
                int(r.max_n),
                float(r.tao_in),
                float(r.alpha_in),
                r.reward_shape or "?",
            ))
        with self._cursor() as cur:
            cur.executemany("""
                INSERT OR IGNORE INTO snapshots
                  (netuid, ts, block, burn_tao, price_tao_per_alpha,
                   emission_per_day, active_miners, top1_share, top10_share,
                   subnetwork_n, max_n, tao_in, alpha_in, reward_shape)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, params)
            return cur.rowcount or 0

    def history(self, netuid: int, hours: float = 24.0) -> list[dict]:
        cutoff = int(time.time()) - int(hours * 3600)
        with self._cursor() as cur:
            cur.execute("""
                SELECT ts, block, burn_tao, price_tao_per_alpha,
                       emission_per_day, active_miners, top1_share,
                       subnetwork_n, max_n, tao_in
                  FROM snapshots
                 WHERE netuid = ? AND ts >= ?
                 ORDER BY ts ASC
            """, (netuid, cutoff))
            return [dict(r) for r in cur.fetchall()]

    def latest_two(self, netuid: int) -> tuple[dict | None, dict | None]:
        """Return (newest, second-newest) snapshots for delta computations."""
        with self._cursor() as cur:
            cur.execute("""
                SELECT * FROM snapshots
                 WHERE netuid = ?
                 ORDER BY ts DESC
                 LIMIT 2
            """, (netuid,))
            rows = [dict(r) for r in cur.fetchall()]
        a = rows[0] if len(rows) > 0 else None
        b = rows[1] if len(rows) > 1 else None
        return a, b

    def snapshot_at_or_before(self, netuid: int, ts: int) -> dict | None:
        with self._cursor() as cur:
            cur.execute("""
                SELECT * FROM snapshots
                 WHERE netuid = ? AND ts <= ?
                 ORDER BY ts DESC LIMIT 1
            """, (netuid, ts))
            r = cur.fetchone()
            return dict(r) if r else None

    # -- identity / new subnets ------------------------------------------

    def upsert_identity(self, ts: int, rows) -> list[int]:
        """Update last_seen_ts for known netuids, insert new ones.
        Returns the list of netuids that were brand new."""
        new_ones: list[int] = []
        with self._cursor() as cur:
            for r in rows:
                cur.execute(
                    "SELECT netuid FROM subnet_identity WHERE netuid = ?",
                    (r.netuid,))
                if cur.fetchone() is None:
                    new_ones.append(r.netuid)
                    cur.execute("""
                        INSERT INTO subnet_identity
                          (netuid, name, category, first_seen_ts, last_seen_ts)
                        VALUES (?, ?, ?, ?, ?)
                    """, (r.netuid, r.name, r.category, ts, ts))
                else:
                    cur.execute("""
                        UPDATE subnet_identity
                           SET name = ?, category = ?, last_seen_ts = ?
                         WHERE netuid = ?
                    """, (r.name, r.category, ts, r.netuid))
        return new_ones

    # -- alerts ----------------------------------------------------------

    def insert_alert(self, ts: int, kind: str, netuid: int | None,
                     name: str | None, message: str,
                     payload: str | None = None) -> int | None:
        with self._cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO alerts (ts, kind, netuid, name, message, payload)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (ts, kind, netuid, name, message, payload))
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

    def recent_alerts(self, limit: int = 50) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("""
                SELECT id, ts, kind, netuid, name, message, payload
                  FROM alerts
                 ORDER BY ts DESC, id DESC
                 LIMIT ?
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]

    def alert_exists_recently(self, kind: str, netuid: int | None,
                              within_seconds: int) -> bool:
        cutoff = int(time.time()) - within_seconds
        with self._cursor() as cur:
            if netuid is None:
                cur.execute("""
                    SELECT 1 FROM alerts
                     WHERE kind = ? AND ts >= ? LIMIT 1
                """, (kind, cutoff))
            else:
                cur.execute("""
                    SELECT 1 FROM alerts
                     WHERE kind = ? AND netuid = ? AND ts >= ? LIMIT 1
                """, (kind, netuid, cutoff))
            return cur.fetchone() is not None
