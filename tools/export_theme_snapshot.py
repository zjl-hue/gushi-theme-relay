"""Export a validated EastMoney theme snapshot for GitHub Actions."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from .datacenter_themes import DataCenterClient, build_batches
    from .eastmoney_themes import EastMoneyClient, collect_batches
    from .theme_snapshot import build_snapshot, validate_snapshot
except ImportError:  # pragma: no cover - direct script execution
    from datacenter_themes import DataCenterClient, build_batches
    from eastmoney_themes import EastMoneyClient, collect_batches
    from theme_snapshot import build_snapshot, validate_snapshot


DEFAULT_MIN_ROWS = {"concept": 1000, "hot": 500}
DEFAULT_MIN_COVERAGE = {"concept": 0.0, "hot": 0.0}


def _default_trade_date() -> str:
    return datetime.now(timezone.utc).astimezone().date().isoformat()


def write_snapshot(path: str | os.PathLike[str], payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--trade-date", default=None)
    parser.add_argument(
        "--provider",
        choices=("datacenter", "push2"),
        default="datacenter",
        help="theme relation provider (datacenter is the resilient default)",
    )
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--hot-limit", type=int, default=50)
    parser.add_argument("--max-errors", type=int, default=30)
    parser.add_argument("--min-concept-rows", type=int, default=DEFAULT_MIN_ROWS["concept"])
    parser.add_argument("--min-hot-rows", type=int, default=DEFAULT_MIN_ROWS["hot"])
    args = parser.parse_args(argv)

    trade_date = args.trade_date or _default_trade_date()
    if args.provider == "datacenter":
        client = DataCenterClient(timeout=args.timeout)
        batches = build_batches(
            client.get_rows(), trade_date, hot_limit=args.hot_limit
        )
        source = "eastmoney_datacenter"
    else:
        client = EastMoneyClient(timeout=args.timeout)
        batches = collect_batches(
            client,
            trade_date,
            hot_limit=args.hot_limit,
            max_errors=args.max_errors,
        )
        source = "eastmoney_push2"
    payload = build_snapshot(trade_date, batches, source=source)
    validate_snapshot(
        payload,
        expected_trade_date=trade_date,
        live_count=0,
        min_rows={
            "concept": args.min_concept_rows,
            "hot": args.min_hot_rows,
        },
        min_coverage=DEFAULT_MIN_COVERAGE,
    )
    write_snapshot(args.output, payload)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "trade_date": trade_date,
                "payload_sha256": payload["payload_sha256"],
                "concept_rows": len(payload["partitions"]["concept"]["rows"]),
                "hot_rows": len(payload["partitions"]["hot"]["rows"]),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
