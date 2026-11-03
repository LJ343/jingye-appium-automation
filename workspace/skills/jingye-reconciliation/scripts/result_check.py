# coding: utf-8
"""
result_check.py

基于 yyb_search.py 生成的 Excel 与磅票 PDF，执行采购/销售核验并生成 _check.xlsx。

核心原则：
1. PDF 与 Excel 先按“运单号 + 车牌号”严格匹配，避免串单。
2. 8 个线程只负责 PDF 文本解析、必要时的视觉模型识别和结构化结果返回；Excel 仅由主线程串行写入。
3. 文本层关键字段完整可靠时直接解析；文本缺失、字段不完整、乱码或图片型 PDF 才将全部页面发送给 qwen3-vl-plus 视觉兜底。
4. 识别结果按 PDF SHA256 缓存，脚本中断后重跑不会重复调用已经成功识别的 PDF。

依赖：
    pip install openpyxl pymupdf pillow requests
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import html
import io
import json
import logging
import math
import mimetypes
import os
import re
import shutil
import smtplib
import sys
import threading
import time
import unicodedata
from collections import defaultdict
from copy import copy
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Iterable, Optional

from openpyxl.utils import get_column_letter


import fitz  # PyMuPDF
import requests
from PIL import Image
from openpyxl import load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from freight_confirmation import (
    FreightData,
    FreightRecord,
    date_part as freight_date_part,
    load_freight_data,
    normalize_spec as normalize_freight_spec,
)


# ============================================================================
# 配置
# ============================================================================

BASE_DIR = Path(os.getenv("APP_BASE_DIR", r"D:\openclaw_data\data\jingyeapp"))

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.163.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_AUTH_CODE = os.getenv("SMTP_AUTH_CODE", '')
SMTP_SENDER = os.getenv("SMTP_SENDER", "configured-address-03@example.invalid")
# SMTP_RECEIVER = os.getenv("SMTP_RECEIVER", "configured-address-02@example.invalid,configured-address-01@example.invalid,configured-address-04@example.invalid")
# SMTP_RECEIVER = os.getenv("SMTP_RECEIVER", "configured-address-02@example.invalid,configured-address-01@example.invalid")
SMTP_RECEIVER = os.getenv("SMTP_RECEIVER", "configured-address-02@example.invalid")
SMTP_SUBJECT = os.getenv("SMTP_SUBJECT", "敬业钢铁运输明细")
REPORT_START_DATE = os.getenv("JINGYE_START_DATE", "").strip()
REPORT_END_DATE = os.getenv("JINGYE_END_DATE", "").strip()
REPORT_SALES_START_DATE = os.getenv("JINGYE_SALES_START_DATE", "").strip()
REPORT_SALES_END_DATE = os.getenv("JINGYE_SALES_END_DATE", "").strip()

EXCEL_INPUT_PATH = Path(
    os.getenv(
        "RESULT_CHECK_EXCEL_INPUT",
        str(BASE_DIR / "excel_ori" / "远行-敬业运输明细-指定的时间段-唯一标识.xlsx"),
    )
)
EXCEL_OUTPUT_DIR = Path(
    os.getenv("RESULT_CHECK_EXCEL_OUTPUT_DIR", str(BASE_DIR / "excel_check"))
)
RESULT_CHECK_CACHE_DIR = Path(
    os.getenv("RESULT_CHECK_CACHE_DIR", str(BASE_DIR / "result_check_cache"))
)
SOURCE_CONTEXT_PATH = Path(
    os.getenv("JINGYE_SOURCE_CONTEXT", str(BASE_DIR / "source_context" / "legacy.jsonl"))
)
FREIGHT_CONFIRMATION_PATH = Path(
    os.getenv(
        "JINGYE_FREIGHT_CONFIRMATION",
        str(BASE_DIR / "freight_confirmation" / "legacy.jsonl"),
    )
)

PDF_PURCHASE_HEBEI_DIR = Path(
    os.getenv("PDF_PURCHASE_HEBEI_DIR", str(BASE_DIR / "pdf_caigouhebei"))
)
PDF_PURCHASE_SHANGHAI_DIR = Path(
    os.getenv("PDF_PURCHASE_SHANGHAI_DIR", str(BASE_DIR / "pdf_caigoushanghai"))
)
PDF_SALES_HEBEI_DIR = Path(
    os.getenv("PDF_SALES_HEBEI_DIR", str(BASE_DIR / "pdf_xiaoshouhebei"))
)
PDF_SALES_SHANGHAI_DIR = Path(
    os.getenv("PDF_SALES_SHANGHAI_DIR", str(BASE_DIR / "pdf_xiaoshoushanghai"))
)

# 建议在系统环境变量中设置 DASHSCOPE_API_KEY；这里保留用户提供的默认值以便直接运行。
DASHSCOPE_API_KEY = os.getenv(
    "DASHSCOPE_API_KEY",
    "",
)
DASHSCOPE_BASE_URL = os.getenv(
    "DASHSCOPE_BASE_URL",
    "https://llm-d795ptikou79i3xy.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)
MODEL_NAME = os.getenv("RESULT_CHECK_MODEL_NAME", "qwen3-vl-plus")

VLM_IMAGE_MAX_SIDE = int(os.getenv("VLM_IMAGE_MAX_SIDE", "2200"))
VLM_RENDER_DPI = int(os.getenv("VLM_RENDER_DPI", "190"))
VLM_JPEG_QUALITY = int(os.getenv("VLM_JPEG_QUALITY", "92"))
VLM_MAX_PAGES = int(os.getenv("VLM_MAX_PAGES", "4"))
VLM_EXTRACTION_TEMPERATURE = float(os.getenv("VLM_EXTRACTION_TEMPERATURE", "0.1"))
VLM_MAX_TOKENS = int(os.getenv("VLM_MAX_TOKENS", "1800"))
VLM_CONNECT_TIMEOUT_SECONDS = int(os.getenv("VLM_CONNECT_TIMEOUT_SECONDS", "8"))
VLM_READ_TIMEOUT_SECONDS = int(os.getenv("VLM_READ_TIMEOUT_SECONDS", "120"))
VLM_REQUEST_RETRIES = int(os.getenv("VLM_REQUEST_RETRIES", "3"))
AUDIT_MAX_WORKERS = int(os.getenv("AUDIT_MAX_WORKERS", "8"))

# 吨数比较容差。PDF 与 Excel 通常保留两位小数；0.02 可吸收显示/浮点误差。
WEIGHT_TOLERANCE = float(os.getenv("WEIGHT_TOLERANCE", "0.02"))
FREIGHT_WEIGHT_ABSOLUTE_TOLERANCE = 0.1
FREIGHT_WEIGHT_RELATIVE_TOLERANCE = 0.01
MIN_VALID_PDF_BYTES = int(os.getenv("MIN_VALID_PDF_BYTES", "1024"))
PROMPT_VERSION = "2026-08-27-v5"

SHEET_DEFINITIONS = (
    ("采购单-河北众沃", "purchase", PDF_PURCHASE_HEBEI_DIR),
    ("采购单-上海宾迪", "purchase", PDF_PURCHASE_SHANGHAI_DIR),
    ("销售单-河北众沃", "sales", PDF_SALES_HEBEI_DIR),
    ("销售单-上海宾迪", "sales", PDF_SALES_SHANGHAI_DIR),
)

# 已知模板的固定列，仅在表头文字没有被正确识别时作为保守兜底。
PURCHASE_FALLBACK_COLUMNS = {
    "waybill": 2,
    "car": 3,
    "goods": 4,
    "destination": 5,
    "date": 7,          # 采购表的“卸货时间/卸货日期”
    "load_weight": 8,   # 装货重量/装车净重
    "unload_weight": 9, # 卸货重量/卸车净重
    "settlement_weight": 10,
}
SALES_FALLBACK_COLUMNS = {
    "waybill": 2,
    "car": 3,
    "goods": 4,
    "destination": 5,
    "date": 6,          # 装货时间/装货日期
    "load_weight": 7,
    "ticket_net": 8,
    "spec": 9,
    "theoretical": 10,
    "settlement_quantity": 11,
    "unit_price": 12,
    "remark1": 18,
    "remark2": 19,
}

HEADER_ALIASES = {
    "waybill": ("运单号", "单号"),
    "car": ("车牌号", "车号"),
    "goods": ("货物名称", "物料", "货名"),
    "carrier": ("承运商",),
    "destination": ("卸货地", "卸货库房", "收货地点", "收货"),
    "date_purchase": ("卸货时间", "卸货日期"),
    "date_sales": ("装货时间", "装货日期"),
    "load_weight": ("装货重量", "装车净重", "原发数"),
    "unload_weight": ("卸货重量", "卸车净重", "净重"),
    "settlement_weight": ("结算重量",),
    "ticket_net": ("磅票净重",),
    "spec": ("规格",),
    "theoretical": ("理重",),
    "settlement_quantity": ("结算量",),
    "unit_price": ("单价",),
    "remark1": ("备注1",),
    "remark2": ("备注2",),
}

PDF_FILENAME_RE = re.compile(
    r"^(?P<waybill>.+?)_(?P<car>[^_]+)_(?P<day>\d{8})_(?P<clock>\d{6})(?:_(?P<index>\d+))?$",
    re.IGNORECASE,
)

PLATE_RE = re.compile(
    r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5,6}",
    re.IGNORECASE,
)
WAYBILL_RE = re.compile(r"YS[A-Z0-9]+", re.IGNORECASE)

_thread_local = threading.local()


# ============================================================================
# 数据结构
# ============================================================================

@dataclasses.dataclass(frozen=True)
class PdfRef:
    path: Path
    kind: str
    waybill: str
    car: str
    download_time: dt.datetime
    attachment_index: int
    sha256: str
    size: int


@dataclasses.dataclass
class ShippingDetail:
    spec: str = ""
    theoretical_weight: Optional[float] = None


@dataclasses.dataclass
class PdfExtraction:
    path: Path
    kind: str
    car_no: str = ""
    event_datetime: str = ""
    event_date: str = ""
    goods_name: str = ""
    receiver: str = ""
    destination: str = ""
    destination_raw: str = ""
    original_quantity: Optional[float] = None
    net_weight: Optional[float] = None
    shipping_details: list[ShippingDetail] = dataclasses.field(default_factory=list)
    raw_remark: str = ""
    confidence: float = 0.0
    warnings: list[str] = dataclasses.field(default_factory=list)
    error: str = ""
    extraction_method: str = ""
    from_cache: bool = False


@dataclasses.dataclass
class SheetLayout:
    sheet_name: str
    kind: str
    header_row: int
    columns: dict[str, int]


@dataclasses.dataclass
class RowTask:
    sheet_name: str
    kind: str
    row: int
    sequence: Any
    waybill: str
    car: str
    goods: str
    destination: str
    load_weight: Optional[float]
    unload_weight: Optional[float]
    pdfs: list[PdfRef]
    related_pdfs: list[PdfRef]
    matching_notes: list[str]
    source_detail: str
    source_keys: tuple[tuple[str, str], ...]


@dataclasses.dataclass
class SalesDetailRow:
    pdf: Optional[PdfRef] = None
    pdf_index: int = 0
    detail_index: int = 0
    pdf_first: bool = True
    event_date: str = ""
    match_event_datetime: str = ""
    net_weight: Optional[float] = None
    goods_name: str = ""
    destination: str = ""
    spec: str = ""
    theoretical_weight: Optional[float] = None
    settlement_quantity: Optional[float] = None
    unit_price: Optional[float] = None
    freight_record: Optional[FreightRecord] = None
    freight_match_note: str = ""


@dataclasses.dataclass
class SourceContextIndex:
    by_row: dict[tuple[str, int], list[tuple[str, str]]] = dataclasses.field(default_factory=dict)
    by_key: dict[tuple[str, str, str], list[tuple[str, str]]] = dataclasses.field(default_factory=dict)
    by_pdf_path: dict[str, list[tuple[str, str]]] = dataclasses.field(default_factory=dict)
    by_pdf_name: dict[tuple[str, str], list[tuple[str, str]]] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class EmailPdfAttachment:
    path: Path
    filename: str
    content: bytes = dataclasses.field(repr=False)


# ============================================================================
# 基础工具
# ============================================================================

def setup_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    # OpenClaw 流水线会将三个脚本的 stdout/stderr 统一写入 run_log_*.log。
    if os.getenv("JINGYE_UNIFIED_LOG", "0").strip().lower() not in {"1", "true", "yes"}:
        handlers.append(logging.FileHandler(output_dir / "result_check.log", encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def normalize_header(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    return re.sub(r"[\s\r\n\t:：()（）\[\]【】,_，。/\\-]+", "", text).lower()


def normalize_compare(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).upper()
    return re.sub(r"[^0-9A-Z\u4e00-\u9fff]+", "", text)


def normalize_waybill(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().replace(" ", "")
    match = WAYBILL_RE.search(text)
    return match.group(0).upper() if match else re.sub(r"[^A-Z0-9]", "", text)


def normalize_car(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().replace(" ", "")
    match = PLATE_RE.search(text)
    return match.group(0).upper() if match else re.sub(r"[\s:：]", "", text)


def canonical_path_key(value: Any) -> str:
    try:
        resolved = Path(str(value or "")).expanduser().resolve(strict=False)
        return os.path.normcase(str(resolved))
    except Exception:
        return os.path.normcase(os.path.abspath(str(value or "")))


def append_unique_source(
    mapping: dict[Any, list[tuple[str, str]]],
    key: Any,
    source_kind: Any,
    source_value: Any,
) -> None:
    item = (str(source_kind or "").strip(), str(source_value or "").strip())
    if not item[0] or not item[1]:
        return
    values = mapping.setdefault(key, [])
    if item not in values:
        values.append(item)


def load_source_context(path: Path) -> SourceContextIndex:
    """读取抓取阶段的 JSONL 来源索引；坏行只告警，不影响旧数据核验。"""
    index = SourceContextIndex()
    if not path.is_file():
        logging.warning("来源索引不存在，将按旧数据兼容规则标注：%s", path)
        return index

    current_excel_key = canonical_path_key(EXCEL_INPUT_PATH)
    with path.open("r", encoding="utf-8") as context_file:
        for line_number, raw_line in enumerate(context_file, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("记录不是JSON对象")
            except Exception as exc:
                logging.warning("来源索引第%d行无法解析，已跳过：%s", line_number, exc)
                continue

            record_excel = str(record.get("excel_path") or "").strip()
            if record_excel and canonical_path_key(record_excel) != current_excel_key:
                # legacy.jsonl 可能积累多次直接运行的数据，必须避免跨 Excel 串单。
                continue

            sheet = str(record.get("sheet") or "").strip()
            waybill = normalize_waybill(record.get("waybill"))
            car = normalize_car(record.get("car"))
            source_kind = record.get("source_kind")
            source_value = record.get("source_value")
            event = str(record.get("event") or "").strip().lower()

            if event == "row" and sheet and waybill:
                append_unique_source(
                    index.by_key,
                    (sheet, waybill, car),
                    source_kind,
                    source_value,
                )

            if event == "row" and sheet:
                try:
                    excel_row = int(record.get("excel_row"))
                except (TypeError, ValueError):
                    excel_row = 0
                if excel_row > 0:
                    append_unique_source(
                        index.by_row,
                        (sheet, excel_row),
                        source_kind,
                        source_value,
                    )

            pdf_path = str(record.get("pdf_path") or "").strip()
            if event == "pdf" and pdf_path:
                append_unique_source(
                    index.by_pdf_path,
                    canonical_path_key(pdf_path),
                    source_kind,
                    source_value,
                )
                if sheet:
                    append_unique_source(
                        index.by_pdf_name,
                        (sheet, Path(pdf_path).name.casefold()),
                        source_kind,
                        source_value,
                    )

    return index


def merge_source_entries(*groups: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    merged: list[tuple[str, str]] = []
    for group in groups:
        for item in group:
            if item not in merged:
                merged.append(item)
    return merged


def sources_for_pdf(
    index: SourceContextIndex,
    sheet_name: str,
    path: Path,
) -> list[tuple[str, str]]:
    return merge_source_entries(
        index.by_pdf_path.get(canonical_path_key(path), []),
        index.by_pdf_name.get((sheet_name, path.name.casefold()), []),
    )


def sources_for_row(
    index: SourceContextIndex,
    sheet_name: str,
    row: int,
    waybill: str,
    car: str,
    related_pdfs: Iterable[PdfRef],
) -> list[tuple[str, str]]:
    row_sources = merge_source_entries(
        index.by_row.get((sheet_name, row), []),
        index.by_key.get((sheet_name, normalize_waybill(waybill), normalize_car(car)), []),
    )
    if row_sources:
        return row_sources
    # 兼容抓取过程在写入行来源前中断的情况；仅在没有行级记录时才回退 PDF 来源，
    # 避免车牌不一致时把“记录来源车牌”和“问题PDF来源车牌”混在一起。
    return merge_source_entries(
        *(sources_for_pdf(index, sheet_name, ref.path) for ref in related_pdfs)
    )


def format_source_detail(
    sheet_name: str,
    sources: Iterable[tuple[str, str]],
    fallback_car: str = "",
    fallback_origin: str = "excel",
) -> str:
    source_list = list(sources)
    if "河北众沃" in sheet_name:
        accounts = list(dict.fromkeys(value for kind, value in source_list if kind == "login_account"))
        if accounts:
            suffix = "（来源记录存在多个值）" if len(accounts) > 1 else ""
            return "登录账号：" + "、".join(accounts) + suffix
        return "登录账号：未记录（来源索引缺失或为升级前旧数据）"

    if "上海宾迪" in sheet_name:
        selected_cars = list(dict.fromkeys(value for kind, value in source_list if kind == "selected_car"))
        if selected_cars:
            suffix = "（来源记录存在多个值）" if len(selected_cars) > 1 else ""
            return "查询车牌：" + "、".join(selected_cars) + suffix
        normalized_fallback = normalize_car(fallback_car)
        if normalized_fallback:
            if fallback_origin == "pdf_filename":
                return (
                    "查询车牌：未记录（来源索引缺失；"
                    f"PDF文件名车牌{normalized_fallback}仅供人工线索，不作为选车证据）"
                )
            return (
                f"查询车牌：{normalized_fallback}"
                "（来源索引缺失，回退Excel车牌；不作为抓取时选车证据）"
            )
        return "查询车牌：未记录（来源索引缺失或为升级前旧数据）"

    return "来源：未记录"


def to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if math.isnan(float(value)):
            return None
        return float(value)
    text = unicodedata.normalize("NFKC", str(value)).replace(",", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def format_number(value: Optional[float]) -> str:
    if value is None:
        return ""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def nearly_equal(a: Optional[float], b: Optional[float], tolerance: float = WEIGHT_TOLERANCE) -> bool:
    return a is not None and b is not None and abs(a - b) <= tolerance


def text_contains(expected: str, actual: str) -> bool:
    exp = normalize_compare(expected)
    act = normalize_compare(actual)
    if not exp or not act:
        return False
    return exp in act or act in exp


def is_sales_destination_only_warning(value: Any) -> bool:
    """销售PDF地址只用于多附件差异展示，单纯地址识别提示不作为异常。"""
    text = normalize_compare(value)
    destination_markers = ("收货地址", "收货地点", "卸货地", "地址", "地点")
    other_field_markers = (
        "货物", "货名", "车牌", "日期", "时间", "净重", "重量", "规格", "理重", "运单"
    )
    return (
        any(marker in text for marker in destination_markers)
        and not any(marker in text for marker in other_field_markers)
    )


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def validate_pdf_file(path: Path) -> tuple[bool, str]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, f"无法读取文件属性: {exc}"
    if size < MIN_VALID_PDF_BYTES:
        return False, f"PDF文件过小或为空: {size} bytes"
    try:
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                return False, "文件头不是 %PDF-"
        with fitz.open(path) as doc:
            if doc.page_count < 1:
                return False, "PDF没有页面"
    except Exception as exc:
        return False, f"PDF结构损坏: {exc}"
    return True, ""


def safe_json_dump(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def get_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _thread_local.session = session
    return session


# ============================================================================
# Excel 表头、行与样式
# ============================================================================

EXCEL_NUMBER_FORMAT = '#,##0.###'
STYLE_THIN_SIDE = Side(style="thin", color="D9D9D9")
STYLE_MEDIUM_SIDE = Side(style="medium", color="7F8C8D")
STYLE_PDF_SIDE = Side(style="thin", color="9FBAD0")
STYLE_NO_SIDE = Side(style=None)
STYLE_ALT_FILL = PatternFill("solid", fgColor="F7FAFC")
STYLE_PDF_FILL = PatternFill("solid", fgColor="EEF6FF")
STYLE_SUMMARY_HEADER_FILL = PatternFill("solid", fgColor="365F91")
STYLE_SUMMARY_ALERT_FILL = PatternFill("solid", fgColor="FFF2CC")
STYLE_SUMMARY_HEADER_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
SALES_DETAIL_HEADERS = ("结算量", "单价")
NUMERIC_HEADERS = {
    "装货重量", "卸货重量", "结算重量", "磅票净重", "理重", "结算量", "单价",
}


def ensure_sales_detail_columns(ws) -> None:
    """兼容旧模板：在“理重”后补齐结算量、单价，并保持后续原有列整体顺移。"""
    header_row = find_header_row(ws, "sales")
    theoretical_col = find_column(ws, header_row, HEADER_ALIASES["theoretical"])
    if theoretical_col is None:
        theoretical_col = SALES_FALLBACK_COLUMNS["theoretical"]

    for merged in list(ws.merged_cells.ranges):
        if merged.min_row == merged.max_row == 1 and merged.min_col == 1:
            ws.unmerge_cells(str(merged))

    for offset, header in enumerate(SALES_DETAIL_HEADERS, start=1):
        target_col = theoretical_col + offset
        if normalize_header(ws.cell(header_row, target_col).value) == normalize_header(header):
            continue
        ws.insert_cols(target_col, 1)
        source = ws.cell(header_row, theoretical_col)
        target = ws.cell(header_row, target_col, value=header)
        target._style = copy(source._style)
        target.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(target_col)].width = 12

    last_col = ws.max_column
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)


def ensure_workbook_schema(wb) -> None:
    for sheet_name, kind, _directory in SHEET_DEFINITIONS:
        if kind == "sales" and sheet_name in wb.sheetnames:
            ensure_sales_detail_columns(wb[sheet_name])


def set_recalculation_mode(wb) -> None:
    if hasattr(wb, "calculation"):
        wb.calculation.calcMode = "auto"
        wb.calculation.fullCalcOnLoad = True
        wb.calculation.forceFullCalc = True

def find_header_row(ws, kind: str) -> int:
    aliases = [
        *HEADER_ALIASES["waybill"],
        *HEADER_ALIASES["car"],
        *HEADER_ALIASES["goods"],
        *HEADER_ALIASES["remark1"],
    ]
    if kind == "sales":
        aliases.extend(HEADER_ALIASES["ticket_net"])
    best_row, best_score = 2, -1
    alias_norms = {normalize_header(x) for x in aliases}
    for row in range(1, min(ws.max_row, 10) + 1):
        score = 0
        for col in range(1, ws.max_column + 1):
            if normalize_header(ws.cell(row, col).value) in alias_norms:
                score += 1
        if score > best_score:
            best_row, best_score = row, score
    return best_row


def find_column(ws, header_row: int, aliases: Iterable[str]) -> Optional[int]:
    expected = {normalize_header(alias) for alias in aliases}
    for col in range(1, ws.max_column + 1):
        if normalize_header(ws.cell(header_row, col).value) in expected:
            return col
    return None


def resolve_sheet_layout(ws, kind: str) -> SheetLayout:
    header_row = find_header_row(ws, kind)
    fallback = dict(PURCHASE_FALLBACK_COLUMNS if kind == "purchase" else SALES_FALLBACK_COLUMNS)
    carrier_col = find_column(ws, header_row, HEADER_ALIASES["carrier"]) if kind == "purchase" else None
    if carrier_col == PURCHASE_FALLBACK_COLUMNS["goods"] + 1:
        fallback = {key: column + (column >= carrier_col) for key, column in fallback.items()}
    columns: dict[str, int] = {}
    if carrier_col is not None:
        columns["carrier"] = carrier_col

    columns["waybill"] = find_column(ws, header_row, HEADER_ALIASES["waybill"]) or fallback["waybill"]
    columns["car"] = find_column(ws, header_row, HEADER_ALIASES["car"]) or fallback["car"]
    columns["goods"] = find_column(ws, header_row, HEADER_ALIASES["goods"]) or fallback["goods"]
    columns["destination"] = (
        find_column(ws, header_row, HEADER_ALIASES["destination"]) or fallback["destination"]
    )
    columns["load_weight"] = (
        find_column(ws, header_row, HEADER_ALIASES["load_weight"]) or fallback["load_weight"]
    )
    remark1 = find_column(ws, header_row, HEADER_ALIASES["remark1"])
    if remark1 is None:
        if kind == "sales":
            remark1 = SALES_FALLBACK_COLUMNS["remark1"]
        else:
            raise RuntimeError(f"子表 {ws.title} 未找到“备注1”列，无法安全写入核验结果")
    columns["remark1"] = remark1

    if kind == "purchase":
        columns["date"] = (
            find_column(ws, header_row, HEADER_ALIASES["date_purchase"])
            or fallback["date"]
        )
        columns["unload_weight"] = (
            find_column(ws, header_row, HEADER_ALIASES["unload_weight"])
            or fallback["unload_weight"]
        )
        columns["settlement_weight"] = (
            find_column(ws, header_row, HEADER_ALIASES["settlement_weight"])
            or fallback["settlement_weight"]
        )
    else:
        columns["date"] = (
            find_column(ws, header_row, HEADER_ALIASES["date_sales"])
            or SALES_FALLBACK_COLUMNS["date"]
        )
        columns["ticket_net"] = (
            find_column(ws, header_row, HEADER_ALIASES["ticket_net"])
            or SALES_FALLBACK_COLUMNS["ticket_net"]
        )
        columns["spec"] = (
            find_column(ws, header_row, HEADER_ALIASES["spec"])
            or SALES_FALLBACK_COLUMNS["spec"]
        )
        columns["theoretical"] = (
            find_column(ws, header_row, HEADER_ALIASES["theoretical"])
            or SALES_FALLBACK_COLUMNS["theoretical"]
        )
        columns["settlement_quantity"] = (
            find_column(ws, header_row, HEADER_ALIASES["settlement_quantity"])
            or SALES_FALLBACK_COLUMNS["settlement_quantity"]
        )
        columns["unit_price"] = (
            find_column(ws, header_row, HEADER_ALIASES["unit_price"])
            or SALES_FALLBACK_COLUMNS["unit_price"]
        )

    return SheetLayout(ws.title, kind, header_row, columns)


def set_cell_value(ws, row: int, col: int, value: Any, comment: str = "") -> None:
    cell = ws.cell(row, col)
    cell.value = None if value == "" else value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        cell.number_format = EXCEL_NUMBER_FORMAT
        cell.alignment = Alignment(horizontal="right", vertical="center")
    elif isinstance(value, str) and value.startswith("="):
        cell.number_format = EXCEL_NUMBER_FORMAT
        cell.alignment = Alignment(horizontal="right", vertical="center")
    else:
        cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    if comment:
        cell.comment = Comment(comment, "result_check.py")


def estimate_visual_lines(value: Any, col_width: float) -> int:
    if value is None or value == "":
        return 1
    text = str(value)
    explicit_lines = text.split("\n")
    capacity = max(6.0, col_width * 1.15)
    total = 0
    for line in explicit_lines:
        visual_len = 0.0
        for ch in line:
            visual_len += 1.0 if "\u4e00" <= ch <= "\u9fff" else 0.58
        total += max(1, math.ceil(visual_len / capacity))
    return max(1, total)


def adjust_row_height(ws, row, cols):

    max_lines = 1

    for col in cols:

        value = ws.cell(row=row, column=col).value

        if value is None:
            continue

        value = str(value)

        col_letter = get_column_letter(col)

        width = ws.column_dimensions[col_letter].width
        if width is None:
            width = 12

        chars_per_line = max(int(width * 1.6), 8)

        lines = 0
        for text in value.split("\n"):
            lines += max(1, math.ceil(len(text) / chars_per_line))

        max_lines = max(max_lines, lines)

    ws.row_dimensions[row].height = max(20, max_lines * 18)

def append_existing_remark(existing: Any, audit_text: str) -> str:
    existing_text = str(existing or "").strip()
    # 重跑脚本时移除上一次由本脚本写入的核验段，避免重复堆叠。
    if "【磅票核验】" in existing_text:
        existing_text = existing_text.split("【磅票核验】", 1)[0].rstrip()
    result = f"【磅票核验】{audit_text}"
    return f"{existing_text}\n{result}" if existing_text else result


# ============================================================================
# PDF 目录扫描与严格匹配
# ============================================================================

def parse_pdf_ref(path: Path, kind: str) -> tuple[Optional[PdfRef], str]:
    valid, error = validate_pdf_file(path)
    if not valid:
        return None, error
    match = PDF_FILENAME_RE.match(path.stem)
    if not match:
        return None, "文件名不符合 运单号_车牌号_YYYYMMDD_HHMMSS[_序号].pdf 规则"
    try:
        timestamp = dt.datetime.strptime(
            match.group("day") + match.group("clock"), "%Y%m%d%H%M%S"
        )
    except ValueError as exc:
        return None, f"文件名时间无效: {exc}"
    try:
        digest = file_sha256(path)
    except Exception as exc:
        return None, f"计算SHA256失败: {exc}"
    return (
        PdfRef(
            path=path,
            kind=kind,
            waybill=normalize_waybill(match.group("waybill")),
            car=normalize_car(match.group("car")),
            download_time=timestamp,
            attachment_index=int(match.group("index") or 1),
            sha256=digest,
            size=path.stat().st_size,
        ),
        "",
    )


def scan_pdf_directory(directory: Path, kind: str) -> tuple[list[PdfRef], list[dict[str, str]]]:
    refs: list[PdfRef] = []
    issues: list[dict[str, str]] = []
    if not directory.exists():
        issues.append({"pdf": str(directory), "issue": "PDF目录不存在"})
        return refs, issues
    for path in sorted(directory.rglob("*.pdf")):
        ref, error = parse_pdf_ref(path, kind)
        if ref is None:
            issues.append({"pdf": str(path), "issue": error})
        else:
            refs.append(ref)
    return refs, issues


def dedupe_same_content(refs: list[PdfRef]) -> tuple[list[PdfRef], list[str]]:
    grouped: dict[str, list[PdfRef]] = defaultdict(list)
    for ref in refs:
        grouped[ref.sha256].append(ref)
    kept: list[PdfRef] = []
    notes: list[str] = []
    for digest, group in grouped.items():
        group.sort(key=lambda x: (x.download_time, x.attachment_index, x.path.name))
        kept.append(group[-1])
        if len(group) > 1:
            notes.append(
                f"发现{len(group)}份内容完全相同的重复PDF，按最新文件核验：{group[-1].path.name}"
            )
    kept.sort(key=lambda x: (x.attachment_index, x.download_time, x.path.name))
    return kept, notes


def build_pdf_index(refs: list[PdfRef]):
    exact: dict[tuple[str, str], list[PdfRef]] = defaultdict(list)
    by_waybill: dict[str, list[PdfRef]] = defaultdict(list)
    for ref in refs:
        exact[(ref.waybill, ref.car)].append(ref)
        by_waybill[ref.waybill].append(ref)
    return exact, by_waybill


def match_row_pdfs(
    waybill: str,
    car: str,
    kind: str,
    exact_index: dict[tuple[str, str], list[PdfRef]],
    waybill_index: dict[str, list[PdfRef]],
) -> tuple[list[PdfRef], list[PdfRef], list[str]]:
    notes: list[str] = []
    wb = normalize_waybill(waybill)
    plate = normalize_car(car)
    refs = list(exact_index.get((wb, plate), []))
    related_refs = list(refs)

    if not refs:
        same_waybill = list(waybill_index.get(wb, []))
        related_refs = list(same_waybill)
        cars = {item.car for item in same_waybill}
        # 只有 Excel 车牌为空且该运单号在 PDF 中仅对应一个车牌时，才允许按运单号兜底。
        if not plate and len(cars) == 1:
            refs = same_waybill
            notes.append("Excel车牌为空，按唯一运单号匹配PDF")
        elif same_waybill:
            notes.append(
                "存在同运单号PDF但车牌不一致："
                + ", ".join(f"{item.car}（{item.path.name}）" for item in same_waybill)
            )

    refs, duplicate_notes = dedupe_same_content(refs)
    notes.extend(duplicate_notes)

    if kind == "purchase" and len(refs) > 1:
        # 采购要求一行只对应一份PDF。多份不同内容时不能猜测，防止错配。
        notes.append(
            "采购行匹配到多份不同内容PDF，未自动选择：" + ", ".join(x.path.name for x in refs)
        )
        return [], related_refs, notes
    return refs, related_refs, notes


# ============================================================================
# PDF 文本提取、页面渲染与本地确定性解析
# ============================================================================

def extract_embedded_text(path: Path) -> str:
    chunks: list[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            chunks.append(page.get_text("text") or "")
    return "\n".join(chunks).strip()


def render_pdf_pages_as_data_urls(path: Path) -> list[str]:
    data_urls: list[str] = []
    zoom = VLM_RENDER_DPI / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(path) as doc:
        if doc.page_count > VLM_MAX_PAGES:
            raise RuntimeError(f"PDF共{doc.page_count}页，超过允许的{VLM_MAX_PAGES}页")
        for page_number in range(doc.page_count):
            page = doc.load_page(page_number)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            max_side = max(image.size)
            if max_side > VLM_IMAGE_MAX_SIDE:
                ratio = VLM_IMAGE_MAX_SIDE / max_side
                image = image.resize(
                    (max(1, round(image.width * ratio)), max(1, round(image.height * ratio))),
                    Image.Resampling.LANCZOS,
                )
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=VLM_JPEG_QUALITY, optimize=True)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            data_urls.append(f"data:image/jpeg;base64,{encoded}")
    return data_urls


def find_line_value(text: str, label: str, excluded_prefixes: tuple[str, ...] = ()) -> str:
    label_re = re.escape(label)
    for line in text.splitlines():
        normalized = unicodedata.normalize("NFKC", line).strip()
        if any(normalized.startswith(prefix) for prefix in excluded_prefixes):
            continue
        match = re.match(rf"^{label_re}\s*[:：]\s*(.+?)\s*$", normalized)
        if match:
            return match.group(1).strip()
    return ""


def normalize_pdf_field_value(value: Any) -> str:
    """规范 PDF 字段换行，但不改写字段语义。"""
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).replace("\u00a0", " ")
    pieces = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.splitlines()]
    # 磅票字段经常在中文词语或地址中间自动换行，不能额外插入空格。
    return re.sub(r"[ \t]+", " ", "".join(piece for piece in pieces if piece)).strip()


def extract_multiline_field(
    text: str,
    label: str,
    stop_labels: tuple[str, ...],
    excluded_prefixes: tuple[str, ...] = (),
) -> str:
    """提取从指定标签到下一个字段标签之间的跨行内容。"""
    lines = [
        unicodedata.normalize("NFKC", line).replace("\u00a0", " ").strip()
        for line in text.splitlines()
    ]
    label_pattern = re.compile(rf"^{re.escape(label)}\s*[:：]\s*(.*)$")
    stop_patterns = [
        re.compile(rf"^{re.escape(stop_label)}\s*[:：]")
        for stop_label in stop_labels
    ]

    for index, line in enumerate(lines):
        if any(line.startswith(prefix) for prefix in excluded_prefixes):
            continue
        match = label_pattern.match(line)
        if not match:
            continue

        pieces = [match.group(1).strip()]
        for following in lines[index + 1:]:
            if any(pattern.match(following) for pattern in stop_patterns):
                break
            if re.fullmatch(r"-{4,}", following):
                break
            if following:
                pieces.append(following)
        return normalize_pdf_field_value("\n".join(pieces))
    return ""


def text_field_quality(value: str, field: str) -> int:
    """对文本层/VLM字段做保守质量评分；只识别明显截断、串字段或乱码。"""
    cleaned = normalize_pdf_field_value(value)
    compact = normalize_compare(cleaned)
    if not compact:
        return -100

    score = min(len(compact), 30)
    if any(marker in cleaned for marker in ("\ufffd", "□", "�")):
        score -= 80
    if cleaned.count("?") >= 2 or cleaned.count("？") >= 2:
        score -= 30

    if field == "goods_name":
        if len(compact) > 60:
            score -= 40
        if any(marker in compact for marker in ("供货", "收货", "毛重", "皮重", "净重", "备注", "配货明细")):
            score -= 50
    elif field == "destination":
        if compact in {"无", "未知", "未识别", "无法识别"}:
            score -= 80
        if any(marker in compact for marker in ("备注", "配货明细", "毛重", "皮重", "净重")):
            score -= 50
    return score


def choose_goods_name(local_value: str, visual_value: str) -> str:
    """文本层通常更精确；明显不完整或低质量时允许 VLM 纠正。"""
    local_clean = normalize_pdf_field_value(local_value)
    visual_clean = normalize_pdf_field_value(visual_value)
    local_score = text_field_quality(local_clean, "goods_name")
    visual_score = text_field_quality(visual_clean, "goods_name")

    if local_score < 0:
        return visual_clean if visual_score >= 0 else local_clean
    if visual_score < 0:
        return local_clean
    if text_contains(local_clean, visual_clean):
        # 两者语义一致时保留更完整的视觉结果，例如文本层在自动换行处截断。
        return (
            visual_clean
            if len(normalize_compare(visual_clean)) > len(normalize_compare(local_clean))
            else local_clean
        )
    # 两者都像正常货名且存在冲突时保持确定性文本结果，避免扩大既有行为。
    return visual_clean if visual_score >= local_score + 12 else local_clean


def choose_sales_destination(local_value: str, visual_value: str) -> str:
    """销售 PDF 地址优先使用 VLM 清洗值，视觉值无效时回退完整文本字段。"""
    local_clean = normalize_pdf_field_value(local_value)
    visual_clean = normalize_pdf_field_value(visual_value)
    if text_field_quality(visual_clean, "destination") >= 0:
        return visual_clean
    return local_clean


def parse_last_standard_datetime(text: str) -> str:
    values = re.findall(r"20\d{2}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}:\d{2}", text)
    if not values:
        return ""
    return values[-1].replace("/", "-")


def parse_number_after_label(text: str, label: str) -> Optional[float]:
    match = re.search(
        rf"{re.escape(label)}\s*[:：]\s*([-+]?\d+(?:\.\d+)?)",
        unicodedata.normalize("NFKC", text),
    )
    return float(match.group(1)) if match else None


def parse_shipping_details_from_text(text: str) -> list[ShippingDetail]:
    compact = unicodedata.normalize("NFKC", text)
    compact = re.sub(r"\s+", "", compact)
    if "配货明细" not in compact:
        return []
    detail_text = compact.split("配货明细", 1)[1]
    groups = re.findall(r"\[([^\[\]]+)\]", detail_text)
    details: list[ShippingDetail] = []
    for group in groups:
        if "理重" not in group:
            continue
        cleaned = group.replace("，", ",")
        theory_match = re.search(r",([-+]?\d+(?:\.\d+)?)\(?理重\)?", cleaned)
        if not theory_match:
            continue
        theoretical = float(theory_match.group(1))
        before_theory = cleaned[: theory_match.start()]
        parts = [part.strip() for part in before_theory.split(",") if part.strip()]
        # 第一段是配货单号，剩余部分是尺寸、件数等规格信息。
        spec = ",".join(parts[1:]) if len(parts) >= 2 else before_theory
        details.append(ShippingDetail(spec=spec, theoretical_weight=theoretical))
    return details


def count_expected_shipping_details(text: str) -> int:
    """统计文本层中应当存在的配货明细组数，用于发现跨页或乱码造成的漏提取。"""
    compact = unicodedata.normalize("NFKC", text)
    compact = re.sub(r"\s+", "", compact)
    if "配货明细" not in compact:
        return 0
    detail_text = compact.split("配货明细", 1)[1]
    return sum(
        1
        for group in re.findall(r"\[([^\[\]]+)\]", detail_text)
        if "理重" in group
    )


def local_parse(path: Path, kind: str, text: str) -> PdfExtraction:
    result = PdfExtraction(path=path, kind=kind, extraction_method="text")
    result.car_no = find_line_value(text, "车号")
    result.event_datetime = parse_last_standard_datetime(text)
    result.event_date = result.event_datetime[:10] if result.event_datetime else ""
    result.goods_name = extract_multiline_field(
        text,
        "货名",
        ("供货", "收货", "收货单位", "毛重", "皮重", "净重", "备注"),
    ) or find_line_value(text, "货名")
    result.net_weight = parse_number_after_label(text, "净重")

    if kind == "purchase":
        # 必须取“收货”，不能误取“收货单位”。
        result.receiver = find_line_value(text, "收货", excluded_prefixes=("收货单位",))
        result.original_quantity = parse_number_after_label(text, "原发数")
    else:
        result.destination_raw = extract_multiline_field(
            text,
            "收货地点",
            ("备注", "配货明细"),
        ) or find_line_value(text, "收货地点")
        result.destination = result.destination_raw
        result.shipping_details = parse_shipping_details_from_text(text)
        compact = unicodedata.normalize("NFKC", text)
        remark_match = re.search(
            r"备注\s*[:：]\s*(.*?)(?:\n\s*\S*磅\s+20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}|$)",
            compact,
            re.DOTALL,
        )
        if remark_match:
            result.raw_remark = re.sub(r"\s+", " ", remark_match.group(1)).strip()
    return result


def embedded_text_reliability_issues(result: PdfExtraction, text: str) -> list[str]:
    """返回需要 VLM 视觉兜底的原因；空列表表示文本层可直接使用。"""
    issues: list[str] = []
    normalized_text = unicodedata.normalize("NFKC", text or "")
    compact_text = normalize_compare(normalized_text)

    if len(compact_text) < 80:
        issues.append("PDF文本层过短或不存在")
    if any(marker in normalized_text for marker in ("\ufffd", "□", "�")):
        issues.append("PDF文本层含乱码占位符")

    required_labels = ["车号", "货名", "净重"]
    required_labels.extend(
        ["收货", "原发数"]
        if result.kind == "purchase"
        else ["收货地点"]
    )
    for label in required_labels:
        if not re.search(rf"(?m)^\s*{re.escape(label)}\s*[:：]", normalized_text):
            issues.append(f"文本层缺少“{label}”标签")

    normalized_car = normalize_car(result.car_no)
    if not normalized_car or not PLATE_RE.fullmatch(normalized_car):
        issues.append("车牌字段缺失或格式异常")
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", result.event_datetime):
        issues.append("下半部分标准时间缺失或格式异常")
    if text_field_quality(result.goods_name, "goods_name") < 0:
        issues.append("货名字段缺失、乱码或串字段")
    if result.net_weight is None or result.net_weight <= 0:
        issues.append("净重字段缺失或不是有效正数")

    if result.kind == "purchase":
        receiver = normalize_pdf_field_value(result.receiver)
        if not receiver or any(marker in receiver for marker in ("\ufffd", "□", "�")):
            issues.append("采购收货字段缺失或乱码")
        if result.original_quantity is None or result.original_quantity <= 0:
            issues.append("原发数字段缺失或不是有效正数")
    else:
        if text_field_quality(result.destination_raw, "destination") < 0:
            issues.append("销售收货地点缺失、乱码或串字段")
        expected_details = count_expected_shipping_details(normalized_text)
        if expected_details <= 0:
            issues.append("文本层未找到配货明细组")
        elif len(result.shipping_details) != expected_details:
            issues.append(
                f"配货明细提取不完整（应有{expected_details}组，取得{len(result.shipping_details)}组）"
            )
        if any(
            not normalize_pdf_field_value(detail.spec)
            or detail.theoretical_weight is None
            or detail.theoretical_weight < 0
            for detail in result.shipping_details
        ):
            issues.append("配货明细规格或理重字段不完整")

    return list(dict.fromkeys(issues))


# ============================================================================
# 视觉模型提示词、请求与结果合并
# ============================================================================

PURCHASE_SYSTEM_PROMPT = """你是敬业运输磅票核验的视觉信息抽取器。必须逐页独立观察PDF图片，严格区分字段标签；PDF内嵌文本可能换行、截断或乱码，只能作为辅助，不能替代视觉判断。返回单个JSON对象，不要解释、不要Markdown、不要推测不存在的值。"""

PURCHASE_USER_PROMPT = """请识别这份采购计量单的所有页面。若内容因排版挤到第2页，两页属于同一张计量单，必须合并理解。

