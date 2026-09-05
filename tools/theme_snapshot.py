"""Validation and serialization contract for relay theme snapshots."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any


class SnapshotValidationError(ValueError):
    """Raised when a relay snapshot is unsafe to import."""


_STOCK_CODE = re.compile(r"^\d{6}$")
_PARTITIONS = ("concept", "hot")


def canonical_payload(payload: dict[str, Any]) -> bytes:
    """Return the deterministic UTF-8 representation used for hashing."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalize_row(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        values = {
            "stock_code": raw.get("stock_code") or raw.get("code"),
            "block_code": raw.get("block_code") or raw.get("theme_code") or "",
            "block_name": raw.get("block_name") or raw.get("theme_name"),
            "change_rate": raw.get("change_rate"),
            "rank": raw.get("rank"),
        }
    else:
        values_tuple = tuple(raw)
        if len(values_tuple) < 4:
            raise SnapshotValidationError("row has fewer than four fields")
        values = {
            "stock_code": values_tuple[0],
            "block_code": values_tuple[1],
            "block_name": values_tuple[2],
            "change_rate": values_tuple[3],
            "rank": values_tuple[4] if len(values_tuple) > 4 else None,
        }
    stock_code = str(values["stock_code"] or "").strip()
    block_code = str(values["block_code"] or "").strip()
    block_name = str(values["block_name"] or "").strip()
    if not _STOCK_CODE.fullmatch(stock_code):
        raise SnapshotValidationError(f"invalid stock_code: {stock_code!r}")
    if not block_name:
        raise SnapshotValidationError("row has empty block_name")
    change_rate = values["change_rate"]
    if change_rate is not None:
        try:
            change_rate = float(change_rate)
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError("invalid change_rate") from exc
    rank = values["rank"]
    if rank is not None:
        try:
            rank = int(rank)
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError("invalid rank") from exc
    return {
        "stock_code": stock_code,
        "block_code": block_code,
        "block_name": block_name,
        "change_rate": change_rate,
        "rank": rank,
    }


def _normalize_partition(batch: dict[str, Any]) -> dict[str, Any]:
    rows = [_normalize_row(row) for row in list(batch.get("rows") or [])]
    return {
        "rows": rows,
        "board_count": int(batch.get("board_count") or 0),
        "errors": int(batch.get("errors") or 0),
    }


def build_snapshot(
    trade_date: str,
    batches: dict[str, dict[str, Any]],
    *,
    source: str = "eastmoney",
    generated_at: str | None = None,
) -> dict[str, Any]:
    partitions = {
        name: _normalize_partition(batches.get(name) or {}) for name in _PARTITIONS
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "source": source,
        "trade_date": str(trade_date),
        "generated_at": generated_at
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "partitions": partitions,
    }
    payload["payload_sha256"] = hashlib.sha256(canonical_payload(payload)).hexdigest()
    return payload


def _parse_trade_date(value: Any) -> date:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError("invalid trade_date") from exc


def validate_snapshot(
    payload: dict[str, Any],
    *,
    expected_trade_date: str | None,
    live_count: int,
    min_rows: dict[str, int],
    min_coverage: dict[str, float],
    max_age_days: int = 3,
) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise SnapshotValidationError("unsupported schema_version")
    if not str(payload.get("source") or "").strip():
        raise SnapshotValidationError("missing source")
    trade_day = _parse_trade_date(payload.get("trade_date"))
    if expected_trade_date and str(payload.get("trade_date")) != expected_trade_date:
        raise SnapshotValidationError("trade_date does not match live data")
    if max_age_days >= 0 and trade_day < date.today() - timedelta(days=max_age_days):
        raise SnapshotValidationError("trade_date is too old")

    supplied_digest = str(payload.get("payload_sha256") or "")
    unsigned = dict(payload)
    unsigned.pop("payload_sha256", None)
    expected_digest = hashlib.sha256(canonical_payload(unsigned)).hexdigest()
    if supplied_digest != expected_digest:
        raise SnapshotValidationError("payload digest mismatch")

    raw_partitions = payload.get("partitions")
    if not isinstance(raw_partitions, dict):
        raise SnapshotValidationError("partitions must be an object")
    metrics: dict[str, dict[str, Any]] = {}
    for name in _PARTITIONS:
        partition = raw_partitions.get(name)
        if not isinstance(partition, dict) or not isinstance(partition.get("rows"), list):
            raise SnapshotValidationError(f"partition {name} is invalid")
        rows = [_normalize_row(row) for row in partition["rows"]]
        keys = [
            (row["stock_code"], row["block_code"], row["block_name"])
            for row in rows
        ]
        if len(keys) != len(set(keys)):
            raise SnapshotValidationError(f"partition {name} has duplicate rows")
        row_count = len(rows)
        stock_count = len({row["stock_code"] for row in rows})
        coverage = stock_count / max(1, int(live_count or 0))
        required_rows = int(min_rows.get(name, 0))
        required_coverage = float(min_coverage.get(name, 0.0))
        if row_count < required_rows:
            raise SnapshotValidationError(
                f"partition {name} rows below minimum: {row_count} < {required_rows}"
            )
        if live_count and coverage < required_coverage:
            raise SnapshotValidationError(
                f"partition {name} coverage below threshold: {coverage:.4f} < {required_coverage:.4f}"
            )
        metrics[name] = {
            "rows": row_count,
            "stock_count": stock_count,
            "coverage": round(coverage, 4),
            "board_count": int(partition.get("board_count") or 0),
            "errors": int(partition.get("errors") or 0),
            "trade_date": str(payload["trade_date"]),
            "source": str(payload["source"]),
            "theme_type": name,
        }
    return metrics
