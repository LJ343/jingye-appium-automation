# coding: utf-8
"""运费确认阶段的纯数据模型、规范化、JSONL 持久化和断点索引。"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional


WAYBILL_RE = re.compile(r"YS[A-Z0-9]+", re.IGNORECASE)
WAYBILL_DATE_RE = re.compile(r"^YS(\d{8})", re.IGNORECASE)
FREIGHT_RETRY_DAYS = 14
PLATE_RE = re.compile(
    r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5,6}",
    re.IGNORECASE,
)
PIECE_SUFFIX_RE = re.compile(
    r"(?:[,，;；/\s]*|[（(])\d+(?:\.\d+)?\s*(?:件|张|支|卷|根|块|片|捆|个)[）)]?\s*$",
    re.IGNORECASE,
)


def normalize_waybill(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().replace(" ", "")
    match = WAYBILL_RE.search(text)
    return match.group(0).upper() if match else re.sub(r"[^A-Z0-9]", "", text)


def waybill_date_part(value: Any) -> str:
    """Return the calendar date encoded by a normal YS waybill number."""
    match = WAYBILL_DATE_RE.match(normalize_waybill(value))
    if not match:
        return ""
    try:
        return dt.datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
    except ValueError:
        return ""


def freight_scan_past_target_date(card_date: Any, oldest_target_date: Any) -> bool:
    """A descending freight scan is past scope only on a strictly older date."""
    current = date_part(card_date)
    oldest = date_part(oldest_target_date)
    return bool(current and oldest and current < oldest)


def freight_target_date(waybill: Any, app_export_time: Any = None) -> str:
    """Use the YS number date first, then the sales export date."""
    return waybill_date_part(waybill) or date_part(app_export_time)


def freight_target_age_days(
    waybill: Any,
    app_export_time: Any,
    anchor_date: Any,
) -> Optional[int]:
    """Return target age in calendar days, or None when either date is unknown."""
    target_value = freight_target_date(waybill, app_export_time)
    anchor_value = date_part(anchor_date)
    if not target_value or not anchor_value:
        return None
    try:
        target = dt.date.fromisoformat(target_value)
        anchor = dt.date.fromisoformat(anchor_value)
    except ValueError:
        return None
    return (anchor - target).days


def freight_target_within_retry_window(
    waybill: Any,
    app_export_time: Any,
    anchor_date: Any,
    retry_days: int = FREIGHT_RETRY_DAYS,
) -> bool:
    """Keep unknown/future dates safe; stop automatic retries only after the limit."""
    age_days = freight_target_age_days(waybill, app_export_time, anchor_date)
    return age_days is None or age_days <= max(0, int(retry_days))


def normalize_car(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().replace(" ", "")
    match = PLATE_RE.search(text)
    return match.group(0).upper() if match else re.sub(r"[\s:：]", "", text)


def normalize_spec(value: Any) -> str:
    """生成严格规格键；统一符号/单位/数字格式，不做相似规格猜测。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    if not text:
        return ""
    text = re.sub(r"^\s*(?:规格|尺寸规格|尺寸)\s*[:：]\s*", "", text)
    text = re.sub(r"[，,;；。]+\s*$", "", text)
    text = PIECE_SUFFIX_RE.sub("", text)
    # 先去除紧跟数字的毫米单位，才能识别“18毫米·1500毫米”等分隔写法。
    text = re.sub(r"(?<=\d)\s*(?:MM|毫米)", "", text)
    for marker in ("×", "✕", "✖", "⨉", "＊", "∗", "Χ", "Х"):
        text = text.replace(marker, "*")
    text = re.sub(
        r"(?<=[0-9A-ZΦ])\s*(?:X|·|∙)\s*(?=[0-9A-ZΦ])",
        "*",
        text,
    )
    text = text.replace("φ", "Φ")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，,;；。]+$", "", text)
    text = re.sub(r"\*+", "*", text)

    def canonical_number(match: re.Match[str]) -> str:
        token = match.group(0)
        try:
            number = format(Decimal(token), "f")
        except InvalidOperation:
            return token
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        return number or "0"

    text = re.sub(r"\d+(?:\.\d+)?", canonical_number, text)
    return text.strip("*(),，;；。")