关键规则：
1. event_datetime：取票据下半部分、横线“共1联”之前的“磅房/站点名称 + YYYY-MM-DD HH:MM:SS”中的标准时间。不要取毛重、皮重、净重后面的202607...紧凑时间。
2. goods_name：从图片中定位“货名：”，提取其后直到“供货：”开始前的完整内容；跨行时拼接完整，保留型号/补充名称。不得从供货库房、牌号、规格或备注推断货名。
3. receiver：只取“收货：”后的地点，例如“原料三车间烧结料场”。绝对不要取“收货单位：敬业钢铁有限公司”。
4. original_quantity：取“原发数：”后的数字。
5. net_weight：取“净重：”后、“吨”前的数字。
6. car_no：取“车号：”后的车牌。
7. 印章可能遮挡文字，需要结合上下文识别；无法确认时返回空值并在warnings说明。

只返回以下JSON结构：
{
  "doc_type": "purchase",
  "car_no": "",
  "event_datetime": "YYYY-MM-DD HH:MM:SS或空字符串",
  "goods_name": "",
  "receiver": "",
  "original_quantity": null,
  "net_weight": null,
  "confidence": 0.0,
  "warnings": []
}

PDF内嵌文本仅作辅助，图片内容为最终依据：
---BEGIN AUX TEXT---
{aux_text}
---END AUX TEXT---
"""

SALES_SYSTEM_PROMPT = """你是敬业运输销售磅票核验的视觉信息抽取器。无论PDF是否带内嵌文本，都必须逐页独立观察图片并完成视觉核对；辅助文本可能换行、截断或乱码，不能直接照抄。严格按字段标签重组跨行、跨页内容，返回单个JSON对象，不要解释、不要Markdown、不要输出思考过程。"""

SALES_USER_PROMPT = """请识别这份销售计量单的所有页面。第2页可能只有尾部内容，但仍属于同一张计量单，必须合并理解。

