#!/usr/bin/env python3
# coding: utf-8
"""Shared local fact store for Jingye reconciliation and platform sync.

This module is import-safe: it never parses CLI arguments, starts Appium, sends
network requests, or uploads files during import.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import concurrent.futures
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import fitz
from openpyxl import load_workbook
from PIL import Image


PARSER_VERSION = "jingye-ticket-hybrid-v2"
WEIGHT_TOLERANCE_KG = 10
DEFAULT_BASE_DIR = Path(r"D:\openclaw_data\data\jingyeapp")
DEFAULT_SALES_LOADING_POINT_NAME = "河北敬业钢铁"
DEFAULT_DRIVER_ROSTER_FILENAME = "driver_roster.csv"
FREIGHT_RETRY_DAYS = 14
FREIGHT_WAYBILL_DATE_RE = re.compile(r"^YS(\d{8})", re.IGNORECASE)

PURCHASE_UNLOADING_POINT_PREFIX = '河北敬业钢铁'

LOADING_DATE_ALIASES = (
    "装货日期", "实际装货日期", "装车日期", "发货日期", "起运日期"
)
UNLOADING_DATE_ALIASES = (
    "卸货日期", "实际卸货日期", "卸车日期", "收货日期", "到货日期"
)
LOADING_POINT_ALIASES = (
    "装货点", "装货点名称", "装货地点", "装货地", "装货地址",
    "发货点名称", "发货地", "发货地址", "起运地",
)
UNLOADING_POINT_ALIASES = (
    "卸货点", "卸货点名称", "卸货地点", "卸货地", "卸货地址",
    "卸货库房", "收货点名称", "收货地", "收货地址", "目的地",
)
LOADING_WEIGHT_ALIASES = (
    "装货重量", "装货净重", "装车净重", "发货重量", "原发数"
)
UNLOADING_WEIGHT_ALIASES = (
    "卸货重量", "卸货净重", "卸车净重", "收货重量"
)

STANDARD_FIELD_ALIASES = {
    "waybill_no": ("运单号", "单号"),
    "plate_no": ("车牌号", "车号"),
    "cargo_name": ("货物名称", "物料", "货名"),
    "source_loading_date": LOADING_DATE_ALIASES,
    "source_unloading_date": UNLOADING_DATE_ALIASES,
    "source_loading_point_name": LOADING_POINT_ALIASES,
    "source_unloading_point_name": UNLOADING_POINT_ALIASES,
    "source_loading_weight": LOADING_WEIGHT_ALIASES,
    "source_unloading_weight": UNLOADING_WEIGHT_ALIASES,
}

PDF_FILENAME_RE = re.compile(
    r"^(?P<waybill>.+?)_(?P<car>[^_]+)_(?P<day>\d{8})_(?P<clock>\d{6})"
    r"(?:_(?P<index>\d+))?$",
    re.IGNORECASE,
)
WEIGHT_LINE_RE = re.compile(
    r"(毛重|皮重|净重)\s*[：:]\s*([0-9]+(?:\.[0-9]+)?)"
    r"\s*吨/[^/\r\n]*/(\d{14})"
)
STANDARD_TIME_RE = re.compile(r"(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
ISO_LOCAL_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

PDF_ROOTS = (
    ("PURCHASE", "河北众沃", "pdf_caigouhebei"),
    ("PURCHASE", "上海宾迪", "pdf_caigoushanghai"),
    ("SALES", "河北众沃", "pdf_xiaoshouhebei"),
    ("SALES", "上海宾迪", "pdf_xiaoshoushanghai"),
)


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    stats_json TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK(mode IN ('AUTO', 'BACKFILL', 'LEGACY')),
    anchor_date TEXT NOT NULL DEFAULT '',
    purchase_start TEXT NOT NULL DEFAULT '',
    purchase_end TEXT NOT NULL DEFAULT '',
    sales_start TEXT NOT NULL DEFAULT '',
    sales_end TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS crawl_scopes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES crawl_runs(run_id) ON DELETE CASCADE,
    sheet_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_value TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('PURCHASE', 'SALES', 'FREIGHT')),
    window_start TEXT NOT NULL DEFAULT '',
    window_end TEXT NOT NULL DEFAULT '',
    records_seen INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(run_id, sheet_name, source_kind, source_value, direction)
);

CREATE TABLE IF NOT EXISTS waybills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL CHECK(direction IN ('PURCHASE', 'SALES')),
    waybill_no TEXT NOT NULL,
    plate_no TEXT NOT NULL,
    cargo_name TEXT NOT NULL DEFAULT '',
    destination TEXT NOT NULL DEFAULT '',
    load_weight_kg INTEGER,
    unload_weight_kg INTEGER,
    source_loading_date TEXT NOT NULL DEFAULT '',
    source_unloading_date TEXT NOT NULL DEFAULT '',
    source_loading_point_name TEXT NOT NULL DEFAULT '',
    source_unloading_point_name TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(direction, waybill_no, plate_no)
);

CREATE TABLE IF NOT EXISTS source_context_events (
    event_hash TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    excel_path TEXT NOT NULL DEFAULT '',
    sheet_name TEXT NOT NULL,
    excel_row TEXT NOT NULL DEFAULT '',
    waybill_no TEXT NOT NULL,
    plate_no TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_value TEXT NOT NULL,
    pdf_path TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    waybill_id INTEGER REFERENCES waybills(id) ON DELETE SET NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_context_waybill
ON source_context_events(waybill_id);
CREATE INDEX IF NOT EXISTS idx_source_context_scope
ON source_context_events(sheet_name, source_kind, source_value);

CREATE TABLE IF NOT EXISTS waybill_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    waybill_id INTEGER NOT NULL REFERENCES waybills(id) ON DELETE CASCADE,
    sheet_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_value TEXT NOT NULL,
    plate_no TEXT NOT NULL,
    first_seen_run_id TEXT NOT NULL,
    last_seen_run_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(waybill_id, sheet_name, source_kind, source_value, plate_no)
);
CREATE INDEX IF NOT EXISTS idx_waybill_sources_scope
ON waybill_sources(sheet_name, source_kind, source_value);

CREATE TABLE IF NOT EXISTS waybill_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    waybill_id INTEGER NOT NULL REFERENCES waybills(id) ON DELETE CASCADE,
    source_owner TEXT NOT NULL,
    excel_path TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    excel_row INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    row_json TEXT NOT NULL,
    UNIQUE(waybill_id, excel_path, sheet_name, excel_row)
);

CREATE TABLE IF NOT EXISTS purchase_observation_baselines (
    waybill_id INTEGER NOT NULL REFERENCES waybills(id) ON DELETE CASCADE,
    sheet_name TEXT NOT NULL,
    observation_id INTEGER NOT NULL REFERENCES waybill_observations(id),
    row_json TEXT NOT NULL,
    PRIMARY KEY(waybill_id, sheet_name)
);

CREATE TABLE IF NOT EXISTS waybill_attributes (
    waybill_id INTEGER NOT NULL REFERENCES waybills(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    source_column TEXT NOT NULL,
    value_text TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_observation_id INTEGER REFERENCES waybill_observations(id) ON DELETE SET NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(waybill_id, field_key)
);
CREATE INDEX IF NOT EXISTS idx_waybill_attributes_field_key
ON waybill_attributes(field_key);

CREATE TABLE IF NOT EXISTS sync_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS driver_roster (
    plate_no TEXT PRIMARY KEY,
    driver_name TEXT NOT NULL,
    driver_phone TEXT NOT NULL,
    source_owner TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    source_file TEXT NOT NULL DEFAULT '',
    source_row INTEGER,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_driver_roster_active
ON driver_roster(active);

CREATE TABLE IF NOT EXISTS documents (
    sha256 TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    page_count INTEGER,
    parser_version TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    pdf_business_code TEXT NOT NULL DEFAULT '',
    pdf_plate_no TEXT NOT NULL DEFAULT '',
    pdf_goods_name TEXT NOT NULL DEFAULT '',
    gross_weight_kg INTEGER,
    tare_weight_kg INTEGER,
    net_weight_kg INTEGER,
    gross_time TEXT,
    tare_time TEXT,
    net_time TEXT,
    document_time TEXT,
    parse_error TEXT NOT NULL DEFAULT '',
    parser_source TEXT NOT NULL DEFAULT 'TEXT',
    text_parse_error TEXT NOT NULL DEFAULT '',
    vlm_model TEXT NOT NULL DEFAULT '',
    vlm_prompt_version TEXT NOT NULL DEFAULT '',
    vlm_confidence REAL,
    vlm_warnings_json TEXT NOT NULL DEFAULT '[]',
    vlm_raw_json TEXT NOT NULL DEFAULT '',
    vlm_from_cache INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL REFERENCES documents(sha256) ON DELETE CASCADE,
    path TEXT NOT NULL,
    direction TEXT NOT NULL,
    source_owner TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    UNIQUE(sha256, path)
);

CREATE TABLE IF NOT EXISTS waybill_documents (
    waybill_id INTEGER NOT NULL REFERENCES waybills(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL REFERENCES documents(sha256) ON DELETE CASCADE,
    attachment_index INTEGER NOT NULL DEFAULT 1,
    download_time TEXT,
    first_linked_at TEXT NOT NULL,
    PRIMARY KEY(waybill_id, sha256)
);

CREATE TABLE IF NOT EXISTS logical_tickets (
    waybill_id INTEGER PRIMARY KEY REFERENCES waybills(id) ON DELETE CASCADE,
    side TEXT NOT NULL CHECK(side IN ('LOADING', 'UNLOADING')),
    method TEXT NOT NULL,
    external_ticket_no TEXT NOT NULL,
    gross_weight_kg INTEGER,
    tare_weight_kg INTEGER,
    net_weight_kg INTEGER,
    entry_time TEXT,
    exit_time TEXT,
    weigh_time TEXT,
    document_set_hash TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    validation_message TEXT NOT NULL DEFAULT '',
    artifact_type TEXT,
    artifact_path TEXT,
    artifact_sha256 TEXT,
    artifact_size_bytes INTEGER,
    artifact_page_count INTEGER,
    oss_url TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS freight_targets (
    target_key TEXT PRIMARY KEY,
    waybill_id INTEGER REFERENCES waybills(id) ON DELETE SET NULL,
    sheet_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_value TEXT NOT NULL,
    waybill_no TEXT NOT NULL,
    plate_no TEXT NOT NULL,
    app_export_time TEXT NOT NULL DEFAULT '',
    excel_row TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING', 'NOT_READY', 'PARTIAL', 'COMPLETE', 'ERROR', 'SOURCE_UNKNOWN')),
    first_seen_run_id TEXT NOT NULL,
    last_seen_run_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_attempt_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_freight_targets_scope_status
ON freight_targets(sheet_name, source_kind, source_value, status);
CREATE INDEX IF NOT EXISTS idx_freight_targets_waybill
ON freight_targets(waybill_no, plate_no);

CREATE TABLE IF NOT EXISTS freight_records (
    record_key TEXT PRIMARY KEY,
    card_key TEXT NOT NULL,
    target_key TEXT REFERENCES freight_targets(target_key) ON DELETE SET NULL,
    run_id TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_value TEXT NOT NULL,
    plate_no TEXT NOT NULL,
    waybill_no TEXT NOT NULL,
    outbound_datetime TEXT NOT NULL DEFAULT '',
    raw_spec TEXT NOT NULL DEFAULT '',
    normalized_spec TEXT NOT NULL DEFAULT '',
    settlement_quantity REAL NOT NULL,
    unit_price REAL NOT NULL,
    card_occurrence INTEGER NOT NULL DEFAULT 1,
    record_occurrence INTEGER NOT NULL DEFAULT 1,
    recorded_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_freight_records_target
ON freight_records(target_key);
CREATE INDEX IF NOT EXISTS idx_freight_records_scope_waybill
ON freight_records(sheet_name, source_kind, source_value, waybill_no);

CREATE TABLE IF NOT EXISTS freight_failures (
    failure_key TEXT PRIMARY KEY,
    card_key TEXT NOT NULL,
    target_key TEXT REFERENCES freight_targets(target_key) ON DELETE SET NULL,
    run_id TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_value TEXT NOT NULL,
    plate_no TEXT NOT NULL,
    waybill_no TEXT NOT NULL,
    outbound_datetime TEXT NOT NULL DEFAULT '',
    raw_spec TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    card_occurrence INTEGER NOT NULL DEFAULT 1,
    recorded_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_freight_failures_target
ON freight_failures(target_key);

CREATE TABLE IF NOT EXISTS freight_scope_runs (
    run_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_value TEXT NOT NULL,
    target_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    missing_waybills_json TEXT NOT NULL DEFAULT '[]',
    failed_card_count INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY(run_id, scope_key)
);

CREATE TABLE IF NOT EXISTS submissions (
    request_id TEXT PRIMARY KEY,
    waybill_id INTEGER NOT NULL UNIQUE REFERENCES waybills(id),
    source_system TEXT NOT NULL,
    external_waybill_no TEXT NOT NULL,
    external_ticket_no TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    response_code INTEGER,
    response_message TEXT,
    response_json TEXT,
    platform_waybill_id TEXT,
    first_prepared_at TEXT NOT NULL,
    last_attempt_at TEXT,
    success_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_submission_waybill_no
ON submissions(source_system, external_waybill_no);
CREATE UNIQUE INDEX IF NOT EXISTS idx_submission_ticket_no
ON submissions(source_system, external_ticket_no);
CREATE INDEX IF NOT EXISTS idx_submission_status ON submissions(status);

CREATE TABLE IF NOT EXISTS submission_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    waybill_id INTEGER NOT NULL,
    revision_no INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    response_code INTEGER,
    response_message TEXT,
    response_json TEXT,
    archived_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    UNIQUE(request_id, revision_no)
);

CREATE TABLE IF NOT EXISTS upload_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES submissions(request_id),
    attempted_at TEXT NOT NULL,
    method TEXT NOT NULL,
    http_status INTEGER,
    response_code INTEGER,
    state TEXT NOT NULL,
    response_message TEXT NOT NULL DEFAULT '',
    response_json TEXT NOT NULL DEFAULT ''
);
"""


