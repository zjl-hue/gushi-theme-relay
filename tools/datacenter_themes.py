"""EastMoney F10 concept relations from the public data-center endpoint.

The push2 quote endpoints are frequently blocked from hosted runners.  The
F10 report is a separate, HTTPS-only endpoint and exposes the precise
stock-to-theme relations needed by the relay without requiring a token.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any


REPORT_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_NAME = "RPT_F10_CORETHEME_BOARDTYPE"
REPORT_COLUMNS = (
    "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,"
    "BOARD_CODE,BOARD_NAME,IS_PRECISE,BOARD_RANK"
)
HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://emweb.eastmoney.com/",
    "User-Agent": "Mozilla/5.0",
}
_STOCK_CODE = re.compile(r"^\d{6}$")


class DataCenterError(RuntimeError):
    """Raised when the public F10 report cannot be fetched or parsed."""


def _stock_code(value: object) -> str | None:
    text = str(value or "").strip()
    if text.isdigit():
        text = text.zfill(6)
    return text if _STOCK_CODE.fullmatch(text) else None


def _board_code(value: object) -> str | None:
    text = str(value or "").strip()
    if text.upper().startswith("BK"):
        digits = text[2:]
    else:
        digits = text
    if not digits.isdigit():
        return None
    return f"BK{int(digits):04d}"


def parse_rows(rows: Iterable[dict[str, Any]]) -> list[tuple[str, str, str, None, None]]:
    """Keep precise F10 concepts and normalize them to the relay row shape."""
    result: list[tuple[str, str, str, None, None]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in rows:
        if not isinstance(raw, dict) or str(raw.get("IS_PRECISE") or "") != "1":
            continue
        stock_code = _stock_code(raw.get("SECURITY_CODE"))
        block_code = _board_code(raw.get("BOARD_CODE"))
        block_name = str(raw.get("BOARD_NAME") or "").strip()
        if not stock_code or not block_code or not block_name:
            continue
        key = (stock_code, block_code, block_name)
        if key in seen:
            continue
        seen.add(key)
        result.append((stock_code, block_code, block_name, None, None))
    return result


def build_batches(
    rows: Iterable[tuple[str, str, str, None, None]],
    trade_date: str,
    *,
    hot_limit: int = 50,
) -> dict[str, dict[str, Any]]:
    """Build concept and deterministic hot partitions from normalized rows."""
    concept_rows = list(rows)
    counts = Counter(row[1] for row in concept_rows)
    names = {row[1]: row[2] for row in concept_rows}
    hot_codes = [
        code
        for code, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[
            : max(1, int(hot_limit))
        ]
    ]
    hot_rank = {code: index for index, code in enumerate(hot_codes, 1)}
    hot_rows = [
        (stock, code, names[code], None, hot_rank[code])
        for stock, code, _name, _change, _rank in concept_rows
        if code in hot_rank
    ]
    return {
        "concept": {
            "rows": concept_rows,
            "board_count": len(counts),
            "errors": 0,
            "trade_date": trade_date,
        },
        "hot": {
            "rows": hot_rows,
            "board_count": len(hot_codes),
            "errors": 0,
            "trade_date": trade_date,
        },
    }


class DataCenterClient:
    def __init__(
        self,
        *,
        timeout: float = 30,
        page_size: int = 5000,
        max_pages: int = 100,
        opener: Callable[..., Any] | None = None,
    ):
        self.timeout = timeout
        self.page_size = max(1, int(page_size))
        self.max_pages = max(1, int(max_pages))
        self.opener = opener or urllib.request.urlopen

    def _get_page(self, page_number: int) -> dict[str, Any]:
        params = {
            "reportName": REPORT_NAME,
            "columns": REPORT_COLUMNS,
            "source": "WEB",
            "client": "WEB",
            "sortColumns": "BOARD_RANK",
            "sortTypes": "1",
            "pageNumber": str(page_number),
            "pageSize": str(self.page_size),
        }
        url = f"{REPORT_URL}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=HEADERS)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise DataCenterError("EastMoney data-center request failed") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise DataCenterError("EastMoney data-center response is invalid")
        return payload

    def get_rows(self) -> list[tuple[str, str, str, None, None]]:
        first = self._get_page(1)
        result = first.get("result") or {}
        if not isinstance(result, dict):
            raise DataCenterError("EastMoney data-center result is invalid")
        raw_rows = list(result.get("data") or [])
        pages = min(int(result.get("pages") or 1), self.max_pages)
        for page_number in range(2, pages + 1):
            page = self._get_page(page_number)
            page_result = page.get("result") or {}
            if not isinstance(page_result, dict):
                raise DataCenterError("EastMoney data-center page is invalid")
            raw_rows.extend(page_result.get("data") or [])
        rows = parse_rows(raw_rows)
        if not rows:
            raise DataCenterError("EastMoney data-center precise concepts are empty")
        return rows