关键规则：
1. event_datetime：取票据下半部分、横线“共1联”之前的“磅房/站点名称 + YYYY-MM-DD HH:MM:SS”中的标准时间。不要取毛重、皮重、净重后的202607...紧凑时间。
2. goods_name：从图片中定位“货名：”，提取其后直到“供货：”开始前的完整内容；跨行时拼接完整。必须忠实抄录，不得把供货库房、配货牌号、材质牌号、规格或备注当成货名，也不得概括或改写货名。
3. net_weight：取“净重：”后、“吨”前的数字。
4. destination_raw：从图片中提取“收货地点：”之后到“备注：”之前的完整原文，可跨多行；不得混入备注字段。
5. destination：只从destination_raw中提取真实的最终目的地物理地址：
   - 去除联系人、电话、扫码说明、装卸时间、单卷重量、车辆要求及其他操作备注。
   - 去除“有限公司、有限责任公司、股份有限公司、公司”等纯公司后缀；物流园、工业园、厂区、库房、仓库、港口、码头等地点名称必须保留。
   - 没有详细街道门牌时，输出原文中最像最终卸货地的地点名称。
   - destination禁止输出空；但只能依据PDF图片和destination_raw，禁止使用APP地址、文件名、其他PDF或常识凭空补写。
6. raw_remark：取“备注：”后的完整文字，可跨多行。
7. shipping_details：从“配货明细”之后提取每一组方括号内容。每组通常为：
   [配货单号,尺寸规格,件数,数字(理重)]
   - spec只保留尺寸规格与件数，例如“18mm*2000mm*5800mm,20件”，不要配货单号。
   - theoretical_weight取“理重”前的数字。
   - 有几组就全部返回，包括0件、0理重的组。
   - “理 重”可能被换行或空格拆开，仍按“理重”识别。
8. car_no：取“车号：”后的车牌。
9. 印章可能遮挡文字，需要结合图片上下文识别；除destination外，无法确认的字段返回空值并在warnings说明。不得因辅助文本非空就忽略图片。

只返回以下JSON结构：
{
  "doc_type": "sales",
  "car_no": "",
  "event_datetime": "YYYY-MM-DD HH:MM:SS或空字符串",
  "goods_name": "",
  "net_weight": null,
  "destination_raw": "",
  "destination": "",
  "raw_remark": "",
  "shipping_details": [
    {"spec": "18mm*2000mm*5800mm,20件", "theoretical_weight": 32.78}
  ],
  "confidence": 0.0,
  "warnings": []
}