@dataclasses.dataclass(frozen=True)
class ParsedTicket:
    sha256: str
    size_bytes: int
    page_count: int
    business_code: str
    plate_no: str
    goods_name: str
    gross_weight_kg: Optional[int]
    tare_weight_kg: Optional[int]
    net_weight_kg: Optional[int]
    gross_time: str
    tare_time: str
    net_time: str
    document_time: str
    status: str
    error: str = ""
    parser_source: str = "TEXT"
    text_parse_error: str = ""
    vlm_model: str = ""
    vlm_prompt_version: str = ""
    vlm_confidence: Optional[float] = None
    vlm_warnings_json: str = "[]"
    vlm_raw_json: str = ""
    vlm_from_cache: bool = False


@dataclasses.dataclass(frozen=True)
class Candidate:
    waybill_id: int
    direction: str
    waybill_no: str
    payload: Optional[dict[str, Any]]
    rejection_reason: str = ""


def now_text() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def normalize_waybill(value: Any) -> str:
    return str(value or "").strip().upper()


def normalize_plate(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def normalize_driver_phone(value: Any) -> str:
    return re.sub(r"[\s-]+", "", str(value or "").strip())


def normalize_compare(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", unicodedata.normalize("NFKC", str(value or ""))).lower()


def normalize_source_date(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = (
        unicodedata.normalize("NFKC", text)
        .replace("年", "-")
        .replace("月", "-")
        .replace("日", "")
        .replace("/", "-")
        .strip()
    )
    for pattern in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = dt.datetime.strptime(normalized, pattern)
            if pattern == "%Y-%m-%d":
                return parsed.date().isoformat()
            return parsed.isoformat(timespec="seconds")
        except ValueError:
            continue
    return text


def freight_target_date(
    waybill: Any,
    app_export_time: Any = None,
) -> Optional[dt.date]:
    """Use the date encoded in a YS waybill, falling back to APP export time."""
    match = FREIGHT_WAYBILL_DATE_RE.match(normalize_waybill(waybill))
    if match:
        try:
            return dt.datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            pass
    normalized = normalize_source_date(app_export_time)
    match = re.search(r"\d{4}-\d{2}-\d{2}", normalized)
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(0))
    except ValueError:
        return None


def freight_target_within_retry_window(
    waybill: Any,
    app_export_time: Any,
    anchor_date: Any,
    retry_days: int = FREIGHT_RETRY_DAYS,
) -> bool:
    """The limit is inclusive; unknown dates remain eligible instead of being lost."""
    target_date = freight_target_date(waybill, app_export_time)
    normalized_anchor = normalize_source_date(anchor_date)
    anchor_match = re.search(r"\d{4}-\d{2}-\d{2}", normalized_anchor)
    if target_date is None or anchor_match is None:
        return True
    try:
        anchor = dt.date.fromisoformat(anchor_match.group(0))
    except ValueError:
        return True
    return (anchor - target_date).days <= max(0, int(retry_days))


def canonical_field_key(source_column: Any) -> str:
    normalized = normalize_compare(source_column)
    for field_key, aliases in STANDARD_FIELD_ALIASES.items():
        if normalized in {normalize_compare(alias) for alias in aliases}:
            return field_key
    return f"excel.{normalized or 'unnamed'}"


def attribute_value_text(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value).strip()


def json_default(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=json_default)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decimal_to_kg(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return int((number * Decimal("1000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def kg_to_json_number(value: int) -> int | float:
    if value % 1000 == 0:
        return value // 1000
    return float(Decimal(value) / Decimal("1000"))


def valid_weight_triplet(gross: Optional[int], tare: Optional[int], net: Optional[int]) -> bool:
    if gross is None or tare is None or net is None:
        return False
    if gross <= 0 or tare <= 0 or net <= 0 or gross < tare:
        return False
    return abs((gross - tare) - net) <= WEIGHT_TOLERANCE_KG


def parse_compact_time(value: str) -> str:
    parsed = dt.datetime.strptime(value, "%Y%m%d%H%M%S")
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def find_line_value(text: str, label: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(label)}\s*[：:]\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def parse_pdf_ticket(path: Path, known_sha256: str = "") -> ParsedTicket:
    digest = known_sha256 or file_sha256(path)
    size = path.stat().st_size
    page_count = 0
    try:
        if size < 1024:
            raise ValueError("PDF文件小于1024字节")
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError("文件头不是PDF")
        document = fitz.open(path)
        try:
            page_count = document.page_count
            text = "\n".join(page.get_text("text") for page in document)
        finally:
            document.close()

        values: dict[str, tuple[int, str]] = {}
        for label, weight, compact_time in WEIGHT_LINE_RE.findall(text):
            kg = decimal_to_kg(weight)
            if kg is not None:
                values[label] = (kg, parse_compact_time(compact_time))

        business_match = re.search(
            r"(?m)^\s*业务\s*[：:]\s*([A-Za-z0-9-]{1,64})\s*$", text
        ) or re.search(r"业务\s*[：:]\s*([A-Za-z0-9-]{1,64})", text)
        standard_times = STANDARD_TIME_RE.findall(text)
        document_time = standard_times[-1].replace(" ", "T") if standard_times else ""

        missing = [name for name in ("毛重", "皮重", "净重") if name not in values]
        if missing:
            raise ValueError("缺少重量行：" + "、".join(missing))
        gross, gross_time = values["毛重"]
        tare, tare_time = values["皮重"]
        net, net_time = values["净重"]
        if not valid_weight_triplet(gross, tare, net):
            raise ValueError("毛皮净重关系不满足接口校验")
        if net_time != max(gross_time, tare_time):
            raise ValueError("净重时间不是毛重、皮重中的完成时间")

        return ParsedTicket(
            sha256=digest,
            size_bytes=size,
            page_count=page_count,
            business_code=business_match.group(1) if business_match else "",
            plate_no=normalize_plate(find_line_value(text, "车号")),
            goods_name=find_line_value(text, "货名"),
            gross_weight_kg=gross,
            tare_weight_kg=tare,
            net_weight_kg=net,
            gross_time=gross_time,
            tare_time=tare_time,
            net_time=net_time,
            document_time=document_time,
            status="VALID",
        )
    except Exception as exc:
        return ParsedTicket(
            sha256=digest,
            size_bytes=size,
            page_count=page_count,
            business_code="",
            plate_no="",
            goods_name="",
            gross_weight_kg=None,
            tare_weight_kg=None,
            net_weight_kg=None,
            gross_time="",
            tare_time="",
            net_time="",
            document_time="",
            status="INVALID",
            error=f"{type(exc).__name__}: {exc}",
        )


def normalize_ticket_time(value: Any, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError("视觉结果缺少称重时间")
        return ""
    compact = re.sub(r"\D", "", text)
    if len(compact) == 14:
        return parse_compact_time(compact)
    normalized = text.replace("/", "-").replace(" ", "T")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"视觉时间格式无效: {text}") from exc
    return parsed.isoformat(timespec="seconds")


def parse_pdf_ticket_with_fallback(
    path: Path,
    known_sha256: str,
    cache_dir: Path,
) -> ParsedTicket:
    text_result = parse_pdf_ticket(path, known_sha256)
    if text_result.status == "VALID":
        return text_result

    vlm_model = ""
    prompt_version = ""
    try:
        from jingye_vlm import (
            MODEL_NAME,
            PROMPT_VERSION,
            extract_ticket_with_vlm,
        )

        vlm_model = MODEL_NAME
        prompt_version = PROMPT_VERSION
        with fitz.open(path) as document:
            auxiliary_text = "\n".join(page.get_text("text") for page in document)
        visual = extract_ticket_with_vlm(
            path,
            sha256=known_sha256,
            aux_text=auxiliary_text,
            cache_dir=cache_dir,
        )
        gross = decimal_to_kg(visual.get("gross_weight"))
        tare = decimal_to_kg(visual.get("tare_weight"))
        net = decimal_to_kg(visual.get("net_weight"))
        if not valid_weight_triplet(gross, tare, net):
            raise ValueError("视觉结果毛皮净重关系不满足接口校验")
        gross_time = normalize_ticket_time(visual.get("gross_time"))
        tare_time = normalize_ticket_time(visual.get("tare_time"))
        net_time = normalize_ticket_time(visual.get("net_time"))
        if net_time != max(gross_time, tare_time):
            raise ValueError("视觉结果净重时间不是毛重、皮重中的完成时间")
        warnings = visual.get("warnings") or []
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
        try:
            confidence = float(visual.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        raw_result = {
            key: value
            for key, value in visual.items()
            if not str(key).startswith("_")
        }
        return ParsedTicket(
            sha256=known_sha256,
            size_bytes=path.stat().st_size,
            page_count=int(visual.get("_page_count") or text_result.page_count or 0),
            business_code=str(visual.get("business_code") or "").strip(),
            plate_no=normalize_plate(visual.get("plate_no")),
            goods_name=str(visual.get("goods_name") or "").strip(),
            gross_weight_kg=gross,
            tare_weight_kg=tare,
            net_weight_kg=net,
            gross_time=gross_time,
            tare_time=tare_time,
            net_time=net_time,
            document_time=normalize_ticket_time(
                visual.get("document_time"), required=False
            ),
            status="VALID",
            parser_source="VLM_FALLBACK",
            text_parse_error=text_result.error,
            vlm_model=vlm_model,
            vlm_prompt_version=prompt_version,
            vlm_confidence=max(0.0, min(1.0, confidence)),
            vlm_warnings_json=canonical_json([str(item) for item in warnings]),
            vlm_raw_json=canonical_json(raw_result),
            vlm_from_cache=bool(visual.get("_from_cache")),
        )
    except Exception as exc:
        return dataclasses.replace(
            text_result,
            parser_source="INVALID",
            text_parse_error=text_result.error,
            vlm_model=vlm_model,
            vlm_prompt_version=prompt_version,
            error=(
                f"{text_result.error}; VLM兜底失败: "
                f"{type(exc).__name__}: {exc}"
            ),
        )


DOCUMENT_AUDIT_COLUMNS = {
    "parser_source": "TEXT NOT NULL DEFAULT 'TEXT'",
    "text_parse_error": "TEXT NOT NULL DEFAULT ''",
    "vlm_model": "TEXT NOT NULL DEFAULT ''",
    "vlm_prompt_version": "TEXT NOT NULL DEFAULT ''",
    "vlm_confidence": "REAL",
    "vlm_warnings_json": "TEXT NOT NULL DEFAULT '[]'",
    "vlm_raw_json": "TEXT NOT NULL DEFAULT ''",
    "vlm_from_cache": "INTEGER NOT NULL DEFAULT 0",
}

WAYBILL_STANDARD_COLUMNS = {
    "source_loading_date": "TEXT NOT NULL DEFAULT ''",
    "source_unloading_date": "TEXT NOT NULL DEFAULT ''",
    "source_loading_point_name": "TEXT NOT NULL DEFAULT ''",
    "source_unloading_point_name": "TEXT NOT NULL DEFAULT ''",
}

LOGICAL_TICKET_TIME_COLUMNS = {
    'entry_time': 'TEXT',
    'exit_time': 'TEXT',
}

FREIGHT_RECORD_INSTANCE_COLUMNS = {
    'card_occurrence': 'INTEGER NOT NULL DEFAULT 1',
    'record_occurrence': 'INTEGER NOT NULL DEFAULT 1',
}


def ensure_document_audit_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(documents)")
    }
    for column, definition in DOCUMENT_AUDIT_COLUMNS.items():
        if column not in existing:
            connection.execute(
                f"ALTER TABLE documents ADD COLUMN {column} {definition}"
            )


def ensure_waybill_standard_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(waybills)")
    }
    for column, definition in WAYBILL_STANDARD_COLUMNS.items():
        if column not in existing:
            connection.execute(
                f"ALTER TABLE waybills ADD COLUMN {column} {definition}"
            )
    connection.execute(
        """
        UPDATE waybills
        SET source_unloading_point_name=destination
        WHERE TRIM(source_unloading_point_name)='' AND TRIM(destination)!=''
        """
    )


def ensure_logical_ticket_time_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row['name'])
        for row in connection.execute('PRAGMA table_info(logical_tickets)')
    }
    for column, definition in LOGICAL_TICKET_TIME_COLUMNS.items():
        if column not in existing:
            connection.execute(
                f'ALTER TABLE logical_tickets ADD COLUMN {column} {definition}'
            )


def ensure_freight_instance_schema(connection: sqlite3.Connection) -> None:
    record_columns = {
        str(row['name']) for row in connection.execute('PRAGMA table_info(freight_records)')
    }
    for column, definition in FREIGHT_RECORD_INSTANCE_COLUMNS.items():
        if column not in record_columns:
            connection.execute(
                f'ALTER TABLE freight_records ADD COLUMN {column} {definition}'
            )

    failure_columns = {
        str(row['name']) for row in connection.execute('PRAGMA table_info(freight_failures)')
    }
    if 'failure_key' not in failure_columns:
        connection.execute('DROP INDEX IF EXISTS idx_freight_failures_target')
        connection.execute('ALTER TABLE freight_failures RENAME TO freight_failures_legacy')
        connection.execute(
            '''
            CREATE TABLE freight_failures (
                failure_key TEXT PRIMARY KEY,
                card_key TEXT NOT NULL,
                target_key TEXT REFERENCES freight_targets(target_key) ON DELETE SET NULL,
                run_id TEXT NOT NULL,
                sheet_name TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_value TEXT NOT NULL,
                plate_no TEXT NOT NULL,
                waybill_no TEXT NOT NULL,
                outbound_datetime TEXT NOT NULL DEFAULT '',
                raw_spec TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                card_occurrence INTEGER NOT NULL DEFAULT 1,
                recorded_at TEXT NOT NULL,
                raw_json TEXT NOT NULL
            )
            '''
        )
        connection.execute(
            '''
            INSERT INTO freight_failures(
                failure_key, card_key, target_key, run_id, sheet_name,
                source_kind, source_value, plate_no, waybill_no,
                outbound_datetime, raw_spec, error_message, attempt,
                card_occurrence, recorded_at, raw_json
            )
            SELECT card_key, card_key, target_key, run_id, sheet_name,
                   source_kind, source_value, plate_no, waybill_no,
                   outbound_datetime, raw_spec, error_message, attempt,
                   1, recorded_at, raw_json
            FROM freight_failures_legacy
            '''
        )
        connection.execute('DROP TABLE freight_failures_legacy')
    elif 'card_occurrence' not in failure_columns:
        connection.execute(
            'ALTER TABLE freight_failures ADD COLUMN card_occurrence INTEGER NOT NULL DEFAULT 1'
        )
    connection.execute(
        'CREATE INDEX IF NOT EXISTS idx_freight_failures_target ON freight_failures(target_key)'
    )


def create_business_views(connection: sqlite3.Connection) -> None:
    connection.executescript(
        f"""
        DROP VIEW IF EXISTS v_waybill_business_facts;
        CREATE VIEW v_waybill_business_facts AS
        WITH settings AS (
            SELECT
                COALESCE(
                    MAX(CASE WHEN key='sales_loading_point_name' THEN value END),
                    '{DEFAULT_SALES_LOADING_POINT_NAME}'
                ) AS sales_loading_point_name,
                COALESCE(
                    MAX(CASE WHEN key='purchase_unloading_point_prefix' THEN value END),
                    '{PURCHASE_UNLOADING_POINT_PREFIX}'
                ) AS purchase_unloading_point_prefix
            FROM sync_settings
        ), base AS (
            SELECT
                w.*,
                COALESCE(
                    NULLIF(w.source_unloading_point_name, ''),
                    NULLIF(w.destination, '')
                ) AS raw_unloading_point_name,
                settings.sales_loading_point_name,
                settings.purchase_unloading_point_prefix,
                (
                    SELECT a.value_text FROM waybill_attributes a
                    WHERE a.waybill_id=w.id AND a.field_key='excel.承运商'
                ) AS purchase_carrier_name,
                lt.side,
                lt.method,
                lt.external_ticket_no,
                lt.gross_weight_kg,
                lt.tare_weight_kg,
                lt.net_weight_kg,
                lt.entry_time,
                lt.exit_time,
                lt.weigh_time,
                lt.validation_status,
                lt.validation_message,
                lt.artifact_type,
                lt.artifact_path,
                lt.artifact_sha256,
                lt.artifact_size_bytes,
                lt.artifact_page_count,
                lt.oss_url
            FROM waybills w
            LEFT JOIN logical_tickets lt ON lt.waybill_id=w.id
            CROSS JOIN settings
        )
        SELECT
            id AS waybill_id,
            direction,
            waybill_no,
            plate_no,
            cargo_name,
            source_loading_date,
            source_unloading_date,
            source_loading_point_name,
            raw_unloading_point_name AS source_unloading_point_name,
            load_weight_kg AS excel_loading_weight_kg,
            unload_weight_kg AS excel_unloading_weight_kg,
            CASE
                WHEN direction='SALES' THEN COALESCE(
                    NULLIF(source_loading_point_name, ''),
                    sales_loading_point_name
                )
                ELSE NULLIF(source_loading_point_name, '')
            END AS loading_point_name,
            raw_unloading_point_name AS unloading_point_name,
            CASE
                WHEN direction='SALES' THEN weigh_time
                ELSE NULLIF(source_loading_date, '')
            END AS loading_time,
            CASE
                WHEN direction='SALES' THEN net_weight_kg
                ELSE load_weight_kg
            END AS loading_weight_kg,
            CASE
                WHEN direction='PURCHASE' THEN weigh_time
                ELSE NULLIF(source_unloading_date, '')
            END AS unloading_time,
            CASE
                WHEN direction='PURCHASE' THEN net_weight_kg
                ELSE unload_weight_kg
            END AS unloading_weight_kg,
            CASE
                WHEN direction='SALES' THEN COALESCE(
                    NULLIF(source_loading_point_name, ''),
                    sales_loading_point_name
                )
                ELSE NULLIF(TRIM(purchase_carrier_name), '')
            END AS platform_loading_point_name,
            CASE
                WHEN direction='PURCHASE'
                     AND raw_unloading_point_name!=''
                     AND raw_unloading_point_name NOT LIKE
                         purchase_unloading_point_prefix || '%'
                    THEN purchase_unloading_point_prefix ||
                         raw_unloading_point_name
                ELSE raw_unloading_point_name
            END AS platform_unloading_point_name,
            side AS ticket_side,
            method AS ticket_method,
            external_ticket_no,
            gross_weight_kg,
            tare_weight_kg,
            net_weight_kg,
            entry_time,
            exit_time,
            weigh_time,
            validation_status,
            validation_message,
            artifact_type,
            artifact_path,
            artifact_sha256,
            artifact_size_bytes,
            artifact_page_count,
            oss_url,
            (
                SELECT COUNT(*)
                FROM waybill_documents wd
                WHERE wd.waybill_id=base.id
            ) AS local_pdf_count,
            first_seen_at,
            last_seen_at
        FROM base;
        """
    )


def update_business_settings(
    connection: sqlite3.Connection,
    *,
    project_name: str = "",
    sales_loading_point_name: str = DEFAULT_SALES_LOADING_POINT_NAME,
) -> None:
    values = {
        "project_name": str(project_name or "").strip(),
        "sales_loading_point_name": str(
            sales_loading_point_name or DEFAULT_SALES_LOADING_POINT_NAME
        ).strip(),
        "purchase_unloading_point_prefix": PURCHASE_UNLOADING_POINT_PREFIX,
    }
    current_time = now_text()
    for key, value in values.items():
        connection.execute(
            """
            INSERT INTO sync_settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, value, current_time),
        )


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=60000")
    connection.executescript(SCHEMA_SQL)
    ensure_document_audit_columns(connection)
    ensure_waybill_standard_columns(connection)
    ensure_logical_ticket_time_columns(connection)
    ensure_freight_instance_schema(connection)
    create_business_views(connection)
    connection.execute(
        'INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)',
        ('schema_version', '9'),
    )
    connection.commit()
    return connection


def _driver_csv_value(row: dict[str, Any], *aliases: str) -> str:
    for alias in aliases:
        value = row.get(alias)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _driver_active_value(value: Any) -> int:
    text = normalize_compare(value)
    if not text:
        return 1
    if text in {"1", "true", "yes", "y", "是", "启用", "在用"}:
        return 1
    if text in {"0", "false", "no", "n", "否", "停用", "禁用"}:
        return 0
    raise ValueError(f"无法识别司机启用状态：{value}")


def import_driver_roster(
    connection: sqlite3.Connection, path: Path
) -> dict[str, Any]:
    """Validate and upsert upload-only vehicle/driver snapshots from CSV."""
    path = path.resolve()
    if not path.is_file():
        return {
            "path": str(path),
            "exists": False,
            "rows": 0,
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
        }

    parsed: list[dict[str, Any]] = []
    seen_plates: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f"司机主数据CSV缺少表头：{path}")
        for row_number, row in enumerate(reader, 2):
            if not any(str(value or "").strip() for value in row.values()):
                continue
            plate_no = normalize_plate(
                _driver_csv_value(row, "plate_no", "车牌号", "车牌")
            )
            driver_name = _driver_csv_value(
                row, "driver_name", "司机姓名", "司机名"
            )
            driver_phone = normalize_driver_phone(
                _driver_csv_value(
                    row,
                    "driver_phone",
                    "司机手机号",
                    "手机号",
                    "手机号码",
                    "联系电话",
                    "电话",
                )
            )
            source_owner = _driver_csv_value(
                row, "source_owner", "来源公司", "所属公司", "承运公司"
            )
            active = _driver_active_value(
                _driver_csv_value(row, "active", "启用", "是否启用")
            )
            if not plate_no:
                raise ValueError(f"{path.name}第{row_number}行缺少车牌号")
            if len(plate_no) > 32:
                raise ValueError(f"{path.name}第{row_number}行车牌号超过32字符")
            if not driver_name:
                raise ValueError(f"{path.name}第{row_number}行缺少司机姓名")
            if len(driver_name) > 64:
                raise ValueError(f"{path.name}第{row_number}行司机姓名超过64字符")
            if not re.fullmatch(r"\+?[0-9]{6,31}", driver_phone):
                raise ValueError(f"{path.name}第{row_number}行司机手机号格式无效")
            if plate_no in seen_plates:
                raise ValueError(f"{path.name}第{row_number}行车牌号重复：{plate_no}")
            seen_plates.add(plate_no)
            parsed.append(
                {
                    "plate_no": plate_no,
                    "driver_name": driver_name,
                    "driver_phone": driver_phone,
                    "source_owner": source_owner,
                    "active": active,
                    "source_row": row_number,
                }
            )

    stats = Counter()
    current_time = now_text()
    for item in parsed:
        existing = connection.execute(
            "SELECT * FROM driver_roster WHERE plate_no=?", (item["plate_no"],)
        ).fetchone()
        values = (
            item["driver_name"],
            item["driver_phone"],
            item["source_owner"],
            item["active"],
            str(path),
            item["source_row"],
        )
        if existing is None:
            stats["inserted"] += 1
        elif values == (
            existing["driver_name"],
            existing["driver_phone"],
            existing["source_owner"],
            existing["active"],
            existing["source_file"],
            existing["source_row"],
        ):
            stats["unchanged"] += 1
            continue
        else:
            stats["updated"] += 1
        connection.execute(
            """
            INSERT INTO driver_roster(
                plate_no, driver_name, driver_phone, source_owner, active,
                source_file, source_row, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plate_no) DO UPDATE SET
                driver_name=excluded.driver_name,
                driver_phone=excluded.driver_phone,
                source_owner=excluded.source_owner,
                active=excluded.active,
                source_file=excluded.source_file,
                source_row=excluded.source_row,
                updated_at=excluded.updated_at
            """,
            (item["plate_no"], *values, current_time),
        )
    connection.commit()
    return {
        "path": str(path),
        "exists": True,
        "rows": len(parsed),
        "inserted": int(stats["inserted"]),
        "updated": int(stats["updated"]),
        "unchanged": int(stats["unchanged"]),
        "active": sum(int(item["active"]) for item in parsed),
    }


def driver_for_plate(
    connection: sqlite3.Connection, plate_no: Any
) -> Optional[sqlite3.Row]:
    """Prefer an exact canonical plate; only then try one missing-D alias."""
    normalized = normalize_plate(plate_no)
    if not normalized:
        return None
    exact = connection.execute(
        "SELECT * FROM driver_roster WHERE active=1 AND plate_no=?",
        (normalized,),
    ).fetchone()
    if exact is not None or normalized.endswith("D"):
        return exact
    return connection.execute(
        "SELECT * FROM driver_roster WHERE active=1 AND plate_no=?",
        (f"{normalized}D",),
    ).fetchone()


def sheet_direction(sheet_name: str) -> Optional[str]:
    if sheet_name.startswith("采购单-"):
        return "PURCHASE"
    if sheet_name.startswith("销售单-"):
        return "SALES"
    return None


def header_index(row: Iterable[Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, value in enumerate(row):
        normalized = normalize_compare(value)
        if normalized:
            result.setdefault(normalized, index)
    return result


def get_column(headers: dict[str, int], *aliases: str) -> Optional[int]:
    for alias in aliases:
        normalized = normalize_compare(alias)
        if normalized in headers:
            return headers[normalized]
    return None


def _cell(row: tuple[Any, ...], index: Optional[int]) -> Any:
    return row[index] if index is not None and index < len(row) else None


def upsert_waybill(
    connection: sqlite3.Connection,
    direction: str,
    waybill_no: str,
    plate_no: str,
    cargo_name: str = "",
    destination: str = "",
    load_weight_kg: Optional[int] = None,
    unload_weight_kg: Optional[int] = None,
    source_loading_date: str = "",
    source_unloading_date: str = "",
    source_loading_point_name: str = "",
    source_unloading_point_name: str = "",
) -> int:
    current = connection.execute(
        "SELECT * FROM waybills WHERE direction=? AND waybill_no=? AND plate_no=?",
        (direction, waybill_no, plate_no),
    ).fetchone()
    current_time = now_text()
    if current is None:
        cursor = connection.execute(
            """
            INSERT INTO waybills(
                direction, waybill_no, plate_no, cargo_name, destination,
                load_weight_kg, unload_weight_kg,
                source_loading_date, source_unloading_date,
                source_loading_point_name, source_unloading_point_name,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                direction,
                waybill_no,
                plate_no,
                cargo_name,
                destination,
                load_weight_kg,
                unload_weight_kg,
                source_loading_date,
                source_unloading_date,
                source_loading_point_name,
                source_unloading_point_name or destination,
                current_time,
                current_time,
            ),
        )
        return int(cursor.lastrowid)
    connection.execute(
        """
        UPDATE waybills SET
            cargo_name=?, destination=?, load_weight_kg=?, unload_weight_kg=?,
            source_loading_date=?, source_unloading_date=?,
            source_loading_point_name=?, source_unloading_point_name=?,
            last_seen_at=? WHERE id=?
        """,
        (
            cargo_name or current["cargo_name"],
            destination or current["destination"],
            load_weight_kg if load_weight_kg is not None else current["load_weight_kg"],
            unload_weight_kg if unload_weight_kg is not None else current["unload_weight_kg"],
            source_loading_date or current["source_loading_date"],
            source_unloading_date or current["source_unloading_date"],
            source_loading_point_name or current["source_loading_point_name"],
            source_unloading_point_name
            or destination
            or current["source_unloading_point_name"],
            current_time,
            current["id"],
        ),
    )
    return int(current["id"])


def clean_purchase_carrier(value: Any) -> str:
    text = str(value or '').strip()
    text = re.sub(r'^承运商\s*[:：]\s*', '', text).strip()
    return '' if text == '无' else text


def seed_purchase_observation_baselines(connection: sqlite3.Connection) -> int:
    """Freeze the effective purchase row while retaining later raw observations."""
    observations = connection.execute(
        """
        WITH ranked AS (
            SELECT o.*, ROW_NUMBER() OVER (
                PARTITION BY o.waybill_id, o.sheet_name
                ORDER BY o.observed_at DESC, o.id DESC
            ) AS rank_no
            FROM waybill_observations o
            JOIN waybills w ON w.id=o.waybill_id
            WHERE w.direction='PURCHASE'
        )
        SELECT r.*, a.value_text AS carrier_value, source.sheet_name AS carrier_sheet
        FROM ranked r
        LEFT JOIN purchase_observation_baselines b
          ON b.waybill_id=r.waybill_id AND b.sheet_name=r.sheet_name
        LEFT JOIN waybill_attributes a
          ON a.waybill_id=r.waybill_id AND a.field_key='excel.承运商'
        LEFT JOIN waybill_observations source
          ON source.id=a.source_observation_id AND source.waybill_id=r.waybill_id
        WHERE r.rank_no=1 AND b.waybill_id IS NULL
        """
    ).fetchall()
    created = 0
    for observation in observations:
        baseline_json = observation['row_json']
        try:
            baseline_row = json.loads(baseline_json)
        except (TypeError, ValueError):
            baseline_row = None
        if isinstance(baseline_row, dict):
            carrier = (
                clean_purchase_carrier(observation['carrier_value'])
                if observation['carrier_sheet'] == observation['sheet_name'] else ''
            )
            if not carrier:
                carrier = clean_purchase_carrier(baseline_row.get('承运商'))
            if carrier or '承运商' in baseline_row:
                baseline_row['承运商'] = carrier
            baseline_json = canonical_json(baseline_row)
        result = connection.execute(
            """
            INSERT OR IGNORE INTO purchase_observation_baselines(
                waybill_id, sheet_name, observation_id, row_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                observation['waybill_id'], observation['sheet_name'],
                observation['id'], baseline_json,
            ),
        )
        created += result.rowcount
    return created


def store_waybill_attributes(
    connection: sqlite3.Connection,
    *,
    waybill_id: int,
    observation_id: int,
    observed_at: str,
    row_object: dict[str, Any],
    purchase_carrier_only: bool = False,
) -> int:
    stored = 0
    waybill = connection.execute(
        'SELECT direction FROM waybills WHERE id=?', (waybill_id,)
    ).fetchone()
    is_purchase = bool(waybill and waybill['direction'] == 'PURCHASE')
    for source_column, value in row_object.items():
        field_key = canonical_field_key(source_column)
        if purchase_carrier_only and field_key != 'excel.承运商':
            continue
        is_purchase_carrier = is_purchase and field_key == 'excel.承运商'
        if is_purchase_carrier:
            value = clean_purchase_carrier(value)
        if value in (None, ""):
            continue
        update_condition = (
            "TRIM(waybill_attributes.value_text)=''"
            if is_purchase_carrier
            else """
                excluded.observed_at > waybill_attributes.observed_at
                OR (
                    excluded.observed_at = waybill_attributes.observed_at
                    AND excluded.source_observation_id >=
                        COALESCE(waybill_attributes.source_observation_id, -1)
                )
            """
        )
        connection.execute(
            f"""
            INSERT INTO waybill_attributes(
                waybill_id, field_key, source_column, value_text, value_json,
                source_observation_id, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(waybill_id, field_key) DO UPDATE SET
                source_column=excluded.source_column,
                value_text=excluded.value_text,
                value_json=excluded.value_json,
                source_observation_id=excluded.source_observation_id,
                observed_at=excluded.observed_at
            WHERE {update_condition}
            """,
            (
                waybill_id,
                field_key,
                str(source_column).strip(),
                attribute_value_text(value),
                canonical_json(value),
                observation_id,
                observed_at,
            ),
        )
        stored += 1
    return stored


def import_excel_history(connection: sqlite3.Connection, base_dir: Path) -> dict[str, int]:
    stats = Counter()
    existing_purchases = {
        (row['waybill_no'], row['plate_no']): int(row['id'])
        for row in connection.execute(
            "SELECT id, waybill_no, plate_no FROM waybills WHERE direction='PURCHASE'"
        )
    }
    stats['purchase_baselines_created'] += seed_purchase_observation_baselines(connection)
    excel_files = sorted(
        (base_dir / "excel_ori").glob("*.xlsx"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
    for excel_path in excel_files:
        workbook = load_workbook(excel_path, read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets:
                direction = sheet_direction(sheet.title)
                if not direction:
                    continue
                owner = sheet.title.split("-", 1)[1] if "-" in sheet.title else ""
                header_values = tuple(
                    cell.value for cell in next(sheet.iter_rows(min_row=2, max_row=2))
                )
                headers = header_index(header_values)
                waybill_col = get_column(headers, "运单号", "单号")
                plate_col = get_column(headers, "车牌号", "车号")
                cargo_col = get_column(headers, "货物名称", "物料", "货名")
                loading_point_col = get_column(headers, *LOADING_POINT_ALIASES)
                destination_col = get_column(headers, *UNLOADING_POINT_ALIASES)
                loading_date_col = get_column(headers, *LOADING_DATE_ALIASES)
                unloading_date_col = get_column(headers, *UNLOADING_DATE_ALIASES)
                load_col = get_column(headers, *LOADING_WEIGHT_ALIASES)
                unload_col = get_column(headers, *UNLOADING_WEIGHT_ALIASES)
                if waybill_col is None or plate_col is None:
                    stats["excel_sheets_missing_key_columns"] += 1
                    continue
                for row_number, row in enumerate(
                    sheet.iter_rows(min_row=3, values_only=True), start=3
                ):
                    waybill = normalize_waybill(_cell(row, waybill_col))
                    plate = normalize_plate(_cell(row, plate_col))
                    if not waybill or not plate:
                        continue
                    cargo = str(_cell(row, cargo_col) or "").strip()
                    loading_point = str(
                        _cell(row, loading_point_col) or ""
                    ).strip()
                    destination = str(_cell(row, destination_col) or "").strip()
                    loading_date = normalize_source_date(
                        _cell(row, loading_date_col)
                    )
                    unloading_date = normalize_source_date(
                        _cell(row, unloading_date_col)
                    )
                    load_kg = decimal_to_kg(_cell(row, load_col))
                    unload_kg = decimal_to_kg(_cell(row, unload_col))
                    preserved_purchase_id = (
                        existing_purchases.get((waybill, plate))
                        if direction == 'PURCHASE' else None
                    )
                    if preserved_purchase_id is not None:
                        waybill_id = preserved_purchase_id
                        stats['purchase_rows_preserved'] += 1
                    else:
                        waybill_id = upsert_waybill(
                            connection,
                            direction=direction,
                            waybill_no=waybill,
                            plate_no=plate,
                            cargo_name=cargo,
                            destination=destination,
                            load_weight_kg=load_kg,
                            unload_weight_kg=unload_kg,
                            source_loading_date=loading_date,
                            source_unloading_date=unloading_date,
                            source_loading_point_name=loading_point,
                            source_unloading_point_name=destination,
                        )
                    row_object = {
                        str(header_values[index] or f"列{index + 1}"): value
                        for index, value in enumerate(row)
                        if value not in (None, "")
                    }
                    observed_at = dt.datetime.fromtimestamp(
                        excel_path.stat().st_mtime
                    ).isoformat(timespec="seconds")
                    connection.execute(
                        """
                        INSERT INTO waybill_observations(
                            waybill_id, source_owner, excel_path, sheet_name,
                            excel_row, observed_at, row_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(
                            waybill_id, excel_path, sheet_name, excel_row
                        ) DO UPDATE SET
                            source_owner=excluded.source_owner,
                            observed_at=excluded.observed_at,
                            row_json=excluded.row_json
                        """,
                        (
                            waybill_id,
                            owner,
                            str(excel_path.resolve()),
                            sheet.title,
                            row_number,
                            observed_at,
                            canonical_json(row_object),
                        ),
                    )
                    observation = connection.execute(
                        """
                        SELECT id FROM waybill_observations
                        WHERE waybill_id=? AND excel_path=?
                          AND sheet_name=? AND excel_row=?
                        """,
                        (
                            waybill_id,
                            str(excel_path.resolve()),
                            sheet.title,
                            row_number,
                        ),
                    ).fetchone()
                    if observation is None:
                        raise RuntimeError("写入Excel观察记录后未能重新定位记录")
                    stats["excel_fields_seen"] += store_waybill_attributes(
                        connection,
                        waybill_id=waybill_id,
                        observation_id=int(observation["id"]),
                        observed_at=observed_at,
                        row_object=row_object,
                        purchase_carrier_only=preserved_purchase_id is not None,
                    )
                    stats["excel_rows_seen"] += 1
        finally:
            workbook.close()
        stats["excel_files"] += 1
    stats['purchase_baselines_created'] += seed_purchase_observation_baselines(connection)
    connection.commit()
    stats["unique_waybills"] = connection.execute("SELECT COUNT(*) FROM waybills").fetchone()[0]
    stats["observations"] = connection.execute("SELECT COUNT(*) FROM waybill_observations").fetchone()[0]
    stats["waybill_attributes"] = connection.execute(
        "SELECT COUNT(*) FROM waybill_attributes"
    ).fetchone()[0]
    stats["attribute_keys"] = connection.execute(
        "SELECT COUNT(DISTINCT field_key) FROM waybill_attributes"
    ).fetchone()[0]
    return dict(stats)


def _event_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _source_value(source_kind: Any, source_value: Any) -> str:
    value = str(source_value or '').strip()
    return normalize_plate(value) if str(source_kind or '').strip() == 'selected_car' else value


def _source_scope_key(sheet_name: Any, source_kind: Any, source_value: Any) -> str:
    return '|'.join(
        (
            str(sheet_name or '').strip(),
            str(source_kind or '').strip(),
            _source_value(source_kind, source_value),
        )
    )


def _find_waybill_id(
    connection: sqlite3.Connection,
    *,
    sheet_name: str,
    waybill_no: str,
    plate_no: str,
) -> Optional[int]:
    direction = sheet_direction(sheet_name)
    if not direction or not waybill_no or not plate_no:
        return None
    row = connection.execute(
        'SELECT id FROM waybills WHERE direction=? AND waybill_no=? AND plate_no=?',
        (direction, waybill_no, plate_no),
    ).fetchone()
    return int(row['id']) if row is not None else None


def import_source_context_history(
    connection: sqlite3.Connection, base_dir: Path
) -> dict[str, int]:
    '''Import recorded login-account/selected-car evidence without guessing.'''
    stats = Counter()
    root = base_dir / 'source_context'
    if not root.exists():
        return dict(stats)
    paths = sorted(root.glob('*.jsonl'), key=lambda item: (item.stat().st_mtime_ns, item.name))
    for path in paths:
        stats['files_seen'] += 1
        fallback_time = dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec='seconds')
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except Exception:
            stats['files_failed'] += 1
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError('JSONL event is not an object')
            except Exception:
                stats['invalid_lines'] += 1
                continue
            run_id = str(value.get('run_id') or path.stem).strip()
            sheet_name = str(value.get('sheet') or '').strip()
            waybill_no = normalize_waybill(value.get('waybill'))
            plate_no = normalize_plate(value.get('car'))
            source_kind = str(value.get('source_kind') or '').strip()
            source_value = _source_value(source_kind, value.get('source_value'))
            recorded_at = str(value.get('recorded_at') or fallback_time).strip()
            waybill_id = _find_waybill_id(
                connection, sheet_name=sheet_name,
                waybill_no=waybill_no, plate_no=plate_no,
            )
            raw_json = canonical_json(value)
            connection.execute(
                '''
                INSERT INTO source_context_events(
                    event_hash, run_id, event_name, excel_path, sheet_name,
                    excel_row, waybill_no, plate_no, source_kind, source_value,
                    pdf_path, recorded_at, waybill_id, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_hash) DO UPDATE SET
                    waybill_id=COALESCE(excluded.waybill_id, source_context_events.waybill_id),
                    raw_json=excluded.raw_json
                ''',
                (
                    _event_hash(value), run_id, str(value.get('event') or '').strip(),
                    str(value.get('excel_path') or '').strip(), sheet_name,
                    str(value.get('excel_row') or '').strip(), waybill_no, plate_no,
                    source_kind, source_value, str(value.get('pdf_path') or '').strip(),
                    recorded_at, waybill_id, raw_json,
                ),
            )
            stats['events_seen'] += 1
            if waybill_id is None:
                stats['unlinked_events'] += 1
                continue
            connection.execute(
                '''
                INSERT INTO waybill_sources(
                    waybill_id, sheet_name, source_kind, source_value, plate_no,
                    first_seen_run_id, last_seen_run_id, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(waybill_id, sheet_name, source_kind, source_value, plate_no)
                DO UPDATE SET
                    last_seen_run_id=excluded.last_seen_run_id,
                    last_seen_at=MAX(waybill_sources.last_seen_at, excluded.last_seen_at)
                ''',
                (
                    waybill_id, sheet_name, source_kind, source_value, plate_no,
                    run_id, run_id, recorded_at, recorded_at,
                ),
            )
            stats['linked_events'] += 1
    connection.commit()
    stats['stored_events'] = connection.execute(
        'SELECT COUNT(*) FROM source_context_events'
    ).fetchone()[0]
    stats['waybill_sources'] = connection.execute(
        'SELECT COUNT(*) FROM waybill_sources'
    ).fetchone()[0]
    return dict(stats)


def _freight_target_key(value: dict[str, Any]) -> str:
    return '|'.join(
        (
            _source_scope_key(
                value.get('sheet'), value.get('source_kind'), value.get('source_value')
            ),
            normalize_waybill(value.get('waybill')),
            normalize_plate(value.get('car')),
        )
    )


def _freight_card_key(value: dict[str, Any]) -> str:
    stable = '|'.join(
        (
            _source_scope_key(
                value.get('sheet'), value.get('source_kind'), value.get('source_value')
            ),
            normalize_waybill(value.get('waybill')),
            str(value.get('outbound_datetime') or '').strip(),
            str(value.get('normalized_spec') or value.get('raw_spec') or '').strip().upper(),
        )
    )
    return hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]


def _optional_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def _freight_fact_key(value: dict[str, Any]) -> str:
    stable = '|'.join(
        (
            str(value.get('card_key') or _freight_card_key(value)),
            str(_optional_float(value.get('settlement_quantity'))),
            str(_optional_float(value.get('unit_price'))),
        )
    )
    return hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]


def _freight_record_key(value: dict[str, Any]) -> str:
    fact_key = _freight_fact_key(value)
    occurrence = _positive_int(value.get('record_occurrence'))
    if occurrence == 1:
        return fact_key
    return hashlib.sha256(f'{fact_key}|{occurrence}'.encode('utf-8')).hexdigest()[:24]


def _freight_failure_key(value: dict[str, Any]) -> str:
    card_key = str(value.get('card_key') or _freight_card_key(value)).strip()
    occurrence = _positive_int(value.get('card_occurrence'))
    if occurrence == 1:
        return card_key
    return hashlib.sha256(f'{card_key}|{occurrence}'.encode('utf-8')).hexdigest()[:24]


def _freight_common(
    connection: sqlite3.Connection,
    value: dict[str, Any],
    source_path: Path,
    fallback_recorded_at: str,
) -> dict[str, Any]:
    sheet_name = str(value.get('sheet') or '').strip()
    source_kind = str(value.get('source_kind') or '').strip()
    source_value = _source_value(source_kind, value.get('source_value'))
    waybill_no = normalize_waybill(value.get('waybill'))
    plate_no = normalize_plate(value.get('car'))
    target_key = str(value.get('target_key') or _freight_target_key(value)).strip()
    return {
        'run_id': str(value.get('run_id') or source_path.stem).strip(),
        'sheet_name': sheet_name,
        'source_kind': source_kind,
        'source_value': source_value,
        'scope_key': _source_scope_key(sheet_name, source_kind, source_value),
        'waybill_no': waybill_no,
        'plate_no': plate_no,
        'target_key': target_key,
        'waybill_id': _find_waybill_id(
            connection,
            sheet_name=sheet_name,
            waybill_no=waybill_no,
            plate_no=plate_no,
        ),
        'recorded_at': str(value.get('recorded_at') or fallback_recorded_at).strip(),
        'raw_json': canonical_json(value),
    }


def _import_freight_target(
    connection: sqlite3.Connection,
    common: dict[str, Any],
    value: dict[str, Any],
) -> None:
    connection.execute(
        '''
        INSERT INTO freight_targets(
            target_key, waybill_id, sheet_name, source_kind, source_value,
            waybill_no, plate_no, app_export_time, excel_row, status,
            first_seen_run_id, last_seen_run_id, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
        ON CONFLICT(target_key) DO UPDATE SET
            waybill_id = COALESCE(freight_targets.waybill_id, excluded.waybill_id),
            app_export_time = COALESCE(NULLIF(excluded.app_export_time, ''), freight_targets.app_export_time),
            excel_row = COALESCE(excluded.excel_row, freight_targets.excel_row),
            last_seen_run_id = excluded.last_seen_run_id,
            last_seen_at = MAX(freight_targets.last_seen_at, excluded.last_seen_at)
        ''',
        (
            common['target_key'], common['waybill_id'], common['sheet_name'],
            common['source_kind'], common['source_value'], common['waybill_no'],
            common['plate_no'], str(value.get('app_export_time') or '').strip(),
            str(value.get('excel_row') or '').strip(), common['run_id'], common['run_id'],
            common['recorded_at'], common['recorded_at'],
        ),
    )


def _import_freight_success(
    connection: sqlite3.Connection,
    common: dict[str, Any],
    value: dict[str, Any],
) -> bool:
    quantity = _optional_float(value.get('settlement_quantity'))
    unit_price = _optional_float(value.get('unit_price'))
    if quantity is None or unit_price is None:
        return False
    _import_freight_target(connection, common, value)
    card_key = str(value.get('card_key') or _freight_card_key(value)).strip()
    record_key = str(value.get('record_key') or _freight_record_key(value)).strip()
    failure_key = str(value.get('failure_key') or _freight_failure_key(value)).strip()
    card_occurrence = _positive_int(value.get('card_occurrence'))
    record_occurrence = _positive_int(value.get('record_occurrence'))
    connection.execute(
        '''
        INSERT INTO freight_records(
            record_key, card_key, target_key, run_id, sheet_name,
            source_kind, source_value, plate_no, waybill_no, outbound_datetime,
            raw_spec, normalized_spec, settlement_quantity, unit_price,
            card_occurrence, record_occurrence, recorded_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(record_key) DO UPDATE SET
            target_key = COALESCE(freight_records.target_key, excluded.target_key),
            run_id = excluded.run_id,
            recorded_at = MAX(freight_records.recorded_at, excluded.recorded_at),
            raw_json = excluded.raw_json
        ''',
        (
            record_key, card_key, common['target_key'], common['run_id'],
            common['sheet_name'], common['source_kind'],
            common['source_value'], common['plate_no'], common['waybill_no'],
            str(value.get('outbound_datetime') or '').strip(),
            str(value.get('raw_spec') or '').strip(),
            str(value.get('normalized_spec') or '').strip(), quantity, unit_price,
            card_occurrence, record_occurrence,
            common['recorded_at'], common['raw_json'],
        ),
    )
    connection.execute(
        'DELETE FROM freight_failures WHERE failure_key = ?', (failure_key,)
    )
    connection.execute(
        '''
        UPDATE freight_targets
        SET status = CASE WHEN status = 'COMPLETE' THEN status ELSE 'PARTIAL' END,
            waybill_id = COALESCE(waybill_id, ?),
            last_seen_run_id = ?,
            last_seen_at = MAX(last_seen_at, ?)
        WHERE target_key = ?
        ''',
        (
            common['waybill_id'], common['run_id'], common['recorded_at'],
            common['target_key'],
        ),
    )
    return True


def _import_freight_failure(
    connection: sqlite3.Connection,
    common: dict[str, Any],
    value: dict[str, Any],
) -> None:
    _import_freight_target(connection, common, value)
    card_key = str(value.get('card_key') or _freight_card_key(value)).strip()
    failure_key = str(value.get('failure_key') or _freight_failure_key(value)).strip()
    card_occurrence = _positive_int(value.get('card_occurrence'))
    error = str(value.get('error') or '').strip()
    try:
        attempt = max(1, int(value.get('attempt') or 1))
    except (TypeError, ValueError):
        attempt = 1
    connection.execute(
        '''
        INSERT INTO freight_failures(
            failure_key, card_key, target_key, run_id, sheet_name, source_kind,
            source_value, plate_no, waybill_no, outbound_datetime, raw_spec,
            error_message, attempt, card_occurrence, recorded_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(failure_key) DO UPDATE SET
            target_key = COALESCE(freight_failures.target_key, excluded.target_key),
            run_id = excluded.run_id,
            error_message = excluded.error_message,
            attempt = MAX(freight_failures.attempt, excluded.attempt),
            recorded_at = MAX(freight_failures.recorded_at, excluded.recorded_at),
            raw_json = excluded.raw_json
        ''',
        (
            failure_key, card_key, common['target_key'], common['run_id'],
            common['sheet_name'],
            common['source_kind'], common['source_value'],
            common['plate_no'], common['waybill_no'],
            str(value.get('outbound_datetime') or '').strip(),
            str(value.get('raw_spec') or '').strip(), error, attempt,
            card_occurrence, common['recorded_at'], common['raw_json'],
        ),
    )
    not_ready = any(
        marker in error
        for marker in ('未找到', '尚未', '暂无', '未生成', '没有匹配')
    )
    next_status = 'NOT_READY' if not_ready else 'ERROR'
    connection.execute(
        '''
        UPDATE freight_targets
        SET status = CASE
                WHEN status = 'PARTIAL' OR EXISTS(
                    SELECT 1 FROM freight_records r
                    WHERE r.target_key = freight_targets.target_key
                ) THEN 'PARTIAL'
                ELSE ?
            END,
            last_seen_run_id = ?,
            last_seen_at = MAX(last_seen_at, ?)
        WHERE target_key = ?
        ''',
        (next_status, common['run_id'], common['recorded_at'], common['target_key']),
    )


def _import_freight_scope_complete(
    connection: sqlite3.Connection,
    common: dict[str, Any],
    value: dict[str, Any],
) -> None:
    missing_value = value.get('missing_waybills')
    if not isinstance(missing_value, list):
        missing_value = []
    missing_waybills = sorted(
        {normalize_waybill(item) for item in missing_value if normalize_waybill(item)}
    )
    completed_value = value.get('completed_waybills')
    failed_value = value.get('failed_waybills')
    has_waybill_manifest = isinstance(completed_value, list) or isinstance(failed_value, list)
    completed_waybills = {
        normalize_waybill(item)
        for item in (completed_value if isinstance(completed_value, list) else [])
        if normalize_waybill(item)
    }
    failed_waybills = {
        normalize_waybill(item)
        for item in (failed_value if isinstance(failed_value, list) else [])
        if normalize_waybill(item)
    }
    existed = connection.execute(
        'SELECT 1 FROM freight_scope_runs WHERE run_id = ? AND scope_key = ?',
        (common['run_id'], common['scope_key']),
    ).fetchone()
    connection.execute(
        '''
        INSERT INTO freight_scope_runs(
            run_id, scope_key, sheet_name, source_kind, source_value,
            target_count, success_count, missing_waybills_json,
            failed_card_count, recorded_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, scope_key) DO UPDATE SET
            target_count = excluded.target_count,
            success_count = excluded.success_count,
            missing_waybills_json = excluded.missing_waybills_json,
            failed_card_count = excluded.failed_card_count,
            recorded_at = MAX(freight_scope_runs.recorded_at, excluded.recorded_at),
            raw_json = excluded.raw_json
        ''',
        (
            common['run_id'], common['scope_key'], common['sheet_name'],
            common['source_kind'], common['source_value'],
            int(value.get('target_count') or 0), int(value.get('success_count') or 0),
            canonical_json(missing_waybills), int(value.get('failed_card_count') or 0),
            common['recorded_at'], common['raw_json'],
        ),
    )
    if not existed:
        connection.execute(
            '''
            UPDATE freight_targets
            SET attempt_count = attempt_count + 1,
                last_attempt_at = ?,
                last_seen_run_id = ?
            WHERE sheet_name = ? AND source_kind = ? AND source_value = ?
                AND status != 'COMPLETE'
            ''',
            (
                common['recorded_at'], common['run_id'], common['sheet_name'],
                common['source_kind'], common['source_value'],
            ),
        )
    targets = connection.execute(
        '''
        SELECT target_key, waybill_no, status
        FROM freight_targets
        WHERE sheet_name = ? AND source_kind = ? AND source_value = ?
        ''',
        (common['sheet_name'], common['source_kind'], common['source_value']),
    ).fetchall()
    missing_set = set(missing_waybills)
    for target in targets:
        target_key = str(target['target_key'])
        target_waybill = normalize_waybill(target['waybill_no'])
        record_count = int(
            connection.execute(
                'SELECT COUNT(*) FROM freight_records WHERE target_key = ?',
                (target_key,),
            ).fetchone()[0]
        )
        if has_waybill_manifest and target_waybill in completed_waybills and record_count:
            connection.execute(
                'DELETE FROM freight_failures WHERE target_key = ?', (target_key,)
            )
        elif (
            not has_waybill_manifest
            and record_count
            and target_waybill not in missing_set
        ):
            connection.execute(
                '''
                DELETE FROM freight_failures
                WHERE target_key = ? AND TRIM(raw_spec) = ''
                    AND TRIM(outbound_datetime) = ''
                ''',
                (target_key,),
            )
        failure_count = int(
            connection.execute(
                'SELECT COUNT(*) FROM freight_failures WHERE target_key = ?',
                (target_key,),
            ).fetchone()[0]
        )
        if target_waybill in missing_set:
            next_status = 'PARTIAL' if record_count else 'NOT_READY'
        elif has_waybill_manifest and target_waybill in failed_waybills:
            next_status = 'PARTIAL' if record_count else 'ERROR'
        elif has_waybill_manifest and target_waybill in completed_waybills:
            next_status = 'COMPLETE' if record_count and not failure_count else 'PARTIAL'
        elif has_waybill_manifest:
            next_status = str(target['status'])
        elif record_count and not failure_count:
            next_status = 'COMPLETE'
        elif record_count:
            next_status = 'PARTIAL'
        else:
            next_status = str(target['status'])
        connection.execute(
            'UPDATE freight_targets SET status = ? WHERE target_key = ?',
            (next_status, target_key),
        )


def import_freight_history(
    connection: sqlite3.Connection, base_dir: Path
) -> dict[str, int]:
    '''Import all durable freight-confirmation events idempotently.'''
    stats = Counter()
    root = base_dir / 'freight_confirmation'
    if not root.exists():
        return dict(stats)
    paths = sorted(root.glob('*.jsonl'), key=lambda item: (item.stat().st_mtime_ns, item.name))
    for path in paths:
        stats['files_seen'] += 1
        fallback_time = dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec='seconds')
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except Exception:
            stats['files_failed'] += 1
            continue
        parsed_events: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError('JSONL event is not an object')
                parsed_events.append(value)
            except Exception:
                stats['invalid_lines'] += 1
                continue

        completed_scope_tokens = {
            (
                str(value.get('run_id') or path.stem).strip(),
                _source_scope_key(
                    value.get('sheet'),
                    value.get('source_kind'),
                    value.get('source_value'),
                ),
            )
            for value in parsed_events
            if str(value.get('event') or '').strip() == 'freight_scope_complete'
        }
        incomplete_scope_facts: set[tuple[str, str, str, str]] = set()
        for value in parsed_events:
            try:
                event_name = str(value.get('event') or '').strip()
                common = _freight_common(connection, value, path, fallback_time)
                if event_name == 'sales_target':
                    _import_freight_target(connection, common, value)
                    stats['targets_seen'] += 1
                elif event_name == 'freight_success':
                    scope_token = (common['run_id'], common['scope_key'])
                    if scope_token not in completed_scope_tokens:
                        fact_token = (
                            common['run_id'],
                            common['scope_key'],
                            common['target_key'],
                            _freight_fact_key(value),
                        )
                        if fact_token in incomplete_scope_facts:
                            stats['incomplete_duplicate_success_events_ignored'] += 1
                            continue
                        incomplete_scope_facts.add(fact_token)
                    if not _import_freight_success(connection, common, value):
                        raise ValueError('freight success is missing quantity or unit price')
                    stats['success_events_seen'] += 1
                elif event_name == 'freight_failure':
                    _import_freight_failure(connection, common, value)
                    stats['failure_events_seen'] += 1
                elif event_name == 'freight_scope_complete':
                    _import_freight_scope_complete(connection, common, value)
                    stats['scope_events_seen'] += 1
                else:
                    stats['ignored_events'] += 1
            except Exception:
                stats['invalid_lines'] += 1
                continue
    connection.commit()
    for name, table in (
        ('stored_targets', 'freight_targets'),
        ('stored_records', 'freight_records'),
        ('stored_failures', 'freight_failures'),
        ('stored_scope_runs', 'freight_scope_runs'),
    ):
        stats[name] = int(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
    return dict(stats)


def export_pending_freight_seed(
    connection: sqlite3.Connection,
    output_path: Path,
    run_id: str,
    anchor_date: Any = None,
    retry_days: int = FREIGHT_RETRY_DAYS,
) -> dict[str, int]:
    '''Seed unresolved targets that remain inside the automatic retry window.'''
    all_targets = connection.execute(
        '''
        SELECT * FROM freight_targets
        WHERE status != 'COMPLETE'
          AND source_kind IN ('login_account', 'selected_car')
          AND TRIM(source_value) != ''
          AND TRIM(sheet_name) != ''
          AND TRIM(waybill_no) != ''
        ORDER BY sheet_name, source_kind, source_value, app_export_time, target_key
        ''',
    ).fetchall()
    effective_anchor = anchor_date or dt.date.today()
    targets = [
        row for row in all_targets
        if freight_target_within_retry_window(
            row['waybill_no'],
            row['app_export_time'],
            effective_anchor,
            retry_days,
        )
    ]
    target_keys = {str(row['target_key']) for row in targets}
    recorded_at = now_text()
    events: list[dict[str, Any]] = []
    for row in targets:
        events.append(
            {
                'version': 1,
                'event': 'sales_target',
                'run_id': run_id,
                'sheet': row['sheet_name'],
                'source_kind': row['source_kind'],
                'source_value': row['source_value'],
                'waybill': row['waybill_no'],
                'car': row['plate_no'],
                'app_export_time': row['app_export_time'],
                'excel_row': row['excel_row'],
                'target_key': row['target_key'],
                'recorded_at': recorded_at,
                'seeded_from_database': True,
            }
        )
    records = connection.execute(
        '''
        SELECT r.*
        FROM freight_records r
        JOIN freight_targets t ON t.target_key = r.target_key
        WHERE t.status != 'COMPLETE'
          AND t.source_kind IN ('login_account', 'selected_car')
          AND TRIM(t.source_value) != ''
        ORDER BY r.sheet_name, r.source_kind, r.source_value,
                 r.outbound_datetime, r.record_key
        ''',
    ).fetchall()
    records_seeded = 0
    for row in records:
        if str(row['target_key'] or '') not in target_keys:
            continue
        events.append(
            {
                'version': 1,
                'event': 'freight_success',
                'run_id': run_id,
                'sheet': row['sheet_name'],
                'source_kind': row['source_kind'],
                'source_value': row['source_value'],
                'car': row['plate_no'],
                'waybill': row['waybill_no'],
                'outbound_datetime': row['outbound_datetime'],
                'raw_spec': row['raw_spec'],
                'normalized_spec': row['normalized_spec'],
                'settlement_quantity': row['settlement_quantity'],
                'unit_price': row['unit_price'],
                'card_key': row['card_key'],
                'record_key': row['record_key'],
                'card_occurrence': row['card_occurrence'],
                'record_occurrence': row['record_occurrence'],
                'recorded_at': recorded_at,
                'seeded_from_database': True,
            }
        )
        records_seeded += 1
    payload = ''.join(canonical_json(event) + '\n' for event in events).encode('utf-8')
    atomic_write_bytes(output_path, payload)
    scopes = {
        _source_scope_key(row['sheet_name'], row['source_kind'], row['source_value'])
        for row in targets
    }
    return {
        'targets_seeded': len(targets),
        'targets_skipped_by_age': len(all_targets) - len(targets),
        'records_seeded': records_seeded,
        'scopes_seeded': len(scopes),
    }


def store_parsed_document(connection: sqlite3.Connection, parsed: ParsedTicket) -> None:
    current_time = now_text()
    connection.execute(
        """
        INSERT INTO documents(
            sha256, size_bytes, page_count, parser_version, parse_status,
            pdf_business_code, pdf_plate_no, pdf_goods_name,
            gross_weight_kg, tare_weight_kg, net_weight_kg,
            gross_time, tare_time, net_time, document_time, parse_error,
            parser_source, text_parse_error, vlm_model, vlm_prompt_version,
            vlm_confidence, vlm_warnings_json, vlm_raw_json, vlm_from_cache,
            created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(sha256) DO UPDATE SET
            size_bytes=excluded.size_bytes,
            page_count=excluded.page_count,
            parser_version=excluded.parser_version,
            parse_status=excluded.parse_status,
            pdf_business_code=excluded.pdf_business_code,
            pdf_plate_no=excluded.pdf_plate_no,
            pdf_goods_name=excluded.pdf_goods_name,
            gross_weight_kg=excluded.gross_weight_kg,
            tare_weight_kg=excluded.tare_weight_kg,
            net_weight_kg=excluded.net_weight_kg,
            gross_time=excluded.gross_time,
            tare_time=excluded.tare_time,
            net_time=excluded.net_time,
            document_time=excluded.document_time,
            parse_error=excluded.parse_error,
            parser_source=excluded.parser_source,
            text_parse_error=excluded.text_parse_error,
            vlm_model=excluded.vlm_model,
            vlm_prompt_version=excluded.vlm_prompt_version,
            vlm_confidence=excluded.vlm_confidence,
            vlm_warnings_json=excluded.vlm_warnings_json,
            vlm_raw_json=excluded.vlm_raw_json,
            vlm_from_cache=excluded.vlm_from_cache,
            updated_at=excluded.updated_at
        """,
        (
            parsed.sha256,
            parsed.size_bytes,
            parsed.page_count,
            PARSER_VERSION,
            parsed.status,
            parsed.business_code,
            parsed.plate_no,
            parsed.goods_name,
            parsed.gross_weight_kg,
            parsed.tare_weight_kg,
            parsed.net_weight_kg,
            parsed.gross_time or None,
            parsed.tare_time or None,
            parsed.net_time or None,
            parsed.document_time or None,
            parsed.error,
            parsed.parser_source,
            parsed.text_parse_error,
            parsed.vlm_model,
            parsed.vlm_prompt_version,
            parsed.vlm_confidence,
            parsed.vlm_warnings_json,
            parsed.vlm_raw_json,
            int(parsed.vlm_from_cache),
            current_time,
            current_time,
        ),
    )


def import_pdf_history(connection: sqlite3.Connection, base_dir: Path) -> dict[str, int]:
    stats = Counter()
    records: list[tuple[str, str, Path, str, str, str, str, int, int]] = []
    parse_jobs: dict[str, Path] = {}
    for direction, owner, folder_name in PDF_ROOTS:
        root = base_dir / folder_name
        if not root.exists():
            continue
        for path in root.rglob("*.pdf"):
            match = PDF_FILENAME_RE.match(path.stem)
            if not match:
                stats["invalid_pdf_filenames"] += 1
                continue
            waybill = normalize_waybill(match.group("waybill"))
            plate = normalize_plate(match.group("car"))
            digest = file_sha256(path)
            existing = connection.execute(
                "SELECT parser_version, parse_status FROM documents WHERE sha256=?",
                (digest,),
            ).fetchone()
            if (
                existing is None
                or existing["parser_version"] != PARSER_VERSION
                or existing["parse_status"] != "VALID"
            ):
                parse_jobs.setdefault(digest, path)
            else:
                stats["cached_pdf_parses"] += 1
            records.append(
                (
                    direction,
                    owner,
                    path,
                    waybill,
                    plate,
                    digest,
                    parse_compact_time(match.group("day") + match.group("clock")),
                    int(match.group("index") or 1),
                    path.stat().st_mtime_ns,
                )
            )
            stats["pdf_paths_seen"] += 1

    if parse_jobs:
        try:
            configured_workers = int(os.getenv("AUDIT_MAX_WORKERS", "8"))
        except ValueError:
            configured_workers = 8
        worker_count = max(1, min(32, configured_workers, len(parse_jobs)))
        cache_dir = base_dir / "platform_sync" / "vlm_ticket_cache"
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_count
        ) as executor:
            futures = {
                executor.submit(
                    parse_pdf_ticket_with_fallback,
                    path,
                    digest,
                    cache_dir,
                ): digest
                for digest, path in parse_jobs.items()
            }
            for future in concurrent.futures.as_completed(futures):
                parsed = future.result()
                store_parsed_document(connection, parsed)
                stats["unique_pdfs_parsed"] += 1
                if parsed.parser_source == "TEXT":
                    stats["text_parsed_documents"] += 1
                elif parsed.parser_source == "VLM_FALLBACK":
                    stats["vlm_fallback_documents"] += 1
                    if parsed.vlm_from_cache:
                        stats["vlm_cache_hits"] += 1
                else:
                    stats["invalid_documents"] += 1

    for (
        direction,
        owner,
        path,
        waybill,
        plate,
        digest,
        download_time,
        attachment_index,
        mtime_ns,
    ) in records:
            connection.execute(
                """
                INSERT OR IGNORE INTO document_locations(
                    sha256, path, direction, source_owner, mtime_ns
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (digest, str(path.resolve()), direction, owner, mtime_ns),
            )
            waybill_id = upsert_waybill(connection, direction, waybill, plate)
            connection.execute(
                """
                INSERT OR IGNORE INTO waybill_documents(
                    waybill_id, sha256, attachment_index, download_time, first_linked_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    waybill_id,
                    digest,
                    attachment_index,
                    download_time,
                    now_text(),
                ),
            )
    connection.commit()
    stats["unique_documents"] = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    stats["document_locations"] = connection.execute("SELECT COUNT(*) FROM document_locations").fetchone()[0]
    return dict(stats)


def compatible_text(values: Iterable[str]) -> bool:
    normalized = [normalize_compare(value) for value in values if normalize_compare(value)]
    if len(normalized) <= 1:
        return True
    first = normalized[0]
    return all(first in value or value in first for value in normalized[1:])


def rebuild_logical_tickets(connection: sqlite3.Connection) -> dict[str, int]:
    stats = Counter()
    waybills = connection.execute("SELECT * FROM waybills ORDER BY id").fetchall()
    for waybill in waybills:
        documents = connection.execute(
            """
            SELECT d.* FROM waybill_documents wd
            JOIN documents d ON d.sha256=wd.sha256
            WHERE wd.waybill_id=?
            ORDER BY COALESCE(d.net_time, wd.download_time), wd.attachment_index, d.sha256
            """,
            (waybill["id"],),
        ).fetchall()
        set_hash = hashlib.sha256(
            "|".join(sorted(row["sha256"] for row in documents)).encode("utf-8")
        ).hexdigest()
        side = "LOADING" if waybill["direction"] == "SALES" else "UNLOADING"
        suffix = "L" if side == "LOADING" else "U"
        method = "SINGLE"
        status = "VALID"
        message = ""
        gross = tare = net = None
        entry_time = exit_time = weigh_time = None

        if not documents:
            status, message = "REVIEW_REQUIRED", "未找到PDF"
        elif any(row["parse_status"] != "VALID" for row in documents):
            status, message = "REVIEW_REQUIRED", "存在无法解析或校验失败的PDF"
        elif any(
            row["pdf_plate_no"] and normalize_plate(row["pdf_plate_no"]) != waybill["plate_no"]
            for row in documents
        ):
            status, message = "REVIEW_REQUIRED", "PDF车牌与运单车牌不一致"
        elif len(documents) == 1:
            row = documents[0]
            gross, tare, net, weigh_time = (
                row["gross_weight_kg"],
                row["tare_weight_kg"],
                row["net_weight_kg"],
                row["net_time"],
            )
            if waybill["direction"] == "SALES":
                entry_time, exit_time = row["tare_time"], row["gross_time"]
            else:
                entry_time, exit_time = row["gross_time"], row["tare_time"]
        elif waybill["direction"] != "SALES":
            status, message = "REVIEW_REQUIRED", "采购运单存在多份不同内容PDF，禁止自动选择"
        else:
            # 销售货物名称以 Excel 运单为准。PDF 各阶段货名仍保留在
            # documents 中供审计，但不同货名不阻断连续称重链聚合。
            if not compatible_text(row["pdf_goods_name"] for row in documents):
                message = "多份PDF货物名称不一致，平台货物名称以Excel为准"
            method = "CHAIN_AGGREGATE"
            suffix = "L-AGG"
            for index in range(1, len(documents)):
                if abs(documents[index]["tare_weight_kg"] - documents[index - 1]["gross_weight_kg"]) > WEIGHT_TOLERANCE_KG:
                    status, message = "REVIEW_REQUIRED", "多阶段链不连续：后一阶段皮重不等于前一阶段毛重"
                    break
            if status == "VALID":
                gross = documents[-1]["gross_weight_kg"]
                tare = documents[0]["tare_weight_kg"]
                net = sum(row["net_weight_kg"] for row in documents)
                entry_time = documents[0]["tare_time"]
                exit_time = documents[-1]["gross_time"]
                weigh_time = documents[-1]["net_time"]
                load_weight = waybill["load_weight_kg"]
                if load_weight is None:
                    status, message = "REVIEW_REQUIRED", "多阶段聚合缺少运单装货重量"
                elif abs(net - load_weight) > WEIGHT_TOLERANCE_KG:
                    status, message = "REVIEW_REQUIRED", "阶段净重合计与运单装货重量不一致"
                elif not valid_weight_triplet(gross, tare, net):
                    status, message = "REVIEW_REQUIRED", "聚合后的毛皮净重关系无效"

        if status == "VALID":
            if not entry_time or not exit_time:
                status, message = "REVIEW_REQUIRED", "磅单缺少进厂时间或出厂时间"
            elif not ISO_LOCAL_RE.match(entry_time) or not ISO_LOCAL_RE.match(exit_time):
                status, message = "REVIEW_REQUIRED", "磅单进出厂时间格式无效"
            elif exit_time < entry_time:
                status, message = "REVIEW_REQUIRED", "磅单出厂时间早于进厂时间"

        external_ticket_no = f"AGENT-JY-{waybill['waybill_no']}-{suffix}"
        existing = connection.execute(
            "SELECT * FROM logical_tickets WHERE waybill_id=?", (waybill["id"],)
        ).fetchone()
        preserve_artifact = bool(existing and existing["document_set_hash"] == set_hash)
        connection.execute(
            """
            INSERT INTO logical_tickets(
                waybill_id, side, method, external_ticket_no,
                gross_weight_kg, tare_weight_kg, net_weight_kg,
                entry_time, exit_time, weigh_time,
                document_set_hash, validation_status, validation_message,
                artifact_type, artifact_path, artifact_sha256,
                artifact_size_bytes, artifact_page_count, oss_url, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(waybill_id) DO UPDATE SET
                side=excluded.side,
                method=excluded.method,
                external_ticket_no=excluded.external_ticket_no,
                gross_weight_kg=excluded.gross_weight_kg,
                tare_weight_kg=excluded.tare_weight_kg,
                net_weight_kg=excluded.net_weight_kg,
                entry_time=excluded.entry_time,
                exit_time=excluded.exit_time,
                weigh_time=excluded.weigh_time,
                document_set_hash=excluded.document_set_hash,
                validation_status=excluded.validation_status,
                validation_message=excluded.validation_message,
                artifact_type=excluded.artifact_type,
                artifact_path=excluded.artifact_path,
                artifact_sha256=excluded.artifact_sha256,
                artifact_size_bytes=excluded.artifact_size_bytes,
                artifact_page_count=excluded.artifact_page_count,
                oss_url=excluded.oss_url,
                updated_at=excluded.updated_at
            """,
            (
                waybill["id"],
                side,
                method,
                external_ticket_no,
                gross,
                tare,
                net,
                entry_time,
                exit_time,
                weigh_time,
                set_hash,
                status,
                message,
                existing["artifact_type"] if preserve_artifact else None,
                existing["artifact_path"] if preserve_artifact else None,
                existing["artifact_sha256"] if preserve_artifact else None,
                existing["artifact_size_bytes"] if preserve_artifact else None,
                existing["artifact_page_count"] if preserve_artifact else None,
                existing["oss_url"] if preserve_artifact else None,
                now_text(),
            ),
        )
        stats[status] += 1
        stats[method] += 1
    connection.commit()
    return dict(stats)


def refresh_facts(
    connection: sqlite3.Connection,
    base_dir: Path,
    *,
    project_name: str = "",
    sales_loading_point_name: str = DEFAULT_SALES_LOADING_POINT_NAME,
) -> dict[str, Any]:
    started = now_text()
    cursor = connection.execute(
        "INSERT INTO import_runs(started_at, status) VALUES (?, 'RUNNING')", (started,)
    )
    run_id = int(cursor.lastrowid)
    connection.commit()
    try:
        update_business_settings(
            connection,
            project_name=project_name,
            sales_loading_point_name=sales_loading_point_name,
        )
        stats = {
            "driver_roster": import_driver_roster(
                connection,
                base_dir / "platform_sync" / DEFAULT_DRIVER_ROSTER_FILENAME,
            ),
            "excel": import_excel_history(connection, base_dir),
            "pdf": import_pdf_history(connection, base_dir),
            "logical_tickets": rebuild_logical_tickets(connection),
        }
        stats['source_context'] = import_source_context_history(connection, base_dir)
        stats['freight'] = import_freight_history(connection, base_dir)
        connection.execute(
            """
            UPDATE import_runs SET finished_at=?, status='SUCCESS', stats_json=? WHERE id=?
            """,
            (now_text(), canonical_json(stats), run_id),
        )
        connection.commit()
        return stats
    except Exception as exc:
        connection.execute(
            """
            UPDATE import_runs SET finished_at=?, status='FAILED', error_message=? WHERE id=?
            """,
            (now_text(), f"{type(exc).__name__}: {exc}", run_id),
        )
        connection.commit()
        raise


def payload_ticket(logical: sqlite3.Row) -> dict[str, Any]:
    ticket = {
        "externalTicketNo": logical["external_ticket_no"],
        "grossWeight": kg_to_json_number(logical["gross_weight_kg"]),
        "tareWeight": kg_to_json_number(logical["tare_weight_kg"]),
        "netWeight": kg_to_json_number(logical["net_weight_kg"]),
        "weighTime": logical["weigh_time"],
    }
    if logical["entry_time"]:
        ticket["entryTime"] = logical["entry_time"]
    if logical["exit_time"]:
        ticket["exitTime"] = logical["exit_time"]
    return ticket


def platform_unloading_point_name(direction: str, destination: Any) -> str:
    # Keep the Excel destination unchanged; derive only the platform display label.
    point_name = str(destination or '').strip()
    if direction != 'PURCHASE' or not point_name:
        return point_name
    if point_name.startswith(PURCHASE_UNLOADING_POINT_PREFIX):
        return point_name
    return f'{PURCHASE_UNLOADING_POINT_PREFIX}{point_name}'


def validate_payload(payload: dict[str, Any]) -> str:
    lengths = {
        "requestId": 128,
        "sourceSystem": 64,
        "externalWaybillNo": 128,
        "plateNo": 32,
        "projectName": 128,
        "loadingPointName": 128,
        "unloadingPointName": 128,
        "cargoName": 128,
        "driverName": 64,
        "driverPhone": 32,
    }
    for field, maximum in lengths.items():
        if field in payload and len(str(payload[field])) > maximum:
            return f"{field}超过{maximum}字符"
    for required in ("requestId", "sourceSystem", "externalWaybillNo", "plateNo", "projectName"):
        if not str(payload.get(required) or "").strip():
            return f"缺少{required}"
    ticket_objects = [payload.get("loadingTicket"), payload.get("unloadingTicket")]
    ticket_objects = [value for value in ticket_objects if value]
    if len(ticket_objects) != 1:
        return "敬业单侧磅单请求必须且只能提供一张逻辑磅单"
    ticket = ticket_objects[0]
    if not str(ticket.get("externalTicketNo") or "").strip():
        return "缺少externalTicketNo"
    if len(str(ticket["externalTicketNo"])) > 128:
        return "externalTicketNo超过128字符"
    gross = decimal_to_kg(ticket.get("grossWeight"))
    tare = decimal_to_kg(ticket.get("tareWeight"))
    net = decimal_to_kg(ticket.get("netWeight"))
    if not valid_weight_triplet(gross, tare, net):
        return "磅单毛皮净重不满足接口校验"
    if not ISO_LOCAL_RE.match(str(ticket.get("weighTime") or "")):
        return "weighTime不是yyyy-MM-ddTHH:mm:ss"
    entry_time = str(ticket.get("entryTime") or "")
    exit_time = str(ticket.get("exitTime") or "")
    if entry_time and not ISO_LOCAL_RE.match(entry_time):
        return "entryTime不是yyyy-MM-ddTHH:mm:ss"
    if exit_time and not ISO_LOCAL_RE.match(exit_time):
        return "exitTime不是yyyy-MM-ddTHH:mm:ss"
    if entry_time and exit_time and exit_time < entry_time:
        return "exitTime早于entryTime"
    image_url = str(ticket.get("imageUrl") or "")
    if image_url and (not image_url.startswith(("http://", "https://")) or len(image_url) > 500):
        return "imageUrl不是有效HTTP(S)地址或超过500字符"
    return ""


def iter_candidates(
    connection: sqlite3.Connection,
    source_system: str,
    project_name: str,
    *,
    sales_loading_point_name: str = DEFAULT_SALES_LOADING_POINT_NAME,
    require_attachment: bool = False,
    direction: str = "",
    waybill_numbers: Iterable[str] = (),
) -> Iterator[Candidate]:
    wanted = {normalize_waybill(value) for value in waybill_numbers if normalize_waybill(value)}
    query = """
        SELECT *
        FROM v_waybill_business_facts
        ORDER BY direction, waybill_no, plate_no
    """
    seen_external_waybills: set[str] = set()
    seen_ticket_numbers: set[str] = set()
    seen_request_ids: set[str] = set()
    submissions_by_waybill = {
        int(item['waybill_id']): item
        for item in connection.execute('SELECT * FROM submissions')
    }
    for row in connection.execute(query):
        if direction and row["direction"] != direction:
            continue
        if wanted and row["waybill_no"] not in wanted:
            continue
        submission = submissions_by_waybill.get(int(row['waybill_id']))
        if row['direction'] == 'PURCHASE' and submission is not None and submission['status'] == 'SUCCESS':
            try:
                frozen_payload = json.loads(submission['payload_json'])
                frozen_ticket = frozen_payload.get('unloadingTicket') or frozen_payload.get('loadingTicket')
                frozen_request_id = str(frozen_payload['requestId'])
                frozen_waybill_no = str(frozen_payload['externalWaybillNo'])
                frozen_ticket_no = str(frozen_ticket['externalTicketNo'])
            except (AttributeError, KeyError, TypeError, ValueError):
                yield Candidate(row['waybill_id'], row['direction'], row['waybill_no'], None, '已成功请求冻结内容损坏，需核实，未重复上传')
                continue
            seen_request_ids.add(frozen_request_id)
            seen_external_waybills.add(frozen_waybill_no)
            seen_ticket_numbers.add(frozen_ticket_no)
            yield Candidate(row['waybill_id'], row['direction'], row['waybill_no'], frozen_payload)
            continue
        if row["validation_status"] != "VALID":
            yield Candidate(row["waybill_id"], row["direction"], row["waybill_no"], None, row["validation_message"] or "逻辑磅单未通过校验")
            continue
        if not str(row["platform_unloading_point_name"] or "").strip():
            yield Candidate(row["waybill_id"], row["direction"], row["waybill_no"], None, "缺少卸货点名称")
            continue
        effective_sales_loading_point = str(
            row["source_loading_point_name"]
            or sales_loading_point_name
            or ""
        ).strip()
        if row["direction"] == "SALES" and not effective_sales_loading_point:
            yield Candidate(row["waybill_id"], row["direction"], row["waybill_no"], None, "销售运单缺少装货点名称")
            continue
        purchase_loading_point = str(row['platform_loading_point_name'] or '').strip()
        if row['direction'] == 'PURCHASE' and not purchase_loading_point:
            yield Candidate(row['waybill_id'], row['direction'], row['waybill_no'], None, '采购运单缺少承运商（平台装货点）')
            continue
        if require_attachment and not row["oss_url"]:
            yield Candidate(row["waybill_id"], row["direction"], row["waybill_no"], None, "缺少OSS附件URL")
            continue

        request_id = f"AGENT-JY-{row['direction']}-{row['waybill_no']}"
        ticket = payload_ticket(row)
        if row["oss_url"]:
            ticket["imageUrl"] = row["oss_url"]
        payload: dict[str, Any] = {
            "requestId": request_id,
            "sourceSystem": source_system,
            "externalWaybillNo": row["waybill_no"],
            "plateNo": row["plate_no"],
            "projectName": project_name,
            "loadingTicket" if row["ticket_side"] == "LOADING" else "unloadingTicket": ticket,
            "unloadingPointName": str(
                row["platform_unloading_point_name"]
            ).strip(),
        }
        if row["direction"] == "SALES":
            payload["loadingPointName"] = effective_sales_loading_point
        else:
            payload['loadingPointName'] = purchase_loading_point
        if row["cargo_name"]:
            payload["cargoName"] = row["cargo_name"][:128]
        driver = driver_for_plate(connection, row["plate_no"])
        if driver is not None:
            payload["driverName"] = driver["driver_name"]
            payload["driverPhone"] = driver["driver_phone"]

        reason = validate_payload(payload)
        if (
            not reason and row['direction'] == 'PURCHASE'
            and submission is not None
        ):
            frozen_payload = json.loads(submission['payload_json'])
            if frozen_payload.get('loadingPointName') != payload.get('loadingPointName'):
                reason = '采购承运商装货点与已冻结请求不一致，需核实平台状态，未自动修改或重传'
        if request_id in seen_request_ids:
            reason = reason or "requestId本地冲突"
        if row["waybill_no"] in seen_external_waybills:
            reason = reason or "externalWaybillNo本地冲突"
        if row["external_ticket_no"] in seen_ticket_numbers:
            reason = reason or "externalTicketNo本地冲突"
        seen_request_ids.add(request_id)
        seen_external_waybills.add(row["waybill_no"])
        seen_ticket_numbers.add(row["external_ticket_no"])
        yield Candidate(
            row["waybill_id"],
            row["direction"],
            row["waybill_no"],
            None if reason else payload,
            reason,
        )


def document_paths_for_waybill(connection: sqlite3.Connection, waybill_id: int) -> list[Path]:
    rows = connection.execute(
        """
        SELECT d.sha256, d.net_time,
               (SELECT dl.path FROM document_locations dl
                WHERE dl.sha256=d.sha256 ORDER BY dl.mtime_ns DESC, dl.path LIMIT 1) AS path
        FROM waybill_documents wd
        JOIN documents d ON d.sha256=wd.sha256
        WHERE wd.waybill_id=?
        ORDER BY d.net_time, wd.attachment_index, d.sha256
        """,
        (waybill_id,),
    ).fetchall()
    return [Path(row["path"]) for row in rows if row["path"]]


def safe_artifact_stem(direction: str, waybill_no: str, set_hash: str) -> str:
    safe_waybill = re.sub(r"[^0-9A-Za-z_.-]+", "_", waybill_no).strip("._") or "waybill"
    return f"{direction}_{safe_waybill}_{set_hash[:12]}"


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def merge_pdfs(paths: list[Path]) -> tuple[bytes, int]:
    output = fitz.open()
    try:
        for path in paths:
            source = fitz.open(path)
            try:
                output.insert_pdf(source)
            finally:
                source.close()
        return output.tobytes(garbage=4, deflate=True), output.page_count
    finally:
        output.close()


def pdfs_to_long_jpeg(
    paths: list[Path], *, dpi: int = 144, max_width: int = 1600, quality: int = 88
) -> tuple[bytes, int]:
    images: list[Image.Image] = []
    page_count = 0
    scale = dpi / 72.0
    for path in paths:
        document = fitz.open(path)
        try:
            for page in document:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
                if image.width > max_width:
                    ratio = max_width / image.width
                    image = image.resize((max_width, max(1, int(image.height * ratio))), Image.Resampling.LANCZOS)
                images.append(image)
                page_count += 1
        finally:
            document.close()
    if not images:
        raise ValueError("没有可渲染的PDF页面")
    gap = 18
    width = max(image.width for image in images)
    total_height = sum(image.height for image in images) + gap * (len(images) - 1)
    if total_height > 60000:
        ratio = 60000 / total_height
        resized: list[Image.Image] = []
        for image in images:
            resized.append(
                image.resize(
                    (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                    Image.Resampling.LANCZOS,
                )
            )
        images = resized
        width = max(image.width for image in images)
        gap = max(2, int(gap * ratio))
        total_height = sum(image.height for image in images) + gap * (len(images) - 1)
    canvas = Image.new("RGB", (width, total_height), "white")
    top = 0
    for image in images:
        left = (width - image.width) // 2
        canvas.paste(image, (left, top))
        top += image.height + gap
        image.close()
    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
    canvas.close()
    return output.getvalue(), page_count


def build_attachment_artifact(
    connection: sqlite3.Connection,
    waybill_id: int,
    output_dir: Path,
    artifact_format: str = "jpg",
    *,
    force: bool = False,
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT w.direction, w.waybill_no, lt.*
        FROM logical_tickets lt JOIN waybills w ON w.id=lt.waybill_id
        WHERE lt.waybill_id=?
        """,
        (waybill_id,),
    ).fetchone()
    if row is None or row["validation_status"] != "VALID":
        raise ValueError("逻辑磅单不存在或尚未通过校验")
    artifact_format = artifact_format.lower()
    if artifact_format not in {"jpg", "pdf"}:
        raise ValueError("附件格式只能是jpg或pdf")
    if (
        not force
        and row["artifact_type"] == artifact_format
        and row["artifact_path"]
        and Path(row["artifact_path"]).is_file()
    ):
        return {
            "path": row["artifact_path"],
            "sha256": row["artifact_sha256"],
            "pages": row["artifact_page_count"],
            "bytes": row["artifact_size_bytes"],
            "cached": True,
        }
    source_paths = document_paths_for_waybill(connection, waybill_id)
    if artifact_format == "pdf":
        content, pages = merge_pdfs(source_paths)
    else:
        content, pages = pdfs_to_long_jpeg(source_paths)
    extension = "jpg" if artifact_format == "jpg" else "pdf"
    output_path = output_dir / f"{safe_artifact_stem(row['direction'], row['waybill_no'], row['document_set_hash'])}.{extension}"
    atomic_write_bytes(output_path, content)
    digest = hashlib.sha256(content).hexdigest()
    connection.execute(
        """
        UPDATE logical_tickets SET
            artifact_type=?, artifact_path=?, artifact_sha256=?,
            artifact_size_bytes=?, artifact_page_count=?, oss_url=NULL, updated_at=?
        WHERE waybill_id=?
        """,
        (
            artifact_format,
            str(output_path.resolve()),
            digest,
            len(content),
            pages,
            now_text(),
            waybill_id,
        ),
    )
    connection.commit()
    return {
        "path": str(output_path.resolve()),
        "sha256": digest,
        "pages": pages,
        "bytes": len(content),
        "cached": False,
    }


def status_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    def scalar(sql: str, parameters: tuple[Any, ...] = ()) -> int:
        return int(connection.execute(sql, parameters).fetchone()[0])

    return {
        'source_context_events': scalar('SELECT COUNT(*) FROM source_context_events'),
        'waybill_sources': scalar('SELECT COUNT(*) FROM waybill_sources'),
        'freight_targets': scalar('SELECT COUNT(*) FROM freight_targets'),
        'freight_pending': scalar(
            'SELECT COUNT(*) FROM freight_targets WHERE status != \'COMPLETE\''
        ),
        'freight_complete': scalar(
            'SELECT COUNT(*) FROM freight_targets WHERE status = \'COMPLETE\''
        ),
        'freight_records': scalar('SELECT COUNT(*) FROM freight_records'),
        'freight_failures': scalar('SELECT COUNT(*) FROM freight_failures'),
        "waybills": scalar("SELECT COUNT(*) FROM waybills"),
        "purchase_waybills": scalar("SELECT COUNT(*) FROM waybills WHERE direction='PURCHASE'"),
        "sales_waybills": scalar("SELECT COUNT(*) FROM waybills WHERE direction='SALES'"),
        "observations": scalar("SELECT COUNT(*) FROM waybill_observations"),
        "waybill_attributes": scalar("SELECT COUNT(*) FROM waybill_attributes"),
        "driver_roster": scalar("SELECT COUNT(*) FROM driver_roster"),
        "active_drivers": scalar("SELECT COUNT(*) FROM driver_roster WHERE active=1"),
        "waybills_with_driver": scalar(
            """
            SELECT COUNT(*) FROM waybills w
            WHERE EXISTS (
                SELECT 1 FROM driver_roster d
                WHERE d.active=1 AND (
                    d.plate_no=UPPER(REPLACE(w.plate_no, ' ', ''))
                    OR (
                        UPPER(REPLACE(w.plate_no, ' ', '')) NOT LIKE '%D'
                        AND d.plate_no=UPPER(REPLACE(w.plate_no, ' ', '')) || 'D'
                    )
                )
            )
            """
        ),
        "attribute_keys": scalar(
            "SELECT COUNT(DISTINCT field_key) FROM waybill_attributes"
        ),
        "source_loading_dates": scalar(
            "SELECT COUNT(*) FROM waybills WHERE TRIM(source_loading_date)!=''"
        ),
        "source_unloading_dates": scalar(
            "SELECT COUNT(*) FROM waybills WHERE TRIM(source_unloading_date)!=''"
        ),
        "source_loading_points": scalar(
            "SELECT COUNT(*) FROM waybills WHERE TRIM(source_loading_point_name)!=''"
        ),
        "source_unloading_points": scalar(
            "SELECT COUNT(*) FROM waybills WHERE TRIM(source_unloading_point_name)!=''"
        ),
        "unique_documents": scalar("SELECT COUNT(*) FROM documents"),
        "document_locations": scalar("SELECT COUNT(*) FROM document_locations"),
        "valid_documents": scalar("SELECT COUNT(*) FROM documents WHERE parse_status='VALID'"),
        "text_parsed_documents": scalar(
            "SELECT COUNT(*) FROM documents WHERE parse_status='VALID' AND parser_source='TEXT'"
        ),
        "vlm_fallback_documents": scalar(
            "SELECT COUNT(*) FROM documents WHERE parse_status='VALID' AND parser_source='VLM_FALLBACK'"
        ),
        "vlm_cached_documents": scalar(
            "SELECT COUNT(*) FROM documents WHERE vlm_from_cache=1"
        ),
        "invalid_documents": scalar(
            "SELECT COUNT(*) FROM documents WHERE parse_status!='VALID'"
        ),
        "valid_logical_tickets": scalar("SELECT COUNT(*) FROM logical_tickets WHERE validation_status='VALID'"),
        "review_required": scalar("SELECT COUNT(*) FROM logical_tickets WHERE validation_status!='VALID'"),
        "waybills_with_unloading_point": scalar(
            "SELECT COUNT(*) FROM v_waybill_business_facts "
            "WHERE TRIM(unloading_point_name)!=''"
        ),
        "sales_with_loading_point": scalar(
            "SELECT COUNT(*) FROM v_waybill_business_facts "
            "WHERE direction='SALES' AND TRIM(loading_point_name)!=''"
        ),
        "sales_business_facts_ready": scalar(
            """
            SELECT COUNT(*) FROM v_waybill_business_facts
            WHERE direction='SALES'
              AND validation_status='VALID'
              AND TRIM(COALESCE(loading_point_name, ''))!=''
              AND TRIM(COALESCE(unloading_point_name, ''))!=''
              AND loading_time IS NOT NULL
              AND loading_weight_kg IS NOT NULL
              AND local_pdf_count>0
            """
        ),
        "purchase_business_facts_ready": scalar(
            """
            SELECT COUNT(*) FROM v_waybill_business_facts
            WHERE direction='PURCHASE'
              AND validation_status='VALID'
              AND TRIM(COALESCE(unloading_point_name, ''))!=''
              AND loading_time IS NOT NULL
              AND loading_weight_kg IS NOT NULL
              AND unloading_time IS NOT NULL
              AND unloading_weight_kg IS NOT NULL
              AND local_pdf_count>0
            """
        ),
        "attachment_artifacts": scalar("SELECT COUNT(*) FROM logical_tickets WHERE artifact_path IS NOT NULL"),
        "oss_urls": scalar("SELECT COUNT(*) FROM logical_tickets WHERE oss_url IS NOT NULL"),
        "valid_tickets_without_submission": scalar(
            """
            SELECT COUNT(*) FROM logical_tickets lt
            WHERE lt.validation_status='VALID'
              AND NOT EXISTS (
                  SELECT 1 FROM submissions s WHERE s.waybill_id=lt.waybill_id
              )
            """
        ),
        "prepared": scalar("SELECT COUNT(*) FROM submissions WHERE status='PREPARED'"),
        "successful": scalar("SELECT COUNT(*) FROM submissions WHERE status='SUCCESS'"),
        "unuploaded": scalar("SELECT COUNT(*) FROM submissions WHERE status='UNUPLOADED'"),
        "retryable": scalar("SELECT COUNT(*) FROM submissions WHERE status IN ('RETRY','UNKNOWN')"),
        "rejected": scalar("SELECT COUNT(*) FROM submissions WHERE status='REJECTED'"),
        "submission_revisions": scalar("SELECT COUNT(*) FROM submission_revisions"),
    }
