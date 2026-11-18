"""SQLite price-history store behind the `poe_market` MCP server.

`trend_watcher.py` writes snapshots into this database; `poe_market.py` reads
them back. Keeping the read and write sides in one module guarantees they agree
on the schema.

Layout
------
snapshots        one row per collection run (fetched_at, source)
prices           one row per (snapshot, item) with the chaos value

The database lives next to this module as `price_history.db`. Override the
location with the `POE_PRICE_DB` environment variable.

Design notes
------------
* Connections are opened per call and closed immediately. The MCP server is
  single-threaded and the write volume is a few hundred rows per snapshot, so a
  pool would add complexity for no gain.
* The schema is created lazily, so importing this module never touches disk —
  which matters for `poe_all.py`, which imports every sub-server at startup.
* All functions return plain dicts/lists so they serialize straight to JSON.

Version: 1.0
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at  TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'poe.ninja'
);

CREATE TABLE IF NOT EXISTS prices (
    snapshot_id   INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    category      TEXT,
    chaos_value   REAL,
    divine_value  REAL,
    listing_count INTEGER,
    PRIMARY KEY (snapshot_id, name)
);

CREATE INDEX IF NOT EXISTS idx_prices_name ON prices(name);
CREATE INDEX IF NOT EXISTS idx_snapshots_fetched ON snapshots(fetched_at);
"""


def db_path() -> Path:
    """Return the SQLite file this module reads and writes."""
    override = os.environ.get("POE_PRICE_DB", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent / "price_history.db"


def _connect() -> sqlite3.Connection:
    """Open the database, creating the schema on first use."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── write side (used by trend_watcher.py) ────────────────────────────────────

def record_snapshot(rows: list[dict], source: str = "poe.ninja",
                    fetched_at: str | None = None) -> dict:
    """Store one collection run.

    `rows` entries need `name` and `chaos_value`; `category`, `divine_value` and
    `listing_count` are optional. Rows without a positive chaos value are
    dropped — a zero price means "not traded", not "worthless", and keeping it
    would poison the risers/fallers maths.

    Returns a summary dict.
    """
    stamp = fetched_at or _now()
    payload = [
        (
            str(r["name"]),
            r.get("category"),
            float(r["chaos_value"]),
            float(r["divine_value"]) if r.get("divine_value") is not None else None,
            int(r["listing_count"]) if r.get("listing_count") is not None else None,
        )
        for r in rows
        if r.get("name") and r.get("chaos_value") and float(r["chaos_value"]) > 0
    ]

    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO snapshots (fetched_at, source) VALUES (?, ?)",
                (stamp, source),
            )
            snapshot_id = cur.lastrowid
            conn.executemany(
                "INSERT OR REPLACE INTO prices "
                "(snapshot_id, name, category, chaos_value, divine_value, listing_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(snapshot_id, *row) for row in payload],
            )
    finally:
        conn.close()

    return {"snapshot_id": snapshot_id, "fetched_at": stamp, "items": len(payload)}


# ── read side (used by poe_market.py) ────────────────────────────────────────

def snapshot_count() -> int:
    """Number of collection runs stored."""
    conn = _connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])
    finally:
        conn.close()


def get_snapshot_times() -> list[str]:
    """Snapshot timestamps, newest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT fetched_at FROM snapshots ORDER BY fetched_at DESC, id DESC"
        ).fetchall()
        return [r["fetched_at"] for r in rows]
    finally:
        conn.close()


def _latest_rows(where: str = "", params: tuple = ()) -> list[dict]:
    """Latest known price per item, optionally filtered."""
    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            WITH last AS (
                SELECT name, MAX(snapshot_id) AS snapshot_id
                FROM prices
                GROUP BY name
            )
            SELECT p.name, p.category, p.chaos_value, p.divine_value,
                   p.listing_count, s.fetched_at
            FROM prices p
            JOIN last l ON l.name = p.name AND l.snapshot_id = p.snapshot_id
            JOIN snapshots s ON s.id = p.snapshot_id
            {where}
            ORDER BY p.chaos_value DESC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "name": r["name"],
            "category": r["category"],
            "chaos_value": round(r["chaos_value"], 2) if r["chaos_value"] is not None else None,
            "last_price": round(r["chaos_value"], 2) if r["chaos_value"] is not None else None,
            "divine_value": r["divine_value"],
            "listing_count": r["listing_count"],
            "fetched_at": r["fetched_at"],
        }
        for r in rows
    ]


