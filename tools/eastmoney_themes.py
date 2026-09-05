"""EastMoney concept and hot-theme provider using curl_cffi."""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - exercised by the CLI configuration check
    curl_requests = None


# EastMoney rotates the numbered push2 front doors.  The first two entries
# preserve the original route; the additional numbered hosts are useful from
# CI runners whose egress IPs receive a 502/connection reset on those routes.
CATALOG_HOSTS = (
    "82.push2.eastmoney.com",
    "push2.eastmoney.com",
    "17.push2.eastmoney.com",
    "79.push2.eastmoney.com",
    "73.push2.eastmoney.com",
)
MEMBER_HOSTS = (
    "29.push2.eastmoney.com",
    "push2.eastmoney.com",
    "17.push2.eastmoney.com",
    "79.push2.eastmoney.com",
    "73.push2.eastmoney.com",
)
API_PATH = "/api/qt/clist/get"
UT = "bd1d9ddb04089700cf9c27f6f7426281"
HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://quote.eastmoney.com/",
}


class EastMoneyError(RuntimeError):
    """A safe, provider-specific error suitable for state files and logs."""


def _code6(value: object) -> str | None:
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", str(value or ""))
    return match.group(1) if match else None


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _diff(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    try:
        data = payload["data"]
        rows = data.get("diff") or []
        total = int(data.get("total") or len(rows))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise EastMoneyError("EastMoney response schema is invalid") from exc
    if not isinstance(rows, list):
        raise EastMoneyError("EastMoney response rows are invalid")
    return [row for row in rows if isinstance(row, dict)], total


def parse_catalog(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse and deduplicate EastMoney concept-board rows."""
    rows, _ = _diff(payload)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        code = str(row.get("f12") or row.get("code") or "").strip()
        name = str(row.get("f14") or row.get("name") or "").strip()
        if not code or not name or code in seen:
            continue
        seen.add(code)
        result.append({"code": code, "name": name, "change_rate": _number(row.get("f3"))})
    if not result:
        raise EastMoneyError("EastMoney concept catalogue is empty")
    return result


def parse_members(
    payload: dict[str, Any], board: dict[str, Any], rank: int | None = None
) -> list[tuple[str, str, str, float | None, int | None]]:
    """Parse board members into the store's normalized row shape."""
    rows, _ = _diff(payload)
    result = []
    seen: set[str] = set()
    board_code = str(board.get("code") or "").strip()
    board_name = str(board.get("name") or "").strip()
    change_rate = _number(board.get("change_rate"))
    for row in rows:
        code = _code6(row.get("f12") or row.get("code"))
        name = str(row.get("f14") or row.get("name") or "").strip()
        if not code or not board_code or not board_name or code in seen:
            continue
        seen.add(code)
        result.append((code, board_code, board_name, change_rate, rank))
    return result


class EastMoneyClient:
    def __init__(
        self,
        session: Any | None = None,
        *,
        timeout: float = 20,
        retries: int = 2,
        catalog_hosts: Iterable[str] = CATALOG_HOSTS,
        member_hosts: Iterable[str] = MEMBER_HOSTS,
    ):
        if session is None:
            if curl_requests is None:
                raise EastMoneyError("curl_cffi is not installed")
            session = curl_requests.Session(impersonate="chrome")
        self.session = session
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.catalog_hosts = tuple(catalog_hosts)
        self.member_hosts = tuple(member_hosts)

    def _get_json(self, hosts: Iterable[str], params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for host in hosts:
            for _ in range(self.retries + 1):
                try:
                    response = self.session.get(
                        f"https://{host}{API_PATH}",
                        params=params,
                        headers=HEADERS,
                        impersonate="chrome",
                        timeout=self.timeout,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise EastMoneyError("EastMoney response is not an object")
                    return payload
                except Exception as exc:  # noqa: BLE001 - provider fallback boundary
                    last_error = exc
        raise EastMoneyError("EastMoney request failed") from last_error

    def get_catalog(self) -> list[dict[str, Any]]:
        params = {
            "pn": "1",
            "pz": "1000",
            "po": "1",
            "np": "1",
            "ut": UT,
            "fltt": "2",
            "invt": "2",
            "fid": "f12",
            "fs": "m:90 t:3 f:!50",
            "fields": "f2,f3,f4,f8,f12,f14,f15,f16,f17,f18,f20,f21,f24,f25,f22,f33,f11",
        }
        return parse_catalog(self._get_json(self.catalog_hosts, params))

    def get_members(self, board_code: str) -> list[dict[str, str]]:
        members: list[dict[str, str]] = []
        page = 1
        total = None
        while total is None or len(members) < total:
            params = {
                "pn": str(page),
                "pz": "100",
                "po": "1",
                "np": "1",
                "ut": UT,
                "fltt": "2",
                "invt": "2",
                "fid": "f12",
                "fs": f"b:{board_code} f:!50",
                "fields": "f12,f14,f2,f3",
            }
            payload = self._get_json(self.member_hosts, params)
            rows, page_total = _diff(payload)
            total = page_total
            if not rows:
                break
            for row in rows:
                code = _code6(row.get("f12") or row.get("code"))
                name = str(row.get("f14") or row.get("name") or "").strip()
                if code and name:
                    members.append({"code": code, "name": name})
            page += 1
            if page > 100:
                raise EastMoneyError("EastMoney member pagination exceeded limit")
        unique: dict[str, dict[str, str]] = {}
        for row in members:
            unique.setdefault(row["code"], row)
        return list(unique.values())


def _member_rows(
    client: Any, board: dict[str, Any], rank: int | None
) -> list[tuple[str, str, str, float | None, int | None]]:
    members = client.get_members(board["code"])
    if isinstance(members, dict):
        return parse_members(members, board, rank)
    rows = []
    seen: set[str] = set()
    for member in members or []:
        code = _code6(member.get("code") if isinstance(member, dict) else member)
        if not code or code in seen:
            continue
        seen.add(code)
        rows.append(
            (
                code,
                board["code"],
                board["name"],
                _number(board.get("change_rate")),
                rank,
            )
        )
    return rows


def _collect_type(client: Any, boards: list[dict[str, Any]], rank_hot: bool, max_errors: int) -> dict[str, Any]:
    rows = []
    seen = set()
    errors = 0
    for index, board in enumerate(boards, 1):
        rank = index if rank_hot else None
        try:
            current = _member_rows(client, board, rank)
        except Exception:  # noqa: BLE001 - keep the other boards and type alive
            errors += 1
            if errors >= max(1, max_errors):
                break
            continue
        for row in current:
            key = (row[0], row[1])
            if key not in seen:
                seen.add(key)
                rows.append(row)
    return {"rows": rows, "board_count": len(boards), "errors": errors}


def collect_batches(
    client: Any,
    trade_date: str,
    *,
    hot_limit: int = 50,
    max_errors: int = 30,
) -> dict[str, dict[str, Any]]:
    """Collect concept and hot batches independently."""
    catalog = client.get_catalog()
    hot_boards = sorted(
        catalog,
        key=lambda row: _number(row.get("change_rate"))
        if _number(row.get("change_rate")) is not None
        else float("-inf"),
        reverse=True,
    )[: max(1, int(hot_limit))]
    concept = _collect_type(client, catalog, False, max_errors)
    hot = _collect_type(client, hot_boards, True, max_errors)
    for batch in (concept, hot):
        batch["trade_date"] = trade_date
    return {"concept": concept, "hot": hot}