PDF内嵌文本仅作辅助，图片内容为最终依据：
---BEGIN AUX TEXT---
{aux_text}
---END AUX TEXT---
"""


def extract_message_content(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict):
                pieces.append(str(item.get("text") or item.get("content") or ""))
            else:
                pieces.append(str(item))
        return "".join(pieces).strip()
    return ""


def parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        value = json.loads(text[start : end + 1])
        if isinstance(value, dict):
            return value
    raise ValueError(f"模型返回不是有效JSON对象: {text[:500]}")


def build_vlm_payload(kind: str, aux_text: str, image_urls: list[str], simple_mode: bool) -> dict[str, Any]:
    if kind == "purchase":
        system_prompt = PURCHASE_SYSTEM_PROMPT
        user_prompt = PURCHASE_USER_PROMPT.replace("{aux_text}", aux_text[:12000])
    else:
        system_prompt = SALES_SYSTEM_PROMPT
        user_prompt = SALES_USER_PROMPT.replace("{aux_text}", aux_text[:12000])

    content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
    for image_url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": image_url}})

    payload: dict[str, Any] = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": VLM_EXTRACTION_TEMPERATURE,
        "max_tokens": VLM_MAX_TOKENS,
        "stream": False,
    }
    if not simple_mode:
        # 兼容端点若不支持，会在400后自动退化重试。
        payload["response_format"] = {"type": "json_object"}
        payload["enable_thinking"] = False
    return payload


def call_vlm(kind: str, aux_text: str, image_urls: list[str]) -> dict[str, Any]:
    if not DASHSCOPE_API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY为空")
    url = DASHSCOPE_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    session = get_session()
    last_error: Optional[Exception] = None

    for request_attempt in range(1, VLM_REQUEST_RETRIES + 1):
        for simple_mode in (False, True):
            payload = build_vlm_payload(kind, aux_text, image_urls, simple_mode=simple_mode)
            try:
                response = session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=(VLM_CONNECT_TIMEOUT_SECONDS, VLM_READ_TIMEOUT_SECONDS),
                )
                if response.status_code == 200:
                    data = response.json()
                    content = extract_message_content(data)
                    if not content:
                        raise RuntimeError(f"模型content为空: {json.dumps(data, ensure_ascii=False)[:1200]}")
                    return parse_json_object(content)

                # 第一次带 response_format/enable_thinking 请求若400，立即用兼容模式再试。
                if response.status_code == 400 and not simple_mode:
                    continue
                if response.status_code in (408, 409, 425, 429) or response.status_code >= 500:
                    raise RuntimeError(
                        f"VLM临时错误 HTTP {response.status_code}: {response.text[:800]}"
                    )
                raise RuntimeError(
                    f"VLM请求失败 HTTP {response.status_code}: {response.text[:1000]}"
                )
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                # simple_mode失败后才进入下一次退避重试。
                if not simple_mode:
                    continue
        if request_attempt < VLM_REQUEST_RETRIES:
            sleep_seconds = min(12.0, 1.8 ** request_attempt)
            time.sleep(sleep_seconds)

    raise RuntimeError(f"VLM连续请求失败: {last_error}")


def normalize_shipping_details(value: Any) -> list[ShippingDetail]:
    details: list[ShippingDetail] = []
    if not isinstance(value, list):
        return details
    for item in value:
        if not isinstance(item, dict):
            continue
        spec = str(item.get("spec") or "").strip()
        weight = to_float(item.get("theoretical_weight"))
        if spec or weight is not None:
            details.append(ShippingDetail(spec=spec, theoretical_weight=weight))
    return details


def vlm_dict_to_extraction(path: Path, kind: str, value: dict[str, Any]) -> PdfExtraction:
    event_datetime = str(value.get("event_datetime") or "").strip()
    result = PdfExtraction(
        path=path,
        kind=kind,
        car_no=str(value.get("car_no") or "").strip(),
        event_datetime=event_datetime,
        event_date=event_datetime[:10] if re.match(r"^20\d{2}-\d{2}-\d{2}", event_datetime) else "",
        goods_name=str(value.get("goods_name") or "").strip(),
        receiver=str(value.get("receiver") or "").strip(),
        destination=str(value.get("destination") or "").strip(),
        destination_raw=str(value.get("destination_raw") or "").strip(),
        original_quantity=to_float(value.get("original_quantity")),
        net_weight=to_float(value.get("net_weight")),
        shipping_details=normalize_shipping_details(value.get("shipping_details")),
        raw_remark=str(value.get("raw_remark") or "").strip(),
        confidence=float(to_float(value.get("confidence")) or 0.0),
        warnings=[str(x) for x in (value.get("warnings") or []) if str(x).strip()],
        extraction_method="vlm",
    )
    return result


def merge_local_and_vlm(local: PdfExtraction, visual: PdfExtraction) -> PdfExtraction:
    """按字段质量合并文本层与视觉结果；销售地址使用视觉清洗值并保留原文。"""
    merged = PdfExtraction(path=local.path, kind=local.kind, extraction_method="vlm_fallback")
    merged.car_no = local.car_no or visual.car_no
    merged.event_datetime = local.event_datetime or visual.event_datetime
    merged.event_date = merged.event_datetime[:10] if merged.event_datetime else visual.event_date
    merged.goods_name = choose_goods_name(local.goods_name, visual.goods_name)
    merged.net_weight = local.net_weight if local.net_weight is not None else visual.net_weight
    merged.confidence = visual.confidence
    merged.warnings = list(dict.fromkeys([*local.warnings, *visual.warnings]))

    if local.kind == "purchase":
        merged.receiver = local.receiver or visual.receiver
        merged.original_quantity = (
            local.original_quantity
            if local.original_quantity is not None
            else visual.original_quantity
        )
    else:
        local_destination_raw = normalize_pdf_field_value(local.destination_raw or local.destination)
        visual_destination_raw = normalize_pdf_field_value(visual.destination_raw)
        merged.destination_raw = (
            local_destination_raw
            if text_field_quality(local_destination_raw, "destination") >= 0
            else visual_destination_raw
        )
        if not merged.destination_raw:
            merged.destination_raw = visual_destination_raw
        merged.destination = choose_sales_destination(
            merged.destination_raw,
            visual.destination,
        )
        merged.raw_remark = local.raw_remark or visual.raw_remark
        merged.shipping_details = local.shipping_details or visual.shipping_details
    return merged


def extraction_to_cache_dict(result: PdfExtraction) -> dict[str, Any]:
    return {
        "prompt_version": PROMPT_VERSION,
        "model": MODEL_NAME,
        "kind": result.kind,
        "path_name": result.path.name,
        "car_no": result.car_no,
        "event_datetime": result.event_datetime,
        "event_date": result.event_date,
        "goods_name": result.goods_name,
        "receiver": result.receiver,
        "destination": result.destination,
        "destination_raw": result.destination_raw,
        "original_quantity": result.original_quantity,
        "net_weight": result.net_weight,
        "shipping_details": [dataclasses.asdict(x) for x in result.shipping_details],
        "raw_remark": result.raw_remark,
        "confidence": result.confidence,
        "warnings": result.warnings,
        "error": result.error,
        "extraction_method": result.extraction_method,
    }


def extraction_from_cache(path: Path, value: dict[str, Any]) -> PdfExtraction:
    return PdfExtraction(
        path=path,
        kind=str(value.get("kind") or ""),
        car_no=str(value.get("car_no") or ""),
        event_datetime=str(value.get("event_datetime") or ""),
        event_date=str(value.get("event_date") or ""),
        goods_name=str(value.get("goods_name") or ""),
        receiver=str(value.get("receiver") or ""),
        destination=str(value.get("destination") or ""),
        destination_raw=str(value.get("destination_raw") or value.get("destination") or ""),
        original_quantity=to_float(value.get("original_quantity")),
        net_weight=to_float(value.get("net_weight")),
        shipping_details=normalize_shipping_details(value.get("shipping_details")),
        raw_remark=str(value.get("raw_remark") or ""),
        confidence=float(to_float(value.get("confidence")) or 0.0),
        warnings=[str(x) for x in (value.get("warnings") or [])],
        error=str(value.get("error") or ""),
        extraction_method=str(value.get("extraction_method") or ""),
        from_cache=True,
    )


def process_one_pdf(ref: PdfRef, cache_dir: Path, force: bool = False, no_vlm: bool = False) -> PdfExtraction:
    cache_key = hashlib.sha256(
        f"{ref.sha256}|{ref.kind}|{MODEL_NAME}|{PROMPT_VERSION}".encode("utf-8")
    ).hexdigest()
    cache_path = cache_dir / f"{cache_key}.json"
    if cache_path.exists() and not force:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                cached.get("prompt_version") == PROMPT_VERSION
                and cached.get("model") == MODEL_NAME
                and not cached.get("error")
            ):
                return extraction_from_cache(ref.path, cached)
        except Exception:
            pass

    try:
        text = extract_embedded_text(ref.path)
        local_result = local_parse(ref.path, ref.kind, text)
        if no_vlm:
            result = local_result
            result.extraction_method = "text_debug"
            result.warnings.append("本次使用 --no-vlm，仅使用PDF内嵌文本解析")
        else:
            reliability_issues = embedded_text_reliability_issues(local_result, text)
            if not reliability_issues:
                result = local_result
                logging.info("PDF文本层字段完整，直接解析并跳过VLM：%s", ref.path.name)
            else:
                logging.info(
                    "PDF文本层需要VLM视觉兜底：%s；原因：%s",
                    ref.path.name,
                    "；".join(reliability_issues),
                )
                images = render_pdf_pages_as_data_urls(ref.path)
                value = call_vlm(ref.kind, text, images)
                visual_result = vlm_dict_to_extraction(ref.path, ref.kind, value)
                result = merge_local_and_vlm(local_result, visual_result)
        safe_json_dump(cache_path, extraction_to_cache_dict(result))
        return result
    except Exception as exc:
        result = PdfExtraction(path=ref.path, kind=ref.kind, error=str(exc))
        # 错误缓存仅用于诊断，不作为下次成功缓存使用。
        try:
            safe_json_dump(cache_path, extraction_to_cache_dict(result))
        except Exception:
            pass
        return result


# ============================================================================
# 行核验与写入
# ============================================================================

def build_row_tasks(
    wb,
    scanned_refs: dict[str, list[PdfRef]],
    source_index: SourceContextIndex,
) -> tuple[list[RowTask], list[dict[str, Any]], set[Path]]:
    row_tasks: list[RowTask] = []
    summary: list[dict[str, Any]] = []
    matched_paths: set[Path] = set()

    for sheet_name, kind, _directory in SHEET_DEFINITIONS:
        if sheet_name not in wb.sheetnames:
            summary.append({
                "type": "工作表缺失",
                "sheet": sheet_name,
                "row": "",
                "waybill": "",
                "car": "",
                "pdf": "",
                "result": "Excel中不存在该工作表",
            })
            continue
        ws = wb[sheet_name]
        layout = resolve_sheet_layout(ws, kind)
        exact_index, waybill_index = build_pdf_index(scanned_refs[sheet_name])

        for row in range(layout.header_row + 1, ws.max_row + 1):
            waybill = str(ws.cell(row, layout.columns["waybill"]).value or "").strip()
            if not normalize_waybill(waybill):
                continue
            car = str(ws.cell(row, layout.columns["car"]).value or "").strip()
            goods = str(ws.cell(row, layout.columns["goods"]).value or "").strip()
            destination = str(ws.cell(row, layout.columns["destination"]).value or "").strip()
            load_weight = to_float(ws.cell(row, layout.columns["load_weight"]).value)
            unload_weight = (
                to_float(ws.cell(row, layout.columns["unload_weight"]).value)
                if kind == "purchase"
                else None
            )
            pdfs, related_pdfs, notes = match_row_pdfs(
                waybill, car, kind, exact_index, waybill_index
            )
            for pdf in related_pdfs:
                matched_paths.add(pdf.path.resolve())
            row_sources = sources_for_row(
                source_index,
                sheet_name,
                row,
                waybill,
                car,
                related_pdfs,
            )
            source_detail = format_source_detail(
                sheet_name,
                row_sources,
                fallback_car=car,
            )
            row_tasks.append(
                RowTask(
                    sheet_name=sheet_name,
                    kind=kind,
                    row=row,
                    sequence=ws.cell(row, 1).value,
                    waybill=normalize_waybill(waybill),
                    car=normalize_car(car),
                    goods=goods,
                    destination=destination,
                    load_weight=load_weight,
                    unload_weight=unload_weight,
                    pdfs=pdfs,
                    related_pdfs=related_pdfs,
                    matching_notes=notes,
                    source_detail=source_detail,
                    source_keys=tuple(row_sources),
                )
            )
    return row_tasks, summary, matched_paths


def pdf_mapping_comment(groups: list[tuple[PdfRef, int]]) -> str:
    lines = ["PDF附件与真实明细行对应关系："]
    for index, (ref, line_count) in enumerate(groups, start=1):
        lines.append(f"附件{index}（{max(1, line_count)}条明细） -> {ref.path.name}")
    return "\n".join(lines)


def audit_purchase_row(task: RowTask, extractions: dict[Path, PdfExtraction]):
    issues = list(task.matching_notes)
    if not task.pdfs:
        issues.append("未找到唯一对应的采购磅票PDF")
        return {
            "remark": "核验异常：" + "；".join(issues),
            "date": "",
            "comment": "",
            "status": "异常",
        }

    ref = task.pdfs[0]
    ext = extractions.get(ref.path.resolve())
    if ext is None or ext.error:
        issues.append(f"PDF识别失败：{ext.error if ext else '无识别结果'}")
        return {
            "remark": "核验异常：" + "；".join(issues),
            "date": "",
            "comment": ref.path.name,
            "status": "异常",
        }

    if normalize_car(ext.car_no) and normalize_car(ext.car_no) != task.car:
        issues.append(f"车牌不一致（表格{task.car}，PDF{ext.car_no}）")

    if not task.goods:
        issues.append("表格货物名称为空，无法比对")
    elif not ext.goods_name:
        issues.append("PDF未识别到货名")
    elif not text_contains(task.goods, ext.goods_name):
        issues.append(f"货物名称不一致（表格“{task.goods}”，PDF“{ext.goods_name}”）")

    if not task.destination:
        issues.append("表格卸货地为空，无法比对")
    elif not ext.receiver:
        issues.append("PDF未识别到“收货”地点")
    elif not text_contains(task.destination, ext.receiver):
        issues.append(f"卸货地不一致（表格“{task.destination}”，PDF收货“{ext.receiver}”）")

    if task.load_weight is None:
        issues.append("表格装货重量为空，无法与原发数比对")
    elif ext.original_quantity is None:
        issues.append("PDF未识别到原发数")
    elif not nearly_equal(task.load_weight, ext.original_quantity):
        issues.append(
            f"装货重量与原发数不一致（表格{format_number(task.load_weight)}，PDF{format_number(ext.original_quantity)}）"
        )

    if task.unload_weight is None:
        issues.append("表格卸货重量为空，无法与PDF净重比对")
    elif ext.net_weight is None:
        issues.append("PDF未识别到净重")
    elif not nearly_equal(task.unload_weight, ext.net_weight):
        issues.append(
            f"卸货重量与PDF净重不一致（表格{format_number(task.unload_weight)}，PDF{format_number(ext.net_weight)}）"
        )

    if not ext.event_date:
        issues.append("PDF未识别到下半部分标准日期")
    if ext.warnings:
        issues.extend(f"识别提示：{warning}" for warning in ext.warnings)

    if issues:
        remark = "核验异常：" + "；".join(issues)
        status = "异常"
    else:
        remark = (
            "核验正常：PDF与表格一一对应；货物名称、卸货地、装货重量、卸货重量均匹配。"
        )
        status = "正常"
    return {
        "remark": remark,
        "date": ext.event_date,
        "comment": f"对应PDF：{ref.path.name}\n识别时间：{ext.event_datetime}",
        "status": status,
    }


def _normalized_source_pair(kind: Any, value: Any) -> tuple[str, str]:
    source_kind = str(kind or "").strip()
    source_value = str(value or "").strip()
    if source_kind == "selected_car":
        source_value = normalize_car(source_value)
    return source_kind, source_value


def _freight_source_matches(task: RowTask, source_kind: str, source_value: str) -> bool:
    expected = _normalized_source_pair(source_kind, source_value)
    same_kind = {
        _normalized_source_pair(kind, value)
        for kind, value in task.source_keys
        if str(kind or "").strip() == expected[0]
    }
    return not same_kind or expected in same_kind


def _freight_target_matches_task(task: RowTask, target: dict[str, Any]) -> bool:
    if str(target.get("sheet") or "").strip() != task.sheet_name:
        return False
    if normalize_waybill(target.get("waybill")) != task.waybill:
        return False
    target_car = normalize_car(target.get("car"))
    if target_car and task.car and target_car != task.car:
        return False
    return _freight_source_matches(
        task,
        str(target.get("source_kind") or ""),
        str(target.get("source_value") or ""),
    )


def _freight_record_matches_task(task: RowTask, record: FreightRecord) -> bool:
    if record.sheet != task.sheet_name or record.waybill != task.waybill:
        return False
    if record.car and task.car and record.car != task.car:
        return False
    return _freight_source_matches(task, record.source_kind, record.source_value)


def _freight_failure_matches_task(task: RowTask, failure: dict[str, Any]) -> bool:
    if str(failure.get("sheet") or "").strip() != task.sheet_name:
        return False
    if normalize_waybill(failure.get("waybill")) != task.waybill:
        return False
    failure_car = normalize_car(failure.get("car"))
    if failure_car and task.car and failure_car != task.car:
        return False
    return _freight_source_matches(
        task,
        str(failure.get("source_kind") or ""),
        str(failure.get("source_value") or ""),
    )


FREIGHT_PENDING_NOT_FOUND_ERROR = "运费确认列表未找到该销售运单"
FREIGHT_PENDING_REMARK = "运费确认暂未生成，本次未取得结算量和单价。"
FREIGHT_UNMATCHED_DETAIL_NOTE = "异常：运费确认记录暂未匹配到PDF规格"


def _freight_failure_is_pending(failure: dict[str, Any]) -> bool:
    """正常遍历后未找到运单属于业务延迟，不作为采集异常。"""
    return str(failure.get("error") or "").strip() == FREIGHT_PENDING_NOT_FOUND_ERROR


def _assign_freight_record(detail: SalesDetailRow, record: FreightRecord) -> None:
    detail.settlement_quantity = record.settlement_quantity
    detail.unit_price = record.unit_price
    detail.freight_record = record


def _append_unmatched_freight_detail(
    details: list[SalesDetailRow],
    record: FreightRecord,
) -> None:
    """Preserve an unmatched freight fact without guessing which PDF row owns it."""
    details.append(
        SalesDetailRow(
            pdf_first=False,
            spec=record.raw_spec,
            settlement_quantity=record.settlement_quantity,
            unit_price=record.unit_price,
            freight_record=record,
            freight_match_note=FREIGHT_UNMATCHED_DETAIL_NOTE,
        )
    )


def _freight_datetimes_compatible(detail_value: str, record_value: str) -> bool:
    """Accept the same date and a narrow cross-midnight window; missing time is neutral."""
    detail_date = freight_date_part(detail_value)
    record_date = freight_date_part(record_value)
    if not detail_date or not record_date:
        return True
    if detail_date == record_date:
        return True
    try:
        detail_time = dt.datetime.fromisoformat(str(detail_value).strip().replace(" ", "T"))
        record_time = dt.datetime.fromisoformat(str(record_value).strip().replace(" ", "T"))
    except ValueError:
        return False
    return abs(detail_time - record_time) <= dt.timedelta(hours=6)


def _freight_weight_matches(
    detail: SalesDetailRow, record: FreightRecord
) -> bool:
    """Use weight only as strict disambiguation evidence inside one waybill/spec group."""
    if detail.theoretical_weight is None:
        return False
    tolerance = max(
        FREIGHT_WEIGHT_ABSOLUTE_TOLERANCE,
        abs(detail.theoretical_weight) * FREIGHT_WEIGHT_RELATIVE_TOLERANCE,
    )
    return bool(
        nearly_equal(
            detail.theoretical_weight,
            record.settlement_quantity,
            tolerance=tolerance,
        )
        and _freight_datetimes_compatible(
            detail.match_event_datetime, record.outbound_datetime
        )
    )


def _assign_unique_freight_weight_matches(
    remaining_details: list[tuple[int, SalesDetailRow]],
    remaining_records: list[FreightRecord],
    used_records: set[str],
) -> tuple[list[tuple[int, SalesDetailRow]], list[FreightRecord]]:
    """Assign only mutually unique compatible weight matches; leave all ties unresolved."""
    while remaining_details and remaining_records:
        unique_pairs: list[tuple[tuple[int, SalesDetailRow], FreightRecord]] = []
        for indexed_detail in remaining_details:
            detail = indexed_detail[1]
            matching_records = [
                record
                for record in remaining_records
                if _freight_weight_matches(detail, record)
            ]
            if len(matching_records) != 1:
                continue
            record = matching_records[0]
            matching_details = [
                item
                for item in remaining_details
                if _freight_weight_matches(item[1], record)
            ]
            if len(matching_details) == 1:
                unique_pairs.append((indexed_detail, record))

        if not unique_pairs:
            break
        matched_detail_ids = {id(item[1]) for item, _record in unique_pairs}
        matched_record_keys = {record.record_key for _item, record in unique_pairs}
        for indexed_detail, record in unique_pairs:
            _assign_freight_record(indexed_detail[1], record)
            used_records.add(record.record_key)
        remaining_details = [
            item for item in remaining_details if id(item[1]) not in matched_detail_ids
        ]
        remaining_records = [
            record
            for record in remaining_records
            if record.record_key not in matched_record_keys
        ]
    return remaining_details, remaining_records


def _assign_unique_freight_date_matches(
    remaining_details: list[tuple[int, SalesDetailRow]],
    remaining_records: list[FreightRecord],
    used_records: set[str],
) -> tuple[list[tuple[int, SalesDetailRow]], list[FreightRecord]]:
    """Assign only reciprocal one-to-one calendar-date matches."""
    while remaining_details and remaining_records:
        unique_pairs: list[tuple[tuple[int, SalesDetailRow], FreightRecord]] = []
        for indexed_detail in remaining_details:
            detail_date = freight_date_part(indexed_detail[1].match_event_datetime)
            if not detail_date:
                continue
            matching_records = [
                record for record in remaining_records
                if freight_date_part(record.outbound_datetime) == detail_date
            ]
            if len(matching_records) != 1:
                continue
            record = matching_records[0]
            matching_details = [
                item for item in remaining_details
                if freight_date_part(item[1].match_event_datetime) == detail_date
            ]
            if len(matching_details) == 1:
                unique_pairs.append((indexed_detail, record))
        if not unique_pairs:
            break
        matched_detail_ids = {id(item[1]) for item, _record in unique_pairs}
        matched_record_keys = {record.record_key for _item, record in unique_pairs}
        for indexed_detail, record in unique_pairs:
            _assign_freight_record(indexed_detail[1], record)
            used_records.add(record.record_key)
        remaining_details = [
            item for item in remaining_details if id(item[1]) not in matched_detail_ids
        ]
        remaining_records = [
            record for record in remaining_records
            if record.record_key not in matched_record_keys
        ]
    return remaining_details, remaining_records


def _freight_records_functionally_equivalent(records: list[FreightRecord]) -> bool:
    if not records:
        return False
    first = records[0]
    return all(
        nearly_equal(item.settlement_quantity, first.settlement_quantity)
        and nearly_equal(item.unit_price, first.unit_price, tolerance=0.000001)
        for item in records[1:]
    )


def _freight_records_are_same_business_fact(
    left: FreightRecord,
    right: FreightRecord,
) -> bool:
    """Ignore record/card identity only when every displayed business value is equal."""
    return bool(
        left.normalized_spec == right.normalized_spec
        and left.outbound_datetime == right.outbound_datetime
        and nearly_equal(
            left.settlement_quantity,
            right.settlement_quantity,
            tolerance=0.000001,
        )
        and nearly_equal(left.unit_price, right.unit_price, tolerance=0.000001)
    )


def _collapse_surplus_equivalent_freight_records(
    indexed_details: list[tuple[int, SalesDetailRow]],
    records: list[FreightRecord],
) -> tuple[list[FreightRecord], list[FreightRecord]]:
    """Suppress exact duplicate facts only beyond this PDF spec group's row capacity.

    The database deliberately preserves physical instances. Reconciliation therefore
    keeps as many identical facts as there are PDF detail rows, so two genuinely
    identical cards can still fill two rows. With no PDF rows this helper is not used,
    and all extracted freight facts remain visible as unmatched abnormal rows.
    """
    capacity_per_fact = max(1, len(indexed_details))
    representatives: list[FreightRecord] = []
    retained_counts: list[int] = []
    retained: list[FreightRecord] = []
    suppressed: list[FreightRecord] = []

    for record in records:
        group_index = next(
            (
                index
                for index, representative in enumerate(representatives)
                if _freight_records_are_same_business_fact(record, representative)
            ),
            None,
        )
        if group_index is None:
            representatives.append(record)
            retained_counts.append(1)
            retained.append(record)
            continue
        if retained_counts[group_index] < capacity_per_fact:
            retained_counts[group_index] += 1
            retained.append(record)
        else:
            suppressed.append(record)
    return retained, suppressed


def match_freight_to_sales_details(
    task: RowTask,
    details: list[SalesDetailRow],
    freight_data: Optional[FreightData],
) -> tuple[list[str], set[str], bool, str]:
    """按运单、来源、车牌、规范规格、唯一重量和日期安全地一对一匹配。"""
    if freight_data is None:
        return [], set(), False, ""
    matching_targets = [
        target for target in freight_data.targets.values()
        if _freight_target_matches_task(task, target)
    ]
    records = sorted(
        (
            record for record in freight_data.records.values()
            if _freight_record_matches_task(task, record)
        ),
        key=lambda item: (item.outbound_datetime, item.record_key),
    )
    failures = [
        failure for failure in freight_data.failures.values()
        if _freight_failure_matches_task(task, failure)
    ]
    applicable = bool(matching_targets or records or failures)
    if not applicable:
        return [], set(), False, ""

    handled_keys = {record.record_key for record in records}
    pending_failures = [failure for failure in failures if _freight_failure_is_pending(failure)]
    hard_failures = [failure for failure in failures if not _freight_failure_is_pending(failure)]
    failure_issues = [
        "运费确认采集失败："
        + str(failure.get("error") or "未知错误")
        + (
            f"（规格“{failure.get('raw_spec')}”）"
            if str(failure.get("raw_spec") or "").strip() else ""
        )
        for failure in hard_failures
    ]
    if not records:
        if failure_issues:
            return failure_issues, handled_keys, True, ""
        pending_remark = (
            FREIGHT_PENDING_REMARK
            if pending_failures or matching_targets
            else ""
        )
        return [], handled_keys, True, pending_remark

    issues: list[str] = list(failure_issues)
    detail_groups: dict[str, list[tuple[int, SalesDetailRow]]] = defaultdict(list)
    record_groups: dict[str, list[FreightRecord]] = defaultdict(list)
    for index, detail in enumerate(details):
        normalized_spec = normalize_freight_spec(detail.spec)
        if normalized_spec:
            detail_groups[normalized_spec].append((index, detail))
        elif detail.pdf is not None:
            issues.append(
                f"运费确认无法匹配：附件{detail.pdf_index}第{detail.detail_index}组PDF规格为空"
            )
    for record in records:
        record_groups[record.normalized_spec].append(record)

    used_records: set[str] = set()
    for normalized_spec, indexed_details in detail_groups.items():
        spec_records = record_groups.get(normalized_spec, [])
        if not spec_records:
            for _index, detail in indexed_details:
                issues.append(
                    f"运费确认未匹配到规格“{detail.spec}”"
                    f"（附件{detail.pdf_index}第{detail.detail_index}组）"
                )
            continue

        remaining_details = list(indexed_details)
        remaining_records = list(spec_records)

        # Historical backfills and a current crawl can preserve different technical
        # identities for the same displayed card. Collapse only exact surplus facts
        # in the reconciliation candidate set; never mutate the fact database.
        remaining_records, duplicate_records = (
            _collapse_surplus_equivalent_freight_records(
                remaining_details,
                remaining_records,
            )
        )
        used_records.update(record.record_key for record in duplicate_records)
        if duplicate_records:
            logging.info(
                "Collapsed equivalent freight facts for reconciliation: "
                "%s / %s / %s (%d surplus)",
                task.sheet_name,
                task.waybill,
                normalized_spec,
                len(duplicate_records),
            )

        # 无论候选数量是否相等，先用互相唯一的理重/结算量关系消歧。
        # 仅允许同日或六小时内跨午夜；重量重复、缺失或非唯一时不猜测。
        remaining_details, remaining_records = _assign_unique_freight_weight_matches(
            remaining_details,
            remaining_records,
            used_records,
        )

        remaining_details, remaining_records = _assign_unique_freight_date_matches(
            remaining_details,
            remaining_records,
            used_records,
        )

        # 只剩唯一一对时可以安全配对；多条运费数值完全等价时，互换不影响表格结果。
        safe_equal_count = (
            remaining_details
            and len(remaining_details) == len(remaining_records)
            and (
                len(remaining_details) == 1
                or _freight_records_functionally_equivalent(remaining_records)
            )
        )
        if safe_equal_count:
            for (_index, detail), record in zip(remaining_details, remaining_records):
                _assign_freight_record(detail, record)
                used_records.add(record.record_key)
            remaining_details = []
            remaining_records = []

        if remaining_details:
            remaining_detail_count = len(remaining_details)
            remaining_record_count = len(remaining_records)
            for _index, detail in remaining_details:
                if remaining_records:
                    issues.append(
                        f"运费确认规格匹配歧义“{detail.spec}”"
                        f"（剩余PDF候选{remaining_detail_count}行，"
                        f"运费候选{remaining_record_count}条；"
                        f"附件{detail.pdf_index}第{detail.detail_index}组）"
                    )
                else:
                    issues.append(
                        f"运费确认未匹配到规格“{detail.spec}”"
                        f"（附件{detail.pdf_index}第{detail.detail_index}组）"
                    )

    for record in records:
        if record.record_key not in used_records:
            issues.append(
                f"运费确认记录未匹配到PDF规格“{record.raw_spec}”"
                f"（出库时间{record.outbound_datetime or '未知'}）"
            )
            _append_unmatched_freight_detail(details, record)
    return list(dict.fromkeys(issues)), handled_keys, True, ""


def audit_sales_row(
    task: RowTask,
    extractions: dict[Path, PdfExtraction],
    freight_data: Optional[FreightData] = None,
    handled_freight_keys: Optional[set[str]] = None,
):
    issues = list(task.matching_notes)
    if not task.pdfs:
        issues.append("未找到对应的销售磅票PDF")
        details: list[SalesDetailRow] = []
        (
            freight_issues,
            handled_keys,
            freight_applicable,
            freight_note,
        ) = match_freight_to_sales_details(task, details, freight_data)
        if handled_freight_keys is not None:
            handled_freight_keys.update(handled_keys)
        issues.extend(freight_issues)
        return {
            "remark": "核验异常：" + "；".join(dict.fromkeys(issues)),
            "details": details or [SalesDetailRow()],
            "goods_once": True,
            "destination_once": True,
            "comment": "",
            "status": "异常",
            "pdf_problem": True,
            "freight_applicable": freight_applicable,
            "freight_note": freight_note,
        }

    details: list[SalesDetailRow] = []
    mapping_groups: list[tuple[PdfRef, int]] = []
    valid_net_weights: list[float] = []
    goods_once = bool(task.goods)
    # 销售卸货地主值始终取表格中的 APP/地址模型结果；PDF 地址只用于
    # 多 PDF 之间判断是否需要在后续 PDF 首行补充不同地址。
    destination_once = True

    for index, ref in enumerate(task.pdfs, start=1):
        attachment_label = f"附件{index}（{ref.path.name}）"
        ext = extractions.get(ref.path.resolve())
        if ext is None or ext.error:
            issues.append(f"{attachment_label}识别失败：{ext.error if ext else '无识别结果'}")
            details.append(SalesDetailRow(pdf=ref, pdf_index=index, detail_index=1))
            mapping_groups.append((ref, 1))
            goods_once = False
            destination_once = False
            continue

        if normalize_car(ext.car_no) and normalize_car(ext.car_no) != task.car:
            issues.append(f"{attachment_label}车牌不一致（表格{task.car}，PDF{ext.car_no}）")

        if not task.goods:
            issues.append("表格货物名称为空，无法比对")
            goods_once = False
        elif not ext.goods_name:
            issues.append(f"{attachment_label}未识别到货名")
            goods_once = False
        elif not text_contains(task.goods, ext.goods_name):
            issues.append(
                f"{attachment_label}货物名称不一致（表格“{task.goods}”，PDF“{ext.goods_name}”）"
            )
            goods_once = False

        detail_count = max(1, len(ext.shipping_details))
        mapping_groups.append((ref, detail_count))
        if ext.net_weight is not None:
            valid_net_weights.append(ext.net_weight)
        if not ext.event_date:
            issues.append(f"{attachment_label}未识别到下半部分标准日期")
        if ext.net_weight is None:
            issues.append(f"{attachment_label}未识别到净重")

        if ext.shipping_details:
            for detail_index, detail in enumerate(ext.shipping_details):
                is_first = detail_index == 0
                details.append(
                    SalesDetailRow(
                        pdf=ref,
                        pdf_index=index,
                        detail_index=detail_index + 1,
                        pdf_first=is_first,
                        event_date=ext.event_date if is_first else "",
                        match_event_datetime=ext.event_datetime or ext.event_date,
                        net_weight=ext.net_weight if is_first else None,
                        goods_name=ext.goods_name if is_first else "",
                        destination=ext.destination if is_first else "",
                        spec=detail.spec,
                        theoretical_weight=detail.theoretical_weight,
                    )
                )
                if not detail.spec:
                    issues.append(f"{attachment_label}第{detail_index + 1}组配货明细未识别到规格")
                if detail.theoretical_weight is None:
                    issues.append(f"{attachment_label}第{detail_index + 1}组配货明细未识别到理重")
            # 理重只负责识别并写入表格，不再将理重合计与对应PDF磅票净重比较，
            # 也不基于这两个字段之间的关系给出正常或异常结论。
        else:
            details.append(
                SalesDetailRow(
                    pdf=ref,
                    pdf_index=index,
                    detail_index=1,
                    pdf_first=True,
                    event_date=ext.event_date,
                    match_event_datetime=ext.event_datetime or ext.event_date,
                    net_weight=ext.net_weight,
                    goods_name=ext.goods_name,
                    destination=ext.destination,
                )
            )
            issues.append(f"{attachment_label}未识别到配货明细/规格/理重")

        if ext.warnings:
            issues.extend(
                f"{attachment_label}识别提示：{warning}"
                for warning in ext.warnings
                if not is_sales_destination_only_warning(warning)
            )

    # 不比较 PDF 地址与表格卸货地。仅比较同一运单各 PDF 的地址：首个有效
    # PDF 地址作为内部基准且不写回；后续首次出现的不同地址保留在对应 PDF
    # 首条明细行。只有一个有效地址或所有 PDF 地址相同时，全部继续使用表格值。
    baseline_pdf_destination = ""
    seen_pdf_destinations: set[str] = set()
    has_additional_pdf_destination = False
    for detail in details:
        if not detail.pdf_first:
            detail.destination = ""
            continue
        normalized_destination = normalize_compare(detail.destination)
        if not normalized_destination:
            detail.destination = ""
            continue
        if not baseline_pdf_destination:
            baseline_pdf_destination = normalized_destination
            seen_pdf_destinations.add(normalized_destination)
            detail.destination = ""
            continue
        if normalized_destination in seen_pdf_destinations:
            detail.destination = ""
            continue
        seen_pdf_destinations.add(normalized_destination)
        has_additional_pdf_destination = True
    destination_once = not has_additional_pdf_destination

    if task.load_weight is None:
        issues.append("表格装货重量为空，无法与全部磅票净重合计比对")
    elif not valid_net_weights:
        issues.append("未取得任何有效磅票净重，无法与装货重量比对")
    else:
        net_sum = sum(valid_net_weights)
        if not nearly_equal(net_sum, task.load_weight):
            issues.append(
                f"磅票净重合计与装货重量不一致（净重合计{format_number(net_sum)}，表格装货重量{format_number(task.load_weight)}）"
            )

    pdf_problem = bool(issues)
    (
        freight_issues,
        handled_keys,
        freight_applicable,
        freight_note,
    ) = match_freight_to_sales_details(task, details, freight_data)
    if handled_freight_keys is not None:
        handled_freight_keys.update(handled_keys)
    issues.extend(freight_issues)

    if issues:
        remark = "核验异常：" + "；".join(dict.fromkeys(issues))
        status = "异常"
    else:
        net_sum = sum(valid_net_weights)
        remark = (
            f"核验正常：共{len(task.pdfs)}份PDF；货物名称匹配；卸货地以表格AI输出地址为准；磅票净重合计"
            f"{format_number(net_sum)}与装货重量一致。理重仅提取展示，不参与净重一致性判定。"
        )
        if freight_applicable and not freight_note:
            remark += "运费确认结算量、单价已按运单和规格匹配。"
        status = "正常"

    return {
        "remark": remark,
        "details": details or [SalesDetailRow()],
        "goods_once": goods_once,
        "destination_once": destination_once,
        "comment": pdf_mapping_comment(mapping_groups),
        "status": status,
        "pdf_problem": pdf_problem,
        "freight_applicable": freight_applicable,
        "freight_note": freight_note,
    }


def enrich_abnormal_remark(task: RowTask, remark: str, *, include_pdfs: bool = True) -> str:
    details = [str(remark or "").strip(), f"异常来源：{task.source_detail}"]
    pdf_names = list(dict.fromkeys(ref.path.name for ref in task.related_pdfs))
    if include_pdfs and pdf_names:
        details.append("关联PDF：" + "、".join(pdf_names))
    return "；".join(part for part in details if part)


def sequence_is_even(value: Any) -> bool:
    try:
        return int(float(value)) % 2 == 0
    except (TypeError, ValueError):
        return False


def purchase_settlement_formula(layout: SheetLayout, row: int) -> str:
    load_col = get_column_letter(layout.columns["load_weight"])
    unload_col = get_column_letter(layout.columns["unload_weight"])
    return (
        f'=IF(COUNT({load_col}{row}:{unload_col}{row})<2,"",'
        f'MIN({load_col}{row},{unload_col}{row}))'
    )


def style_purchase_row(ws, row: int, layout: SheetLayout, sequence: Any) -> None:
    fill = STYLE_ALT_FILL if sequence_is_even(sequence) else PatternFill()
    border = Border(
        left=STYLE_THIN_SIDE,
        right=STYLE_THIN_SIDE,
        top=STYLE_THIN_SIDE,
        bottom=STYLE_THIN_SIDE,
    )
    for col in range(1, ws.max_column + 1):
        cell = ws.cell(row, col)
        header = str(ws.cell(layout.header_row, col).value or "").strip()
        cell.border = border
        cell.fill = fill
        if header in NUMERIC_HEADERS or cell.data_type == "f":
            cell.number_format = EXCEL_NUMBER_FORMAT
            cell.alignment = Alignment(horizontal="right", vertical="center")
        elif col == 1:
            cell.alignment = Alignment(horizontal="center", vertical="center")
        else:
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = max(22, ws.row_dimensions[row].height or 0)


def sales_detail_comment(detail: SalesDetailRow) -> str:
    parts: list[str] = []
    if detail.freight_match_note:
        parts.append(detail.freight_match_note)
    if detail.pdf is not None:
        parts.extend([f"对应PDF：{detail.pdf.path.name}", f"附件序号：{detail.pdf_index}"])
        if detail.detail_index:
            parts.append(f"PDF内明细序号：{detail.detail_index}")
    if detail.freight_record is not None:
        parts.extend([
            f"运费确认出库时间：{detail.freight_record.outbound_datetime}",
            f"运费确认规格：{detail.freight_record.raw_spec}",
        ])
    return "\n".join(parts)


def style_sales_group(
    ws,
    layout: SheetLayout,
    start_row: int,
    end_row: int,
    sequence: Any,
    pdf_start_rows: set[int],
    goods_once: bool,
    destination_once: bool,
) -> None:
    group_cols = {
        1,
        layout.columns["waybill"],
        layout.columns["car"],
        layout.columns["load_weight"],
        *range(layout.columns["unit_price"] + 1, ws.max_column + 1),
    }
    if goods_once:
        group_cols.add(layout.columns["goods"])
    if destination_once:
        group_cols.add(layout.columns["destination"])

    pdf_cols = {layout.columns["date"], layout.columns["ticket_net"]}
    if not goods_once:
        pdf_cols.add(layout.columns["goods"])
    if not destination_once:
        pdf_cols.add(layout.columns["destination"])

    detail_cols = {
        layout.columns["spec"],
        layout.columns["theoretical"],
        layout.columns["settlement_quantity"],
        layout.columns["unit_price"],
    }
    base_fill = STYLE_ALT_FILL if sequence_is_even(sequence) else PatternFill()

    for row in range(start_row, end_row + 1):
        is_first = row == start_row
        is_last = row == end_row
        is_pdf_start = row in pdf_start_rows
        for col in range(1, ws.max_column + 1):
            cell = ws.cell(row, col)
            if is_first:
                top_side = STYLE_MEDIUM_SIDE
            elif col in group_cols:
                top_side = STYLE_NO_SIDE
            elif col in pdf_cols:
                top_side = STYLE_PDF_SIDE if is_pdf_start else STYLE_NO_SIDE
            else:
                top_side = STYLE_PDF_SIDE if is_pdf_start and col in detail_cols else STYLE_THIN_SIDE

            if is_last:
                bottom_side = STYLE_MEDIUM_SIDE
            elif col in group_cols or col in pdf_cols:
                bottom_side = STYLE_NO_SIDE
            else:
                bottom_side = STYLE_THIN_SIDE

            cell.border = Border(
                left=STYLE_THIN_SIDE,
                right=STYLE_THIN_SIDE,
                top=top_side,
                bottom=bottom_side,
            )
            if is_pdf_start and not is_first and col in (pdf_cols | detail_cols):
                cell.fill = STYLE_PDF_FILL
            else:
                cell.fill = base_fill

            header = str(ws.cell(layout.header_row, col).value or "").strip()
            if col == layout.columns["unit_price"]:
                cell.number_format = '#,##0.00'
                cell.alignment = Alignment(horizontal="right", vertical="center")
            elif header in NUMERIC_HEADERS or cell.data_type == "f":
                cell.number_format = EXCEL_NUMBER_FORMAT
                cell.alignment = Alignment(horizontal="right", vertical="center")
            elif col in {1, layout.columns["date"]}:
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

        adjust_row_height(ws, row, range(1, ws.max_column + 1))


def append_task_summary(
    summary: list[dict[str, Any]],
    task: RowTask,
    result: dict[str, Any],
    start_row: int,
    end_row: int,
) -> None:
    is_abnormal = result["status"] != "正常"
    summary_refs = (
        task.related_pdfs
        if is_abnormal and result.get("pdf_problem", True)
        else (task.pdfs if not is_abnormal else [])
    )
    row_label: Any = start_row if start_row == end_row else f"{start_row}-{end_row}"
    summary.append({
        "type": "行核验",
        "sheet": task.sheet_name,
        "sequence": task.sequence,
        "row": row_label,
        "waybill": task.waybill,
        "car": task.car,
        "source": task.source_detail if is_abnormal else "",
        "pdf": "\n".join(dict.fromkeys(ref.path.name for ref in summary_refs)),
        "pdf_paths": [str(ref.path) for ref in summary_refs] if is_abnormal else [],
        "result": result["status"] + "：" + result["remark"],
    })


def apply_purchase_results(
    wb,
    tasks: list[RowTask],
    extractions: dict[Path, PdfExtraction],
    summary: list[dict[str, Any]],
) -> None:
    layout_cache: dict[str, SheetLayout] = {}
    for task in tasks:
        ws = wb[task.sheet_name]
        layout = layout_cache.setdefault(task.sheet_name, resolve_sheet_layout(ws, "purchase"))
        result = audit_purchase_row(task, extractions)
        is_abnormal = result["status"] != "正常"
        if is_abnormal:
            result["remark"] = enrich_abnormal_remark(task, result["remark"])

        if task.load_weight is not None:
            set_cell_value(ws, task.row, layout.columns["load_weight"], task.load_weight)
        else:
            ws.cell(task.row, layout.columns["load_weight"]).value = None
        if task.unload_weight is not None:
            set_cell_value(ws, task.row, layout.columns["unload_weight"], task.unload_weight)
        else:
            ws.cell(task.row, layout.columns["unload_weight"]).value = None

        set_cell_value(
            ws,
            task.row,
            layout.columns["settlement_weight"],
            purchase_settlement_formula(layout, task.row),
        )
        if result["date"]:
            set_cell_value(ws, task.row, layout.columns["date"], result["date"], result["comment"])

        existing = ws.cell(task.row, layout.columns["remark1"]).value
        set_cell_value(
            ws,
            task.row,
            layout.columns["remark1"],
            append_existing_remark(existing, result["remark"]),
            result["comment"],
        )
        style_purchase_row(ws, task.row, layout, task.sequence)
        text_columns = {
            layout.columns["goods"],
            layout.columns["destination"],
            layout.columns["date"],
            layout.columns["remark1"],
        }
        if "carrier" in layout.columns:
            text_columns.add(layout.columns["carrier"])
        adjust_row_height(ws, task.row, text_columns)
        append_task_summary(summary, task, result, task.row, task.row)


def apply_sales_sheet_results(
    ws,
    tasks: list[RowTask],
    extractions: dict[Path, PdfExtraction],
    summary: list[dict[str, Any]],
    freight_data: Optional[FreightData] = None,
    handled_freight_keys: Optional[set[str]] = None,
) -> None:
    if not tasks:
        return
    layout = resolve_sheet_layout(ws, "sales")
    max_col = ws.max_column
    prepared: list[dict[str, Any]] = []

    for task in sorted(tasks, key=lambda item: item.row):
        result = audit_sales_row(
            task,
            extractions,
            freight_data=freight_data,
            handled_freight_keys=handled_freight_keys,
        )
        prepared.append({
            "task": task,
            "result": result,
            "values": [ws.cell(task.row, col).value for col in range(1, max_col + 1)],
            "comments": [copy(ws.cell(task.row, col).comment) for col in range(1, max_col + 1)],
        })

    for item in reversed(prepared):
        extra_rows = max(0, len(item["result"]["details"]) - 1)
        if extra_rows:
            ws.insert_rows(item["task"].row + 1, extra_rows)

    offset = 0
    for item in prepared:
        task: RowTask = item["task"]
        result: dict[str, Any] = item["result"]
        details: list[SalesDetailRow] = result["details"]
        start_row = task.row + offset
        end_row = start_row + len(details) - 1
        offset += len(details) - 1

        for row in range(start_row, end_row + 1):
            for col in range(1, max_col + 1):
                ws.cell(row, col).value = None
                ws.cell(row, col).comment = None

        for col, value in enumerate(item["values"], start=1):
            ws.cell(start_row, col).value = value
        for col, comment in enumerate(item["comments"], start=1):
            ws.cell(start_row, col).comment = copy(comment)

        set_cell_value(ws, start_row, 1, task.sequence)
        set_cell_value(ws, start_row, layout.columns["waybill"], task.waybill)
        set_cell_value(ws, start_row, layout.columns["car"], task.car)
        if task.load_weight is not None:
            set_cell_value(ws, start_row, layout.columns["load_weight"], task.load_weight)
        else:
            ws.cell(start_row, layout.columns["load_weight"]).value = None

        original_settlement = to_float(item["values"][layout.columns["settlement_quantity"] - 1])
        original_unit_price = to_float(item["values"][layout.columns["unit_price"] - 1])
        for row in range(start_row, end_row + 1):
            ws.cell(row, layout.columns["settlement_quantity"]).value = None
            ws.cell(row, layout.columns["unit_price"]).value = None
        if not result.get("freight_applicable") and original_settlement is not None:
            set_cell_value(
                ws,
                start_row,
                layout.columns["settlement_quantity"],
                original_settlement,
            )
        if not result.get("freight_applicable") and original_unit_price is not None:
            set_cell_value(ws, start_row, layout.columns["unit_price"], original_unit_price)

        pdf_start_rows: set[int] = set()
        for detail_offset, detail in enumerate(details):
            row = start_row + detail_offset
            comment = sales_detail_comment(detail)
            if detail.pdf_first:
                pdf_start_rows.add(row)
            set_cell_value(ws, row, layout.columns["date"], detail.event_date, comment)
            set_cell_value(ws, row, layout.columns["ticket_net"], detail.net_weight, comment)
            set_cell_value(ws, row, layout.columns["spec"], detail.spec, comment)
            set_cell_value(
                ws,
                row,
                layout.columns["theoretical"],
                detail.theoretical_weight,
                comment,
            )
            if detail.settlement_quantity is not None:
                set_cell_value(
                    ws,
                    row,
                    layout.columns["settlement_quantity"],
                    detail.settlement_quantity,
                    comment,
                )
                ws.cell(row, layout.columns["settlement_quantity"]).number_format = '#,##0.###'
            if detail.unit_price is not None:
                set_cell_value(
                    ws,
                    row,
                    layout.columns["unit_price"],
                    detail.unit_price,
                    comment,
                )
                ws.cell(row, layout.columns["unit_price"]).number_format = '#,##0.00'

            if result["goods_once"]:
                goods_value = task.goods if detail_offset == 0 else ""
            else:
                goods_value = detail.goods_name if detail.pdf_first else ""
            goods_comment = comment
            if detail.pdf_first and task.goods:
                goods_comment = "\n".join(part for part in [comment, f"APP货物名称：{task.goods}"] if part)
            set_cell_value(ws, row, layout.columns["goods"], goods_value, goods_comment)

            if detail_offset == 0:
                destination_value = task.destination
            elif result["destination_once"]:
                destination_value = ""
            else:
                destination_value = detail.destination if detail.pdf_first else ""
            destination_comment = comment
            if detail.pdf_first and task.destination:
                destination_comment = "\n".join(
                    part for part in [comment, f"APP卸货地：{task.destination}"] if part
                )
            set_cell_value(
                ws,
                row,
                layout.columns["destination"],
                destination_value,
                destination_comment,
            )

        is_abnormal = result["status"] != "正常"
        if is_abnormal:
            result["remark"] = enrich_abnormal_remark(
                task,
                result["remark"],
                include_pdfs=result.get("pdf_problem", True),
            )
        existing_remark = item["values"][layout.columns["remark1"] - 1]
        business_remark = result["remark"]
        freight_note = str(result.get("freight_note") or "").strip()
        if freight_note:
            separator = "" if business_remark.endswith(("。", "；", ";", "\n")) else "；"
            business_remark = f"{business_remark}{separator}{freight_note}"
        set_cell_value(
            ws,
            start_row,
            layout.columns["remark1"],
            append_existing_remark(existing_remark, business_remark),
            result["comment"],
        )

        style_sales_group(
            ws,
            layout,
            start_row,
            end_row,
            task.sequence,
            pdf_start_rows,
            result["goods_once"],
            result["destination_once"],
        )
        append_task_summary(summary, task, result, start_row, end_row)

    ws.auto_filter.ref = f"A{layout.header_row}:{get_column_letter(ws.max_column)}{ws.max_row}"


def apply_results_to_workbook(
    wb,
    row_tasks: list[RowTask],
    extractions: dict[Path, PdfExtraction],
    summary: list[dict[str, Any]],
    freight_data: Optional[FreightData] = None,
) -> set[str]:
    handled_freight_keys: set[str] = set()
    purchase_tasks = [task for task in row_tasks if task.kind == "purchase"]
    apply_purchase_results(wb, purchase_tasks, extractions, summary)

    for sheet_name, kind, _directory in SHEET_DEFINITIONS:
        if kind != "sales" or sheet_name not in wb.sheetnames:
            continue
        sheet_tasks = [task for task in row_tasks if task.sheet_name == sheet_name]
        apply_sales_sheet_results(
            wb[sheet_name],
            sheet_tasks,
            extractions,
            summary,
            freight_data=freight_data,
            handled_freight_keys=handled_freight_keys,
        )

    for sheet_name, kind, _directory in SHEET_DEFINITIONS:
        if sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]
        layout = resolve_sheet_layout(ws, kind)
        ws.auto_filter.ref = (
            f"A{layout.header_row}:{get_column_letter(ws.max_column)}{max(layout.header_row, ws.max_row)}"
        )
    set_recalculation_mode(wb)
    return handled_freight_keys


def prepare_problem_pdf_attachments(rows: list[dict[str, Any]]) -> list[EmailPdfAttachment]:
    """为异常 PDF 去重编号，并把每条异常与实际邮件附件名双向对应。"""
    attachments: list[EmailPdfAttachment] = []
    attachment_by_path: dict[str, EmailPdfAttachment] = {}

    for item in rows:
        aliases: list[str] = []
        unavailable: list[str] = []
        raw_paths = item.get("pdf_paths") or []
        if isinstance(raw_paths, (str, Path)):
            raw_paths = [raw_paths]

        for raw_path in raw_paths:
            path = Path(str(raw_path))
            key = canonical_path_key(path)
            attachment = attachment_by_path.get(key)
            if attachment is None:
                try:
                    if not path.is_file():
                        raise FileNotFoundError("文件不存在")
                    content = path.read_bytes()
                except Exception as exc:
                    unavailable.append(f"{path.name or path}（未附加：{exc}）")
                    continue

                safe_original_name = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", path.name)
                email_name = f"异常PDF_{len(attachments) + 1:03d}_{safe_original_name}"
                attachment = EmailPdfAttachment(path=path, filename=email_name, content=content)
                attachment_by_path[key] = attachment
                attachments.append(attachment)

            if attachment.filename not in aliases:
                aliases.append(attachment.filename)

        item["email_pdf"] = "\n".join([*aliases, *unavailable])

    return attachments


def write_summary_sheet(wb, rows: list[dict[str, Any]]) -> None:
    sheet_name = "核验汇总"
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    headers = [
        "类型", "子表", "序号", "Excel行/行范围", "运单号", "车牌号", "异常来源",
        "关联PDF（原文件名）", "邮件PDF附件名", "核验结果",
    ]
    ws.append(headers)
    for item in rows:
        ws.append([
            item.get("type", ""),
            item.get("sheet", ""),
            item.get("sequence", ""),
            item.get("row", ""),
            item.get("waybill", ""),
            item.get("car", ""),
            item.get("source", ""),
            item.get("pdf", ""),
            item.get("email_pdf", ""),
            item.get("result", item.get("issue", "")),
        ])
    for cell in ws[1]:
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.fill = STYLE_SUMMARY_HEADER_FILL
        cell.font = STYLE_SUMMARY_HEADER_FONT
        cell.border = Border(
            left=STYLE_THIN_SIDE,
            right=STYLE_THIN_SIDE,
            top=STYLE_THIN_SIDE,
            bottom=STYLE_THIN_SIDE,
        )
    widths = {
        "A": 14, "B": 22, "C": 10, "D": 16, "E": 30, "F": 16,
        "G": 34, "H": 55, "I": 60, "J": 90,
    }
    for letter, width in widths.items():
        ws.column_dimensions[letter].width = width
    for row in range(2, ws.max_row + 1):
        result_text = str(ws.cell(row, 10).value or "")
        for col in range(1, 11):
            cell = ws.cell(row, col)
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            cell.border = Border(
                left=STYLE_THIN_SIDE,
                right=STYLE_THIN_SIDE,
                top=STYLE_THIN_SIDE,
                bottom=STYLE_THIN_SIDE,
            )
            if not result_text.startswith("正常："):
                cell.fill = STYLE_SUMMARY_ALERT_FILL
        adjust_row_height(ws, row, range(1, 11))
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:J{ws.max_row}"


def resolve_report_period() -> tuple[str, str]:
    """优先使用命令行时间段；缺失时从本次唯一Excel文件名中恢复。"""
    start = REPORT_START_DATE.strip()
    end = REPORT_END_DATE.strip()
    if not start or not end:
        match = re.search(
            r"(20\d{2}-\d{2}-\d{2})[_-]至[_-](20\d{2}-\d{2}-\d{2})",
            EXCEL_INPUT_PATH.stem,
        )
        if match:
            start = start or match.group(1)
            end = end or match.group(2)
    if start and not end:
        end = start
    if end and not start:
        start = end
    return start, end


def report_period_text() -> str:
    start, end = resolve_report_period()
    if start and end:
        return start if start == end else f"{start} 至 {end}"
    return "本次指定时间段"


def sales_report_period_text() -> str:
    start = REPORT_SALES_START_DATE.strip()
    end = REPORT_SALES_END_DATE.strip()
    if start and end:
        return start if start == end else f"{start} 至 {end}"
    return "锚点日前9个自然日（不含当天）"


def build_email_subject() -> str:
    base_subject = (SMTP_SUBJECT or "敬业钢铁运输明细").strip()
    return (
        f"{base_subject}｜采购{report_period_text()}｜"
        f"销售{sales_report_period_text()}｜德创数字AI对账助手"
    )


def summarize_audit_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    row_items = [item for item in rows if item.get("type") == "行核验"]
    normal_items = [
        item for item in row_items
        if str(item.get("result", "")).startswith("正常：")
    ]
    abnormal_items = [
        item for item in row_items
        if not str(item.get("result", "")).startswith("正常：")
    ]
    supporting_issues = [item for item in rows if item.get("type") != "行核验"]

    sheet_stats: dict[str, dict[str, int]] = {}
    for item in row_items:
        sheet = str(item.get("sheet") or "未分类")
        stats = sheet_stats.setdefault(sheet, {"total": 0, "normal": 0, "abnormal": 0})
        stats["total"] += 1
        if str(item.get("result", "")).startswith("正常："):
            stats["normal"] += 1
        else:
            stats["abnormal"] += 1

    attention_items = [*abnormal_items, *supporting_issues]
    email_pdf_attachments = list(dict.fromkeys(
        name
        for item in rows
        for name in str(item.get("email_pdf") or "").splitlines()
        if name.startswith("异常PDF_")
    ))
    return {
        "row_total": len(row_items),
        "normal": len(normal_items),
        "abnormal": len(abnormal_items),
        "supporting_issues": len(supporting_issues),
        "attention_total": len(attention_items),
        "sheet_stats": sheet_stats,
        "attention_items": attention_items,
        "email_pdf_attachments": email_pdf_attachments,
        "email_pdf_attachment_count": len(email_pdf_attachments),
    }


def format_attention_item(item: dict[str, Any], max_length: int = 220) -> str:
    location = str(item.get("sheet") or "全局")
    sequence = item.get("sequence")
    if sequence not in (None, ""):
        location += f" / 序号{sequence}"
    row = item.get("row")
    if row not in (None, ""):
        location += f" / Excel第{row}行"
    waybill = str(item.get("waybill") or "").strip()
    if waybill:
        location += f" / {waybill}"
    context_parts: list[str] = []
    source = str(item.get("source") or "").strip()
    if source:
        context_parts.append(source)
    pdf_name_list = [
        line.strip() for line in str(item.get("pdf") or "").splitlines() if line.strip()
    ]
    pdf_names = "、".join(pdf_name_list)
    email_pdf = "、".join(
        line.strip() for line in str(item.get("email_pdf") or "").splitlines() if line.strip()
    )
    if email_pdf:
        context_parts.append("问题附件：" + email_pdf)
    elif pdf_names:
        context_parts.append("关联PDF：" + pdf_names)
    if context_parts:
        location += "｜" + "；".join(context_parts)
    result = str(item.get("result", item.get("issue", "")) or "待人工核查").strip()
    result = re.sub(r"\s+", " ", result)

    # 行核验结果已在末尾追加来源和关联PDF；邮件标题区已经展示，按尾部精确匹配去重。
    pdf_suffix = f"；关联PDF：{pdf_names}" if pdf_names else ""
    if pdf_suffix and result.endswith(pdf_suffix):
        result = result[: -len(pdf_suffix)].rstrip()
    source_suffix = f"；异常来源：{source}" if source else ""
    if source_suffix and result.endswith(source_suffix):
        result = result[: -len(source_suffix)].rstrip()

    # 只保留一次关键状态词；单PDF时附件映射已唯一，正文无需重复完整文件名。
    if result.startswith("异常：核验异常："):
        result = result[len("异常："):]
    if len(pdf_name_list) == 1:
        result = result.replace(f"附件1（{pdf_name_list[0]}）", "")
    if len(result) > max_length:
        result = result[: max_length - 1] + "…"
    return f"{location}：{result}"


def build_email_summary(output_path: Path, rows: list[dict[str, Any]]) -> str:
    """生成专业版纯文本邮件摘要，兼容不支持HTML的邮件客户端。"""
    stats = summarize_audit_rows(rows)
    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "德创数字AI对账助手｜敬业钢铁运输智能核验报告",
        "=" * 54,
        "",
        "您好：",
        "",
        "敬业钢铁运输明细自动对账与磅票智能核验已完成。",
        f"采购运单对账周期：{report_period_text()}（按用户指定周期对账）",
        f"销售运单对账周期：{sales_report_period_text()}（跳过任务锚点当天）",
        f"报告生成时间：{generated_at}",
        f"最终核验附件：{output_path.name}",
        "",
        "【本次核验概览】",
        f"• 纳入逐行核验：{stats['row_total']} 条",
        f"• 核验正常：{stats['normal']} 条",
        f"• 异常或待人工复核：{stats['abnormal']} 条",
        f"• PDF异常、未匹配文件或结构性提示：{stats['supporting_issues']} 条",
        f"• 随邮件附加的问题PDF：{stats['email_pdf_attachment_count']} 份",
        "",
        "【分业务表统计】",
    ]

    if stats["sheet_stats"]:
        for sheet, item in stats["sheet_stats"].items():
            lines.append(
                f"• {sheet}：共 {item['total']} 条，正常 {item['normal']} 条，"
                f"异常/待复核 {item['abnormal']} 条"
            )
    else:
        lines.append("• 本次未形成可逐行核验的数据。")

    lines.extend([
        "",
        "【核验口径说明】",
        "• 采购业务按运单号与车牌号匹配磅票，并核对货物、卸货地及装卸重量等关键字段。",
        "• 销售业务按运单号与车牌号匹配全部附件并逐份核对货物；卸货地以表格AI输出地址为准，仅在多个PDF地址彼此不同时补充后续不同地址。",
        "• 销售附件与配货明细展开为真实Excel行；规格与理重继续自动识别，理重合计不与磅票净重比较。",
    ])

    if stats["attention_items"]:
        lines.extend(["", "【重点关注摘要（最多展示10项）】"])
        for index, item in enumerate(stats["attention_items"][:10], start=1):
            lines.append(f"{index}. {format_attention_item(item)}")
        if len(stats["attention_items"]) > 10:
            lines.append(
                f"其余 {len(stats['attention_items']) - 10} 项请在附件“核验汇总”工作表中查看。"
            )
    else:
        lines.extend(["", "【综合结论】", "本次自动核验未发现需要人工关注的异常项。"])

    lines.extend([
        "",
        "【附件说明】",
        f"本邮件已附上最终 Excel 核验报告及 {stats['email_pdf_attachment_count']} 份问题PDF。",
        "问题PDF统一命名为“异常PDF_NNN_原文件名”；请在“核验汇总”的“邮件PDF附件名”列按编号对应异常记录，",
        "并结合各业务工作表中的异常来源、关联PDF及逐行核验说明进行人工复核。",
        "",
        "—— 德创数字AI对账助手",
        "本邮件由智能对账系统自动生成并发送，请勿直接回复。",
    ])
    return "\n".join(lines)


def build_email_html_summary(output_path: Path, rows: list[dict[str, Any]]) -> str:
    """生成适用于主流邮箱客户端的HTML版业务摘要。"""
    stats = summarize_audit_rows(rows)
    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sheet_rows = "".join(
        "<tr>"
        f"<td>{html.escape(sheet)}</td>"
        f"<td style='text-align:center'>{item['total']}</td>"
        f"<td style='text-align:center'>{item['normal']}</td>"
        f"<td style='text-align:center'>{item['abnormal']}</td>"
        "</tr>"
        for sheet, item in stats["sheet_stats"].items()
    ) or "<tr><td colspan='4' style='text-align:center;color:#64748b'>本次未形成可逐行核验的数据</td></tr>"

    if stats["attention_items"]:
        attention_html = "".join(
            f"<li style='margin:0 0 8px 0'>{html.escape(format_attention_item(item))}</li>"
            for item in stats["attention_items"][:10]
        )
        if len(stats["attention_items"]) > 10:
            attention_html += (
                f"<li>其余 {len(stats['attention_items']) - 10} 项请在附件“核验汇总”工作表中查看。</li>"
            )
        attention_section = f"""
        <div style="margin-top:24px">
          <div style="font-size:17px;font-weight:700;color:#0f172a;margin-bottom:10px">重点关注摘要</div>
          <ol style="margin:0;padding-left:22px;color:#334155;line-height:1.7">{attention_html}</ol>
        </div>
        """
    else:
        attention_section = """
        <div style="margin-top:24px;padding:16px 18px;background:#ecfdf5;border:1px solid #a7f3d0;border-radius:10px;color:#065f46">
          <strong>综合结论：</strong>本次自动核验未发现需要人工关注的异常项。
        </div>
        """

    return f"""<!doctype html>
