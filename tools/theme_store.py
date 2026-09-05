"""SQLite storage helpers for source- and type-isolated theme snapshots."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any


_BASE_COLUMNS = {
    "stock_code": "TEXT",
    "block_code": "TEXT",
    "block_name": "TEXT",
    "change_rate": "REAL",
    "date": "TEXT",
    "source": "TEXT NOT NULL DEFAULT 'legacy'",
    "theme_type": "TEXT NOT NULL DEFAULT 'concept'",
    "rank": "INTEGER",
}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _table_columns(conn, table)
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def ensure_theme_schema(conn: sqlite3.Connection) -> None:
    """Create/migrate the theme tables without removing existing rows."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS stock_blocks(
            stock_code TEXT,
            block_code TEXT,
            block_name TEXT,
            change_rate REAL,
            date TEXT,
            source TEXT NOT NULL DEFAULT 'legacy',
            theme_type TEXT NOT NULL DEFAULT 'concept',
            rank INTEGER
        )"""
    )
    _add_missing_columns(conn, "stock_blocks", _BASE_COLUMNS)

    conn.execute(
        """CREATE TABLE IF NOT EXISTS stock_blocks_stage(
            stock_code TEXT NOT NULL,
            block_code TEXT,
            block_name TEXT NOT NULL,
            change_rate REAL,
            date TEXT NOT NULL,
            source TEXT NOT NULL,
            theme_type TEXT NOT NULL,
            rank INTEGER
        )"""
    )
    _add_missing_columns(
        conn,
        "stock_blocks_stage",
        {
            "stock_code": "TEXT",
            "block_code": "TEXT",
            "block_name": "TEXT",
            "change_rate": "REAL",
            "date": "TEXT",
            "source": "TEXT NOT NULL DEFAULT 'legacy'",
            "theme_type": "TEXT NOT NULL DEFAULT 'concept'",
            "rank": "INTEGER",
        },
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_blocks_date ON stock_blocks(date)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stock_blocks_partition "
        "ON stock_blocks(date, source, theme_type)"
    )
    conn.commit()


def _row_values(row: Any) -> tuple[str, str, str, float | None, int | None]:
    if isinstance(row, dict):
        code = row.get("stock_code") or row.get("code")
        block_code = row.get("block_code") or row.get("theme_code")
        block_name = row.get("block_name") or row.get("theme_name")
        change_rate = row.get("change_rate")
        rank = row.get("rank")
    else:
        values = tuple(row)
        if len(values) == 4:
            code, block_code, block_name, change_rate = values
            rank = None
        elif len(values) >= 5:
            code, block_code, block_name, change_rate, rank = values[:5]
        else:
            raise ValueError("theme row requires at least four values")
    code = str(code or "").strip()
    block_code = str(block_code or "").strip()
    block_name = str(block_name or "").strip()
    if not code or not block_name:
        raise ValueError("theme row requires stock_code and block_name")
    try:
        change_rate = float(change_rate) if change_rate is not None else None
    except (TypeError, ValueError):
        change_rate = None
    try:
        rank = int(rank) if rank is not None else None
    except (TypeError, ValueError):
        rank = None
    return code, block_code, block_name, change_rate, rank


def publish_partition(
    conn: sqlite3.Connection,
    rows: Iterable[Any],
    trade_date: str,
    source: str,
    theme_type: str,
    *,
    ensure_schema: bool = True,
    commit: bool = True,
) -> dict[str, Any]:
    """Atomically replace one `(date, source, theme_type)` partition."""
    if ensure_schema:
        ensure_theme_schema(conn)
    normalized = []
    seen = set()
    for raw in rows:
        item = _row_values(raw)
        key = (item[0], item[1], trade_date, source, theme_type)
        if key not in seen:
            seen.add(key)
            normalized.append(item)

    conn.execute("DELETE FROM stock_blocks_stage")
    conn.executemany(
        """INSERT INTO stock_blocks_stage(
            stock_code, block_code, block_name, change_rate, date, source, theme_type, rank
        ) VALUES(?,?,?,?,?,?,?,?)""",
        [
            (item[0], item[1], item[2], item[3], trade_date, source, theme_type, item[4])
            for item in normalized
        ],
    )
    stage_count = conn.execute("SELECT COUNT(*) FROM stock_blocks_stage").fetchone()[0]
    if stage_count != len(normalized):
        conn.rollback()
        raise RuntimeError("theme staging row count mismatch")

    conn.execute(
        "DELETE FROM stock_blocks WHERE date=? AND source=? AND theme_type=?",
        (trade_date, source, theme_type),
    )
    conn.execute(
        """INSERT INTO stock_blocks(
            stock_code, block_code, block_name, change_rate, date, source, theme_type, rank
        )
        SELECT stock_code, block_code, block_name, change_rate, date, source, theme_type, rank
        FROM stock_blocks_stage"""
    )
    if commit:
        conn.commit()
    return partition_stats(conn, trade_date, source, theme_type, 0)


def partition_stats(
    conn: sqlite3.Connection,
    trade_date: str,
    source: str,
    theme_type: str,
    live_count: int,
) -> dict[str, Any]:
    row = conn.execute(
        """SELECT COUNT(*) AS rows, COUNT(DISTINCT stock_code) AS stock_count
        FROM stock_blocks WHERE date=? AND source=? AND theme_type=?""",
        (trade_date, source, theme_type),
    ).fetchone()
    rows = int(row[0] or 0)
    stock_count = int(row[1] or 0)
    coverage = round(stock_count / max(1, int(live_count or 0)), 4)
    return {
        "rows": rows,
        "stock_count": stock_count,
        "coverage": coverage,
        "trade_date": trade_date,
        "source": source,
        "theme_type": theme_type,
    }
