from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Tuple

from thetagang.db import _parse_datetime

FLEX_BASE_URL = (
    "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService"
)

DEFAULT_SECTIONS = [
    "CashTransactions",
    "Dividends",
    "Interest",
    "InterestAccruals",
    "Fees",
    "OtherFees",
    "WithholdingTax",
    "DepositsWithdrawals",
    "Transfers",
]

AMOUNT_KEYS = (
    "amount",
    "netCash",
    "cash",
    "grossAmount",
    "netAmount",
    "proceeds",
    "value",
    "total",
)


@dataclass(frozen=True)
class FlexConfig:
    token: str
    query_id: str
    account_id: Optional[str]
    polling_interval_seconds: int
    timeout_seconds: int
    sections: List[str]


def fetch_cash_transactions(
    config: FlexConfig,
) -> Tuple[List[Mapping[str, Any]], Mapping[str, Any]]:
    root = _fetch_flex_report(config)
    transactions = _extract_cash_transactions(
        root, config.sections, account_id_filter=config.account_id
    )
    statement_info = _extract_statement_info(root)
    return transactions, statement_info


def _fetch_flex_report(config: FlexConfig) -> ET.Element:
    reference_code = _send_request(config.token, config.query_id)
    start = time.monotonic()

    while True:
        root = _get_statement(config.token, reference_code)
        if _looks_like_flex_query(root):
            return root

        status = _get_status(root)
        if status and status.lower() == "pending":
            if time.monotonic() - start > config.timeout_seconds:
                raise RuntimeError("Flex statement request timed out")
            time.sleep(config.polling_interval_seconds)
            continue

        raise RuntimeError(f"Flex statement request failed: status={status}")


def _send_request(token: str, query_id: str) -> str:
    params = {"t": token, "q": query_id}
    url = f"{FLEX_BASE_URL}.SendRequest?{urllib.parse.urlencode(params)}"
    root = _fetch_xml(url)
    status = _get_status(root)
    if status and status.lower() != "success":
        raise RuntimeError(f"Flex SendRequest failed: status={status}")
    reference_code = root.findtext(".//ReferenceCode")
    if not reference_code:
        raise RuntimeError("Flex SendRequest missing reference code")
    return reference_code


def _get_statement(token: str, reference_code: str) -> ET.Element:
    params = {"t": token, "q": reference_code}
    url = f"{FLEX_BASE_URL}.GetStatement?{urllib.parse.urlencode(params)}"
    return _fetch_xml(url)


def _fetch_xml(url: str) -> ET.Element:
    with urllib.request.urlopen(url) as response:
        payload = response.read()
    return ET.fromstring(payload)


def _looks_like_flex_query(root: ET.Element) -> bool:
    if root.tag == "FlexQueryResponse":
        return True
    if root.find(".//FlexStatements") is not None:
        return True
    return False


def _get_status(root: ET.Element) -> Optional[str]:
    status = root.findtext(".//Status")
    if status:
        return status.strip()
    return None


def _extract_statement_info(root: ET.Element) -> Mapping[str, Any]:
    statements = root.findall(".//FlexStatement")
    if not statements:
        return {}
    first = statements[0]
    return {
        "account_id": first.attrib.get("accountId"),
        "from_date": first.attrib.get("fromDate"),
        "to_date": first.attrib.get("toDate"),
        "statement_count": len(statements),
    }


def _extract_cash_transactions(
    root: ET.Element,
    sections: Iterable[str],
    *,
    account_id_filter: Optional[str] = None,
) -> List[Mapping[str, Any]]:
    statements = root.findall(".//FlexStatement")
    if not statements:
        return []

    section_set = {section.strip() for section in sections if section.strip()}
    rows: List[Mapping[str, Any]] = []

    for statement in statements:
        statement_account_id = statement.attrib.get("accountId")
        if account_id_filter and statement_account_id != account_id_filter:
            continue
        for section in statement:
            if section_set and section.tag not in section_set:
                continue
            for row in section:
                raw = dict(row.attrib)
                row_account_id = raw.get("accountId") or statement_account_id
                if account_id_filter and row_account_id != account_id_filter:
                    continue
                tx = _normalize_cash_row(
                    raw,
                    section=section.tag,
                    row_type=row.tag,
                    account_id=row_account_id,
                )
                rows.append(tx)
    return rows


def _normalize_cash_row(
    raw: Mapping[str, Any],
    *,
    section: str,
    row_type: str,
    account_id: Optional[str],
) -> Mapping[str, Any]:
    currency = _first_value(raw, ("currency", "fxCurrency"))
    amount = _parse_amount(_first_value(raw, AMOUNT_KEYS))
    trade_date = _parse_datetime(
        _first_value(raw, ("tradeDate", "date", "reportDate", "dateTime")),
        assume_start_of_day=True,
    )
    settle_date = _parse_datetime(
        _first_value(raw, ("settleDate", "settlementDate")),
        assume_start_of_day=True,
    )
    description = _first_value(raw, ("description", "memo", "detail"))
    symbol = _first_value(raw, ("symbol", "underlyingSymbol", "ticker"))
    con_id = _parse_int(_first_value(raw, ("conid", "conId")))
    asset_category = _first_value(raw, ("assetCategory", "secType"))
    transaction_type = _first_value(raw, ("type", "transactionType", "activityCode"))
    external_id = _first_value(raw, ("transactionID", "tradeID", "id"))

    unique_hash = _hash_cash_row(
        account_id=account_id,
        section=section,
        row_type=row_type,
        currency=currency,
        amount=amount,
        trade_date=trade_date,
        settle_date=settle_date,
        description=description,
        symbol=symbol,
        con_id=con_id,
        asset_category=asset_category,
        transaction_type=transaction_type,
        external_id=external_id,
    )

    return {
        "source": "ibkr_flex",
        "unique_hash": unique_hash,
        "external_id": external_id,
        "account_id": account_id,
        "section": section,
        "row_type": row_type,
        "currency": currency,
        "amount": amount,
        "trade_date": trade_date,
        "settle_date": settle_date,
        "description": description,
        "symbol": symbol,
        "con_id": con_id,
        "asset_category": asset_category,
        "transaction_type": transaction_type,
        "raw_json": json.dumps(raw, default=str),
    }


def _hash_cash_row(**kwargs: Any) -> str:
    payload = "|".join("" if v is None else str(v) for v in kwargs.values())
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _first_value(mapping: Mapping[str, Any], keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _parse_amount(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    raw = value.replace(",", "").strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    raw = value.strip()
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None