<html lang="zh-CN">
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Microsoft YaHei','PingFang SC',Arial,sans-serif;color:#1e293b">
  <div style="max-width:780px;margin:24px auto;padding:0 12px">
    <div style="background:linear-gradient(135deg,#0f3d66,#176b87);padding:28px 32px;border-radius:16px 16px 0 0;color:#fff">
      <div style="font-size:13px;letter-spacing:1px;opacity:.88">DECHUANG DIGITAL · AI RECONCILIATION</div>
      <div style="font-size:26px;font-weight:700;margin-top:8px">敬业钢铁运输智能核验报告</div>
      <div style="font-size:15px;margin-top:10px;opacity:.92">采购运单对账周期：{html.escape(report_period_text())}（按用户指定周期对账）</div>
      <div style="font-size:15px;margin-top:5px;opacity:.92">销售运单对账周期：{html.escape(sales_report_period_text())}（跳过任务锚点当天）</div>
    </div>

    <div style="background:#fff;padding:30px 32px;border-radius:0 0 16px 16px;box-shadow:0 8px 24px rgba(15,23,42,.08)">
      <p style="margin-top:0;line-height:1.8">您好，敬业钢铁运输明细自动对账与磅票智能核验已完成。最终 Excel 报告及 {stats['email_pdf_attachment_count']} 份问题 PDF 已作为本邮件附件发送。</p>

      <table role="presentation" width="100%" cellspacing="10" cellpadding="0" style="margin:18px -10px 8px;width:calc(100% + 20px)">
        <tr>
          <td style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;padding:16px;text-align:center"><div style="font-size:12px;color:#475569">逐行核验</div><div style="font-size:25px;font-weight:700;color:#1d4ed8">{stats['row_total']}</div></td>
          <td style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:10px;padding:16px;text-align:center"><div style="font-size:12px;color:#475569">核验正常</div><div style="font-size:25px;font-weight:700;color:#047857">{stats['normal']}</div></td>
          <td style="background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:16px;text-align:center"><div style="font-size:12px;color:#475569">异常/待复核</div><div style="font-size:25px;font-weight:700;color:#c2410c">{stats['abnormal']}</div></td>
          <td style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:10px;padding:16px;text-align:center"><div style="font-size:12px;color:#475569">附加提示</div><div style="font-size:25px;font-weight:700;color:#475569">{stats['supporting_issues']}</div></td>
        </tr>
      </table>

      <div style="font-size:13px;color:#64748b;margin:12px 0 24px">
        报告生成时间：{generated_at}<br>
        Excel附件：{html.escape(output_path.name)}<br>
        问题PDF附件：{stats['email_pdf_attachment_count']} 份（命名规则：异常PDF_NNN_原文件名）
      </div>

      <div style="font-size:17px;font-weight:700;color:#0f172a;margin-bottom:10px">分业务表统计</div>
      <table width="100%" cellspacing="0" cellpadding="10" style="border-collapse:collapse;border:1px solid #e2e8f0;font-size:14px">
        <thead><tr style="background:#f8fafc"><th style="text-align:left;border-bottom:1px solid #e2e8f0">业务表</th><th style="border-bottom:1px solid #e2e8f0">总数</th><th style="border-bottom:1px solid #e2e8f0">正常</th><th style="border-bottom:1px solid #e2e8f0">异常/待复核</th></tr></thead>
        <tbody>{sheet_rows}</tbody>
      </table>

      <div style="margin-top:24px;padding:18px;background:#f8fafc;border-left:4px solid #176b87;border-radius:8px">
        <div style="font-weight:700;margin-bottom:8px">核验口径说明</div>
        <div style="font-size:14px;line-height:1.8;color:#475569">
          采购运单按用户指定周期 {html.escape(report_period_text())} 对账；销售运单按 {html.escape(sales_report_period_text())} 对账并跳过任务锚点当天。<br>
          采购业务核对货物、卸货地及装卸重量；销售业务逐份核对货物及磅票净重合计与表格装货重量，卸货地以表格AI输出地址为准，仅补充多个PDF之间的不同地址。<br>
          销售附件与配货明细展开为真实Excel行；规格与理重继续自动识别，但理重合计不与磅票净重比较。
        </div>
      </div>

      {attention_section}

      <div style="margin-top:26px;padding:18px;background:#eef6ff;border-radius:10px;color:#1e3a5f;line-height:1.8">
        <strong>附件为本次最重要的交付成果：</strong><br>
        请打开 Excel 附件并优先查看“核验汇总”工作表；按“邮件PDF附件名”列可将异常记录与编号问题PDF一一对应。各业务工作表中保留了原始明细、AI识别字段、异常来源及逐行核验说明。
      </div>

      <div style="margin-top:28px;padding-top:18px;border-top:1px solid #e2e8f0;color:#64748b;font-size:12px;line-height:1.7">
        德创数字AI对账助手自动生成并发送<br>
        本邮件为系统通知，请勿直接回复。
      </div>
    </div>
  </div>