def parse_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number else None
    text = unicodedata.normalize("NFKC", str(value)).replace(",", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def normalize_datetime(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?", text)
    return re.sub(r"\s+", " ", match.group(0)) if match else text


def date_part(value: Any) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return match.group(0) if match else ""


def scope_key(sheet: Any, source_kind: Any, source_value: Any) -> str:
    return "|".join(
        (
            str(sheet or "").strip(),
            str(source_kind or "").strip(),
            normalize_car(source_value) if str(source_kind or "") == "selected_car" else str(source_value or "").strip(),
        )
    )


def target_key(record: dict[str, Any]) -> str:
    return "|".join(
        (
            scope_key(record.get("sheet"), record.get("source_kind"), record.get("source_value")),
            normalize_waybill(record.get("waybill")),
            normalize_car(record.get("car")),
        )
    )


def freight_card_key(record: dict[str, Any]) -> str:
    stable = "|".join(
        (
            scope_key(record.get("sheet"), record.get("source_kind"), record.get("source_value")),
            normalize_waybill(record.get("waybill")),
            normalize_datetime(record.get("outbound_datetime")),
            normalize_spec(record.get("raw_spec") or record.get("normalized_spec")),
        )
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]


def normalize_occurrence(value: Any) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def freight_instance_key(record: dict[str, Any]) -> str:
    """Return the transient list-card identity inside one ordered waybill bundle."""
    card_key = str(record.get("card_key") or freight_card_key(record))
    return f"{card_key}:{normalize_occurrence(record.get('card_occurrence'))}"


def freight_fact_key(record: dict[str, Any]) -> str:
    """Return the legacy content key shared by completely identical freight cards."""
    stable = "|".join(
        (
            freight_card_key(record),
            str(parse_number(record.get("settlement_quantity"))),
            str(parse_number(record.get("unit_price"))),
        )
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]


def freight_record_key(record: dict[str, Any]) -> str:
    """Return a durable key while preserving legacy keys for the first occurrence."""
    fact_key = freight_fact_key(record)
    occurrence = normalize_occurrence(record.get("record_occurrence"))
    if occurrence == 1:
        return fact_key
    return hashlib.sha256(f"{fact_key}|{occurrence}".encode("utf-8")).hexdigest()[:24]


def freight_failure_key(record: dict[str, Any]) -> str:
    """Keep the legacy card key for occurrence one and distinguish later failures."""
    card_key = str(record.get("card_key") or freight_card_key(record))
    occurrence = normalize_occurrence(record.get("card_occurrence"))
    if occurrence == 1:
        return card_key
    return hashlib.sha256(f"{card_key}|{occurrence}".encode("utf-8")).hexdigest()[:24]


def annotate_card_occurrences(
    snapshots: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Number duplicate visible rows from the list-bottom side without collapsing them."""
    counts: dict[str, int] = {}
    annotated: list[dict[str, Any]] = []
    for original in snapshots:
        item = dict(original)
        card_key = str(item.get("card_key") or freight_card_key(item))
        counts[card_key] = counts.get(card_key, 0) + 1
        item["card_key"] = card_key
        item["card_occurrence"] = counts[card_key]
        item["instance_key"] = freight_instance_key(item)
        annotated.append(item)
    return annotated


def next_card_occurrence(counts: dict[str, int], card_key: str) -> int:
    """Reserve the next physical encounter number during one monotonic list scan."""
    normalized_key = str(card_key or "")
    occurrence = counts.get(normalized_key, 0) + 1
    counts[normalized_key] = occurrence
    return occurrence


def append_event(path: os.PathLike[str] | str, event: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(event)
    payload.setdefault("version", 1)
    payload.setdefault("recorded_at", dt.datetime.now().isoformat(timespec="seconds"))
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def make_target_event(
    *,
    run_id: str,
    sheet: str,
    source_kind: str,
    source_value: str,
    waybill: str,
    car: str,
    app_export_time: str,
    excel_row: Any,
) -> dict[str, Any]:
    event = {
        "event": "sales_target",
        "run_id": str(run_id or ""),
        "sheet": str(sheet or "").strip(),
        "source_kind": str(source_kind or "").strip(),
        "source_value": str(source_value or "").strip(),
        "waybill": normalize_waybill(waybill),
        "car": normalize_car(car),
        "app_export_time": normalize_datetime(app_export_time),
        "excel_row": excel_row,
    }
    event["target_key"] = target_key(event)
    return event


def make_freight_event(
    *,
    event_name: str,
    run_id: str,
    sheet: str,
    source_kind: str,
    source_value: str,
    car: str,
    waybill: str,
    outbound_datetime: str,
    raw_spec: str,
    settlement_quantity: Any = None,
    unit_price: Any = None,
    error: str = "",
    attempt: int = 1,
    card_occurrence: int = 1,
    record_occurrence: int = 1,
) -> dict[str, Any]:
    event = {
        "event": event_name,
        "run_id": str(run_id or ""),
        "sheet": str(sheet or "").strip(),
        "source_kind": str(source_kind or "").strip(),
        "source_value": str(source_value or "").strip(),
        "car": normalize_car(car),
        "waybill": normalize_waybill(waybill),
        "outbound_datetime": normalize_datetime(outbound_datetime),
        "raw_spec": str(raw_spec or "").strip(),
        "normalized_spec": normalize_spec(raw_spec),
        "settlement_quantity": parse_number(settlement_quantity),
        "unit_price": parse_number(unit_price),
        "error": str(error or "").strip(),
        "attempt": int(attempt or 1),
        "card_occurrence": normalize_occurrence(card_occurrence),
        "record_occurrence": normalize_occurrence(record_occurrence),
    }
    event["card_key"] = freight_card_key(event)
    event["record_key"] = freight_record_key(event)
    event["failure_key"] = freight_failure_key(event)
    return event


@dataclasses.dataclass(frozen=True)
class FreightRecord:
    record_key: str
    card_key: str
    sheet: str
    source_kind: str
    source_value: str
    car: str
    waybill: str
    outbound_datetime: str
    raw_spec: str
    normalized_spec: str
    settlement_quantity: float
    unit_price: float
    card_occurrence: int = 1
    record_occurrence: int = 1


@dataclasses.dataclass
class FreightData:
    targets: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    records: dict[str, FreightRecord] = dataclasses.field(default_factory=dict)
    failures: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    completed_scopes: set[str] = dataclasses.field(default_factory=set)
    errors: list[str] = dataclasses.field(default_factory=list)

    def targets_for_scope(self, sheet: str, source_kind: str, source_value: str) -> list[dict[str, Any]]:
        expected = scope_key(sheet, source_kind, source_value)
        return [item for item in self.targets.values() if scope_key(item.get("sheet"), item.get("source_kind"), item.get("source_value")) == expected]

    def records_for_scope(self, sheet: str, source_kind: str, source_value: str) -> list[FreightRecord]:
        expected = scope_key(sheet, source_kind, source_value)
        return [item for item in self.records.values() if scope_key(item.sheet, item.source_kind, item.source_value) == expected]


def _record_from_event(value: dict[str, Any]) -> FreightRecord:
    quantity = parse_number(value.get("settlement_quantity"))
    unit_price = parse_number(value.get("unit_price"))
    if quantity is None or unit_price is None:
        raise ValueError("成功事件缺少有效结算量或单价")
    record_key_value = str(value.get("record_key") or freight_record_key(value))
    return FreightRecord(
        record_key=record_key_value,
        card_key=str(value.get("card_key") or freight_card_key(value)),
        sheet=str(value.get("sheet") or "").strip(),
        source_kind=str(value.get("source_kind") or "").strip(),
        source_value=str(value.get("source_value") or "").strip(),
        car=normalize_car(value.get("car")),
        waybill=normalize_waybill(value.get("waybill")),
        outbound_datetime=normalize_datetime(value.get("outbound_datetime")),
        raw_spec=str(value.get("raw_spec") or "").strip(),
        normalized_spec=normalize_spec(value.get("normalized_spec") or value.get("raw_spec")),
        settlement_quantity=quantity,
        unit_price=unit_price,
        card_occurrence=normalize_occurrence(value.get("card_occurrence")),
        record_occurrence=normalize_occurrence(value.get("record_occurrence")),
    )


def load_freight_data(path: os.PathLike[str] | str) -> FreightData:
    result = FreightData()
    source = Path(path)
    if not source.is_file():
        return result
    try:
        lines: Iterable[str] = source.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        result.errors.append(f"无法读取运费确认中间文件：{exc}")
        return result

    for line_no, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
            event_name = str(value.get("event") or "").strip()
            if event_name == "sales_target":
                value["target_key"] = str(value.get("target_key") or target_key(value))
                result.targets[value["target_key"]] = value
            elif event_name == "freight_success":
                record = _record_from_event(value)
                result.records[record.record_key] = record
                result.failures.pop(
                    str(value.get("failure_key") or freight_failure_key(value)), None
                )
            elif event_name == "freight_failure":
                card_key_value = str(value.get("card_key") or freight_card_key(value))
                failure_key_value = str(
                    value.get("failure_key") or freight_failure_key(value)
                )
                value["card_key"] = card_key_value
                value["failure_key"] = failure_key_value
                result.failures[failure_key_value] = value
            elif event_name == "freight_scope_complete":
                completed_scope = scope_key(
                    value.get("sheet"), value.get("source_kind"), value.get("source_value")
                )
                result.completed_scopes.add(completed_scope)
                completed_waybills = {
                    normalize_waybill(item)
                    for item in value.get("completed_waybills", [])
                    if normalize_waybill(item)
                }
                if completed_waybills:
                    for failure_key_value, failure in list(result.failures.items()):
                        if (
                            scope_key(
                                failure.get("sheet"),
                                failure.get("source_kind"),
                                failure.get("source_value"),
                            )
                            == completed_scope
                            and normalize_waybill(failure.get("waybill"))
                            in completed_waybills
                        ):
                            result.failures.pop(failure_key_value, None)
        except Exception as exc:
            result.errors.append(f"运费确认中间文件第{line_no}行无效：{exc}")
    return result