def get_all_latest() -> list[dict]:
    """Latest price for every tracked item."""
    return _latest_rows()


def search_items(query: str, limit: int = 20) -> list[dict]:
    """Substring match on item name against the latest prices."""
    needle = (query or "").strip()
    if not needle:
        return []
    rows = _latest_rows("WHERE p.name LIKE ? COLLATE NOCASE",
                        ("%" + needle + "%",))
    return rows[:max(1, int(limit))]


def get_history(name: str) -> list[dict]:
    """Every stored price for one item, oldest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT s.fetched_at, p.chaos_value, p.divine_value, p.listing_count
            FROM prices p
            JOIN snapshots s ON s.id = p.snapshot_id
            WHERE p.name = ? COLLATE NOCASE
            ORDER BY s.fetched_at ASC, s.id ASC
            """,
            (name,),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "fetched_at": r["fetched_at"],
            "chaos_value": round(r["chaos_value"], 2) if r["chaos_value"] is not None else None,
            "divine_value": r["divine_value"],
            "listing_count": r["listing_count"],
        }
        for r in rows
    ]


def _movers(min_snaps: int, limit: int, mode: str) -> list[dict]:
    """Items whose price moved between their first and last snapshot.

    `mode` is "risers" (only positive), "fallers" (only negative) or "movers"
    (either, ranked by absolute change).
    """
    min_snaps = max(2, int(min_snaps))
    conn = _connect()
    try:
        summary = conn.execute(
            """
            SELECT p.name,
                   p.category,
                   COUNT(*)          AS snapshots,
                   MIN(s.fetched_at) AS first_at,
                   MAX(s.fetched_at) AS last_at
            FROM prices p
            JOIN snapshots s ON s.id = p.snapshot_id
            GROUP BY p.name
            HAVING COUNT(*) >= ?
            """,
            (min_snaps,),
        ).fetchall()

        out = []
        for item in summary:
            first_row = conn.execute(
                """
                SELECT p.chaos_value FROM prices p
                JOIN snapshots s ON s.id = p.snapshot_id
                WHERE p.name = ? AND s.fetched_at = ? LIMIT 1
                """,
                (item["name"], item["first_at"]),
            ).fetchone()
            last_row = conn.execute(
                """
                SELECT p.chaos_value FROM prices p
                JOIN snapshots s ON s.id = p.snapshot_id
                WHERE p.name = ? AND s.fetched_at = ? LIMIT 1
                """,
                (item["name"], item["last_at"]),
            ).fetchone()
            if not first_row or not last_row:
                continue
            first_price = first_row["chaos_value"]
            last_price = last_row["chaos_value"]
            if not first_price or first_price <= 0:
                continue
            change_pct = (last_price - first_price) / first_price * 100.0
            out.append({
                "name": item["name"],
                "category": item["category"],
                "snapshots": item["snapshots"],
                "first_price": round(first_price, 2),
                "last_price": round(last_price, 2),
                "change_pct": round(change_pct, 1),
                "first_at": item["first_at"],
                "last_at": item["last_at"],
            })
    finally:
        conn.close()

    if mode == "risers":
        out = [r for r in out if r["change_pct"] > 0]
        out.sort(key=lambda r: r["change_pct"], reverse=True)
    elif mode == "fallers":
        out = [r for r in out if r["change_pct"] < 0]
        out.sort(key=lambda r: r["change_pct"])
    else:
        out.sort(key=lambda r: abs(r["change_pct"]), reverse=True)

    return out[:max(1, int(limit))]


def get_risers(min_snaps: int = 3, limit: int = 25) -> list[dict]:
    """Biggest positive price change between the first and last snapshot."""
    return _movers(min_snaps, limit, "risers")


def get_fallers(min_snaps: int = 3, limit: int = 25) -> list[dict]:
    """Biggest negative price change between the first and last snapshot."""
    return _movers(min_snaps, limit, "fallers")


def get_movers(min_snaps: int = 3, limit: int = 25) -> list[dict]:
    """Biggest absolute price movement, either direction."""
    return _movers(min_snaps, limit, "movers")


def reset() -> None:
    """Delete every stored snapshot. Used by tests and manual resets."""
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM prices")
            conn.execute("DELETE FROM snapshots")
    finally:
        conn.close()