</body>
</html>"""


def parse_email_recipients(value: str) -> list[str]:
    """解析一个或多个收件人；支持逗号、分号、中文标点或空格分隔。"""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("邮件收件人不能为空")

    normalized = re.sub(r"[；，\s]+", ",", raw.replace(";", ","))
    parsed = [address.strip() for _name, address in getaddresses([normalized]) if address.strip()]

    recipients: list[str] = []
    seen: set[str] = set()
    invalid: list[str] = []
    for address in parsed:
        if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", address):
            invalid.append(address)
            continue
        key = address.lower()
        if key not in seen:
            seen.add(key)
            recipients.append(address)

    if invalid:
        raise ValueError("存在无效的邮件收件人：" + "、".join(invalid))
    if not recipients:
        raise ValueError("未解析到有效的邮件收件人")
    return recipients


def send_excel_email(
    excel_path: Path,
    summary_text: str,
    html_text: str = "",
    pdf_attachments: Iterable[EmailPdfAttachment] = (),
) -> None:
    """通过 SMTP 发送专业版核验摘要、最终 Excel 和异常问题 PDF。"""
    if not excel_path.is_file():
        raise FileNotFoundError(f"Excel 文件不存在：{excel_path}")

    recipients = parse_email_recipients(SMTP_RECEIVER)

    message = EmailMessage()
    message["From"] = f"德创数字AI对账助手 <{SMTP_SENDER}>"
    message["To"] = ", ".join(recipients)
    message["Subject"] = build_email_subject()
    message.set_content(summary_text or "敬业钢铁运输明细见附件。")
    if html_text:
        message.add_alternative(html_text, subtype="html")

    content_type, _ = mimetypes.guess_type(str(excel_path))
    if content_type:
        maintype, subtype = content_type.split("/", 1)
    else:
        maintype = "application"
        subtype = "vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    with excel_path.open("rb") as file:
        message.add_attachment(
            file.read(),
            maintype=maintype,
            subtype=subtype,
            filename=excel_path.name,
        )

    for attachment in pdf_attachments:
        message.add_attachment(
            attachment.content,
            maintype="application",
            subtype="pdf",
            filename=attachment.filename,
        )

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as smtp:
        smtp.login(SMTP_SENDER, SMTP_AUTH_CODE)
        smtp.send_message(message, from_addr=SMTP_SENDER, to_addrs=recipients)


# ============================================================================
# 主流程
# ============================================================================

def output_path_for(input_path: Path, output_dir: Path) -> Path:
    return output_dir / f"{input_path.stem}_check{input_path.suffix}"


def run(force: bool = False, no_vlm: bool = False, max_workers: int = AUDIT_MAX_WORKERS) -> Path:
    EXCEL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    setup_logging(EXCEL_OUTPUT_DIR)

    if not EXCEL_INPUT_PATH.exists():
        raise FileNotFoundError(f"找不到输入Excel：{EXCEL_INPUT_PATH}")

    output_path = output_path_for(EXCEL_INPUT_PATH, EXCEL_OUTPUT_DIR)
    cache_dir = RESULT_CHECK_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    logging.info("STATUS:RESULT_CHECK_START")
    logging.info("读取Excel：%s", EXCEL_INPUT_PATH)
    wb = load_workbook(EXCEL_INPUT_PATH, data_only=False, keep_links=True)
    ensure_workbook_schema(wb)
    source_index = load_source_context(SOURCE_CONTEXT_PATH)
    logging.info("读取来源索引：%s", SOURCE_CONTEXT_PATH)
    freight_data = load_freight_data(FREIGHT_CONFIRMATION_PATH)
    logging.info(
        "读取运费确认索引：%s（目标%d条，成功记录%d条）",
        FREIGHT_CONFIRMATION_PATH,
        len(freight_data.targets),
        len(freight_data.records),
    )

    scanned_refs: dict[str, list[PdfRef]] = {}
    summary: list[dict[str, Any]] = []
    for error in freight_data.errors:
        summary.append({
            "type": "运费数据异常",
            "sheet": "",
            "row": "",
            "waybill": "",
            "car": "",
            "source": "",
            "pdf": "",
            "pdf_paths": [],
            "result": error,
        })
    all_valid_refs: list[tuple[str, PdfRef]] = []

    for sheet_name, kind, directory in SHEET_DEFINITIONS:
        refs, scan_issues = scan_pdf_directory(directory, kind)
        scanned_refs[sheet_name] = refs
        all_valid_refs.extend((sheet_name, ref) for ref in refs)
        for issue in scan_issues:
            issue_path = Path(str(issue.get("pdf", "")))
            issue_source = format_source_detail(
                sheet_name,
                sources_for_pdf(source_index, sheet_name, issue_path),
            )
            summary.append({
                "type": "PDF文件异常",
                "sheet": sheet_name,
                "row": "",
                "waybill": "",
                "car": "",
                "source": issue_source,
                "pdf": issue_path.name if issue_path.suffix.lower() == ".pdf" else str(issue_path),
                "pdf_paths": [str(issue_path)] if issue_path.suffix.lower() == ".pdf" else [],
                "result": issue.get("issue", ""),
            })
        logging.info("%s：扫描到有效PDF %d份，异常文件 %d份", sheet_name, len(refs), len(scan_issues))

    row_tasks, initial_summary, matched_paths = build_row_tasks(wb, scanned_refs, source_index)
    summary.extend(initial_summary)

    for sheet_name, ref in all_valid_refs:
        if ref.path.resolve() not in matched_paths:
            unmatched_source = format_source_detail(
                sheet_name,
                sources_for_pdf(source_index, sheet_name, ref.path),
                fallback_car=ref.car,
                fallback_origin="pdf_filename",
            )
            summary.append({
                "type": "未匹配PDF",
                "sheet": sheet_name,
                "row": "",
                "waybill": ref.waybill,
                "car": ref.car,
                "source": unmatched_source,
                "pdf": ref.path.name,
                "pdf_paths": [str(ref.path)],
                "result": "该PDF未匹配到Excel中的同运单号+同车牌行，请人工核查",
            })

    unique_refs: dict[Path, PdfRef] = {}
    for task in row_tasks:
        for ref in task.pdfs:
            unique_refs[ref.path.resolve()] = ref

    logging.info("需要识别的唯一PDF：%d份；Excel待核验行：%d行", len(unique_refs), len(row_tasks))
    extractions: dict[Path, PdfExtraction] = {}

    worker_count = max(1, min(max_workers, max(1, len(unique_refs))))
    if unique_refs:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="pdf-audit",
        ) as executor:
            future_map = {
                executor.submit(process_one_pdf, ref, cache_dir, force, no_vlm): path
                for path, ref in unique_refs.items()
            }
            completed = 0
            for future in concurrent.futures.as_completed(future_map):
                path = future_map[future]
                completed += 1
                try:
                    result = future.result()
                except Exception as exc:
                    result = PdfExtraction(path=path, kind=unique_refs[path].kind, error=str(exc))
                extractions[path] = result
                if result.error:
                    logging.error("[%d/%d] %s 识别失败：%s", completed, len(unique_refs), path.name, result.error)
                else:
                    if result.from_cache:
                        source = "缓存"
                    elif result.extraction_method == "vlm_fallback":
                        source = "VLM视觉兜底"
                    else:
                        source = "本地文本"
                    logging.info("[%d/%d] %s 识别完成（%s）", completed, len(unique_refs), path.name, source)

    # 所有线程结束后，由主线程一次性、串行写Excel，完全避免并发写冲突。
    handled_freight_keys = apply_results_to_workbook(
        wb,
        row_tasks,
        extractions,
        summary,
        freight_data=freight_data,
    )
    for record in freight_data.records.values():
        if record.record_key in handled_freight_keys:
            continue
        record_source = format_source_detail(
            record.sheet,
            [(record.source_kind, record.source_value)],
            fallback_car=record.car,
        )
        summary.append({
            "type": "未匹配运费记录",
            "sheet": record.sheet,
            "row": "",
            "waybill": record.waybill,
            "car": record.car,
            "source": record_source,
            "pdf": "",
            "pdf_paths": [],
            "result": (
                f"运费确认成功记录未匹配到Excel销售运单：规格“{record.raw_spec}”，"
                f"出库时间{record.outbound_datetime}，请人工核查"
            ),
        })
    problem_pdf_attachments = prepare_problem_pdf_attachments(summary)
    write_summary_sheet(wb, summary)

    temp_output = output_path.with_suffix(".tmp.xlsx")
    if temp_output.exists():
        temp_output.unlink()
    logging.info("保存核验结果：%s", output_path)
    wb.save(temp_output)
    wb.close()
    os.replace(temp_output, output_path)

    logging.info("处理完成。输出文件：%s", output_path)
    logging.info("核验汇总条目：%d", len(summary))
    email_summary = build_email_summary(output_path, summary)
    email_html = build_email_html_summary(output_path, summary)
    logging.info("邮件主题：%s", build_email_subject())
    logging.info("开始发送核验结果邮件：%s", output_path.name)
    try:
        send_excel_email(
            output_path,
            email_summary,
            email_html,
            pdf_attachments=problem_pdf_attachments,
        )
    except Exception as exc:
        logging.exception("核验结果文件已保存，但邮件发送失败：%s", exc)
        raise RuntimeError(
            f"核验结果已保存到：{output_path}，但邮件发送失败：{exc}"
        ) from exc
    logging.info("核验结果邮件发送成功")
    print(f"FINAL_EXCEL_PATH:{output_path}", flush=True)
    print(f"STATUS:EMAIL_SENT|TO:{SMTP_RECEIVER}", flush=True)
    print("STATUS:RESULT_CHECK_DONE", flush=True)
    print(f"✅ 核验结果邮件已发送：{output_path.name}", flush=True)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="核验敬业运输Excel与采购/销售磅票PDF")
    parser.add_argument("--base-dir", default=str(BASE_DIR), help="敬业对账数据根目录")
    parser.add_argument("--excel-input", default=str(EXCEL_INPUT_PATH), help="本次唯一初始Excel完整路径")
    parser.add_argument("--run-id", default=os.getenv("JINGYE_RUN_ID", ""), help="本次任务唯一标识，用于定位隔离后的PDF目录")
    parser.add_argument("--output-dir", default="", help="核验结果目录，默认 base-dir/excel_check")
    parser.add_argument("--cache-dir", default="", help="持久化识别缓存目录，默认 base-dir/result_check_cache")
    parser.add_argument(
        "--email-to",
        default=SMTP_RECEIVER,
        help="核验结果接收邮箱；多个邮箱可用逗号、分号或空格分隔",
    )
    parser.add_argument("--start", default=REPORT_START_DATE, help="本次对账开始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=REPORT_END_DATE, help="本次对账结束日期 YYYY-MM-DD")
    parser.add_argument(
        "--sales-start",
        default=REPORT_SALES_START_DATE,
        help="本次销售对账开始日期 YYYY-MM-DD",
    )
    parser.add_argument(
        "--sales-end",
        default=REPORT_SALES_END_DATE,
        help="本次销售对账结束日期 YYYY-MM-DD",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略已成功的PDF识别缓存，重新解析全部匹配PDF",
    )
    parser.add_argument(
        "--no-vlm",
        action="store_true",
        help="仅用于调试：不调用视觉模型，只解析PDF内嵌文本；扫描PDF将无法可靠识别",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=AUDIT_MAX_WORKERS,
        help=f"PDF识别并发线程数，默认{AUDIT_MAX_WORKERS}",
    )
    return parser.parse_args()


def configure_runtime(args: argparse.Namespace) -> None:
    global BASE_DIR, EXCEL_INPUT_PATH, EXCEL_OUTPUT_DIR, RESULT_CHECK_CACHE_DIR
    global SOURCE_CONTEXT_PATH, FREIGHT_CONFIRMATION_PATH
    global PDF_PURCHASE_HEBEI_DIR, PDF_PURCHASE_SHANGHAI_DIR
    global PDF_SALES_HEBEI_DIR, PDF_SALES_SHANGHAI_DIR, SHEET_DEFINITIONS
    global SMTP_RECEIVER, REPORT_START_DATE, REPORT_END_DATE
    global REPORT_SALES_START_DATE, REPORT_SALES_END_DATE

    BASE_DIR = Path(args.base_dir)
    EXCEL_INPUT_PATH = Path(args.excel_input)
    EXCEL_OUTPUT_DIR = Path(args.output_dir) if args.output_dir else BASE_DIR / "excel_check"
    RESULT_CHECK_CACHE_DIR = Path(args.cache_dir) if args.cache_dir else BASE_DIR / "result_check_cache"
    run_id = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(args.run_id or "")).strip("._")
    SOURCE_CONTEXT_PATH = BASE_DIR / "source_context" / (f"{run_id}.jsonl" if run_id else "legacy.jsonl")
    FREIGHT_CONFIRMATION_PATH = BASE_DIR / "freight_confirmation" / (
        f"{run_id}.jsonl" if run_id else "legacy.jsonl"
    )

    def pdf_dir(name: str) -> Path:
        root = BASE_DIR / name
        return root / run_id if run_id else root

    PDF_PURCHASE_HEBEI_DIR = pdf_dir("pdf_caigouhebei")
    PDF_PURCHASE_SHANGHAI_DIR = pdf_dir("pdf_caigoushanghai")
    PDF_SALES_HEBEI_DIR = pdf_dir("pdf_xiaoshouhebei")
    PDF_SALES_SHANGHAI_DIR = pdf_dir("pdf_xiaoshoushanghai")
    SMTP_RECEIVER = args.email_to
    REPORT_START_DATE = str(args.start or "").strip()
    REPORT_END_DATE = str(args.end or "").strip()
    REPORT_SALES_START_DATE = str(args.sales_start or "").strip()
    REPORT_SALES_END_DATE = str(args.sales_end or "").strip()
    SHEET_DEFINITIONS = (
        ("采购单-河北众沃", "purchase", PDF_PURCHASE_HEBEI_DIR),
        ("采购单-上海宾迪", "purchase", PDF_PURCHASE_SHANGHAI_DIR),
        ("销售单-河北众沃", "sales", PDF_SALES_HEBEI_DIR),
        ("销售单-上海宾迪", "sales", PDF_SALES_SHANGHAI_DIR),
    )


def main() -> int:
    args = parse_args()
    configure_runtime(args)
    try:
        output = run(
            force=args.force,
            no_vlm=args.no_vlm,
            max_workers=max(1, args.max_workers),
        )
        print(f"\n✅ 核验完成：{output}")
        return 0
    except Exception as exc:
        logging.exception("核验失败：%s", exc)
        print(f"\n❌ 核验失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
