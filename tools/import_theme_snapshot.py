"""Download and atomically import a validated relay theme snapshot."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from .theme_snapshot import SnapshotValidationError, validate_snapshot
    from .theme_store import ensure_theme_schema, partition_stats, publish_partition
except ImportError:  # pragma: no cover - direct script execution
    from theme_snapshot import SnapshotValidationError, validate_snapshot
    from theme_store import ensure_theme_schema, partition_stats, publish_partition


DB_SOURCE = "eastmoney_relay"
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
DEFAULT_THRESHOLDS = {"concept": (1000, 0.35), "hot": (500, 0.10)}


def download_snapshot(
    url: str,
    *,
    timeout: float = 30,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    if not str(url).startswith("https://"):
        raise ValueError("snapshot URL must use HTTPS")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "gushi-theme-relay/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise ValueError("snapshot exceeds maximum size")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("snapshot exceeds maximum size")
                chunks.append(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("snapshot download failed") from exc
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("snapshot is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("snapshot root must be an object")
    return payload


def import_snapshot(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    live_count: int,
    thresholds: dict[str, tuple[int, float]] | None = None,
    expected_trade_date: str | None = None,
    max_age_days: int = 3,
) -> dict[str, dict[str, Any]]:
    thresholds = thresholds or DEFAULT_THRESHOLDS
    metrics = validate_snapshot(
        payload,
        expected_trade_date=expected_trade_date,
        live_count=live_count,
        min_rows={name: values[0] for name, values in thresholds.items()},
        min_coverage={name: values[1] for name, values in thresholds.items()},
        max_age_days=max_age_days,
    )
    trade_date = str(payload["trade_date"])
    ensure_theme_schema(conn)
    conn.execute("BEGIN")
    try:
        for theme_type in ("concept", "hot"):
            rows = payload["partitions"][theme_type]["rows"]
            publish_partition(
                conn,
                rows,
                trade_date,
                DB_SOURCE,
                theme_type,
                ensure_schema=False,
                commit=False,
            )
            metrics[theme_type].update(
                partition_stats(conn, trade_date, DB_SOURCE, theme_type, live_count)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return metrics


def _current_trade_date(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT trade_date FROM live_stocks WHERE trade_date IS NOT NULL "
        "GROUP BY trade_date ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()
    if not row or not row[0]:
        raise RuntimeError("live_stocks has no usable trade date")
    return str(row[0])


def _live_count(conn: sqlite3.Connection, trade_date: str) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT code) FROM live_stocks WHERE trade_date=?",
        (trade_date,),
    ).fetchone()
    return int((row or (0,))[0] or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("THEME_SNAPSHOT_URL", ""))
    parser.add_argument("--db", default=None)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-age-days", type=int, default=3)
    parser.add_argument("--min-concept-rows", type=int, default=DEFAULT_THRESHOLDS["concept"][0])
    parser.add_argument("--min-hot-rows", type=int, default=DEFAULT_THRESHOLDS["hot"][0])
    args = parser.parse_args(argv)
    if not args.url:
        print("THEME_SNAPSHOT_URL is not configured", file=sys.stderr)
        return 2

    repo = Path(__file__).resolve().parents[1]
    db_path = Path(args.db) if args.db else repo / "data" / "live.db"
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        trade_date = _current_trade_date(conn)
        payload = download_snapshot(args.url, timeout=args.timeout, max_bytes=args.max_bytes)
        result = import_snapshot(
            conn,
            payload,
            live_count=_live_count(conn, trade_date),
            thresholds={
                "concept": (args.min_concept_rows, 0.35),
                "hot": (args.min_hot_rows, 0.10),
            },
            expected_trade_date=trade_date,
            max_age_days=args.max_age_days,
        )
        print(json.dumps({"status": "ok", "trade_date": trade_date, "partitions": result}, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
