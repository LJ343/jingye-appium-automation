# coding: utf-8
import argparse
import hashlib
import os
import re
import time
import json
import datetime
import subprocess
import shlex
import unicodedata
from pathlib import Path
from copy import copy
import requests
import openpyxl
from openpyxl.styles import Alignment, Border, PatternFill, Side
from openpyxl.utils import get_column_letter
from appium import webdriver
from appium.options.common.base import AppiumOptions
from appium.webdriver.client_config import AppiumClientConfig
import jingye_wireless as wireless_link
import jingye_appium_guard as appium_guard
import jingye_appium_ownership as appium_phone
from appium.webdriver.common.appiumby import AppiumBy
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions import interaction
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)

from freight_confirmation import (
    FREIGHT_RETRY_DAYS,
    annotate_card_occurrences,
    append_event as append_freight_event,
    date_part as freight_date_part,
    freight_card_key,
    freight_fact_key,
    freight_scan_past_target_date,
    freight_target_age_days,
    freight_target_within_retry_window,
    load_freight_data,
    make_freight_event,
    make_target_event,
    next_card_occurrence,
    normalize_car as normalize_freight_car,
    normalize_datetime as normalize_freight_datetime,
    normalize_spec as normalize_freight_spec,
    normalize_waybill as normalize_freight_waybill,
    parse_number as parse_freight_number,
    scope_key as freight_scope_key,
    waybill_date_part as freight_waybill_date_part,
)

# ================= 业务与文件传输配置区 =================
DEFAULT_BASE_DIR = r"D:\openclaw_data\data\jingyeapp"
DEFAULT_START_DATE = "2026-07-12"
DEFAULT_END_DATE = "2026-07-13"


def _valid_date(value):
    try:
        datetime.datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD：{value}") from exc


def _parse_runtime_args():
    parser = argparse.ArgumentParser(description="敬业 App 采购/销售运单抓取与磅单下载")
    parser.add_argument("--start", type=_valid_date, default=os.getenv("TARGET_START_DATE", DEFAULT_START_DATE))
    parser.add_argument("--end", type=_valid_date, default=os.getenv("TARGET_END_DATE", DEFAULT_END_DATE))
    parser.add_argument("--base-dir", default=os.getenv("APP_BASE_DIR", DEFAULT_BASE_DIR))
    parser.add_argument("--excel-path", default=os.getenv("JINGYE_EXCEL_PATH", ""))
    parser.add_argument("--run-id", default=os.getenv("JINGYE_RUN_ID", ""), help="本次任务唯一标识，用于隔离PDF目录")
    parser.add_argument("--appium-url", default=os.getenv("APPIUM_SERVER_URL", "http://127.0.0.1:4723"))
    parser.add_argument(
        '--anchor-date',
        type=_valid_date,
        default=os.getenv('JINGYE_ANCHOR_DATE') or None,
        help='本次运行冻结的上海自然日；未传时在进程启动时确定',
    )
    parser.add_argument(
        '--purchase-start',
        type=_valid_date,
        default=os.getenv('PURCHASE_START_DATE') or None,
    )
    parser.add_argument(
        '--purchase-end',
        type=_valid_date,
        default=os.getenv('PURCHASE_END_DATE') or None,
    )
    parser.add_argument(
        '--sales-start',
        type=_valid_date,
        default=os.getenv('SALES_START_DATE') or None,
    )
    parser.add_argument(
        '--sales-end',
        type=_valid_date,
        default=os.getenv('SALES_END_DATE') or None,
    )
    parser.add_argument(
        '--print-windows',
        action='store_true',
        help='只输出解析后的日期窗口，不创建文件或启动 Appium',
    )
    return parser.parse_args()


_RUNTIME_ARGS = _parse_runtime_args()
TARGET_START_DATE = _RUNTIME_ARGS.start
TARGET_END_DATE = _RUNTIME_ARGS.end
_SHANGHAI_TIMEZONE = datetime.timezone(datetime.timedelta(hours=8))
ANCHOR_DATE = _RUNTIME_ARGS.anchor_date or datetime.datetime.now(
    _SHANGHAI_TIMEZONE
).date().isoformat()
TARGET_START_DATE = _RUNTIME_ARGS.purchase_start or TARGET_START_DATE
TARGET_END_DATE = _RUNTIME_ARGS.purchase_end or TARGET_END_DATE
ANCHOR_DATE_VALUE = datetime.datetime.strptime(ANCHOR_DATE, '%Y-%m-%d').date()
SALES_START_DATE = _RUNTIME_ARGS.sales_start or (
    ANCHOR_DATE_VALUE - datetime.timedelta(days=1)
).isoformat()
SALES_END_DATE = _RUNTIME_ARGS.sales_end or ANCHOR_DATE
SALES_START_DATE_VALUE = datetime.datetime.strptime(SALES_START_DATE, '%Y-%m-%d').date()
SALES_END_DATE_VALUE = datetime.datetime.strptime(SALES_END_DATE, '%Y-%m-%d').date()
if TARGET_START_DATE > TARGET_END_DATE:
    raise SystemExit(f"开始日期不能晚于结束日期：{TARGET_START_DATE} > {TARGET_END_DATE}")

if SALES_START_DATE > SALES_END_DATE:
    raise SystemExit(
        f'销售开始日期不能晚于结束日期：{SALES_START_DATE} > {SALES_END_DATE}'
    )

if _RUNTIME_ARGS.print_windows:
    print(
        json.dumps(
            {
                'anchor_date': ANCHOR_DATE,
                'purchase_start': TARGET_START_DATE,
                'purchase_end': TARGET_END_DATE,
                'sales_start': SALES_START_DATE,
                'sales_end': SALES_END_DATE,
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0)

BASE_DIR = os.path.abspath(_RUNTIME_ARGS.base_dir)
EXCEL_PATH = os.path.abspath(
    _RUNTIME_ARGS.excel_path
    or os.path.join(BASE_DIR, "excel_ori", "远行_敬业运输明细_指定的时间段_唯一标识.xlsx")
)
EXCEL_DIR = os.path.dirname(EXCEL_PATH)
EXCEL_FILENAME = os.path.basename(EXCEL_PATH)

RUN_ID = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(_RUNTIME_ARGS.run_id or "")).strip("._")
SOURCE_CONTEXT_DIR = os.path.join(BASE_DIR, "source_context")
SOURCE_CONTEXT_PATH = os.path.join(
    SOURCE_CONTEXT_DIR,
    f"{RUN_ID}.jsonl" if RUN_ID else "legacy.jsonl",
)
FREIGHT_CONFIRMATION_DIR = os.path.join(BASE_DIR, "freight_confirmation")
FREIGHT_CONFIRMATION_PATH = os.path.join(
    FREIGHT_CONFIRMATION_DIR,
    f"{RUN_ID}.jsonl" if RUN_ID else "legacy.jsonl",
)
ADDRESS_CACHE_VERSION = "2026-08-27-v1"
ADDRESS_CACHE_DIR = os.path.join(BASE_DIR, "address_cache")
ADDRESS_CACHE_PATH = os.path.join(ADDRESS_CACHE_DIR, "address_clean_cache.json")
ADDRESS_LLM_MAX_RETRIES = 2
ADDRESS_LLM_TIMEOUTS = ((5, 25), (5, 35), (5, 45))


def _pdf_local_io_path(path):
    """Keep logical names; use Win32 extended paths only for long local I/O."""
    value = os.fspath(path)
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    import ntpath
    absolute = ntpath.normpath(ntpath.abspath(value))
    # Leave ordinary short paths untouched, including existing sidecar names.
    # 248 also protects directory operations below the traditional MAX_PATH.
    if len(absolute) < 248:
        return value
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _pdf_runtime_dir(folder_name):
    root = os.path.join(BASE_DIR, folder_name)
    return os.path.join(root, RUN_ID) if RUN_ID else root


PDF_PURCHASE_DIR = _pdf_runtime_dir("pdf_caigouhebei")
PDF_SALES_DIR = _pdf_runtime_dir("pdf_xiaoshouhebei")
PDF_PURCHASE_SHANGHAI_DIR = _pdf_runtime_dir("pdf_caigoushanghai")
PDF_SALES_SHANGHAI_DIR = _pdf_runtime_dir("pdf_xiaoshoushanghai")

for _target_dir in (
    BASE_DIR,
    EXCEL_DIR,
    PDF_PURCHASE_DIR,
    PDF_SALES_DIR,
    PDF_PURCHASE_SHANGHAI_DIR,
    PDF_SALES_SHANGHAI_DIR,
    SOURCE_CONTEXT_DIR,
    FREIGHT_CONFIRMATION_DIR,
    ADDRESS_CACHE_DIR,
):
    os.makedirs(_pdf_local_io_path(_target_dir), exist_ok=True)

if not os.path.isfile(EXCEL_PATH):
    raise FileNotFoundError(f"初始 Excel 不存在：{EXCEL_PATH}")

_config_wb = openpyxl.load_workbook(EXCEL_PATH, read_only=True, data_only=False)
try:
    _required_sheets = {
        "销售单-河北众沃", "采购单-河北众沃", "销售单-上海宾迪", "采购单-上海宾迪"
    }
    _missing_sheets = sorted(_required_sheets.difference(_config_wb.sheetnames))
    if _missing_sheets:
        raise RuntimeError("初始 Excel 缺少工作表：" + ", ".join(_missing_sheets))
finally:
    _config_wb.close()

# DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "sk-54b2f9a73745411ca3ed8e014bc2d818")
# DASHSCOPE_BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "http://192.168.0.88:8000/v1")
# MODEL_NAME = os.getenv("YYB_MODEL_NAME", "qwen-max") #本地大模型
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "sk-54b2f9a73745411ca3ed8e014bc2d818")
DASHSCOPE_BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "https://llm-d795ptikou79i3xy.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
MODEL_NAME = os.getenv("YYB_MODEL_NAME", "qwen3.8-flash")
APPIUM_SERVER_URL = _RUNTIME_ARGS.appium_url

ACCOUNT_LIST = [
    "13785425706",
    "19131101393",
    "17303216664",
    "13315152228",
    "18630173675",
    "13143043608",
    "13081029913",
    "18632119185",
    "18332152825",
    "15032668617",
    "15830152319",
]
# ACCOUNT_LIST = [
#     # "17303216664",
#     # "13785425706",
#     # "18630173675",
#     "15830152319",
# ]
_diagnostic_accounts = [
    value.strip()
    for value in os.getenv("JINGYE_DIAGNOSTIC_ACCOUNTS", "").split(",")
    if value.strip()
]
if _diagnostic_accounts:
    ACCOUNT_LIST = _diagnostic_accounts
    print(
        "STATUS:YYB_DIAGNOSTIC_ACCOUNTS|" + ",".join(ACCOUNT_LIST),
        flush=True,
    )
# 上海宾迪特殊账号：采购、销售均需要逐车循环，不能使用“所有车辆”。
SHANGHAI_SPECIAL_ACCOUNT = "15830152319"
SHANGHAI_CARS = [
    "冀A03744D",
    "冀A04431D",
    "冀A04541D",
    "冀A32441D",
    "冀A44034D",
    "冀A44301D",
    "冀A44331D",
    "冀A44351D",
    "冀A44439D",
    "冀A44873D",
]
# SHANGHAI_CARS = [
#     # "冀A32441D",
#     "冀A44034D",
# ]

ADB_SERIAL = os.getenv("ADB_SERIAL", "AGQVVB2B08014709")
PHONE_PDF_GLOB = "/storage/emulated/0/Download/HonorBrowser/*.pdf"
# =======================================================

options = AppiumOptions()
options.load_capabilities({
    "platformName": "Android",
    "appium:automationName": "UiAutomator2",
    "appium:deviceName": ADB_SERIAL,
    "appium:udid": ADB_SERIAL,
    "appium:platformVersion": "15",
    "appium:appPackage": "com.ibb.tizi",
    "appium:appActivity": "com.ibb.tizi.activity.LoginActivity",
    "appium:noReset": True,
    "appium:disableWindowAnimation": True,
    "appium:ensureWebviewsHavePages": True,
    "appium:nativeWebScreenshot": True,
    "appium:newCommandTimeout": 7200,
    "appium:adbExecTimeout": 60000,
    "appium:uiautomator2ServerInstallTimeout": 60000,
    "appium:uiautomator2ServerLaunchTimeout": 60000
})

print(f"STATUS:YYB_START|RUN_ID:{RUN_ID or 'flat'}|START:{TARGET_START_DATE}|END:{TARGET_END_DATE}|EXCEL:{EXCEL_PATH}", flush=True)

print(
    f'STATUS:YYB_WINDOWS|ANCHOR:{ANCHOR_DATE}|'
    f'PURCHASE:{TARGET_START_DATE}..{TARGET_END_DATE}|'
    f'SALES:{SALES_START_DATE}..{SALES_END_DATE}',
    flush=True,
)

APPIUM_PERFORMANCE_SETTINGS = {
    'waitForIdleTimeout': 1500,
    'waitForSelectorTimeout': 500,
    'trackScrollEvents': False,
}
UIAUTOMATOR2_FATAL_MARKERS = (
    'instrumentation process is not running',
    'cannot be proxied to uiautomator2 server',
    'uiautomator2 server cannot proxy',
    'invalid session id',
    'socket hang up',
    'econnrefused',
    'connection refused',
)


class AppiumSessionLostError(RuntimeError):
    """UiAutomator2 instrumentation 已退出，当前 driver 不能继续复用。"""


class AppiumSessionOwnershipError(RuntimeError):
    """A possibly live session cannot be safely replaced or duplicated."""


class SessionCleanupUncertain(AppiumSessionOwnershipError):
    """An ended master entry does not prove the device cleanup has finished."""


class AccountSessionSuspectedError(RuntimeError):
    """页面入口连续跳转失败，当前账号会话可能已被其他设备挤占。"""


class StageRecoveryExhaustedError(RuntimeError):
    """模块内部的有界恢复已经耗尽，账号外层不得再次放大重试。"""


class AccountLoginError(AccountSessionSuspectedError):
    """账号登录或进入首个业务页面失败，可安全地从该账号开头重试。"""


class SalesEntryError(AccountLoginError):
    """河北销售入口经过页面恢复后仍未就绪，必须重试账号且禁止静默跳过。"""


MODULE_ENTRY_STALLED_MAX_ATTEMPTS = 3


driver = None
wait = None
_session_creation_outcome_uncertain = False
_session_cleanup_uncertain = False


def _exception_chain_text(exc):
    """收集异常及 cause/context，避免包装异常隐藏 UiAutomator2 原始错误。"""
    parts = []
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        current = current.__cause__ or current.__context__
    return ' | '.join(parts).lower()


def is_appium_session_lost(exc):
    if isinstance(exc, AppiumSessionLostError):
        return True
    error_text = _exception_chain_text(exc)
    return any(marker in error_text for marker in UIAUTOMATOR2_FATAL_MARKERS)


def raise_if_appium_session_lost(exc, operation):
    if isinstance(exc, (AppiumSessionOwnershipError, wireless_link.ConnectionUnavailable)):
        raise exc
    if isinstance(exc, AppiumSessionLostError):
        raise exc
    if is_appium_session_lost(exc):
        raise AppiumSessionLostError(
            f'{operation}时 UiAutomator2 会话已失效: {exc}'
        ) from exc


def _owned_appium_session_active(session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", session_id):
        raise AppiumSessionOwnershipError("owned_session_id_unknown")
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            APPIUM_SERVER_URL.rstrip("/") + "/session/" + session_id + "/timeouts",
            timeout=(5, 30),
        )
        body = response.json()
        value = body.get("value") if isinstance(body, dict) else None
        if response.status_code == 404 and isinstance(value, dict) and value.get("error") == "invalid session id":
            return False
        if response.status_code == 200 and isinstance(value, dict) and not value.get("error"):
            return True
        raise AppiumSessionOwnershipError("owned_session_status_unconfirmed")
    except (requests.RequestException, ValueError) as exc:
        raise AppiumSessionOwnershipError("owned_session_status_unconfirmed") from exc
    finally:
        session.close()


def _close_owned_appium_session(owned_driver):
    global _session_cleanup_uncertain
    session_id = getattr(owned_driver, "session_id", None)
    reservation = getattr(owned_driver, "_jingye_guard_reservation", None)
    wireless = wireless_link.is_enabled()
    try:
        if wireless:
            if reservation is None:
                raise AppiumSessionOwnershipError("wireless_session_guard_reservation_missing")
            wireless_link.ensure_connected()
        owned_driver.quit()
        if wireless:
            phone_owner = getattr(owned_driver, "_jingye_phone_ownership", None)
            if phone_owner is None:
                raise AppiumSessionOwnershipError("wireless_phone_ownership_missing")
            phone_owner.finish_after_quit(session_id)
            reservation.closed()
        return
    except Exception as exc:
        _session_cleanup_uncertain = True
        if wireless and reservation is not None:
            try:
                reservation.uncertain("cleanup_uncertain")
            except appium_guard.GuardError as guard_error:
                raise SessionCleanupUncertain("cleanup_uncertain_guard_write_or_query_failed") from guard_error
        master_state = "unknown"
        try:
            master_state = "active" if _owned_appium_session_active(session_id) else "ended"
        except AppiumSessionOwnershipError:
            pass
        print(f"STATUS:YYB_SESSION_CLEANUP_UNCERTAIN|MASTER_STATE:{master_state}", flush=True)
        # Automatic timeout removes the master entry before UiA2 cleanup finishes.
        # Even an exact invalid-session response cannot authorize a new Remote here.
        raise SessionCleanupUncertain("owned_session_cleanup_unconfirmed_no_recreate") from exc


def create_appium_session(reason, max_attempts=2):
    """创建并实测一条可用会话；不能只以 Remote 返回成功作为启动成功。"""
    global driver, wait, ADB_SERIAL, _session_creation_outcome_uncertain, _session_cleanup_uncertain
    if _session_cleanup_uncertain or _session_creation_outcome_uncertain:
        raise AppiumSessionOwnershipError("unconfirmed_previous_session_no_recreate")
    last_error = None

    for attempt in range(1, max_attempts + 1):
        wireless = wireless_link.is_enabled()
        reservation = None
        if wireless:
            # Connectivity waiting does not consume a Remote/account retry.
            ADB_SERIAL = wireless_link.ensure_connected()
            options.set_capability("appium:deviceName", ADB_SERIAL)
            options.set_capability("appium:udid", ADB_SERIAL)
            options.set_capability("appium:suppressKillServer", True)
            options.set_capability("appium:newCommandTimeout", 0)
            options.set_capability("appium:ignoreHiddenApiPolicyError", True)
            # Keep the explicitly enabled network-trust helper available while
            # UiAutomation is running. USB behavior is unchanged.
            options.set_capability("appium:disableSuppressAccessibilityService", True)
            try:
                reservation = appium_guard.begin(APPIUM_SERVER_URL)
            except appium_guard.GuardError as exc:
                raise AppiumSessionOwnershipError("wireless_appium_guard_refused_creation") from exc
        candidate = None
        remote_attempted = False
        print(
            f'  [Appium会话] {reason}，正在连接手机 '
            f'({attempt}/{max_attempts})：{APPIUM_SERVER_URL} ...',
            flush=True,
        )
        try:
            if wireless:
                phone_owner = appium_phone.prepare(
                    wireless_link, APPIUM_SERVER_URL, reservation.host,
                    host_probe=appium_guard.query_local_host,
                )
                # Preparation may itself recover a changed TLS endpoint. The
                # Remote must use the transport of that completed idle baseline.
                ADB_SERIAL = phone_owner.creation_transport
                options.set_capability("appium:deviceName", ADB_SERIAL)
                options.set_capability("appium:udid", ADB_SERIAL)
                # Opaque per-creation metadata, read back only from the actual
                # UiA2 session when transport changes before ownership capture.
                options.set_capability(appium_phone.OWNERSHIP_CAPABILITY, phone_owner.creation_token)
                client = AppiumClientConfig(remote_server_addr=APPIUM_SERVER_URL, timeout=260, keep_alive=True)
                phone_owner.begin_remote()
                remote_attempted = True
                candidate = webdriver.Remote(APPIUM_SERVER_URL, options=options, client_config=client)
                candidate._jingye_guard_reservation = reservation
                candidate._jingye_phone_ownership = phone_owner
                reservation.created(candidate.session_id)
                phone_owner.capture(candidate.session_id)
            else:
                candidate = webdriver.Remote(APPIUM_SERVER_URL, options=options)
            try:
                settings = dict(APPIUM_PERFORMANCE_SETTINGS)
                if wireless:
                    settings["shutdownOnPowerDisconnect"] = False
                candidate.update_settings(settings)
                if wireless and candidate.get_settings().get("shutdownOnPowerDisconnect") is not False:
                    raise AppiumSessionOwnershipError("wireless_power_setting_unconfirmed")
            except Exception as settings_error:
                if wireless:
                    raise
                print(f'  ⚠️ Appium 性能设置未应用，将使用默认设置: {settings_error}')

            # 真实执行一次 UiAutomator2 命令，排除“session 建立但 instrumentation 未启动”。
            candidate.get_window_size()
            driver = candidate
            wait = WebDriverWait(
                driver,
                15,
                poll_frequency=0.25,
                ignored_exceptions=(NoSuchElementException, StaleElementReferenceException),
            )
            print(f'  ✅ Appium 会话可用：{driver.session_id}', flush=True)
            return driver
        except Exception as exc:
            last_error = exc
            print(f'  ⚠️ Appium 会话创建失败 ({attempt}/{max_attempts}): {exc}', flush=True)
            if candidate is not None:
                if wireless:
                    _close_owned_appium_session(candidate)
                else:
                    try:
                        candidate.quit()
                    except Exception:
                        pass
            elif wireless:
                if not remote_attempted:
                    _session_cleanup_uncertain = True
                    try:
                        reservation.uncertain("cleanup_uncertain")
                    except appium_guard.GuardError as guard_error:
                        raise AppiumSessionOwnershipError("creation_intent_no_remote_guard_write_failed") from guard_error
                    print("STATUS:YYB_CREATION_INTENT_NO_REMOTE|PHONE_STATE:UNCONFIRMED", flush=True)
                    raise AppiumSessionOwnershipError("creation_intent_no_remote_phone_unconfirmed") from exc
                # An authoritative failed terminal plus verified physical cleanup
                # permits the existing bounded creation retry. Lost/ambiguous
                # outcomes and foreign/active residuals still fail closed.
                try:
                    receipt = phone_owner.finish_failed_creation()
                    reservation.failed_creation_closed(receipt)
                except Exception:
                    pass
                else:
                    print("STATUS:YYB_CREATION_FAILED_CLEANED|RECONNECT_BEFORE_RETRY", flush=True)
                    if attempt < max_attempts:
                        time.sleep(1.5)
                        continue
                    break
                # Remote may have created a server session before its response was lost.
                _session_creation_outcome_uncertain = True
                try:
                    reservation.uncertain("creation_unknown")
                except appium_guard.GuardError as guard_error:
                    raise AppiumSessionOwnershipError("creation_unknown_guard_write_or_query_failed") from guard_error
                raise AppiumSessionOwnershipError("remote_creation_outcome_unconfirmed_no_recreate") from exc
            if attempt < max_attempts:
                time.sleep(1.5)

    driver = None
    wait = None
    raise RuntimeError(f'Appium 会话连续 {max_attempts} 次创建失败') from last_error


def recreate_appium_session(reason):
    """释放旧会话并重建；noReset=True，不会清除 App 登录数据。"""
    global driver, wait
    if _session_cleanup_uncertain or _session_creation_outcome_uncertain:
        raise AppiumSessionOwnershipError("unconfirmed_previous_session_no_recreate")
    old_driver = driver
    if old_driver is not None and wireless_link.is_enabled():
        _close_owned_appium_session(old_driver)
        driver = None
        wait = None
        return create_appium_session(reason)
    driver = None
    wait = None

    if old_driver is not None:
        try:
            old_driver.quit()
        except Exception as exc:
            print(f'  ⚠️ 旧 Appium 会话释放失败，将直接重建: {exc}', flush=True)

    return create_appium_session(reason)

# ================= 核心清洗与工具函数区 =================


def append_source_context(
    event,
    sheet_name,
    waybill,
    car,
    source_kind,
    source_value,
    *,
    excel_row="",
    pdf_path="",
):
    """把抓取当下的账号/选车上下文持久化，供后续异常核验精确追溯。"""
    if not source_kind or not source_value:
        return

    record = {
        "version": 1,
        "event": str(event or "").strip(),
        "run_id": RUN_ID,
        "excel_path": EXCEL_PATH,
        "sheet": str(sheet_name or "").strip(),
        "excel_row": excel_row,
        "waybill": str(waybill or "").strip(),
        "car": str(car or "").strip(),
        "source_kind": str(source_kind or "").strip(),
        "source_value": str(source_value or "").strip(),
        "pdf_path": os.path.abspath(pdf_path) if pdf_path else "",
        "recorded_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    # 单个 Appium 会话串行写入；JSONL 即使任务中断，也能保留已完成记录。
    with open(SOURCE_CONTEXT_PATH, "a", encoding="utf-8", newline="\n") as context_file:
        context_file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        context_file.flush()
        os.fsync(context_file.fileno())

def clean_val(raw_text, is_float=False, is_date=False):
    if not raw_text or raw_text == "无":
        return ""
    if is_date:
        match = re.search(r'\d{4}-\d{2}-\d{2}', raw_text)
        return match.group(0) if match else ""
        
    parts = re.split(r'[:：]', raw_text, maxsplit=1)
    clean_str = parts[-1].strip() if len(parts) > 1 else raw_text.strip()
    
    if is_float:
        nums = re.findall(r"[-+]?\d*\.\d+|\d+", clean_str)
        return float(nums[0]) if nums else ""
    return clean_str


TRANSIENT_UI_ERRORS = (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)


def is_account_relogin_error(exc):
    """识别需要重建会话并重登的入口/UI 故障；业务内容错误不在此列。"""
    if is_appium_session_lost(exc):
        return True

    current = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (AccountSessionSuspectedError, *TRANSIENT_UI_ERRORS)):
            return True
        current = current.__cause__ or current.__context__
    return False


def ensure_native_context():
    """确保元素查询发生在原生 App 上下文。"""
    try:
        if driver.current_context != "NATIVE_APP":
            driver.switch_to.context("NATIVE_APP")
    except Exception as exc:
        if wireless_link.is_enabled():
            raise
        raise_if_appium_session_lost(exc, '切换原生上下文')


def fast_wait(timeout=10):
    return WebDriverWait(
        driver,
        timeout,
        poll_frequency=0.25,
        ignored_exceptions=(NoSuchElementException, StaleElementReferenceException)
    )


def _ui_allowed_packages(locators=()):
    """A browser locator authorizes the browser only for that particular wait."""
    packages = set()
    for locator in locators:
        value = str(locator[1])
        for package in ("com.ibb.tizi", "com.honor.browser"):
            if package in value:
                packages.add(package)
    return packages or {"com.ibb.tizi"}


def _ui_pause_if_external(stage, allowed_packages=None, *, package_probe=None,
                          clock=None, sleeper=None, emit=None,
                          poll_seconds=2.0, heartbeat_seconds=60.0):
    """Read-only pause for a confirmed external foreground; never hide errors.

    No package query occurs in USB mode. A same-package unknown page is not a
    pause, and a probe/session/connection exception is deliberately propagated.
    Hooks are injectable for offline tests; no environment or private UI is read.
    """
    if not wireless_link.is_enabled():
        return False
    allowed = frozenset(allowed_packages or {"com.ibb.tizi"})
    package_probe = package_probe or (lambda: driver.current_package)
    clock = clock or time.monotonic
    sleeper = sleeper or time.sleep
    def default_emit(record):
        print("STATUS:UI_INTERRUPTION|" + json.dumps(record, separators=(',', ':')), flush=True)
    emit = emit or default_emit
    def read_package():
        package = package_probe()
        if not isinstance(package, str) or not package.strip():
            raise WebDriverException("foreground_package_unavailable")
        return package
    if read_package() in allowed:
        return False
    began = last_heartbeat = clock()
    emit({"event": "paused_external_ui", "stage": stage, "elapsedSeconds": 0})
    while True:
        sleeper(poll_seconds)
        package = read_package()
        observed = clock()
        elapsed = round(max(0, observed - began), 3)
        if package in allowed:
            emit({"event": "resumed_allowed_ui", "stage": stage, "elapsedSeconds": elapsed})
            return True
        if observed - last_heartbeat >= heartbeat_seconds:
            emit({"event": "waiting_external_ui", "stage": stage, "elapsedSeconds": elapsed})
            last_heartbeat = observed


def _ui_readonly_wait(condition, timeout, locators=()):
    while True:
        try:
            return fast_wait(timeout).until(condition)
        except TimeoutException as error:
            if not wireless_link.is_enabled() or is_appium_session_lost(error):
                raise
            if not _ui_pause_if_external("element_wait", _ui_allowed_packages(locators)):
                raise
            # Only the original read-only condition is repeated. No click is sent.
            ensure_native_context()


def wait_visible(locator, timeout=10):
    ensure_native_context()
    return _ui_readonly_wait(EC.visibility_of_element_located(locator), timeout, (locator,))


def wait_clickable(locator, timeout=10):
    ensure_native_context()
    return _ui_readonly_wait(EC.element_to_be_clickable(locator), timeout, (locator,))


def wait_nonempty_text(locator, timeout=10):
    """等待元素出现且文本非空，页面快时立即返回。"""
    def condition(d):
        try:
            el = d.find_element(*locator)
            value = (el.text or "").strip()
            return value if value else False
        except (NoSuchElementException, StaleElementReferenceException):
            return False

    ensure_native_context()
    return _ui_readonly_wait(condition, timeout, (locator,))


def wait_for_any_visible(locators, timeout=10):
    """等待任意一个页面标志元素可见。"""
    locators = tuple(locators)
    def condition(d):
        for locator in locators:
            try:
                for el in d.find_elements(*locator):
                    if el.is_displayed():
                        return locator
            except Exception as exc:
                if wireless_link.is_enabled():
                    if not isinstance(exc, (NoSuchElementException, StaleElementReferenceException)):
                        raise
                raise_if_appium_session_lost(exc, '等待页面元素')
                continue
        return False

    ensure_native_context()
    return _ui_readonly_wait(condition, timeout, locators)


def _ui_wireless_click_step(locator, desc, next_locator, timeout, retries):
    """Retry locating only; after click is attempted never replay that action."""
    for attempt in range(1, retries + 1):
        try:
            element = wait_clickable(locator, timeout=timeout)
            break
        except (NoSuchElementException, StaleElementReferenceException, TimeoutException) as error:
            if is_appium_session_lost(error):
                raise
            if attempt >= retries:
                raise
            time.sleep(0.35)
    try:
        element.click()
    except TRANSIENT_UI_ERRORS as error:
        if is_appium_session_lost(error):
            raise
        allowed = _ui_allowed_packages((locator,) + ((next_locator,) if next_locator else ()))
        if not _ui_pause_if_external("click_outcome", allowed) or next_locator is None:
            raise
        # The click might have reached the phone. Only inspect its target now.
        wait_visible(next_locator, timeout=timeout)
        return True
    if next_locator:
        wait_visible(next_locator, timeout=timeout)
    print(f"  ✅ 已点击：{desc}")
    return True


def click_step(locator, desc, next_locator=None, timeout=10, retries=2):
    """重新定位后点击；瞬时失败时仅重试当前点击，不重跑整个账号。"""
    if wireless_link.is_enabled():
        return _ui_wireless_click_step(locator, desc, next_locator, timeout, retries)
    last_error = None
    last_phase = '定位并点击'
    for attempt in range(1, retries + 1):
        # 上一次点击可能已经成功，只是目标页晚于等待窗口出现。此时不要再去
        # 新页面查找并重复点击已经消失的源控件。
        if attempt > 1 and next_locator:
            try:
                wait_visible(next_locator, timeout=min(2.5, timeout))
                print(f"  ✅ 已点击：{desc}（目标页面延迟就绪）")
                return True
            except TRANSIENT_UI_ERRORS as e:
                raise_if_appium_session_lost(e, f'确认“{desc}”目标页面')

        try:
            last_phase = '定位并点击'
            element = wait_clickable(locator, timeout=timeout)
            element.click()
            if next_locator:
                last_phase = f'点击已发送，等待目标元素 {next_locator}'
                wait_visible(next_locator, timeout=timeout)
            print(f"  ✅ 已点击：{desc}")
            return True
        except TRANSIENT_UI_ERRORS as e:
            raise_if_appium_session_lost(e, f'点击“{desc}”')
            last_error = e
            print(
                f"  ⚠️ 点击“{desc}”第 {attempt}/{retries} 次未完成；"
                f"阶段：{last_phase}；{type(e).__name__}: {e}"
            )
            time.sleep(0.35)

    raise AccountSessionSuspectedError(
        f"点击“{desc}”连续 {retries} 次失败；最后阶段：{last_phase}"
    ) from last_error


def click_back_to_list(timeout=10):
    """从详情页返回列表；控件点击失败时再用系统返回键兜底。"""
    try:
        click_step(
            (AppiumBy.ID, "com.ibb.tizi:id/iv_left"),
            "详情页返回按钮",
            next_locator=(AppiumBy.ID, "com.ibb.tizi:id/rv"),
            timeout=timeout,
            retries=2
        )
        return True
    except Exception as first_error:
        raise_if_appium_session_lost(first_error, '点击详情页返回按钮')
        print(f"  ⚠️ 页面返回按钮失败，尝试系统返回键: {first_error}")
        try:
            ensure_native_context()
            driver.back()
            wait_visible((AppiumBy.ID, "com.ibb.tizi:id/rv"), timeout=timeout)
            print("  ✅ 已通过系统返回键回到列表")
            return True
        except Exception as second_error:
            raise_if_appium_session_lost(second_error, '通过系统返回键返回列表')
            print(f"  ❌ 返回列表失败: {second_error}")
            return False


def activate_tizi_and_wait(expected_locators=None, timeout=12):
    """切回货运 App，并等待真实页面元素出现，代替固定 sleep。"""
    ensure_native_context()
    driver.activate_app("com.ibb.tizi")
    ensure_native_context()
    if expected_locators:
        wait_for_any_visible(expected_locators, timeout=timeout)


def clean_optional_float(raw_text):
    """销售单可选重量：缺失时保持空白，不把缺失值写成 0。"""
    if raw_text is None:
        return ""

    text = str(raw_text).strip()
    if not text or text == "无":
        return ""

    nums = re.findall(r"[-+]?\d*\.\d+|\d+", text)
    return float(nums[0]) if nums else ""


EXCEL_NUMBER_FORMAT = '#,##0.###'
EXCEL_NUMERIC_HEADERS = {
    "装货重量", "卸货重量", "结算重量", "磅票净重", "理重", "结算量", "单价",
}
EXCEL_THIN_SIDE = Side(style="thin", color="D9D9D9")
EXCEL_DATA_BORDER = Border(
    left=EXCEL_THIN_SIDE,
    right=EXCEL_THIN_SIDE,
    top=EXCEL_THIN_SIDE,
    bottom=EXCEL_THIN_SIDE,
)
EXCEL_ALT_FILL = PatternFill("solid", fgColor="F7FAFC")


def apply_excel_data_row_style(ws, row, sequence):
    """给抓取阶段写入的整行加边框，最终核验阶段还会按运单/PDF层级重新分组。"""
    max_col = ws.max_column
    use_alt_fill = bool(sequence % 2 == 0)
    for col in range(1, max_col + 1):
        cell = ws.cell(row=row, column=col)
        header = str(ws.cell(row=2, column=col).value or "").strip()
        cell.border = EXCEL_DATA_BORDER
        if use_alt_fill:
            cell.fill = EXCEL_ALT_FILL
        if header in EXCEL_NUMERIC_HEADERS or cell.data_type == "f":
            cell.number_format = EXCEL_NUMBER_FORMAT
            cell.alignment = Alignment(horizontal="right", vertical="center")
        elif col == 1:
            cell.alignment = Alignment(horizontal="center", vertical="center")
        else:
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

    ws.row_dimensions[row].height = 22
    ws.auto_filter.ref = f"A2:{get_column_letter(max_col)}{max(row, 2)}"


_ADDRESS_CACHE = None


def normalize_address_cache_text(value):
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return re.sub(r"\s+", " ", text)


def address_cache_key(raw_text):
    normalized = normalize_address_cache_text(raw_text)
    return hashlib.sha256(
        f"{ADDRESS_CACHE_VERSION}|{normalized}".encode("utf-8")
    ).hexdigest()


def load_address_cache():
    global _ADDRESS_CACHE
    if _ADDRESS_CACHE is not None:
        return _ADDRESS_CACHE
    try:
        if os.path.isfile(ADDRESS_CACHE_PATH):
            with open(ADDRESS_CACHE_PATH, "r", encoding="utf-8") as handle:
                value = json.load(handle)
            _ADDRESS_CACHE = value if isinstance(value, dict) else {}
        else:
            _ADDRESS_CACHE = {}
    except Exception as exc:
        print(f"  ⚠️ 地址缓存读取失败，本次跳过缓存: {exc}")
        _ADDRESS_CACHE = {}
    return _ADDRESS_CACHE


def save_address_cache(cache):
    try:
        os.makedirs(ADDRESS_CACHE_DIR, exist_ok=True)
        temp_path = ADDRESS_CACHE_PATH + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, ADDRESS_CACHE_PATH)
    except Exception as exc:
        print(f"  ⚠️ 地址缓存写入失败，不影响本次结果: {exc}")


def get_cached_address(raw_text):
    cache = load_address_cache()
    item = cache.get(address_cache_key(raw_text))
    if isinstance(item, dict):
        return normalize_address_cache_text(item.get("clean_address"))
    if isinstance(item, str):
        return normalize_address_cache_text(item)
    return ""


def remember_model_address(raw_text, clean_address):
    clean_address = normalize_address_cache_text(clean_address)
    if not clean_address:
        return
    cache = load_address_cache()
    cache[address_cache_key(raw_text)] = {
        "version": ADDRESS_CACHE_VERSION,
        "clean_address": clean_address,
        "source": "llm",
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    save_address_cache(cache)


def clean_llm_address_output(content):
    text = unicodedata.normalize("NFKC", str(content or "")).strip()
    text = re.sub(r"^\x60{3}(?:text)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\x60{3}$", "", text)
    text = text.strip().strip("'\"“”‘’")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (
        normalize_address_cache_text(lines[0].strip("'\"“”‘’"))
        if lines else ""
    )


def _remove_operational_address_text(text):
    text = re.sub(r"1[3-9]\d{9}", "", text)
    text = re.sub(
        r"(收货及扫码电话|收货电话|联系电话|电话)[:：]?\s*",
        "",
        text,
    )
    text = re.sub(
        r"(联系人|联络人|业务员)[:：]?\s*[^,，;；。]*",
        "",
        text,
    )
    text = re.sub(r"(现场扫码收货|现场扫码|扫码收货|扫码)", "", text)
    text = re.sub(r"(股份有限公司|有限责任公司|有限公司|公司)", "", text)
    text = re.sub(r"卸货时间[:：]?\s*[^,，;；。]*", "", text)
    text = re.sub(r"单卷\s*\d+(?:\.\d+)?\s*吨[^,，;；。]*", "", text)
    text = re.sub(
        r"(?:晚上|早上|上午|下午|早)?\s*\d{1,2}[:：]\d{2}\s*[-至到]\s*(?:晚上|早上|上午|下午|早)?\s*\d{1,2}[:：]\d{2}[^,，;；。]*",
        "",
        text,
    )
    text = re.sub(r"(不卸车|卸车要求|装卸车要求|装车要求|收货要求)", "", text)
    return normalize_address_cache_text(text)


def fallback_extract_address_by_rule(raw_text):
    text = normalize_address_cache_text(raw_text)
    if not text:
        return ""
    text = re.split(r"(?:——|--|—{2,}|－{2,})", text, maxsplit=1)[0]
    text = _remove_operational_address_text(text)
    text = re.sub(r"[，,；;。]+", ",", text)

    location_keywords = (
        "省", "市", "区", "县", "镇", "乡", "村", "路", "街", "号",
        "物流园", "工业园", "园区", "厂区", "库房", "仓库", "货场",
        "堆场", "港", "码头", "院", "中心", "基地", "市场",
    )
    bad_keywords = (
        "单卷", "不卸车", "卸车", "装车", "扫码", "电话", "联系人",
        "联络人", "业务员", "晚上", "早上", "上午", "下午",
    )
    candidates = []
    for piece in re.split(r"[,\n]+", text):
        candidate = _remove_operational_address_text(piece).strip(" ,，;；。")
        if not candidate:
            continue
        compact = re.sub(r"\s+", "", candidate)
        has_location = any(keyword in compact for keyword in location_keywords)
        if not has_location and any(keyword in compact for keyword in bad_keywords):
            continue
        score = len(compact)
        if has_location:
            score += 100
        score += sum(12 for keyword in location_keywords if keyword in compact)
        score -= sum(25 for keyword in bad_keywords if keyword in compact)
        candidates.append((score, compact))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    return re.sub(r"\s+", "", text).strip(" ,，;；。")


def should_retry_address_response(status_code):
    return status_code in (408, 409, 425, 429) or status_code >= 500


def extract_address_with_llm(raw_text):
    if not raw_text or raw_text == "无":
        return ""

    raw_text = str(raw_text).strip()
    cached = get_cached_address(raw_text)
    if cached:
        print("  [AI] 命中地址清洗缓存")
        return cached

    prompt = f"""请从以下物流说明中提取真实的最终目的地物理地址。

要求：
1. 只输出地址文本。
2. 不要解释。
3. 不要输出思考过程。
4. 去除联系人、电话、装卸车要求、备注。
5. 去除公司后缀名，例如：有限公司、有限责任公司、股份有限公司、公司。
6. 保留省、市、区、县、镇、街道、路、号、物流园、工业园、厂区、库房、仓库等地点信息。
7. 如果没有详细街道门牌，就输出最像最终卸货地的地点名称。
8. 禁止输出空。

文本：
{raw_text}
"""

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {
                "role": "system",
                "content": "你是地址提取器。只返回最终地址文本，不返回解释，不返回思考过程。"
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "max_tokens": 64,
        "temperature": 0.1,
        "stream": False,

        # 关键：只对本次请求关闭 thinking，不影响 vLLM 服务本身
        "chat_template_kwargs": {
            "enable_thinking": False,
            "preserve_thinking": False
        }
    }

    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json"
    }

    last_error = ""
    attempts = 1 + max(0, ADDRESS_LLM_MAX_RETRIES)
    for attempt in range(1, attempts + 1):
        timeout = ADDRESS_LLM_TIMEOUTS[
            min(attempt - 1, len(ADDRESS_LLM_TIMEOUTS) - 1)
        ]
        try:
            resp = requests.post(
                f"{DASHSCOPE_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
                timeout=timeout
            )

            if resp.status_code == 200:
                data = resp.json()
                choice = data.get("choices", [{}])[0]
                msg = choice.get("message", {}) or {}
                content = clean_llm_address_output(msg.get("content"))
                if content:
                    remember_model_address(raw_text, content)
                    return content
                last_error = (
                    "content为空；finish_reason="
                    + str(choice.get("finish_reason"))
                )
            else:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if not should_retry_address_response(resp.status_code):
                    break

        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = str(exc)
            break

        if attempt < attempts:
            print(
                f"  ⚠️ 地址模型第{attempt}/{attempts}次请求失败，"
                f"准备重试: {last_error}"
            )
            time.sleep(min(3.0, 1.5 * attempt))

    fallback = fallback_extract_address_by_rule(raw_text)
    if fallback and fallback != normalize_address_cache_text(raw_text):
        print(f"  ⚠️ 地址模型连续失败，使用规则兜底: {last_error}")
        return fallback

    print(
        "  ⚠️ 地址模型连续失败，规则兜底未取得更好结果，"
        f"使用原文本: {last_error}"
    )
    return raw_text

def clean_purchase_carrier(value):
    """Keep the carrier name verbatim after removing its optional UI label."""
    text = str(value or "").strip()
    text = re.sub(r"^承运商\s*[：:]\s*", "", text).strip()
    return "" if text == "无" else text


def purchase_excel_columns(ws):
    """Locate purchase fields by header; upgrade only an active legacy sheet."""
    headers = {
        str(ws.cell(2, col).value or "").strip(): col
        for col in range(1, ws.max_column + 1)
    }
    if "承运商" not in headers:
        if headers.get("货物名称") != 4 or headers.get("卸货地") != 5:
            raise ValueError("无法安全识别采购表布局，未插入承运商列")
        old_max = ws.max_column
        title_ranges = [
            str(item) for item in ws.merged_cells.ranges
            if item.min_row == item.max_row == 1
            and item.min_col == 1 and item.max_col == old_max
        ]
        for item in title_ranges:
            ws.unmerge_cells(item)
        widths = {
            col: copy(ws.column_dimensions[get_column_letter(col)])
            for col in range(5, old_max + 1)
        }
        ws.insert_cols(5)
        for col, dimension in widths.items():
            dimension.index = get_column_letter(col + 1)
            dimension.min = dimension.max = col + 1
            ws.column_dimensions[dimension.index] = dimension
        carrier_header = ws.cell(2, 5, "承运商")
        carrier_header._style = copy(ws.cell(2, 4)._style)
        ws.column_dimensions["E"].width = 32
        for row in range(3, ws.max_row + 1):
            ws.cell(row, 5)._style = copy(ws.cell(row, 4)._style)
            settlement = ws.cell(row, 11)
            if settlement.data_type == "f":
                settlement.value = (
                    f'=IF(COUNT(I{row}:J{row})<2,"",MIN(I{row},J{row}))'
                )
        for _item in title_ranges:
            ws.merge_cells(f"A1:{get_column_letter(old_max + 1)}1")
        ws.auto_filter.ref = f"A2:{get_column_letter(ws.max_column)}{max(2, ws.max_row)}"
        headers = {
            str(ws.cell(2, col).value or "").strip(): col
            for col in range(1, ws.max_column + 1)
        }
    required = ("序号", "运单号", "车牌号", "货物名称", "承运商", "卸货地",
                "装货日期", "装货重量", "卸货重量", "结算重量")
    if any(name not in headers for name in required):
        raise ValueError("采购表缺少必要表头，未写入数据")
    return headers


def adjust_purchase_carrier_height(ws, row, column):
    text = str(ws.cell(row, column).value or "")
    width = max(1, int(ws.column_dimensions[get_column_letter(column)].width or 32) - 2)
    lines = sum(
        max(1, (sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
                    for char in line) + width - 1) // width)
        for line in text.split("\n")
    )
    ws.row_dimensions[row].height = max(ws.row_dimensions[row].height or 22, 22, lines * 16 + 6)


def save_crawl_workbook(wb):
    """Use the existing atomic save path for both new rows and carrier fills."""
    temp_excel_path = EXCEL_PATH + ".writing.tmp.xlsx"
    try:
        if os.path.exists(temp_excel_path):
            os.remove(temp_excel_path)
        wb.save(temp_excel_path)
        wb.close()
        os.replace(temp_excel_path, EXCEL_PATH)
    finally:
        try:
            wb.close()
        except Exception:
            pass
        if os.path.exists(temp_excel_path):
            try:
                os.remove(temp_excel_path)
            except Exception:
                pass


def write_data_to_excel(
    info_dict,
    sheet_name,
    is_sales=False,
    source_kind="",
    source_value="",
):
    if not os.path.exists(EXCEL_PATH):
        raise FileNotFoundError(f"Excel不存在：{EXCEL_PATH}")
    wb = openpyxl.load_workbook(EXCEL_PATH)
    if sheet_name not in wb.sheetnames:
        wb.close()
        raise KeyError(f"Excel缺少工作表：{sheet_name}")
    ws = wb[sheet_name]
    purchase_columns = purchase_excel_columns(ws) if not is_sales else None

    incoming_waybill = clean_val(info_dict.get("单号", ""))
    incoming_car = clean_val(info_dict.get("车牌号", ""))
    target_row = 3
    last_seq = 0
    while ws.cell(row=target_row, column=2).value is not None and str(ws.cell(row=target_row, column=2).value).strip() != "":
        existing_waybill = str(ws.cell(row=target_row, column=2).value or "").strip()
        existing_car = str(ws.cell(row=target_row, column=3).value or "").strip()
        if existing_waybill == incoming_waybill and existing_car == incoming_car:
            if not is_sales:
                carrier_cell = ws.cell(target_row, purchase_columns["承运商"])
                carrier = clean_purchase_carrier(info_dict.get("承运商", ""))
                if not clean_purchase_carrier(carrier_cell.value) and carrier:
                    carrier_cell.value = carrier
                    carrier_cell.alignment = Alignment(
                        horizontal="left", vertical="center", wrap_text=True
                    )
                adjust_purchase_carrier_height(ws, target_row, purchase_columns["承运商"])
                save_crawl_workbook(wb)
            else:
                wb.close()
            append_source_context(
                "row",
                sheet_name,
                incoming_waybill,
                incoming_car,
                source_kind,
                source_value,
                excel_row=target_row,
            )
            print(f"  ♻️ Excel {sheet_name} 已存在同运单+同车牌，跳过重复写入：{incoming_waybill}/{incoming_car}")
            return target_row
        val = ws.cell(row=target_row, column=1).value
        if isinstance(val, (int, float)):
            last_seq = int(val)
        target_row += 1

    current_seq = last_seq + 1
    
    if not is_sales:
        purchase_values = {
            "序号": current_seq,
            "运单号": clean_val(info_dict["单号"]),
            "车牌号": clean_val(info_dict["车牌号"]),
            "货物名称": clean_val(info_dict["物料"]),
            "承运商": clean_purchase_carrier(info_dict.get("承运商", "")),
            "卸货地": clean_val(info_dict["卸货库房"]),
            "装货日期": clean_val(info_dict["接单时间"], is_date=True),
            "装货重量": clean_val(info_dict["装车净重"], is_float=True),
            "卸货重量": clean_val(info_dict["卸车净重"], is_float=True),
        }
        row_mapping = {purchase_columns[name]: value for name, value in purchase_values.items()}
    else:
        remark2_col = next(
            (
                col
                for col in range(1, ws.max_column + 1)
                if str(ws.cell(row=2, column=col).value or "").strip() == "备注2"
            ),
            19,
        )
        row_mapping = {
            1: current_seq,
            2: clean_val(info_dict["单号"]),
            3: clean_val(info_dict["车牌号"]),
            4: clean_val(info_dict["货物名称"]),
            5: info_dict["卸货地"],

            # 销售表第 6 列“装货日期”暂时统一留空；预计出场时间不写入该列。
            6: "",

            # 销售表第 7 列“装货重量”：没有值时保持空白，不写成 0。
            7: clean_optional_float(info_dict.get("装货重量", "")),

            # 新模板为第19列；恢复旧任务时按表头定位，避免把备注写入错误列。
            remark2_col: info_dict.get("备注2", "")
        }

    for col_idx, val in row_mapping.items():
        cell = ws.cell(row=target_row, column=col_idx)
        cell.value = val
        if isinstance(val, str) and "\n" in val:
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        elif isinstance(val, float):
            cell.number_format = EXCEL_NUMBER_FORMAT
            cell.alignment = Alignment(horizontal="right", vertical="center")
        elif col_idx == 1:
            cell.alignment = Alignment(horizontal="center", vertical="center")

    if not is_sales:
        settlement_cell = ws.cell(row=target_row, column=purchase_columns["结算重量"])
        load_col = get_column_letter(purchase_columns["装货重量"])
        unload_col = get_column_letter(purchase_columns["卸货重量"])
        settlement_cell.value = (
            f'=IF(COUNT({load_col}{target_row}:{unload_col}{target_row})<2,'
            f'"",MIN({load_col}{target_row},{unload_col}{target_row}))'
        )
        settlement_cell.number_format = EXCEL_NUMBER_FORMAT

    apply_excel_data_row_style(ws, target_row, current_seq)
    if not is_sales:
        adjust_purchase_carrier_height(ws, target_row, purchase_columns["承运商"])
    if hasattr(wb, "calculation"):
        wb.calculation.calcMode = "auto"
        wb.calculation.fullCalcOnLoad = True
        wb.calculation.forceFullCalc = True

    save_crawl_workbook(wb)
    append_source_context(
        "row",
        sheet_name,
        incoming_waybill,
        incoming_car,
        source_kind,
        source_value,
        excel_row=target_row,
    )
    print(f"  📝 Excel {sheet_name} 已追加至第 {target_row} 行")
    return target_row

def swipe_wheel_one_step(driver, start_x, direction):
    actions = ActionChains(driver)
    actions.w3c_actions = ActionBuilder(driver, mouse=PointerInput(interaction.POINTER_TOUCH, "touch"))
    start_y, end_y = (1950, 2020) if direction == 'down' else (2020, 1950)
    actions.w3c_actions.pointer_action.move_to_location(start_x, start_y)
    actions.w3c_actions.pointer_action.pointer_down()
    actions.w3c_actions.pointer_action.pause(0.2) 
    actions.w3c_actions.pointer_action.move_to_location(start_x, end_y)
    actions.w3c_actions.pointer_action.release()
    actions.perform()
    time.sleep(0.3)

def set_smart_date(driver, target_date_str):
    target_date = datetime.datetime.strptime(target_date_str, "%Y-%m-%d")
    now = datetime.datetime.now()
    year_diff, month_diff, day_diff = target_date.year - now.year, target_date.month - now.month, target_date.day - now.day
    
    year_el = wait.until(EC.presence_of_element_located((AppiumBy.ID, "com.ibb.tizi:id/year")))
    month_el = wait.until(EC.presence_of_element_located((AppiumBy.ID, "com.ibb.tizi:id/month")))
    day_el = wait.until(EC.presence_of_element_located((AppiumBy.ID, "com.ibb.tizi:id/day")))
    
    wheel_x = {
        'year': int(year_el.rect['x'] + year_el.rect['width'] / 2),
        'month': int(month_el.rect['x'] + month_el.rect['width'] / 2),
        'day': int(day_el.rect['x'] + day_el.rect['width'] / 2)
    }
    def do_swipe(diff, x_coord):
        if diff == 0: return
        direction = 'up' if diff > 0 else 'down'
        for _ in range(abs(diff)): swipe_wheel_one_step(driver, x_coord, direction)
            
    do_swipe(year_diff, wheel_x['year'])
    do_swipe(month_diff, wheel_x['month'])
    do_swipe(day_diff, wheel_x['day'])
    time.sleep(1)

def swipe_with_brake(driver, start_y, end_y):
    """通用物理防惯性滑动函数（0.3秒强制刹车）"""
    actions = ActionChains(driver)
    actions.w3c_actions = ActionBuilder(driver, mouse=PointerInput(interaction.POINTER_TOUCH, "touch"))
    actions.w3c_actions.pointer_action.move_to_location(500, start_y)
    actions.w3c_actions.pointer_action.pointer_down()
    actions.w3c_actions.pointer_action.pause(0.2)
    actions.w3c_actions.pointer_action.move_to_location(500, end_y)
    actions.w3c_actions.pointer_action.pause(0.3)
    actions.w3c_actions.pointer_action.release()
    actions.perform()
    time.sleep(2)


def read_first_nonempty_child_text(card, resource_id, retries=4, interval=0.25):
    """
    从 RecyclerView 卡片的同一 resource-id 子元素中读取第一个非空文本。

    RecyclerView 在快速/长时间滚动后可能先挂载 ViewHolder，再稍后绑定文本；
    同一 ID 也可能同时存在空占位元素和真实文本元素。因此不能只读 elements[0]。
    """
    for attempt in range(retries):
        try:
            elements = card.find_elements(AppiumBy.ID, resource_id)
            for element in elements:
                value = (element.text or "").strip()
                if value:
                    return value
        except StaleElementReferenceException:
            # 卡片被 RecyclerView 回收，交给外层重新抓取整个 cards 列表。
            return ""
        except Exception as exc:
            raise_if_appium_session_lost(exc, '读取卡片字段')

        if attempt < retries - 1:
            time.sleep(interval)

    return ""


def nudge_sales_list_for_rebind(driver):
    """
    小幅上推列表，使半遮挡卡片完整进入可见区域，并触发 RecyclerView 重新绑定字段。
    只在“运单号可见但预计出场时间暂时为空”时调用，不用于正常翻页。
    """
    actions = ActionChains(driver)
    actions.w3c_actions = ActionBuilder(
        driver,
        mouse=PointerInput(interaction.POINTER_TOUCH, "touch")
    )
    actions.w3c_actions.pointer_action.move_to_location(500, 1750)
    actions.w3c_actions.pointer_action.pointer_down()
    actions.w3c_actions.pointer_action.pause(0.15)
    actions.w3c_actions.pointer_action.move_to_location(500, 1450)
    actions.w3c_actions.pointer_action.pause(0.2)
    actions.w3c_actions.pointer_action.release()
    actions.perform()
    time.sleep(0.75)

def get_visible_item_keys(item_resource_id):
    """获取当前列表中可见单据编号，用于判断列表是否还在移动。"""
    try:
        return tuple(
            (el.text or "").strip()
            for el in driver.find_elements(AppiumBy.ID, item_resource_id)
            if (el.text or "").strip()
        )
    except Exception:
        return tuple()


def swipe_list_toward_top_once(driver):
    """大步向下拖动手指，使 RecyclerView 内容回到更靠近顶部的位置。"""
    actions = ActionChains(driver)
    actions.w3c_actions = ActionBuilder(
        driver,
        mouse=PointerInput(interaction.POINTER_TOUCH, "touch")
    )
    actions.w3c_actions.pointer_action.move_to_location(500, 650)
    actions.w3c_actions.pointer_action.pointer_down()
    actions.w3c_actions.pointer_action.pause(0.15)
    actions.w3c_actions.pointer_action.move_to_location(500, 1950)
    actions.w3c_actions.pointer_action.pause(0.25)
    actions.w3c_actions.pointer_action.release()
    actions.perform()
    time.sleep(0.55)


def reset_recycler_to_top(item_resource_id, desc="当前车辆列表", max_swipes=12):
    """
    将当前运单 RecyclerView 强制复位到顶部。

    仅供上海宾迪逐车查询后调用：切换车牌时 RecyclerView 会保留上一辆车的
    滚动位置，如果不复位，新车辆顶部单据可能被遗漏。
    """
    ensure_native_context()

    try:
        wait_visible((AppiumBy.ID, "com.ibb.tizi:id/rv"), timeout=6)
    except Exception:
        print(f"  ℹ️ {desc} 没有列表容器，按空结果处理，无需回顶。")
        return True

    # 给查询结果一个很短的刷新窗口；query_current_vehicle 已经等过 1 秒。
    time.sleep(0.35)
    visible_keys = get_visible_item_keys(item_resource_id)
    if not visible_keys:
        print(f"  ℹ️ {desc} 当前没有可见单据，无需回顶。")
        return True

    print(f"  [列表复位] {desc} 正在回到最顶部...")

    # 优先使用 UiScrollable 一次性回顶，速度快；驱动/页面不支持时再物理大步滑动。
    try:
        driver.find_element(
            AppiumBy.ANDROID_UIAUTOMATOR,
            'new UiScrollable(new UiSelector().resourceId("com.ibb.tizi:id/rv"))'
            '.setAsVerticalList().flingToBeginning(12)'
        )
        time.sleep(0.7)
    except Exception:
        pass

    previous_keys = get_visible_item_keys(item_resource_id)
    stable_rounds = 0

    for _ in range(max_swipes):
        swipe_list_toward_top_once(driver)
        current_keys = get_visible_item_keys(item_resource_id)

        # 已到顶部时，继续向下拉不会再改变可见单号。连续两次不变才确认，
        # 避免因为某次滑动幅度不足而误判。
        if current_keys and current_keys == previous_keys:
            stable_rounds += 1
        else:
            stable_rounds = 0

        if stable_rounds >= 2:
            print(f"  ✅ {desc} 已回到顶部。")
            return True

        previous_keys = current_keys

    print(f"  ⚠️ {desc} 已执行最大回顶次数，将从当前最靠顶部位置继续处理。")
    return False


FREIGHT_DEBUG = os.getenv("JINGYE_FREIGHT_DEBUG", "").strip().lower() in {
    "1", "true", "yes", "on",
}
try:
    FREIGHT_SCOPE_MAX_ATTEMPTS = max(
        1,
        int(os.getenv("JINGYE_DIAGNOSTIC_FREIGHT_SCOPE_ATTEMPTS", "3")),
    )
except (TypeError, ValueError):
    FREIGHT_SCOPE_MAX_ATTEMPTS = 3
FREIGHT_CARD_MAX_ATTEMPTS = 2
FREIGHT_MAX_TRAVERSAL_STEPS = 100
FREIGHT_FRONTIER_SMALL_STEPS = 4
FREIGHT_INCOMPLETE_ALIGN_MAX_STEPS = 4
FREIGHT_MIN_RELATIVE_FULL_HEIGHT = 0.72
FREIGHT_LIST_ID = "com.ibb.tizi:id/rv"
FREIGHT_WAYBILL_ID = "com.ibb.tizi:id/tv4"
FREIGHT_TIME_ID = "com.ibb.tizi:id/tv2"
FREIGHT_SPEC_ID = "com.ibb.tizi:id/tv8"
FREIGHT_ACTION_ID = "com.ibb.tizi:id/yun_fei_select"
FREIGHT_ENTRY_LOCATORS = {
    "search": (AppiumBy.ID, "com.ibb.tizi:id/search"),
    "business_data": (AppiumBy.ID, "com.ibb.tizi:id/business_data"),
    "sales_car": (AppiumBy.ID, "com.ibb.tizi:id/car_list"),
    "purchase_start_date": (AppiumBy.ID, "com.ibb.tizi:id/start_date"),
    "purchase_end_date": (AppiumBy.ID, "com.ibb.tizi:id/end_date"),
    "park_tab": (AppiumBy.ID, "com.ibb.tizi:id/rd_user_yq"),
    "freight_icon": (AppiumBy.ID, "com.ibb.tizi:id/icon_yuanqu_10"),
    "business_back": (AppiumBy.ID, "com.ibb.tizi:id/image_main"),
    "back": (AppiumBy.ID, "com.ibb.tizi:id/iv_left"),
    "settlement_quantity": (AppiumBy.ID, "com.ibb.tizi:id/jiesuanshu"),
    "unit_price": (AppiumBy.ID, "com.ibb.tizi:id/unit_price"),
}


class FreightTechnicalError(StageRecoveryExhaustedError):
    """运费确认页面经过阶段级恢复后仍无法完整遍历。"""


class FreightEntryError(AccountSessionSuspectedError):
    """运费确认入口经过页面状态恢复后仍未就绪。"""


def persist_sales_freight_target(
    *,
    sheet_name,
    source_kind,
    source_value,
    waybill,
    car,
    app_export_time,
    excel_row,
):
    """销售行成功写入或命中去重行后，立即记录本次运费查询目标。"""
    event = make_target_event(
        run_id=RUN_ID,
        sheet=sheet_name,
        source_kind=source_kind,
        source_value=source_value,
        waybill=waybill,
        car=car,
        app_export_time=app_export_time,
        excel_row=excel_row,
    )
    append_freight_event(FREIGHT_CONFIRMATION_PATH, event)
    return event


def append_freight_scope_complete(sheet_name, source_kind, source_value, **extra):
    event = {
        "version": 1,
        "event": "freight_scope_complete",
        "run_id": RUN_ID,
        "sheet": sheet_name,
        "source_kind": source_kind,
        "source_value": source_value,
        **extra,
    }
    append_freight_event(FREIGHT_CONFIRMATION_PATH, event)


def get_freight_entry_state():
    """识别运费列表、运费详情、业务页、采购页和园区主页。"""
    ensure_native_context()
    locators = FREIGHT_ENTRY_LOCATORS

    # 详情页可能仍保留列表控件，必须用详情数值字段优先识别。
    # sure 是运费列表页本身的全局确定按钮，不能据此判断进入了详情页。
    if (
        locator_visible_now(locators["settlement_quantity"])
        or locator_visible_now(locators["unit_price"])
    ):
        return "freight_detail"

    has_purchase_dates = (
        locator_visible_now(locators["purchase_start_date"])
        or locator_visible_now(locators["purchase_end_date"])
    )
    if has_purchase_dates:
        return "purchase_page"

    has_search = locator_visible_now(locators["search"])
    has_sales_car = locator_visible_now(locators["sales_car"])
    has_business_data = locator_visible_now(locators["business_data"])
    has_freight_list = locator_visible_now((AppiumBy.ID, FREIGHT_LIST_ID))
    has_freight_action = locator_visible_now((AppiumBy.ID, FREIGHT_ACTION_ID))
    has_business_back = locator_visible_now(locators["business_back"])

    # 上海运费页用 business_data，查询后也可用列表/运费按钮识别；
    # 河北空结果页可能只有 search，因此还需排除销售页和采购页标志。
    if has_search and (
        has_business_data
        or has_freight_action
        or (has_freight_list and not has_sales_car)
        or (not has_sales_car and not has_business_back)
    ):
        return "freight_list_ready"

    if locator_visible_now(locators["freight_icon"]):
        return "park_icon_ready"
    if has_business_back:
        return "business_page"
    if locator_visible_now(locators["park_tab"]):
        return "park_home"
    if locator_visible_now(locators["back"]):
        return "inner_page"
    return "unknown"


def wait_for_freight_entry_state(expected_states, timeout=15):
    expected_states = set(expected_states)

    def condition(_driver):
        state = get_freight_entry_state()
        return state if state in expected_states else False

    return fast_wait(timeout).until(condition)


def wait_for_freight_list_ready(timeout=20, stable_rounds=2):
    """运费列表状态连续稳定多轮才视为就绪，避免切页时读取旧控件。"""
    stable_count = 0

    def condition(_driver):
        nonlocal stable_count
        if get_freight_entry_state() == "freight_list_ready":
            stable_count += 1
            return "freight_list_ready" if stable_count >= stable_rounds else False
        stable_count = 0
        return False

    return fast_wait(timeout).until(condition)


def enter_freight_confirmation_page(max_rounds=3):
    """按当前页面状态幂等进入运费列表；已在列表时不重复进入。"""
    print("\n================ 阶段三：运费确认处理 ================")
    activate_tizi_and_wait(timeout=12)
    locators = FREIGHT_ENTRY_LOCATORS
    last_error = None

    for round_no in range(1, max_rounds + 1):
        try:
            state = get_freight_entry_state()
            print(
                f"  [运费确认入口] 第 {round_no}/{max_rounds} 轮，"
                f"当前页面状态：{state}"
            )

            if state == "freight_list_ready":
                wait_for_freight_list_ready(timeout=15)
                print("  ✅ 运费确认列表已就绪，复用当前页面。")
                return True

            if state == "freight_detail":
                if not click_back_to_list(timeout=12):
                    raise RuntimeError("无法从运费详情或确认层返回运费列表")
                wait_for_freight_list_ready(timeout=15)
                print("  ✅ 已从运费详情返回运费列表。")
                return True

            if state == "park_icon_ready":
                click_step(
                    locators["freight_icon"],
                    "运费确认 icon_yuanqu_10",
                    next_locator=locators["search"],
                    timeout=12,
                    retries=3,
                )
                wait_for_freight_list_ready(timeout=20)
                print("  ✅ 已从园区入口进入运费确认列表。")
                return True

            if state == "business_page":
                click_step(
                    locators["business_back"],
                    "返回主业务层 image_main",
                    next_locator=locators["freight_icon"],
                    timeout=12,
                    retries=3,
                )
                continue

            if state == "park_home":
                click_step(
                    locators["park_tab"],
                    "园区页签 rd_user_yq",
                    next_locator=locators["freight_icon"],
                    timeout=12,
                    retries=3,
                )
                continue

            if state in {"purchase_page", "inner_page"}:
                back_button = wait_clickable(locators["back"], timeout=12)
                back_button.click()
                print(f"  [运费确认入口] 已退出 {state}，重新识别页面层级...")
                wait_for_freight_entry_state(
                    {
                        "freight_list_ready",
                        "park_icon_ready",
                        "business_page",
                        "park_home",
                        "purchase_page",
                        "inner_page",
                    },
                    timeout=15,
                )
                continue

            print("  [运费确认入口] 页面标志暂未稳定，先等待后再识别...")
            try:
                wait_for_freight_entry_state(
                    {
                        "freight_list_ready",
                        "freight_detail",
                        "park_icon_ready",
                        "business_page",
                        "park_home",
                        "purchase_page",
                        "inner_page",
                    },
                    timeout=8,
                )
            except TimeoutException:
                print("  [运费确认入口] 页面持续未知，尝试系统返回键恢复层级...")
                driver.back()
                time.sleep(1.0)

        except Exception as exc:
            raise_if_appium_session_lost(exc, "恢复运费确认入口")
            last_error = exc
            try:
                failed_state = get_freight_entry_state()
            except Exception:
                failed_state = "无法识别"
            print(
                f"  ⚠️ [运费确认入口] 第 {round_no}/{max_rounds} 轮未完成；"
                f"当前状态：{failed_state}；{type(exc).__name__}: {exc}"
            )
            if round_no < max_rounds:
                activate_tizi_and_wait(timeout=12)
                time.sleep(0.8)

    raise FreightEntryError(
        f"运费确认入口连续 {max_rounds} 轮恢复失败"
    ) from last_error


def wait_selected_freight_car(car_no, timeout=12):
    expected = normalize_freight_car(car_no)
    locator = (AppiumBy.ID, "com.ibb.tizi:id/business_data")

    def condition(d):
        try:
            values = [
                normalize_freight_car(el.text)
                for el in d.find_elements(*locator)
                if el.is_displayed()
            ]
            return car_no if expected and expected in values else False
        except (NoSuchElementException, StaleElementReferenceException):
            return False

    ensure_native_context()
    return fast_wait(timeout).until(condition)


def select_freight_car(car_no):
    """上海运费确认按完整车牌精确切换，并验证 business_data 的回显。"""
    click_step(
        (AppiumBy.ID, "com.ibb.tizi:id/business_data"),
        f"打开运费确认车牌选择（目标 {car_no}）",
        timeout=12,
        retries=3,
    )
    escaped = str(car_no).replace("\\", "\\\\").replace('"', '\\"')
    option = (
        AppiumBy.ANDROID_UIAUTOMATOR,
        f'new UiSelector().text("{escaped}")',
    )
    click_step(option, f"选择运费确认车牌 {car_no}", timeout=12, retries=3)
    selected = wait_selected_freight_car(car_no, timeout=15)
    print(f"  ✅ 运费确认车牌切换完成：{selected}")


def query_freight_confirmation(desc):
    query_current_vehicle(desc, timeout=15)


def _perform_freight_swipe(start_x, start_y, end_y, *, controlled):
    actions = ActionChains(driver)
    actions.w3c_actions = ActionBuilder(
        driver,
        mouse=PointerInput(interaction.POINTER_TOUCH, "freight_touch"),
    )
    pointer = actions.w3c_actions.pointer_action
    pointer.move_to_location(int(start_x), int(start_y))
    pointer.pointer_down()
    pointer.pause(0.12 if controlled else 0.02)
    pointer.move_to_location(int(start_x), int(end_y))
    if controlled:
        pointer.pause(0.22)
    pointer.release()
    actions.perform()
    time.sleep(0.75 if controlled else 0.45)


def _freight_snapshot(card, rv_rect, sheet_name, source_kind, source_value, car_no):
    outbound = read_first_nonempty_child_text(card, FREIGHT_TIME_ID, retries=2, interval=0.15)
    waybill = read_first_nonempty_child_text(card, FREIGHT_WAYBILL_ID, retries=2, interval=0.15)
    raw_spec = read_first_nonempty_child_text(card, FREIGHT_SPEC_ID, retries=2, interval=0.15)
    try:
        rect = dict(card.rect)
    except Exception:
        rect = {"x": 0, "y": 0, "width": 0, "height": 0}
    try:
        element_id = str(card.id or "")
    except Exception:
        element_id = ""
    rv_top = int(rv_rect.get("y", 0))
    rv_bottom = rv_top + int(rv_rect.get("height", 0))
    card_top = int(rect.get("y", 0))
    card_bottom = card_top + int(rect.get("height", 0))
    try:
        has_action = any(el.is_displayed() for el in card.find_elements(AppiumBy.ID, FREIGHT_ACTION_ID))
    except StaleElementReferenceException:
        has_action = False
    fully_visible = bool(
        card_top >= rv_top - 8
        and card_bottom <= rv_bottom + 8
        and int(rect.get("height", 0)) > 0
    )
    complete = bool(
        outbound
        and normalize_freight_waybill(waybill)
        and normalize_freight_spec(raw_spec)
        and has_action
        and fully_visible
    )
    incomplete_reasons = []
    if not outbound:
        incomplete_reasons.append("缺少出库时间")
    if not normalize_freight_waybill(waybill):
        incomplete_reasons.append("缺少运单号")
    if not normalize_freight_spec(raw_spec):
        incomplete_reasons.append("缺少规格")
    if not has_action:
        incomplete_reasons.append("缺少选择按钮")
    if not fully_visible:
        incomplete_reasons.append("卡片被遮挡")
    identity = {
        "sheet": sheet_name,
        "source_kind": source_kind,
        "source_value": source_value,
        "car": car_no,
        "waybill": waybill,
        "outbound_datetime": outbound,
        "raw_spec": raw_spec,
    }
    return {
        **identity,
        "waybill": normalize_freight_waybill(waybill),
        "normalized_spec": normalize_freight_spec(raw_spec),
        "outbound_datetime": normalize_freight_datetime(outbound),
        "card_key": freight_card_key(identity),
        "fully_visible": fully_visible,
        "complete": complete,
        "has_action": bool(has_action),
        "incomplete_reasons": tuple(incomplete_reasons),
        "rect": rect,
        "element_id": element_id,
        "element": card,
    }


def correct_freight_clipped_visibility(snapshots):
    """Downgrade RecyclerView card fragments whose clipped root still fits the viewport."""
    complete_heights = [
        int((item.get("rect") or {}).get("height", 0))
        for item in snapshots
        if item.get("complete")
        and int((item.get("rect") or {}).get("height", 0)) > 0
    ]
    if not complete_heights:
        return snapshots
    reference_height = max(complete_heights)
    minimum_height = max(
        1,
        int(reference_height * FREIGHT_MIN_RELATIVE_FULL_HEIGHT),
    )
    for item in snapshots:
        height = int((item.get("rect") or {}).get("height", 0))
        if (
            item.get("fully_visible")
            and 0 < height < minimum_height
        ):
            item["fully_visible"] = False
            item["complete"] = False
            reasons = list(item.get("incomplete_reasons") or ())
            if "卡片被遮挡" not in reasons:
                reasons.append("卡片被遮挡")
            item["incomplete_reasons"] = tuple(reasons)
            if FREIGHT_DEBUG:
                print(
                    "  [运费调试判定] RecyclerView 根节点虽在列表边界内，"
                    f"但高度 {height}px 小于完整卡参考 {reference_height}px 的 "
                    f"{FREIGHT_MIN_RELATIVE_FULL_HEIGHT:.0%}，按遮挡残片处理。",
                    flush=True,
                )
    return snapshots


def get_visible_freight_snapshots(sheet_name, source_kind, source_value, car_no, timeout=5):
    while True:
        try:
            rv = wait_visible((AppiumBy.ID, FREIGHT_LIST_ID), timeout=timeout)
            break
        except TimeoutException as error:
            if wireless_link.is_enabled() and is_appium_session_lost(error):
                raise
            if wireless_link.is_enabled() and _ui_pause_if_external("freight_list_empty"):
                continue
            return None, []
    rv_rect = dict(rv.rect)
    try:
        cards = driver.find_elements(
            AppiumBy.XPATH,
            '//*[@resource-id="com.ibb.tizi:id/rv"]/*',
        )
    except Exception as exc:
        raise_if_appium_session_lost(exc, "读取运费确认列表")
        raise
    snapshots = [
        _freight_snapshot(card, rv_rect, sheet_name, source_kind, source_value, car_no)
        for card in cards
    ]
    correct_freight_clipped_visibility(snapshots)
    snapshots.sort(key=lambda item: int(item["rect"].get("y", 0)), reverse=True)
    return rv, annotate_card_occurrences(snapshots)


def log_freight_debug_snapshots(label, snapshots):
    if not FREIGHT_DEBUG:
        return
    print(f"  [运费调试] {label}：可见卡片 {len(snapshots)} 张", flush=True)
    for index, item in enumerate(snapshots, start=1):
        rect = item.get("rect") or {}
        reasons = "、".join(item.get("incomplete_reasons") or ()) or "无"
        element_id = str(item.get("element_id") or "")
        print(
            "  [运费调试卡] "
            f"#{index} id=...{element_id[-16:] if element_id else 'EMPTY'} "
            f"x={int(rect.get('x', 0))} y={int(rect.get('y', 0))} "
            f"w={int(rect.get('width', 0))} h={int(rect.get('height', 0))} "
            f"完整显示={bool(item.get('fully_visible'))} "
            f"可进详情={bool(item.get('complete'))} "
            f"按钮={bool(item.get('has_action'))} "
            f"运单={item.get('waybill') or '?'} "
            f"时间={item.get('outbound_datetime') or '?'} "
            f"规格={item.get('raw_spec') or '?'} "
            f"不完整原因={reasons}",
            flush=True,
        )


def dump_freight_debug_artifacts(label):
    if not FREIGHT_DEBUG or driver is None:
        return
    safe_label = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(label or "freight")).strip("._")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    debug_dir = Path(BASE_DIR) / "freight_debug" / (RUN_ID or "flat")
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = debug_dir / f"{stamp}_{safe_label}.png"
        xml_path = debug_dir / f"{stamp}_{safe_label}.xml"
        driver.save_screenshot(str(screenshot_path))
        xml_path.write_text(driver.page_source, encoding="utf-8")
        print(
            f"  [运费调试现场] SCREENSHOT:{screenshot_path}|XML:{xml_path}",
            flush=True,
        )
    except Exception as debug_error:
        print(
            f"  [运费调试现场] 保存失败：{type(debug_error).__name__}: {debug_error}",
            flush=True,
        )


def freight_visible_signature(sheet_name, source_kind, source_value, car_no):
    _rv, snapshots = get_visible_freight_snapshots(
        sheet_name, source_kind, source_value, car_no, timeout=3
    )
    return tuple(item["instance_key"] for item in snapshots if item["waybill"])


def latest_visible_freight_datetime(sheet_name, source_kind, source_value, car_no):
    _rv, snapshots = get_visible_freight_snapshots(
        sheet_name, source_kind, source_value, car_no, timeout=3
    )
    values = [item["outbound_datetime"] for item in snapshots if item["outbound_datetime"]]
    return max(values) if values else ""


def fling_freight_list_to_bottom(sheet_name, source_kind, source_value, car_no, max_swipes=16):
    """优先原生 flingToEnd；不支持时使用高速度手势，并用卡片签名确认到底。"""
    rv, snapshots = get_visible_freight_snapshots(
        sheet_name, source_kind, source_value, car_no, timeout=6
    )
    if rv is None or not snapshots:
        print("  ℹ️ 运费确认查询没有可见卡片。")
        return None
    print("  [运费列表] 使用最大步幅和惯性快速滑到列表底部...")
    try:
        driver.find_element(
            AppiumBy.ANDROID_UIAUTOMATOR,
            'new UiScrollable(new UiSelector().resourceId("com.ibb.tizi:id/rv"))'
            '.setAsVerticalList().flingToEnd(40)',
        )
        time.sleep(0.8)
    except Exception as exc:
        raise_if_appium_session_lost(exc, "运费列表原生快速到底")

    previous = freight_visible_signature(sheet_name, source_kind, source_value, car_no)
    previous_latest = latest_visible_freight_datetime(
        sheet_name, source_kind, source_value, car_no
    )
    fling_direction = "down"
    stable_rounds = 0
    for _ in range(max_swipes):
        rv, _snapshots = get_visible_freight_snapshots(
            sheet_name, source_kind, source_value, car_no, timeout=4
        )
        if rv is None:
            return False
        rect = rv.rect
        moved = None
        try:
            moved = driver.execute_script(
                "mobile: flingGesture",
                {
                    "elementId": rv.id,
                    "direction": fling_direction,
                    "speed": 7500,
                },
            )
            time.sleep(0.45)
        except Exception as exc:
            raise_if_appium_session_lost(exc, "运费列表惯性滑动")
            x = int(rect["x"] + rect["width"] / 2)
            _perform_freight_swipe(
                x,
                int(rect["y"] + rect["height"] - 100),
                int(rect["y"] + 100),
                controlled=False,
            )
        current = freight_visible_signature(sheet_name, source_kind, source_value, car_no)
        current_latest = latest_visible_freight_datetime(
            sheet_name, source_kind, source_value, car_no
        )
        if (
            moved is not False
            and previous_latest
            and current_latest
            and current_latest < previous_latest
            and fling_direction == "down"
        ):
            # 不同 Appium/系统版本对 fling 方向语义可能相反；用业务时间自动校正。
            fling_direction = "up"
            print("  ℹ️ 检测到运费列表时间向旧方向变化，自动反转惯性滑动方向。")
            driver.execute_script(
                "mobile: flingGesture",
                {
                    "elementId": rv.id,
                    "direction": fling_direction,
                    "speed": 7500,
                },
            )
            time.sleep(0.45)
            current = freight_visible_signature(
                sheet_name, source_kind, source_value, car_no
            )
            current_latest = latest_visible_freight_datetime(
                sheet_name, source_kind, source_value, car_no
            )
        if current and current == previous:
            stable_rounds += 1
        else:
            stable_rounds = 0
        if moved is False or stable_rounds >= 2:
            print("  ✅ 运费确认列表已到达底部。")
            return True
        previous = current
        previous_latest = current_latest
    print("  ⚠️ 运费确认列表达到最大快速滑动次数，将从当前最靠底部位置遍历。")
    return False


def scroll_freight_toward_older(rv, snapshots, *, small=False, frontier=None):
    rect = rv.rect
    full_heights = [
        int(item["rect"].get("height", 0))
        for item in snapshots
        if item.get("fully_visible")
    ]
    card_height = max(full_heights) if full_heights else int(rect["height"] * 0.55)
    fraction = 0.30 if small else 0.80
    distance = min(int(card_height * fraction), int(rect["height"] * (0.35 if small else 0.68)))
    if frontier is not None:
        frontier_rect = frontier.get("rect") or {}
        frontier_height = int(frontier_rect.get("height", 0))
        frontier_bottom = int(frontier_rect.get("y", 0)) + frontier_height
        rv_bottom = int(rect["y"]) + int(rect["height"])
        bottom_gap = max(0, rv_bottom - frontier_bottom)
        # Move the handled bottom-most card out of the complete processing slot.
        # The next identical physical card may otherwise inherit occurrence 1
        # when only one complete card fits in the viewport.
        distance = max(
            distance,
            bottom_gap + max(48, int(frontier_height * 0.08)),
        )
    distance = max(180, distance)
    x = int(rect["x"] + rect["width"] / 2)
    start_y = int(rect["y"] + max(90, rect["height"] * 0.12))
    end_y = min(int(rect["y"] + rect["height"] - 90), start_y + distance)
    _perform_freight_swipe(x, start_y, end_y, controlled=True)


def freight_layout_signature(snapshots):
    """Describe stable visible content and geometry, excluding transient element IDs."""
    return tuple(
        (
            item.get("waybill") or "",
            item.get("outbound_datetime") or "",
            item.get("normalized_spec") or "",
            int((item.get("rect") or {}).get("y", 0)),
            int((item.get("rect") or {}).get("height", 0)),
            bool(item.get("fully_visible")),
            bool(item.get("complete")),
        )
        for item in snapshots
    )


def freight_view_slot_key(item):
    """Identify one physical card slot while the viewport itself has not moved."""
    rect = item.get("rect") or {}
    return (
        item.get("card_key") or "",
        round(int(rect.get("y", 0)) / 8),
        round(int(rect.get("height", 0)) / 8),
    )


def freight_handled_key(item):
    return ("slot", freight_view_slot_key(item))


def same_freight_view_slot(current, expected, tolerance=24):
    """Relocate a card by content group and screen slot, not transient occurrence."""
    if current.get("card_key") != expected.get("card_key"):
        return False
    current_rect = current.get("rect") or {}
    expected_rect = expected.get("rect") or {}
    return abs(
        int(current_rect.get("y", 0)) - int(expected_rect.get("y", 0))
    ) <= tolerance


def same_freight_entity(current, expected, tolerance=24):
    """Track one rebound Appium entity during a single list-scroll phase."""
    current_y = int((current.get("rect") or {}).get("y", 0))
    expected_y = int((expected.get("rect") or {}).get("y", 0))
    # The processed card can only stay in place or move downward. An adjacent
    # identical card entering above the old slot is a new physical card.
    if current_y < expected_y - tolerance:
        return False
    expected_id = str(expected.get("element_id") or "")
    current_id = str(current.get("element_id") or "")
    if expected_id:
        return bool(current_id and expected_id == current_id)
    if current.get("card_key") != expected.get("card_key"):
        return False
    return same_freight_view_slot(current, expected, tolerance=tolerance)


def visible_freight_entity(snapshots, expected):
    exact = next(
        (item for item in snapshots if same_freight_entity(item, expected)),
        None,
    )
    if exact is not None:
        return exact

    expected_id = str(expected.get("element_id") or "")
    if not expected_id:
        raise FreightTechnicalError("运费确认推进前未绑定实体卡元素ID")
    expected_y = int((expected.get("rect") or {}).get("y", 0))
    ambiguous = [
        item for item in snapshots
        if item.get("card_key") == expected.get("card_key")
        and int((item.get("rect") or {}).get("y", 0)) >= expected_y - 24
        and str(item.get("element_id") or "") != expected_id
    ]
    if ambiguous:
        raise FreightTechnicalError(
            "运费确认单次滚动中元素ID发生变化，且相同业务卡仍位于旧卡移动区域；"
            "无法安全区分ID重建与下一张相同卡，停止本范围以防重复或漏卡"
        )
    return None


def advance_past_freight_frontier(
    rv,
    snapshots,
    frontier,
    sheet_name,
    source_kind,
    source_value,
    car_no,
):
    """Do not release a handled card until that exact Appium entity is incomplete."""
    if not str(frontier.get("element_id") or ""):
        raise FreightTechnicalError("运费确认无法取得实体卡元素ID，禁止盲目推进")

    current_rv = rv
    current_snapshots = snapshots
    current_frontier = frontier
    moved_any = False

    log_freight_debug_snapshots("单卡推进前", current_snapshots)
    before = freight_layout_signature(current_snapshots)
    print("  [运费单卡推进] 执行一次大步推进，再用小步对齐；禁止连续大步...")
    scroll_freight_toward_older(
        current_rv,
        current_snapshots,
        small=False,
        frontier=current_frontier,
    )
    next_rv, next_snapshots = get_visible_freight_snapshots(
        sheet_name, source_kind, source_value, car_no, timeout=4
    )
    log_freight_debug_snapshots("一次大步后", next_snapshots)
    after = freight_layout_signature(next_snapshots)
    moved_any = after != before
    same_entity = visible_freight_entity(next_snapshots, frontier)
    if same_entity is None or not same_entity.get("fully_visible"):
        print("  ✅ 一次大步后已处理运费实体卡已退出完整处理位。")
        return bool(next_snapshots)
    current_rv = next_rv or current_rv
    current_snapshots = next_snapshots
    current_frontier = same_entity

    for attempt in range(1, FREIGHT_FRONTIER_SMALL_STEPS + 1):
        before = freight_layout_signature(current_snapshots)
        print(
            f"  [运费单卡推进] 小步对齐 {attempt}/{FREIGHT_FRONTIER_SMALL_STEPS}，"
            "等待已处理实体卡退出完整处理位..."
        )
        scroll_freight_toward_older(
            current_rv,
            current_snapshots,
            small=True,
            frontier=current_frontier,
        )
        next_rv, next_snapshots = get_visible_freight_snapshots(
            sheet_name, source_kind, source_value, car_no, timeout=4
        )
        log_freight_debug_snapshots(
            f"单卡推进小步 {attempt}/{FREIGHT_FRONTIER_SMALL_STEPS} 后",
            next_snapshots,
        )
        after = freight_layout_signature(next_snapshots)
        moved_any = moved_any or after != before
        same_entity = visible_freight_entity(next_snapshots, frontier)
        if same_entity is None or not same_entity.get("fully_visible"):
            print("  ✅ 小步对齐后已处理运费实体卡已退出完整处理位。")
            return bool(next_snapshots)
        current_rv = next_rv or current_rv
        current_snapshots = next_snapshots
        current_frontier = same_entity

    if not moved_any:
        return False
    raise FreightTechnicalError(
        "运费确认列表虽发生移动，但已处理实体卡仍完整可点击；"
        "为防止重复提取，本范围停止并交由恢复流程重试"
    )


def _return_from_freight_detail_if_needed():
    detail_value_visible = (
        locator_visible_now((AppiumBy.ID, "com.ibb.tizi:id/jiesuanshu"))
        or locator_visible_now((AppiumBy.ID, "com.ibb.tizi:id/unit_price"))
    )
    back_without_list = (
        locator_visible_now((AppiumBy.ID, "com.ibb.tizi:id/iv_left"))
        and not locator_visible_now((AppiumBy.ID, FREIGHT_LIST_ID))
    )
    if detail_value_visible or back_without_list:
        if not click_back_to_list(timeout=10):
            raise RuntimeError("运费详情读取后无法返回列表")


def process_one_freight_card(
    snapshot,
    sheet_name,
    source_kind,
    source_value,
    car_no,
    captured_fact_counts,
    card_occurrence,
):
    """重新定位当前卡片后读取详情；成功事件先落盘，再返回列表。"""
    last_error = None
    for attempt in range(1, FREIGHT_CARD_MAX_ATTEMPTS + 1):
        saved_event = None
        try:
            _rv, current_snapshots = get_visible_freight_snapshots(
                sheet_name, source_kind, source_value, car_no, timeout=5
            )
            current = next(
                (
                    item for item in current_snapshots
                    if same_freight_view_slot(item, snapshot, tolerance=48)
                ),
                None,
            )
            if current is None or not current["complete"]:
                raise RuntimeError("目标运费卡片已离开完整可见区域")
            actions = current["element"].find_elements(AppiumBy.ID, FREIGHT_ACTION_ID)
            if not actions:
                raise RuntimeError("目标运费卡片未找到运费选择按钮")
            actions[0].click()
            click_step(
                (AppiumBy.ID, "com.ibb.tizi:id/sure"),
                "运费确认详情确定按钮",
                timeout=10,
                retries=2,
            )
            quantity_text = wait_nonempty_text(
                (AppiumBy.ID, "com.ibb.tizi:id/jiesuanshu"), timeout=12
            )
            unit_price_text = wait_nonempty_text(
                (AppiumBy.ID, "com.ibb.tizi:id/unit_price"), timeout=12
            )
            quantity = parse_freight_number(quantity_text)
            unit_price = parse_freight_number(unit_price_text)
            if quantity is None or unit_price is None:
                raise ValueError(
                    f"结算数或单价不是有效数值：{quantity_text!r}/{unit_price_text!r}"
                )
            fact_identity = {
                **current,
                "settlement_quantity": quantity,
                "unit_price": unit_price,
            }
            fact_key = freight_fact_key(fact_identity)
            record_occurrence = captured_fact_counts.get(fact_key, 0) + 1
            event = make_freight_event(
                event_name="freight_success",
                run_id=RUN_ID,
                sheet=sheet_name,
                source_kind=source_kind,
                source_value=source_value,
                car=car_no,
                waybill=current["waybill"],
                outbound_datetime=current["outbound_datetime"],
                raw_spec=current["raw_spec"],
                settlement_quantity=quantity,
                unit_price=unit_price,
                attempt=attempt,
                card_occurrence=card_occurrence,
                record_occurrence=record_occurrence,
            )
            append_freight_event(FREIGHT_CONFIRMATION_PATH, event)
            captured_fact_counts[fact_key] = record_occurrence
            saved_event = event
            print(
                f"  ✅ 运费确认采集：{current['waybill']} / {current['raw_spec']} / "
                f"结算量 {quantity} / 单价 {unit_price} / "
                f"同卡片组第 {card_occurrence} 张"
            )
            if not click_back_to_list(timeout=12):
                raise RuntimeError("运费详情成功落盘后无法返回列表")
            return event
        except Exception as exc:
            raise_if_appium_session_lost(exc, "读取运费确认卡片详情")
            last_error = exc
            print(
                f"  ⚠️ 运费卡片 {snapshot['waybill']}/{snapshot['raw_spec']} "
                f"第 {attempt}/{FREIGHT_CARD_MAX_ATTEMPTS} 次读取失败：{exc}"
            )
            try:
                _return_from_freight_detail_if_needed()
            except Exception as back_error:
                raise_if_appium_session_lost(back_error, "恢复运费确认列表")
                last_error = back_error
            if saved_event is not None:
                raise FreightTechnicalError(
                    "运费卡片成功事件已落盘，但返回列表失败；交由范围级恢复，禁止重复记为下一实例"
                ) from last_error
            if attempt < FREIGHT_CARD_MAX_ATTEMPTS:
                time.sleep(0.6)

    failure = make_freight_event(
        event_name="freight_failure",
        run_id=RUN_ID,
        sheet=sheet_name,
        source_kind=source_kind,
        source_value=source_value,
        car=car_no,
        waybill=snapshot["waybill"],
        outbound_datetime=snapshot["outbound_datetime"],
        raw_spec=snapshot["raw_spec"],
        error=str(last_error or "未知详情读取错误"),
        attempt=FREIGHT_CARD_MAX_ATTEMPTS,
        card_occurrence=card_occurrence,
    )
    append_freight_event(FREIGHT_CONFIRMATION_PATH, failure)
    return None


def process_freight_scope(sheet_name, source_kind, source_value, car_no):
    data = load_freight_data(FREIGHT_CONFIRMATION_PATH)
    scope = freight_scope_key(sheet_name, source_kind, source_value)
    targets = data.targets_for_scope(sheet_name, source_kind, source_value)
    if scope in data.completed_scopes:
        print(f"  ♻️ 运费确认范围已完成，跳过重复遍历：{source_value}")
        return 0
    eligible_targets = []
    expired_targets = []
    for item in targets:
        if freight_target_within_retry_window(
            item.get("waybill"),
            item.get("app_export_time"),
            ANCHOR_DATE,
            FREIGHT_RETRY_DAYS,
        ):
            eligible_targets.append(item)
        else:
            expired_targets.append(item)
    if expired_targets:
        expired_summary = ", ".join(
            f"{normalize_freight_waybill(item.get('waybill'))}"
            f"（{freight_target_age_days(item.get('waybill'), item.get('app_export_time'), ANCHOR_DATE)}天）"
            for item in expired_targets
        )
        print(
            f"  [运费补查期限] 已跳过超过 {FREIGHT_RETRY_DAYS} 天的目标："
            f"{expired_summary}",
            flush=True,
        )
    targets = eligible_targets
    if not targets:
        if expired_targets:
            print(
                f"  ℹ️ {source_value} 本范围目标均已超过运费自动补查期限，"
                "本轮不进入运费确认查询。"
            )
            return 0
        print(f"  ℹ️ {source_value} 没有实际写入销售表的运单，运费确认无需遍历。")
        append_freight_scope_complete(
            sheet_name, source_kind, source_value, target_count=0, success_count=0
        )
        return 0

    target_waybills = {
        normalize_freight_waybill(item.get("waybill")) for item in targets
        if normalize_freight_waybill(item.get("waybill"))
    }
    seeded_target_waybills = {
        normalize_freight_waybill(item.get("waybill")) for item in targets
        if item.get("seeded_from_database")
        and normalize_freight_waybill(item.get("waybill"))
    }
    current_sales_target_waybills = target_waybills.difference(seeded_target_waybills)
    print(
        f"  [运费目标] {source_value} 本范围待处理 {len(target_waybills)} 个："
        f"{', '.join(sorted(target_waybills))}",
        flush=True,
    )
    if seeded_target_waybills:
        print(
            "  [运费目标来源] 数据库未完成续补："
            f"{', '.join(sorted(seeded_target_waybills))}",
            flush=True,
        )
    if current_sales_target_waybills:
        print(
            "  [运费目标来源] 本轮销售新增："
            f"{', '.join(sorted(current_sales_target_waybills))}",
            flush=True,
        )
    known_scope_records = data.records_for_scope(sheet_name, source_kind, source_value)
    if known_scope_records:
        known_summary = ", ".join(
            f"{record.waybill}/{record.settlement_quantity}/{record.unit_price}"
            for record in known_scope_records
        )
        print(
            f"  [运费断点] 已载入历史成功规格 {len(known_scope_records)} 条："
            f"{known_summary}",
            flush=True,
        )
    target_dates = sorted(
        value
        for value in (
            freight_waybill_date_part(item.get("waybill"))
            or freight_date_part(item.get("app_export_time"))
            for item in targets
        )
        if value
    )
    oldest_target_date = target_dates[0] if target_dates else SALES_START_DATE
    seen_target_waybills = set()
    failed_instance_keys = set()
    failed_waybills = set()
    captured_record_keys = set()
    captured_fact_counts = {}
    captured_card_counts = {}
    handled_view_cards = []
    incomplete_alignment_steps = 0
    boundary_reached = False

    enter_freight_confirmation_page()
    if source_kind == "selected_car":
        select_freight_car(source_value)
    query_freight_confirmation(f"运费确认 {source_value} 查询")
    bottom_confirmed = fling_freight_list_to_bottom(
        sheet_name, source_kind, source_value, car_no
    )
    if bottom_confirmed is False:
        raise FreightTechnicalError(
            f"运费确认 {source_value} 查询后无法确认已到列表底部"
        )

    for step in range(1, FREIGHT_MAX_TRAVERSAL_STEPS + 1):
        rv, snapshots = get_visible_freight_snapshots(
            sheet_name, source_kind, source_value, car_no, timeout=5
        )
        log_freight_debug_snapshots(
            f"范围 {source_value} 遍历第 {step}/{FREIGHT_MAX_TRAVERSAL_STEPS} 步",
            snapshots,
        )
        if rv is None or not snapshots:
            boundary_reached = True
            break

        traversal_cards = [
            item for item in snapshots
            if item.get("fully_visible")
            and item.get("waybill")
            and item.get("outbound_datetime")
        ]
        frontier = next(
            (
                item for item in traversal_cards
                if not any(
                    same_freight_view_slot(item, handled, tolerance=48)
                    for handled in handled_view_cards
                )
            ),
            None,
        )
        topmost_complete_y = min(
            (
                int((item.get("rect") or {}).get("y", 0))
                for item in traversal_cards
            ),
            default=None,
        )

        def partial_ahead(item):
            if item.get("fully_visible") or int((item.get("rect") or {}).get("height", 0)) <= 0:
                return False
            if topmost_complete_y is None:
                return True
            return int((item.get("rect") or {}).get("y", 0)) < topmost_complete_y - 8

        partial_target = any(
            item["waybill"] in target_waybills and partial_ahead(item)
            for item in snapshots
        )
        partial_card_present = any(partial_ahead(item) for item in snapshots)
        if frontier is not None:
            incomplete_alignment_steps = 0
            handled_view_cards.append(frontier)
            card_date = freight_date_part(frontier["outbound_datetime"])
            if frontier["waybill"] in target_waybills:
                if not frontier["complete"]:
                    reasons = "、".join(frontier.get("incomplete_reasons") or ("字段不完整",))
                    raise FreightTechnicalError(
                        f"目标运费卡片已完整显示但不能进入详情："
                        f"{frontier['waybill']}（{reasons}）"
                    )
                seen_target_waybills.add(frontier["waybill"])
                occurrence = next_card_occurrence(
                    captured_card_counts,
                    frontier["card_key"],
                )
                event = process_one_freight_card(
                    frontier,
                    sheet_name,
                    source_kind,
                    source_value,
                    car_no,
                    captured_fact_counts,
                    occurrence,
                )
                if event is not None:
                    captured_record_keys.add(event["record_key"])
                else:
                    failed_instance_keys.add(f"{frontier['card_key']}:{occurrence}")
                    failed_waybills.add(frontier["waybill"])
            else:
                print(
                    "  [运费非目标卡] 已核对并跳过："
                    f"{frontier['waybill']} / {frontier['outbound_datetime']} / "
                    f"{frontier.get('raw_spec') or '?'}（不在本范围目标列表）",
                    flush=True,
                )
                if (
                    not partial_target
                    and freight_scan_past_target_date(card_date, oldest_target_date)
                ):
                    print(
                        f"  ✅ 当前运费卡日期 {card_date} 已严格早于最早待处理日期 "
                        f"{oldest_target_date}，结束本范围遍历。"
                    )
                    boundary_reached = True
                    break
            continue

        if traversal_cards:
            # All complete cards in this viewport were handled from bottom to top.
            # The current snapshots were obtained after any detail-page return,
            # so this is the one safe point to bind a fresh Appium element ID.
            rebound_frontier = traversal_cards[-1]
            rebound_matches_handled = any(
                same_freight_view_slot(rebound_frontier, handled, tolerance=48)
                for handled in handled_view_cards
            )
            if not rebound_matches_handled:
                raise FreightTechnicalError(
                    "运费确认推进前无法按业务内容和原屏幕槽位重新绑定已处理卡片"
                )
            if not str(rebound_frontier.get("element_id") or ""):
                raise FreightTechnicalError(
                    "运费确认推进前已找到原屏幕槽位，但无法取得新的实体卡元素ID"
                )
            print("  [运费实体重绑] 已按业务内容和原屏幕槽位绑定本轮元素ID。")
            advanced = advance_past_freight_frontier(
                rv,
                snapshots,
                rebound_frontier,
                sheet_name,
                source_kind,
                source_value,
                car_no,
            )
        else:
            visible_dates = sorted(
                value
                for value in (
                    freight_date_part(item.get("outbound_datetime"))
                    for item in snapshots
                )
                if value
            )
            if (
                not partial_target
                and oldest_target_date
                and visible_dates
                and visible_dates[0] < oldest_target_date
            ):
                print(
                    f"  ✅ 当前可见运费日期 {visible_dates[0]} 已早于最早目标日期 "
                    f"{oldest_target_date}，结束本范围遍历。"
                )
                boundary_reached = True
                break

            blocked_full_cards = [
                item for item in snapshots
                if item.get("fully_visible")
                and not (item.get("waybill") and item.get("outbound_datetime"))
            ]
            if blocked_full_cards:
                details = "; ".join(
                    "、".join(item.get("incomplete_reasons") or ("字段不完整",))
                    for item in blocked_full_cards
                )
                raise FreightTechnicalError(
                    f"运费确认 {source_value} 存在完整显示但无法识别运单和时间的卡片：{details}"
                )

            incomplete_alignment_steps += 1
            if incomplete_alignment_steps > FREIGHT_INCOMPLETE_ALIGN_MAX_STEPS:
                details = "; ".join(
                    f"{item.get('waybill') or '?'}@{item.get('outbound_datetime') or '?'}"
                    f"[{ '、'.join(item.get('incomplete_reasons') or ('未知',)) }]"
                    for item in snapshots
                )
                raise FreightTechnicalError(
                    f"运费确认 {source_value} 连续 "
                    f"{FREIGHT_INCOMPLETE_ALIGN_MAX_STEPS} 次小步仍没有完整可遍历卡片："
                    f"{details or '无可见卡片信息'}"
                )
            print(
                f"  [运费卡片对齐] 小步 "
                f"{incomplete_alignment_steps}/{FREIGHT_INCOMPLETE_ALIGN_MAX_STEPS}，"
                "等待半遮挡卡片完整进入处理位..."
            )
            previous_layout = freight_layout_signature(snapshots)
            scroll_freight_toward_older(rv, snapshots, small=True)
            _next_rv, next_snapshots = get_visible_freight_snapshots(
                sheet_name, source_kind, source_value, car_no, timeout=4
            )
            advanced = freight_layout_signature(next_snapshots) != previous_layout

        if not advanced:
            if partial_target or partial_card_present:
                raise FreightTechnicalError(
                    f"运费确认 {source_value} 已到物理边界，但目标运单仍有未完整显示卡片"
                )
            print("  ✅ 运费确认列表已到达较旧方向的物理边界。")
            boundary_reached = True
            break
        handled_view_cards.clear()
        if traversal_cards:
            incomplete_alignment_steps = 0

    if not boundary_reached:
        raise FreightTechnicalError(
            f"运费确认 {source_value} 达到最大遍历步数，无法确认列表边界"
        )

    missing_waybills = sorted(target_waybills.difference(seen_target_waybills))
    if missing_waybills:
        print(
            "  ⚠️ 本范围未找到运单："
            f"{', '.join(missing_waybills)}",
            flush=True,
        )
    for waybill in missing_waybills:
        failure = make_freight_event(
            event_name="freight_failure",
            run_id=RUN_ID,
            sheet=sheet_name,
            source_kind=source_kind,
            source_value=source_value,
            car=car_no,
            waybill=waybill,
            outbound_datetime="",
            raw_spec="",
            error="运费确认列表未找到该销售运单",
        )
        append_freight_event(FREIGHT_CONFIRMATION_PATH, failure)

    completed_waybills = sorted(seen_target_waybills.difference(failed_waybills))
    if completed_waybills:
        print(
            "  ✅ 本范围已确认运单："
            f"{', '.join(completed_waybills)}",
            flush=True,
        )
    success_count = len(captured_record_keys)
    append_freight_scope_complete(
        sheet_name,
        source_kind,
        source_value,
        target_count=len(target_waybills),
        success_count=success_count,
        missing_waybills=missing_waybills,
        failed_card_count=len(failed_instance_keys),
        failed_waybills=sorted(failed_waybills),
        completed_waybills=completed_waybills,
    )
    print(
        f"  ✅ 运费确认范围完成：{source_value}，目标运单 {len(target_waybills)} 个，"
        f"成功规格卡片 {success_count} 个，未找到运单 {len(missing_waybills)} 个。"
    )
    return success_count


def run_freight_scope_with_recovery(phone, sheet_name, source_kind, source_value, car_no):
    last_error = None
    rebuild_before_attempt = False
    for attempt in range(1, FREIGHT_SCOPE_MAX_ATTEMPTS + 1):
        try:
            if attempt > 1:
                if rebuild_before_attempt:
                    print(
                        f"  [运费确认恢复] {source_value} 第 "
                        f"{attempt}/{FREIGHT_SCOPE_MAX_ATTEMPTS} 次尝试："
                        "重建会话并重新登录，再按页面状态进入。"
                    )
                    recreate_appium_session(
                        f"运费确认 {source_value} 第 {attempt} 次恢复"
                    )
                    restart_app_keep_state()
                    login_with_account(phone)
                else:
                    activate_tizi_and_wait(timeout=12)
                    current_state = get_freight_entry_state()
                    print(
                        f"  [运费确认恢复] {source_value} 第 "
                        f"{attempt}/{FREIGHT_SCOPE_MAX_ATTEMPTS} 次尝试："
                        f"保留当前会话，识别页面状态为 {current_state}。"
                    )
            return process_freight_scope(
                sheet_name, source_kind, source_value, car_no
            )
        except Exception as exc:
            if isinstance(exc, (AppiumSessionOwnershipError, wireless_link.ConnectionUnavailable)):
                raise
            last_error = exc
            session_lost = is_appium_session_lost(exc)
            relogin_required = is_account_relogin_error(exc)
            dump_freight_debug_artifacts(
                f"{source_value}_attempt_{attempt}_{type(exc).__name__}"
            )
            print(
                f"  ⚠️ 运费确认 {source_value} 第 {attempt}/{FREIGHT_SCOPE_MAX_ATTEMPTS} "
                f"次未完成（会话失效={session_lost}，需要重登={relogin_required}）："
                f"{type(exc).__name__}: {exc}"
            )
            if attempt < FREIGHT_SCOPE_MAX_ATTEMPTS:
                # 入口连续跳转失败通常意味着账号被其他设备挤占，直接重登；
                # 非会话型普通 UI 故障先原地恢复一次，连续失败再重登。
                # 重登录后落在采购页，由入口状态机继续导航当前未完成范围。
                rebuild_before_attempt = relogin_required or attempt >= 2
                time.sleep(1.0)
    raise FreightTechnicalError(
        f"运费确认 {source_value} 连续 {FREIGHT_SCOPE_MAX_ATTEMPTS} 次恢复失败"
    ) from last_error


def run_shanghai_freight_stage_with_recovery(phone):
    total = 0
    for car_no in SHANGHAI_CARS:
        data = load_freight_data(FREIGHT_CONFIRMATION_PATH)
        targets = data.targets_for_scope("销售单-上海宾迪", "selected_car", car_no)
        if not targets:
            print(f"  ℹ️ 上海车辆 {car_no} 没有销售目标，跳过运费确认。")
            continue
        total += run_freight_scope_with_recovery(
            phone,
            "销售单-上海宾迪",
            "selected_car",
            car_no,
            car_no,
        )
    return total


def run_hebei_freight_stage_with_recovery(phone, car_no):
    return run_freight_scope_with_recovery(
        phone,
        "销售单-河北众沃",
        "login_account",
        phone,
        car_no,
    )


def tap_clickable_span_shotgun(driver, element):
    """多点尝试富文本链接；一旦浏览器已打开就立即停止后续点击。"""
    rect = element.rect
    y = int(rect['y'] + rect['height'] / 2)
    for ratio in [0.1, 0.5, 0.9]:
        x = int(rect['x'] + rect['width'] * ratio)
        actions = ActionChains(driver)
        actions.w3c_actions = ActionBuilder(driver, mouse=PointerInput(interaction.POINTER_TOUCH, "touch"))
        actions.w3c_actions.pointer_action.move_to_location(x, y)
        actions.w3c_actions.pointer_action.pointer_down()
        actions.w3c_actions.pointer_action.pause(0.1)
        actions.w3c_actions.pointer_action.pointer_up()
        actions.perform()
        time.sleep(0.3)

        try:
            if driver.current_package == "com.honor.browser":
                break
        except Exception as exc:
            raise_if_appium_session_lost(exc, '检测浏览器前台状态')


def run_device_adb(arguments, *, timeout=30, check=True, retry_safe=False):
    if wireless_link.is_enabled():
        return wireless_link.run_adb(
            ["-s", ADB_SERIAL, *arguments], timeout=timeout, check=check,
            capture_output=True, text=True, retry_safe=retry_safe,
        )
    return subprocess.run(
        ["adb", "-s", ADB_SERIAL, *arguments], timeout=timeout, check=check,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def adb_shell(cmd):
    return run_device_adb(["shell", cmd], timeout=30).stdout.strip()


def get_latest_phone_pdf():
    """返回手机 HonorBrowser 下载目录最新 PDF 路径。"""
    try:
        latest_file = run_device_adb(["shell", "ls", "-t", PHONE_PDF_GLOB], timeout=30, retry_safe=True).stdout.strip()
        if latest_file and "No such file" not in latest_file:
            return latest_file.splitlines()[0].strip()
    except Exception as exc:
        if wireless_link.is_enabled() and isinstance(exc, wireless_link.ConnectionUnavailable):
            raise
        pass
    return ""


def get_phone_file_size(phone_file_path):
    """获取手机端文件字节数；失败返回 -1。"""
    if not phone_file_path:
        return -1

    try:
        # Android 15 的 toybox stat 支持 -c %s。
        result = run_device_adb(["shell", "stat", "-c", "%s", "--", shlex.quote(phone_file_path)], timeout=30, retry_safe=True).stdout
        match = re.search(r"\d+", result)
        if match:
            return int(match.group(0))
    except Exception as exc:
        if wireless_link.is_enabled() and isinstance(exc, wireless_link.ConnectionUnavailable):
            raise
        pass

    try:
        # 个别系统 stat 不兼容时使用 wc 兜底。
        result = run_device_adb(["shell", "wc", "-c", "--", shlex.quote(phone_file_path)], timeout=30, retry_safe=True).stdout
        match = re.search(r"\d+", result)
        if match:
            return int(match.group(0))
    except Exception as exc:
        if wireless_link.is_enabled() and isinstance(exc, wireless_link.ConnectionUnavailable):
            raise
        pass

    return -1


def wait_until_phone_pdf_complete(
    phone_pdf_path,
    timeout=60,
    min_size=1024,
    stable_rounds=3,
    poll_interval=0.7
):
    """
    等待浏览器把 PDF 真正写完：
    1. 文件至少达到 min_size；
    2. 文件大小连续 stable_rounds 次保持不变。

    成功返回最终字节数，超时或仍为 0 字节返回 -1。
    """
    deadline = time.time() + timeout
    last_size = -1
    stable_count = 0

    while time.time() < deadline:
        current_size = get_phone_file_size(phone_pdf_path)

        if current_size >= min_size:
            if current_size == last_size:
                stable_count += 1
            else:
                stable_count = 0

            if stable_count >= stable_rounds:
                print(
                    f"    [PDF校验] 手机端文件已写入完成："
                    f"{current_size} bytes"
                )
                return current_size
        else:
            stable_count = 0

        last_size = current_size
        time.sleep(poll_interval)

    final_size = get_phone_file_size(phone_pdf_path)
    print(
        f"    ⚠️ [PDF校验] 手机端文件在 {timeout} 秒内未完成，"
        f"当前大小：{final_size} bytes"
    )
    return -1


def wait_for_new_phone_pdf(before_pdf, timeout=45):
    """
    等待手机端出现新的 PDF，并确认文件内容已经写完。
    不能只判断文件名出现，否则可能拉取到浏览器刚创建的 0 字节占位文件。
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        latest_pdf = get_latest_phone_pdf()
        if latest_pdf and latest_pdf != before_pdf:
            remaining = max(1, int(deadline - time.time()))
            completed_size = wait_until_phone_pdf_complete(
                latest_pdf,
                timeout=remaining
            )
            if completed_size > 0:
                return latest_pdf
            return ""

        time.sleep(0.5)

    return ""


def wait_for_browser_or_pdf(before_pdf, timeout=12):
    """等待浏览器成为前台，或链接直接触发 PDF 下载。"""
    deadline = time.time() + timeout
    next_pdf_check = 0.0

    while True:
        if time.time() >= deadline:
            if wireless_link.is_enabled() and _ui_pause_if_external(
                    "pdf_browser_transition", {"com.ibb.tizi", "com.honor.browser"}):
                deadline = time.time() + timeout
            else:
                return "timeout", ""
        now = time.time()
        if now >= next_pdf_check:
            latest_pdf = get_latest_phone_pdf()
            if latest_pdf and latest_pdf != before_pdf:
                return "pdf", latest_pdf
            next_pdf_check = now + 0.75

        try:
            ensure_native_context()
            if driver.current_package == "com.honor.browser":
                return "browser", ""
        except Exception as exc:
            if wireless_link.is_enabled():
                raise
            raise_if_appium_session_lost(exc, '等待浏览器打开')

        time.sleep(0.25)

    return "timeout", ""


def _run_adb_for_honor_reclaim(arguments, timeout=8.0):
    '''运行一条严格参数化的 ADB 命令；仅供 Honor 当前标签精确回收。'''
    result = run_device_adb(arguments, timeout=timeout, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()
        raise RuntimeError(
            f'ADB 命令失败（exit={result.returncode}）：{detail or arguments}'
        )
    return (result.stdout or '').strip()


def close_honor_current_webview_target(
    expected_url, timeout=5.0, *, bind_only=False, expected_target_id=None,
):
    '''
    通过 Honor WebView DevTools 精确关闭当前 attached target。

    该路径直接使用浏览器为当前 WebView 分配的 target id，不依赖标签卡片顺序、
    RecyclerView 可见数量或 PDF 标题是否与历史标签重复。
    '''
    expected_url = str(expected_url or '').strip()
    if not re.match(r'^https?://', expected_url, flags=re.IGNORECASE):
        raise RuntimeError('当前 PDF 没有可验证的 http(s) URL')
    if expected_target_id is not None:
        expected_target_id = str(expected_target_id).strip()
        if not re.fullmatch(r'[0-9A-Fa-f]+', expected_target_id):
            raise RuntimeError('下载前绑定的 Honor target id 格式异常')

    pid_text = _run_adb_for_honor_reclaim(
        ['shell', 'pidof', 'com.honor.browser'],
        timeout=timeout,
    )
    browser_pids = [value for value in pid_text.split() if value.isdigit()]
    if len(browser_pids) != 1:
        raise RuntimeError(
            f'Honor 浏览器主进程 PID 不唯一：{browser_pids or "未找到"}'
        )

    socket_name = f'webview_devtools_remote_{browser_pids[0]}'
    unix_sockets = _run_adb_for_honor_reclaim(
        ['shell', 'cat', '/proc/net/unix'],
        timeout=timeout,
    )
    if not re.search(
        rf'@{re.escape(socket_name)}(?:\s|$)',
        unix_sockets,
        flags=re.MULTILINE,
    ):
        raise RuntimeError(f'未找到 Honor 当前进程 DevTools 套接字：{socket_name}')

    forwarded_port = ''
    session = requests.Session()
    session.trust_env = False
    try:
        forwarded_port = _run_adb_for_honor_reclaim(
            ['forward', 'tcp:0', f'localabstract:{socket_name}'],
            timeout=timeout,
        ).strip()
        if not re.fullmatch(r'\d+', forwarded_port):
            raise RuntimeError(f'ADB 未返回有效临时端口：{forwarded_port!r}')

        base_url = f'http://127.0.0.1:{forwarded_port}'
        version_response = session.get(
            f'{base_url}/json/version',
            timeout=timeout,
        )
        version_response.raise_for_status()
        version_info = version_response.json()
        if version_info.get('Android-Package') != 'com.honor.browser':
            raise RuntimeError(
                'DevTools 端口不属于 Honor 浏览器：'
                f'{version_info.get("Android-Package")!r}'
            )

        def fetch_targets(request_timeout=None):
            response = session.get(
                f'{base_url}/json',
                timeout=timeout if request_timeout is None else request_timeout,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, list):
                raise RuntimeError('Honor DevTools target 列表格式异常')
            return result

        def target_description(target):
            raw = target.get('description')
            try:
                description = raw if isinstance(raw, dict) else json.loads(raw or '{}')
            except (TypeError, ValueError):
                return None
            return description if isinstance(description, dict) else None

        def find_current_matches(targets):
            matches = []
            for target in targets:
                if not isinstance(target, dict):
                    continue
                description = target_description(target)
                if description is None:
                    continue
                try:
                    width = int(description.get('width') or 0)
                    height = int(description.get('height') or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if (
                    target.get('type') == 'page'
                    and target.get('url') == expected_url
                    and description.get('attached') is True
                    and description.get('empty') is False
                    and description.get('visible') is True
                    and width > 0
                    and height > 0
                ):
                    matches.append(target)
            return matches

        def target_diagnostic(targets):
            # Only report predicate counts. Download URLs may contain credentials.
            counts = dict(total=len(targets), page=0, exact_url=0,
                          valid_description=0, attached=0, nonempty=0,
                          visible=0, positive_size=0)
            for target in targets:
                if not isinstance(target, dict) or target.get('type') != 'page':
                    continue
                counts['page'] += 1
                if target.get('url') != expected_url:
                    continue
                counts['exact_url'] += 1
                description = target_description(target)
                if description is None:
                    continue
                counts['valid_description'] += 1
                counts['attached'] += description.get('attached') is True
                counts['nonempty'] += description.get('empty') is False
                counts['visible'] += description.get('visible') is True
                try:
                    counts['positive_size'] += (
                        int(description.get('width') or 0) > 0
                        and int(description.get('height') or 0) > 0
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
            return ','.join(f'{key}={value}' for key, value in counts.items())

        # Native PDF controls can settle before the WebView target is attached.
        # Retry only zero matches, within one short deadline; never choose among
        # multiple candidates or weaken the identity checks to force a close.
        discovery_deadline = time.monotonic() + max(0.2, float(timeout))
        def fetch_discovery_targets():
            remaining = max(0.01, discovery_deadline - time.monotonic())
            return fetch_targets(request_timeout=min(max(0.01, float(timeout)), remaining))

        targets = fetch_discovery_targets()
        current_matches = find_current_matches(targets)
        while not current_matches and time.monotonic() < discovery_deadline:
            time.sleep(min(0.2, max(0.0, discovery_deadline - time.monotonic())))
            if time.monotonic() >= discovery_deadline:
                break
            targets = fetch_discovery_targets()
            current_matches = find_current_matches(targets)

        if len(current_matches) != 1:
            raise RuntimeError(
                '无法唯一绑定 Honor 当前 attached PDF target：'
                f'匹配数={len(current_matches)}；{target_diagnostic(targets)}'
            )

        target_id = str(current_matches[0].get('id') or '').strip()
        if not re.fullmatch(r'[0-9A-Fa-f]+', target_id):
            raise RuntimeError(f'Honor 当前 target id 格式异常：{target_id!r}')
        if expected_target_id is not None and target_id != expected_target_id:
            raise RuntimeError('Honor 当前 PDF target 已不是下载前绑定的页面，停止关闭')

        preclose_same_url_ids = {
            str(target.get('id') or '').strip()
            for target in targets
            if isinstance(target, dict) and target.get('url') == expected_url
        }

        # DevTools target 可能在页面跳转时更换。真正发出关闭请求之前再取一次，
        # 只有同一个 target 仍唯一满足当前页身份条件时才允许关闭。
        time.sleep(0.2)
        confirmed_targets = fetch_targets()
        confirmed_matches = find_current_matches(confirmed_targets)
        confirmed_ids = {
            str(target.get('id') or '').strip()
            for target in confirmed_targets
            if isinstance(target, dict) and target.get('url') == expected_url
        }
        preclose_same_url_ids.update(confirmed_ids)
        if (
            len(confirmed_matches) != 1
            or str(confirmed_matches[0].get('id') or '').strip() != target_id
        ):
            raise RuntimeError(
                'Honor 当前 PDF target 在关闭前身份已变化，停止关闭：'
                f'原 target={target_id}，当前匹配数={len(confirmed_matches)}'
            )

        if bind_only:
            return target_id

        close_response = session.get(
            f'{base_url}/json/close/{target_id}',
            timeout=timeout,
        )
        close_response.raise_for_status()
        if 'Target is closing' not in close_response.text:
            raise RuntimeError(
                f'Honor 未确认 target 关闭请求：{close_response.text!r}'
            )

        deadline = time.monotonic() + max(3.0, float(timeout))
        while time.monotonic() < deadline:
            remaining = fetch_targets()
            target_removed = all(
                not isinstance(target, dict)
                or str(target.get('id') or '') != target_id
                for target in remaining
            )
            replacement_matches = [
                target
                for target in find_current_matches(remaining)
                if str(target.get('id') or '').strip()
                not in preclose_same_url_ids
            ]
            if target_removed and not replacement_matches:
                return target_id
            time.sleep(0.2)

        raise RuntimeError(
            'Honor 已接收关闭请求，但原 target 未消失或同 URL target '
            f'被新建替换：{target_id}'
        )
    finally:
        session.close()
        if forwarded_port:
            local_spec = f'tcp:{forwarded_port}'
            cleanup_problem = ''
            for attempt in range(2):
                try:
                    removal = run_device_adb(['forward', '--remove', local_spec], timeout=timeout, check=False)
                    listing = run_device_adb(['forward', '--list'], timeout=timeout, check=False)
                    mapping_still_exists = any(
                        len(parts) >= 2
                        and parts[0] == ADB_SERIAL
                        and parts[1] == local_spec
                        for parts in (
                            line.split()
                            for line in (listing.stdout or '').splitlines()
                        )
                    )
                    if listing.returncode == 0 and not mapping_still_exists:
                        cleanup_problem = ''
                        break
                    removal_detail = (
                        removal.stderr or removal.stdout or ''
                    ).strip()
                    listing_detail = (
                        listing.stderr or listing.stdout or ''
                    ).strip()
                    cleanup_problem = (
                        f'删除 exit={removal.returncode} {removal_detail!r}；'
                        f'复核 exit={listing.returncode} {listing_detail!r}'
                    )
                except Exception as exc:
                    cleanup_problem = f'{type(exc).__name__}: {exc}'
                if attempt == 0:
                    time.sleep(0.2)
            if cleanup_problem:
                print(
                    '    ⚠️ [浏览器回收] ADB 临时转发端口清理未确认：'
                    f'{local_spec}；{cleanup_problem}'
                )


def remember_honor_pdf_reclaim_context(driver):
    '''仅为标签回收，在下载按钮重绑前只读绑定当前 PDF；不影响下载成败。'''
    driver._honor_pdf_reclaim_context = {}
    original_context = None
    try:
        original_context = driver.current_context
        if original_context != 'NATIVE_APP':
            driver.switch_to.context('NATIVE_APP')
        if str(driver.current_package or '') != 'com.honor.browser':
            return

        def read_identity():
            elements = [
                element for element in driver.find_elements(
                    AppiumBy.ID, 'com.honor.browser:id/url_bar',
                ) if element.is_displayed()
            ]
            if len(elements) != 1:
                return '', ''
            element = elements[0]
            title = str(element.get_attribute('text') or element.text or '').strip()
            for attribute in ('contentDescription', 'content-desc', 'name'):
                url = str(element.get_attribute(attribute) or '').strip()
                if title.lower().endswith('.pdf') and re.match(r'^https?://', url, re.I):
                    return title, url
            return '', ''

        title, url = read_identity()
        if not title or not url:
            return
        target_id = close_honor_current_webview_target(url, timeout=2.0, bind_only=True)
        if (
            str(driver.current_package or '') != 'com.honor.browser'
            or read_identity() != (title, url)
        ):
            return
        driver._honor_pdf_reclaim_context = {
            'title': title, 'url': url, 'target_id': target_id,
            'captured_at': time.monotonic(),
        }
        print(f'    [浏览器回收] 已在下载前绑定本次 PDF 页面（target={target_id}）。')
    except Exception as exc:
        # No URL/exception text: document links can contain temporary credentials.
        print(f'    [浏览器回收] 下载前页面绑定未完成（{type(exc).__name__}），继续原下载流程。')
    finally:
        if original_context and original_context != 'NATIVE_APP':
            try:
                driver.switch_to.context(original_context)
            except Exception:
                driver._honor_pdf_reclaim_context = {}


def close_current_honor_pdf_page(driver, timeout=3.0, max_back_steps=3):
    '''精确关闭本次 Honor PDF 当前 target；无法证明身份时绝不操作标签卡。'''
    browser_package = 'com.honor.browser'
    url_bar_id = f'{browser_package}:id/url_bar'
    tab_switcher_id = f'{browser_package}:id/tab_switcher'
    toolbar_id = f'{browser_package}:id/toolbar_root'
    poll_interval = 0.2
    identity_stable_duration = 1.0
    stable_samples_required = 3
    page_ready_timeout = max(6.0, float(timeout) + 2.0)
    _ = max_back_steps  # 保留原调用签名；下载确认页最多只允许一次返回。
    reclaim_context = getattr(driver, '_honor_pdf_reclaim_context', None)
    driver._honor_pdf_reclaim_context = None  # 本次绑定只能消费一次，不跨重试复用。

    def displayed_elements(resource_id):
        return [
            element
            for element in driver.find_elements(AppiumBy.ID, resource_id)
            if element.is_displayed()
        ]

    def read_pdf_identity_once():
        elements = displayed_elements(url_bar_id)
        if len(elements) != 1:
            return '', ''
        element = elements[0]
        title = str(element.get_attribute('text') or element.text or '').strip()
        url = ''
        for attribute in ('contentDescription', 'content-desc', 'name'):
            value = str(element.get_attribute(attribute) or '').strip()
            if re.match(r'^https?://', value, flags=re.IGNORECASE):
                url = value
                break
        if not title.lower().endswith('.pdf') or not url:
            return '', ''
        return title, url

    def wait_stable_pdf_identity(wait_timeout):
        deadline = time.monotonic() + wait_timeout
        last_identity = ('', '')
        stable_since = None
        stable_samples = 0
        home_seen_since = None
        while time.monotonic() < deadline:
            if str(driver.current_package or '') != browser_package:
                return '', ''
            identity = read_pdf_identity_once()
            now = time.monotonic()
            if all(identity):
                if identity == last_identity:
                    stable_samples += 1
                else:
                    last_identity = identity
                    stable_since = now
                    stable_samples = 1
                if (
                    stable_since is not None
                    and stable_samples >= stable_samples_required
                    and now - stable_since >= identity_stable_duration
                ):
                    return identity
            else:
                last_identity = ('', '')
                stable_since = None
                stable_samples = 0

            activity = str(driver.current_activity or '')
            if not all(identity) and activity.endswith('BrowserMainActivity'):
                if home_seen_since is None:
                    home_seen_since = now
                elif now - home_seen_since >= identity_stable_duration:
                    return '', ''
            else:
                home_seen_since = None
            time.sleep(poll_interval)
        return '', ''

    def count_from_value(value):
        match = re.search(r'(\d+)\s*个窗口', str(value or ''))
        return int(match.group(1)) if match else None

    def count_from_unique_element(resource_id):
        elements = displayed_elements(resource_id)
        if len(elements) != 1:
            return None
        element = elements[0]
        for attribute in ('contentDescription', 'content-desc', 'name', 'text'):
            count = count_from_value(element.get_attribute(attribute))
            if count is not None:
                return count
        return None

    def page_window_count_once():
        return count_from_unique_element(tab_switcher_id)

    def browser_window_count_once():
        for resource_id in (tab_switcher_id, toolbar_id):
            count = count_from_unique_element(resource_id)
            if count is not None:
                return count
        return None

    def wait_stable_count(reader, wait_timeout, expected=None, duration=0.6):
        deadline = time.monotonic() + wait_timeout
        last_count = None
        stable_since = None
        stable_samples = 0
        while time.monotonic() < deadline:
            try:
                count = reader()
            except Exception as exc:
                raise_if_appium_session_lost(exc, '等待 Honor 浏览器窗口数')
                count = None
            now = time.monotonic()
            if count is not None and (expected is None or count == expected):
                if count == last_count:
                    stable_samples += 1
                else:
                    last_count = count
                    stable_since = now
                    stable_samples = 1
                if (
                    stable_since is not None
                    and stable_samples >= stable_samples_required
                    and now - stable_since >= duration
                ):
                    return count
            else:
                last_count = None
                stable_since = None
                stable_samples = 0
            time.sleep(0.25)
        return None

    try:
        ensure_native_context()
        if str(driver.current_package or '') != browser_package:
            return False

        print('    [浏览器回收] 正在按当前 WebView target 精确关闭本次 PDF 标签...')
        current_activity = str(driver.current_activity or '')
        if 'DownloadTask' in current_activity:
            driver.back()
            pdf_title, pdf_url = wait_stable_pdf_identity(page_ready_timeout)
            if not pdf_title:
                print(
                    '    [浏览器回收] 返回一次后未稳定到达可识别的 PDF 页，'
                    '不再发送返回键。'
                )
                return False
            print(f'    [浏览器回收] PDF 页面已稳定：{pdf_title}')
        else:
            pdf_title, pdf_url = wait_stable_pdf_identity(timeout)
            if not pdf_title:
                print(
                    '    [浏览器回收] 当前不是可识别的 PDF 页或下载确认页，'
                    '不发送返回键。'
                )
                return False

        expected_url = pdf_url
        expected_target_id = None
        if reclaim_context is not None:
            from urllib.parse import parse_qs, urlsplit

            def permitted_download_transition(preview_url, download_url):
                if preview_url == download_url:
                    return True
                old, new = urlsplit(preview_url), urlsplit(download_url)
                query = parse_qs(new.query, keep_blank_values=True)
                key = query.get('downloadKey', [])
                return (
                    old.scheme == new.scheme == 'http'
                    and old.netloc == 'doc.hbjyjt.com:16050'
                    and old.path == '/' and not old.query
                    and old.fragment.startswith('/thirdView?')
                    and new.netloc == 'doc.hbjyjt.com:16093'
                    and new.path == '/download/doc' and not new.fragment
                    and set(query) == {'downloadKey'} and len(key) == 1
                    and re.fullmatch(
                        r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                        r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', key[0],
                    ) is not None
                )

            try:
                context_valid = (
                    isinstance(reclaim_context, dict)
                    and reclaim_context.get('title') == pdf_title
                    and 0 <= time.monotonic() - float(reclaim_context['captured_at']) <= 180
                    and bool(re.fullmatch(r'[0-9A-Fa-f]+', reclaim_context['target_id']))
                    and permitted_download_transition(reclaim_context['url'], pdf_url)
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                context_valid = False
            if not context_valid:
                print('    [浏览器回收] 下载前绑定缺失、过期或当前 PDF 身份已变化，停止关闭。')
                return False
            expected_url = reclaim_context['url']
            expected_target_id = reclaim_context['target_id']

        before_count = wait_stable_count(
            page_window_count_once,
            page_ready_timeout,
        )
        if before_count is None or before_count <= 1:
            print(
                '    [浏览器回收] PDF 页未取得持续稳定且大于 1 的窗口数，'
                '停止关闭操作。'
            )
            return False

        close_options = {'timeout': max(5.0, float(timeout) + 2.0)}
        if expected_target_id is not None:
            close_options['expected_target_id'] = expected_target_id
        target_id = close_honor_current_webview_target(expected_url, **close_options)
        expected_count = before_count - 1
        after_count = wait_stable_count(
            browser_window_count_once,
            max(10.0, float(timeout) * 3),
            expected=expected_count,
            duration=1.5,
        )
        if after_count == expected_count:
            print(
                '    [浏览器回收] 已按浏览器当前 WebView target 移除本次 PDF 窗口'
                f'（{before_count} -> {expected_count}，target={target_id}）：'
                f'{pdf_title}'
            )
            return True

        print(
            '    [浏览器回收] 当前 target 已发送关闭，但未确认窗口数持续精确减 1'
            f'（期望 {expected_count}，实际未稳定）；不再重复关闭。'
        )
        return False
    except Exception as exc:
        raise_if_appium_session_lost(exc, '精确关闭 Honor 浏览器当前 PDF 标签')
        print(
            f'    [浏览器回收] 当前 PDF target 关闭失败，'
            f'继续 force-stop 兜底：{type(exc).__name__}: {exc}'
        )
        return False


class PdfTransferValidationError(RuntimeError):
    """A downloaded file has not met the transport integrity contract."""


class PdfDownloadIncompleteError(AccountSessionSuspectedError):
    """An expected attachment is missing; the account cannot be complete."""


def _pdf_identity(wb_no, car_no, file_index=""):
    identity = [str(clean_val(wb_no)), str(clean_val(car_no)), str(file_index)]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode("utf-8")).hexdigest()


def _pdf_local_facts(path):
    path = _pdf_local_io_path(path)
    size = os.path.getsize(path)
    if size < 1024:
        raise PdfTransferValidationError("pdf_too_small")
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise PdfTransferValidationError("pdf_header_invalid")
        stream.seek(max(0, size - 1024))
        if b"%%EOF" not in stream.read():
            raise PdfTransferValidationError("pdf_end_marker_missing")
        stream.seek(0)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return size, digest.hexdigest()


def _phone_pdf_sha256(phone_pdf_path, timeout=30):
    result = run_device_adb(["shell", "sha256sum", "--", shlex.quote(phone_pdf_path)], timeout=timeout, retry_safe=True)
    match = re.match(r"^([0-9a-fA-F]{64})\s", result.stdout or "")
    if not match:
        raise PdfTransferValidationError("remote_sha256_unavailable")
    return match.group(1).lower()


def _pdf_integrity_metadata(path, wb_no=None, car_no=None, file_index="", *, expected_identity=None):
    """Return proven metadata, never upgrade an unverified legacy file by guessing."""
    try:
        with open(_pdf_local_io_path(str(path) + ".integrity.json"), "r", encoding="utf-8") as stream:
            metadata = json.load(stream)
        size, digest = _pdf_local_facts(path)
        if (
            metadata.get("schema") != 1
            or metadata.get("verification") != "adb-sha256-v1"
            or metadata.get("identitySHA256") != (expected_identity or _pdf_identity(wb_no, car_no, file_index))
            or metadata.get("bytes") != size
            or metadata.get("sha256") != digest
            or metadata.get("remoteSHA256Before") != digest
            or metadata.get("remoteSHA256After") != digest
            or metadata.get("remoteBytesBefore") != size
            or metadata.get("remoteBytesAfter") != size
        ):
            return None
        return metadata
    except (OSError, ValueError, TypeError, AttributeError, PdfTransferValidationError):
        return None


def require_pdf_downloads_complete(expected_count, downloaded_count):
    if expected_count < 1 or downloaded_count != expected_count:
        raise PdfDownloadIncompleteError(
            f"expected_pdf_incomplete: expected={expected_count}, verified={downloaded_count}"
        )


def _read_pdf_manifest(save_dir):
    path = os.path.join(save_dir, ".pdf-download-manifest.json")
    if not os.path.exists(_pdf_local_io_path(path)):
        return {"schema": 1, "batches": {}}
    try:
        with open(_pdf_local_io_path(path), "r", encoding="utf-8") as stream:
            value = json.load(stream)
        if value.get("schema") != 1 or not isinstance(value.get("batches"), dict):
            raise ValueError("manifest_schema")
        return value
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise PdfDownloadIncompleteError("pdf_manifest_unreadable") from exc


def _write_pdf_manifest(save_dir, manifest):
    path = os.path.join(save_dir, ".pdf-download-manifest.json")
    temporary = path + ".part"
    with open(_pdf_local_io_path(temporary), "w", encoding="utf-8") as stream:
        json.dump(manifest, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(_pdf_local_io_path(temporary), _pdf_local_io_path(path))


def register_pdf_enumeration_pending(save_dir, wb_no, car_no):
    """Remember an observed order before opening its attachment detail page."""
    manifest = _read_pdf_manifest(save_dir)
    key = _pdf_identity(wb_no, car_no, "batch")
    previous = manifest["batches"].get(key, {})
    manifest["batches"][key] = {
        "expectedSlots": previous.get("expectedSlots", []),
        "enumerationComplete": False, "state": "PENDING", "verifiedSlots": {},
    }
    _write_pdf_manifest(save_dir, manifest)


def register_expected_pdf_slots(save_dir, wb_no, car_no, expected_count, *, indexed):
    """Remember every observed expected slot; a later retry cannot silently shrink it."""
    if not isinstance(expected_count, int) or expected_count < 0:
        raise PdfDownloadIncompleteError("pdf_expected_count_unknown")
    manifest = _read_pdf_manifest(save_dir)
    key = _pdf_identity(wb_no, car_no, "batch")
    batch = manifest["batches"].get(key)
    expected = [str(i + 1) for i in range(expected_count)] if indexed else ([""] if expected_count else [])
    if batch is not None:
        if not isinstance(batch.get("expectedSlots"), list):
            raise PdfDownloadIncompleteError("pdf_manifest_slots_invalid")
        previous = set(batch["expectedSlots"])
        if not previous.issubset(set(expected)):
            raise PdfDownloadIncompleteError("pdf_expected_slots_disappeared_on_retry")
    if not expected and batch is None:
        return
    manifest["batches"][key] = {
        "expectedSlots": expected, "enumerationComplete": True,
        "state": "PENDING" if expected else "COMPLETE", "verifiedSlots": {},
    }
    _write_pdf_manifest(save_dir, manifest)


def complete_expected_pdf_slots(save_dir, wb_no, car_no):
    manifest = _read_pdf_manifest(save_dir)
    key = _pdf_identity(wb_no, car_no, "batch")
    batch = manifest["batches"].get(key)
    if not batch or not batch.get("expectedSlots"):
        raise PdfDownloadIncompleteError("pdf_expected_slots_not_registered")
    verified = {}
    for slot in batch["expectedSlots"]:
        path = find_existing_archived_pdf(save_dir, wb_no, car_no, slot)
        if not path:
            raise PdfDownloadIncompleteError("pdf_expected_slot_not_verified")
        metadata = _pdf_integrity_metadata(path, wb_no, car_no, slot)
        if metadata is None:
            raise PdfDownloadIncompleteError("pdf_expected_slot_not_verified")
        verified[slot] = {"filename": os.path.basename(path), "identitySHA256": metadata["identitySHA256"],
                          "sha256": metadata["sha256"], "bytes": metadata["bytes"]}
    batch.update(state="COMPLETE", verifiedSlots=verified)
    _write_pdf_manifest(save_dir, manifest)


def assert_all_pdf_batches_complete(save_dirs):
    """Final crawl gate: partial earlier accounts must not be hidden by later retries."""
    for save_dir in save_dirs:
        for batch in _read_pdf_manifest(save_dir)["batches"].values():
            slots = batch.get("expectedSlots", [])
            verified = batch.get("verifiedSlots", {})
            if (not isinstance(slots, list) or batch.get("state") != "COMPLETE"
                    or set(verified) != set(slots)
                    or (not slots and batch.get("enumerationComplete") is not True)):
                raise PdfDownloadIncompleteError("pdf_manifest_batch_incomplete")
            for slot in slots:
                proof = verified[slot]
                filename = proof.get("filename", "")
                if not filename or os.path.basename(filename) != filename:
                    raise PdfDownloadIncompleteError("pdf_manifest_path_invalid")
                metadata = _pdf_integrity_metadata(
                    os.path.join(save_dir, filename), expected_identity=proof.get("identitySHA256", ""))
                if not metadata or metadata["sha256"] != proof.get("sha256") or metadata["bytes"] != proof.get("bytes"):
                    raise PdfDownloadIncompleteError("pdf_manifest_proof_changed")


def require_single_pdf_download(*args, **kwargs):
    wb_no, car_no, save_dir = args[2], args[3], args[5]
    register_expected_pdf_slots(save_dir, wb_no, car_no, 1, indexed=False)
    result = execute_single_download(*args, **kwargs)
    require_pdf_downloads_complete(1, int(result is True))
    complete_expected_pdf_slots(save_dir, wb_no, car_no)
    return True


def pull_phone_pdf(
    phone_pdf_path,
    wb_no,
    car_no,
    save_dir,
    file_index="",
    source_kind="",
    source_value="",
    sheet_name="",
):
    """Verify remote-before/local/remote-after sizes and SHA256 before publishing a PDF."""
    if not phone_pdf_path:
        return False

    remote_size_before = wait_until_phone_pdf_complete(
        phone_pdf_path,
        timeout=60
    )
    if remote_size_before <= 0:
        print("  ❌ 手机端 PDF 尚未写完或文件为 0KB，本次不拉取。")
        return False

    stamp = datetime.datetime.now()
    clean_car, clean_wb = clean_val(car_no), clean_val(wb_no)
    idx_str = f"_{file_index}" if file_index else ""
    while True:
        timestamp = stamp.strftime("%Y%m%d_%H%M%S")
        new_name = f"{clean_wb}_{clean_car}_{timestamp}{idx_str}.pdf"
        local_target_path = os.path.join(save_dir, new_name)
        if not os.path.exists(_pdf_local_io_path(local_target_path)) and not os.path.exists(_pdf_local_io_path(local_target_path + ".integrity.json")):
            break
        stamp += datetime.timedelta(seconds=1)
    temp_target_path = local_target_path + ".part"
    metadata_path = local_target_path + ".integrity.json"
    metadata_part = metadata_path + ".part"
    published = False

    try:
        if os.path.exists(_pdf_local_io_path(temp_target_path)):
            os.remove(_pdf_local_io_path(temp_target_path))

        remote_hash_before = _phone_pdf_sha256(phone_pdf_path)
        result = run_device_adb(["pull", phone_pdf_path, _pdf_local_io_path(temp_target_path)], timeout=150, retry_safe=True)

        if result.stdout.strip():
            print(result.stdout.strip())

        if not os.path.exists(_pdf_local_io_path(temp_target_path)):
            raise RuntimeError("adb pull 返回成功，但本地临时文件不存在")

        local_size, local_hash = _pdf_local_facts(temp_target_path)
        remote_size_after = get_phone_file_size(phone_pdf_path)
        remote_hash_after = _phone_pdf_sha256(phone_pdf_path)
        if local_size != remote_size_before or local_size != remote_size_after:
            raise PdfTransferValidationError("remote_local_size_mismatch")
        if local_hash != remote_hash_before or local_hash != remote_hash_after:
            raise PdfTransferValidationError("remote_local_sha256_mismatch")
        metadata = {
            "schema": 1,
            "verification": "adb-sha256-v1",
            "identitySHA256": _pdf_identity(wb_no, car_no, file_index),
            "bytes": local_size,
            "sha256": local_hash,
            "remoteBytesBefore": remote_size_before,
            "remoteBytesAfter": remote_size_after,
            "remoteSHA256Before": remote_hash_before,
            "remoteSHA256After": remote_hash_after,
        }
        with open(_pdf_local_io_path(metadata_part), "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(_pdf_local_io_path(temp_target_path), _pdf_local_io_path(local_target_path))
        published = True
        os.replace(_pdf_local_io_path(metadata_part), _pdf_local_io_path(metadata_path))
        append_source_context(
            "pdf",
            sheet_name,
            clean_wb,
            clean_car,
            source_kind,
            source_value,
            pdf_path=local_target_path,
        )
        print(
            f"  ✅ PDF 成功归档并通过完整性校验: "
            f"{new_name} ({local_size} bytes)"
        )
        return True

    except Exception as e:
        reason = str(e) if isinstance(e, PdfTransferValidationError) else type(e).__name__
        diagnostics = {"errorType": type(e).__name__, "pathChars": len(local_target_path),
                       "metadataChars": len(metadata_path), "metadataPartChars": len(metadata_part)}
        for field in ("errno", "winerror"):
            value = getattr(e, field, None)
            if type(value) is int:
                diagnostics[field] = value
        print(f"  ❌ ADB Pull/PDF 校验失败: {reason}|" + json.dumps(diagnostics, separators=(",", ":")))
        # Only this invocation's fresh paths are removed; existing archives are never overwritten.
        for path in (temp_target_path, metadata_part, metadata_path if published else "", local_target_path if published else ""):
            try:
                if path and os.path.exists(_pdf_local_io_path(path)):
                    os.remove(_pdf_local_io_path(path))
            except OSError:
                pass
        if wireless_link.is_enabled() and isinstance(e, wireless_link.ConnectionUnavailable):
            raise
        return False


def safe_click_browser_dialogs(driver, timeout=4):
    """
    下载按钮点击后，只允许点击一次浏览器确认按钮。

    Honor 浏览器在下载完成后可能继续保留同一个 completeButton；如果循环点击，
    第二次点击会打开刚下载的 PDF。这里首次确认后立即返回，随后只通过 ADB
    检查新 PDF 是否真正生成，不再对浏览器做第二次点击。
    """
    selectors = [
        (AppiumBy.ID, "com.honor.browser:id/completeButton", "Honor下载确认按钮"),
        (AppiumBy.ID, "android:id/button1", "系统下载确认按钮"),
        (
            AppiumBy.ANDROID_UIAUTOMATOR,
            'new UiSelector().textContains("继续下载")',
            "继续下载按钮"
        ),
        (
            AppiumBy.ANDROID_UIAUTOMATOR,
            'new UiSelector().text("下载")',
            "下载确认按钮"
        ),
        (
            AppiumBy.ANDROID_UIAUTOMATOR,
            'new UiSelector().text("保存")',
            "保存确认按钮"
        ),
        (
            AppiumBy.ANDROID_UIAUTOMATOR,
            'new UiSelector().text("允许")',
            "权限允许按钮"
        ),
    ]

    deadline = time.time() + timeout
    while time.time() < deadline:
        ensure_native_context()

        for by, value, desc in selectors:
            try:
                elements = driver.find_elements(by, value)
                target = next(
                    (el for el in elements if el.is_displayed() and el.is_enabled()),
                    None
                )
                if target:
                    target.click()
                    print(f"    [浏览器弹窗] 已点击一次：{desc}")
                    return True
            except Exception as exc:
                raise_if_appium_session_lost(exc, '点击浏览器确认按钮')
                continue

        time.sleep(0.2)

    # 有些链接不弹确认框，会直接开始下载；交给后续的新 PDF 校验判断成功与否。
    print("    [浏览器弹窗] 未出现确认框，继续等待 PDF 下载结果。")
    return False


def try_click_download_button(driver, timeout=20, stable_delay=2.0):
    """在统一总超时内轮询 Native 下载按钮，再尝试 WebView。"""
    driver._honor_pdf_reclaim_context = None
    native_selectors = [
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().resourceId("aDownload")', "H5资源ID aDownload"),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("下载")', "文本包含下载"),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("下载")', "描述包含下载"),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("保存")', "文本包含保存"),
    ]

    ready_started_at = time.monotonic()
    deadline = time.time() + timeout
    while time.time() < deadline:
        ensure_native_context()
        for by, value, desc in native_selectors:
            try:
                elements = driver.find_elements(by, value)
                target = next((el for el in elements if el.is_displayed() and el.is_enabled()), None)
                if target:
                    ready_elapsed = time.monotonic() - ready_started_at
                    print(
                        '    [下载器] 浏览器 PDF 下载界面已就绪，'
                        f'耗时 {ready_elapsed:.1f}s；稳定等待 {stable_delay:.1f}s 后点击...'
                    )
                    time.sleep(stable_delay)
                    remember_honor_pdf_reclaim_context(driver)
                    elements = driver.find_elements(by, value)
                    target = next(
                        (el for el in elements if el.is_displayed() and el.is_enabled()),
                        None,
                    )
                    if not target:
                        continue
                    target.click()
                    print(f"    [下载器] 已点击下载按钮：{desc}")
                    return True
            except Exception as exc:
                driver._honor_pdf_reclaim_context = {}
                raise_if_appium_session_lost(exc, '查找浏览器下载按钮')
                continue
        time.sleep(0.25)

    # Native 层未找到时，再尝试 WebView；备用路径同样执行稳定等待和元素重绑。
    try:
        for ctx in driver.contexts:
            if "WEBVIEW" not in ctx:
                continue
            try:
                driver.switch_to.context(ctx)
                elements = driver.find_elements(By.ID, "aDownload")
                target = next((el for el in elements if el.is_displayed() and el.is_enabled()), None)
                if target:
                    ready_elapsed = time.monotonic() - ready_started_at
                    print(
                        '    [下载器] WebView PDF 下载界面已就绪，'
                        f'耗时 {ready_elapsed:.1f}s；稳定等待 {stable_delay:.1f}s 后点击...'
                    )
                    time.sleep(stable_delay)
                    remember_honor_pdf_reclaim_context(driver)
                    elements = driver.find_elements(By.ID, 'aDownload')
                    target = next(
                        (el for el in elements if el.is_displayed() and el.is_enabled()),
                        None,
                    )
                    if not target:
                        continue
                    target.click()
                    print("    [下载器] 已在 WebView 中点击 aDownload")
                    driver.switch_to.context("NATIVE_APP")
                    return True
            except Exception as exc:
                driver._honor_pdf_reclaim_context = {}
                raise_if_appium_session_lost(exc, '在 WebView 中查找下载按钮')
            finally:
                try:
                    driver.switch_to.context("NATIVE_APP")
                except Exception as exc:
                    raise_if_appium_session_lost(exc, '从 WebView 切回原生上下文')
    except Exception as exc:
        raise_if_appium_session_lost(exc, '枚举浏览器 WebView')

    ensure_native_context()
    return False

def refresh_pounds_link(driver, file_index=""):
    """
    下载失败重试时，重新获取详情页里的磅单链接，避免旧 element stale。
    """
    links = driver.find_elements(AppiumBy.ID, "com.ibb.tizi:id/tv_pounds_pic")
    if not links:
        return None

    if str(file_index).isdigit():
        idx = int(file_index) - 1
    else:
        idx = 0

    if idx < 0:
        idx = 0

    if idx >= len(links):
        idx = len(links) - 1

    return links[idx]


def find_existing_archived_pdf(save_dir, wb_no, car_no, file_index=""):
    """查找本次运行中已经完整归档的同一附件，供账号级恢复时安全复用。"""
    clean_car = str(clean_val(car_no) or '').strip()
    clean_wb = str(clean_val(wb_no) or '').strip()
    prefix = f'{clean_wb}_{clean_car}_'
    index_suffix = f'_{file_index}' if file_index else ''
    candidates = []

    try:
        names = os.listdir(_pdf_local_io_path(save_dir))
    except OSError:
        return ''

    for name in names:
        if not name.startswith(prefix) or not name.lower().endswith('.pdf'):
            continue

        stem = os.path.splitext(name)[0]
        if index_suffix and not stem.endswith(index_suffix):
            continue

        path = os.path.join(save_dir, name)
        try:
            if _pdf_integrity_metadata(path, wb_no, car_no, file_index) is None:
                print('    [PDF完整性] 旧归档摘要不可证完整，保留原件并重新获取。')
                continue
            candidates.append(path)
        except OSError:
            continue

    if not candidates:
        return ''
    return max(candidates, key=lambda path: os.path.getmtime(_pdf_local_io_path(path)))


def execute_single_download(
    driver,
    wait,
    wb_no,
    car_no,
    element_to_click,
    save_dir,
    file_index="",
    max_retry=3,
    source_kind="",
    source_value="",
    sheet_name="",
):
    """
    加强版下载器：
    1. 点击富文本链接
    2. 找浏览器下载按钮
    3. 处理弹窗
    4. 校验手机端是否生成新 PDF
    5. 拉取指定新 PDF
    6. 失败最多重试 max_retry 次
    成功返回 True，失败返回 False。
    """
    existing_pdf = find_existing_archived_pdf(
        save_dir,
        wb_no,
        car_no,
        file_index,
    )
    if existing_pdf:
        print(
            f'    ♻️ [断点复用] 已存在有效 PDF，跳过重复下载：'
            f'{os.path.basename(existing_pdf)}'
        )
        return True

    retry_profiles = (
        (18, 20, 4, 45),
        (18, 20, 4, 45),
        (25, 30, 5, 60),
    )
    for attempt in range(1, max_retry + 1):
        print(f"    [下载器] 第 {attempt}/{max_retry} 次尝试下载...")
        (
            browser_open_timeout,
            download_button_timeout,
            dialog_timeout,
            pdf_write_timeout,
        ) = retry_profiles[min(attempt, len(retry_profiles)) - 1]
        if attempt >= 1:
            print(
                "    [下载器] 慢加载保护："
                f"等待浏览器 {browser_open_timeout}s、"
                f"等待下载按钮 {download_button_timeout}s、"
                f"等待 PDF 写完 {pdf_write_timeout}s；元素出现即继续。"
            )

        before_pdf = get_latest_phone_pdf()

        control_failure = False
        try:
            try:
                driver.switch_to.context("NATIVE_APP")
            except Exception as exc:
                raise_if_appium_session_lost(exc, '下载前切换原生上下文')

            if attempt > 1:
                refreshed = refresh_pounds_link(driver, file_index)
                if refreshed is not None:
                    element_to_click = refreshed

            print("    [下载器] 定位富文本标签，开启全覆盖点击...")
            tap_clickable_span_shotgun(driver, element_to_click)

            open_state, direct_pdf = wait_for_browser_or_pdf(
                before_pdf,
                timeout=browser_open_timeout,
            )
            if open_state == "pdf":
                if pull_phone_pdf(
                    direct_pdf,
                    wb_no,
                    car_no,
                    save_dir,
                    file_index,
                    source_kind,
                    source_value,
                    sheet_name,
                ):
                    return True

            print("    [下载器] 尝试点击浏览器下载按钮...")

            if not try_click_download_button(
                driver,
                timeout=download_button_timeout,
            ):
                raise Exception("未找到浏览器下载按钮 aDownload/下载/保存")

            safe_click_browser_dialogs(driver, timeout=dialog_timeout)

            new_pdf = wait_for_new_phone_pdf(
                before_pdf,
                timeout=pdf_write_timeout,
            )
            if not new_pdf:
                raise Exception("点击下载后，手机目录未检测到新的 PDF")

            ok = pull_phone_pdf(
                new_pdf,
                wb_no,
                car_no,
                save_dir,
                file_index,
                source_kind,
                source_value,
                sheet_name,
            )
            if ok:
                return True

            raise Exception("检测到新 PDF，但拉取到本地失败")

        except Exception as e:
            if isinstance(e, (AppiumSessionOwnershipError, wireless_link.ConnectionUnavailable)):
                control_failure = True
                raise
            if is_appium_session_lost(e):
                print('    ❌ [下载器] UiAutomator2 会话已失效，停止普通下载重试。')
                if isinstance(e, AppiumSessionLostError):
                    raise
                raise AppiumSessionLostError(
                    f'下载运单 {wb_no} 时 UiAutomator2 会话已失效: {e}'
                ) from e

            print(f"    ⚠️ [下载器] 第 {attempt} 次失败: {e}")

            if attempt == max_retry:
                print("    ❌ [下载器] 已达到最大重试次数，本附件下载失败。")
                return False

        finally:
            if not control_failure:
                print("    [强制回城] 清理浏览器后台进程，保证不卡顿...")
                try:
                    driver.switch_to.context("NATIVE_APP")
                except Exception as exc:
                    raise_if_appium_session_lost(exc, '下载清理时切换原生上下文')

                browser_tab_closed = close_current_honor_pdf_page(driver)
                if not browser_tab_closed:
                    print(
                        '    ⚠️ [浏览器回收] 未确认本次 PDF 标签已安全关闭；'
                        '不会盲目操作其他标签，继续 force-stop 浏览器。'
                    )

                try:
                    driver.terminate_app("com.honor.browser")
                except Exception as exc:
                    raise_if_appium_session_lost(exc, '下载清理时关闭浏览器')

                print("    [强制回城] 正在切回货运 App...")
                try:
                    activate_tizi_and_wait(
                        [
                            (AppiumBy.ID, "com.ibb.tizi:id/tv_pounds_pic"),
                            (AppiumBy.ID, "com.ibb.tizi:id/iv_left"),
                            (AppiumBy.ID, "com.ibb.tizi:id/rv"),
                        ],
                        timeout=12
                    )
                except Exception as e:
                    raise_if_appium_session_lost(e, '下载后切回货运 App')
                    print(f"    ⚠️ 切回货运 App 后页面未及时就绪: {e}")

    return False

def restart_app_keep_state():
    """关闭后台并重新打开 App；不清数据，等待账号输入框真实出现。"""
    print("  [账号切换] 正在关闭并重新打开货运 App，不清理 App 数据...")

    try:
        driver.terminate_app("com.honor.browser")
    except Exception:
        pass

    try:
        driver.terminate_app("com.ibb.tizi")
    except Exception:
        pass

    time.sleep(0.5)
    driver.activate_app("com.ibb.tizi")
    ensure_native_context()
    wait_visible((AppiumBy.ID, "com.ibb.tizi:id/phone_number"), timeout=15)

def safe_click_if_exists(by, value, timeout=3):
    """有就点，没有就跳过。"""
    try:
        wait_clickable((by, value), timeout=timeout).click()
        return True
    except Exception:
        return False


def _login_with_account_once(phone):
    print(f"\n================ 开始登录账号：{phone} ================")

    phone_input = wait_visible(
        (AppiumBy.ID, "com.ibb.tizi:id/phone_number"),
        timeout=12
    )
    phone_input.clear()
    phone_input.send_keys(phone)

    # 只有协议未勾选时才点击，避免重复点击把已勾选状态取消。
    try:
        agree_box = wait_visible(
            (AppiumBy.ID, "com.ibb.tizi:id/cb_agreement"),
            timeout=2
        )
        checked = str(agree_box.get_attribute("checked") or "").lower()
        is_checked = agree_box.is_selected() or checked in {"true", "1"}
        if not is_checked:
            agree_box.click()
    except Exception:
        pass

    click_step(
        (AppiumBy.ID, "com.ibb.tizi:id/login"),
        "登录按钮",
        timeout=10,
        retries=2
    )

    # 提交按钮可能不是每次都出现，只短时间等待。
    try:
        wait_clickable(
            (AppiumBy.ID, "com.ibb.tizi:id/commit_btn"),
            timeout=5
        ).click()
    except Exception:
        pass

    entry_locator = (
        AppiumBy.ID,
        "com.ibb.tizi:id/waybill_img"
    )
    click_step(
        entry_locator,
        "业务入口",
        next_locator=(AppiumBy.ID, "com.ibb.tizi:id/start_date"),
        timeout=12,
        retries=2
    )

    print(f"✅ 账号 {phone} 登录完成")


def login_with_account(phone):
    """执行一次登录；把登录阶段普通 UI 故障标记为可安全重登的异常。"""
    try:
        _login_with_account_once(phone)
    except Exception as exc:
        raise_if_appium_session_lost(exc, f'账号 {phone} 登录')
        raise AccountLoginError(
            f"账号 {phone} 登录或进入业务页失败：{exc}"
        ) from exc

#======================================================
def click_if_exists(by, value, timeout=5, desc=""):
    try:
        wait_clickable((by, value), timeout=timeout).click()
        if desc:
            print(f"  ✅ 已点击：{desc}")
        return True
    except Exception:
        if desc:
            print(f"  ⚠️ 未找到：{desc}")
        return False


def get_text_if_exists(by, value, timeout=5, desc=""):
    try:
        txt = wait_nonempty_text((by, value), timeout=timeout)
        if desc:
            print(f"  ✅ 已获取：{desc} = {txt}")
        return txt
    except Exception:
        if desc:
            print(f"  ⚠️ 未找到或文本为空：{desc}")
        return ""


def enter_sales_page(max_rounds=6):
    """恢复原始 iv_left -> rd_user_yq -> icon_yuanqu_1 -> search 流程。"""
    print("\n================ 阶段二：开启销售单处理流程 ================")
    ensure_native_context()

    locators = {
        "purchase_start": (AppiumBy.ID, "com.ibb.tizi:id/start_date"),
        "purchase_end": (AppiumBy.ID, "com.ibb.tizi:id/end_date"),
        "park_tab": (AppiumBy.ID, "com.ibb.tizi:id/rd_user_yq"),
        "park_icon": (AppiumBy.ID, "com.ibb.tizi:id/icon_yuanqu_1"),
        "business_back": (AppiumBy.ID, "com.ibb.tizi:id/image_main"),
        "back": (AppiumBy.ID, "com.ibb.tizi:id/iv_left"),
        "search": (AppiumBy.ID, "com.ibb.tizi:id/search"),
        "car_list": (AppiumBy.ID, "com.ibb.tizi:id/car_list"),
    }

    def current_state():
        if (
            locator_visible_now(locators["purchase_start"])
            or locator_visible_now(locators["purchase_end"])
        ):
            return "purchase_page"
        # 原始流程进入园区业务层后立即点击 search；此时 car_list
        # 可能尚未生成，image_main 也可能同时可见，不能据此退出页面。
        if locator_visible_now(locators["search"]):
            return "sales_page"
        if locator_visible_now(locators["park_icon"]):
            return "park_icon_ready"
        if locator_visible_now(locators["park_tab"]):
            return "park_home"
        if locator_visible_now(locators["business_back"]):
            return "business_page"
        if locator_visible_now(locators["back"]):
            return "inner_page"
        return "unknown"

    def click_once(locator, desc):
        nonlocal last_action_state
        last_action_state = current_state()
        wait_clickable(locator, timeout=8).click()
        print(f"  ✅ 已点击：{desc}")

    def wait_state_change(previous_state, timeout=8):
        def condition(_driver):
            state = current_state()
            return state if state not in {previous_state, "unknown"} else False

        return fast_wait(timeout).until(condition)

    def wait_known_state(timeout=10):
        def condition(_driver):
            state = current_state()
            return state if state != "unknown" else False

        return fast_wait(timeout).until(condition)

    last_state = "unknown"
    last_error = None
    last_action_state = None
    bounded_system_back_used = False
    last_stalled_state = None
    stalled_state_attempts = 0

    def register_stalled_state(state):
        nonlocal last_stalled_state, stalled_state_attempts
        if state == last_stalled_state:
            stalled_state_attempts += 1
        else:
            last_stalled_state = state
            stalled_state_attempts = 1
        return stalled_state_attempts

    def reset_stalled_state():
        nonlocal last_stalled_state, stalled_state_attempts
        last_stalled_state = None
        stalled_state_attempts = 0

    for round_no in range(1, max_rounds + 1):
        try:
            last_state = current_state()
            print(
                f"  [阶段二入口] 第 {round_no}/{max_rounds} 轮，"
                f"当前页面状态：{last_state}"
            )
            if last_state == "sales_page":
                break
            if last_state in {"purchase_page", "inner_page"}:
                click_once(locators["back"], "返回按钮 iv_left")
            elif last_state == "park_home":
                click_once(locators["park_tab"], "园区页签 rd_user_yq")
            elif last_state == "park_icon_ready":
                click_once(locators["park_icon"], "园区入口 icon_yuanqu_1")
            elif last_state == "business_page":
                click_once(locators["business_back"], "返回主业务层 image_main")
            else:
                print("  [阶段二入口] 页面处于加载过渡态，等待可识别页面，不消耗为盲点按键。")
                try:
                    recovered_state = wait_known_state(timeout=10)
                    print(f"  [阶段二入口] 页面已恢复识别为：{recovered_state}")
                    if recovered_state == "sales_page":
                        last_state = recovered_state
                        break
                except TimeoutException:
                    diagnostics = diagnose_current_app_page(
                        '阶段二入口持续 unknown'
                    )
                    if (
                        round_no < max_rounds
                        and diagnostics.get('package') == 'com.ibb.tizi'
                        and last_action_state in {'purchase_page', 'inner_page'}
                        and not bounded_system_back_used
                    ):
                        bounded_system_back_used = True
                        print(
                            '  [阶段二入口] 已确认 iv_left 点击后货运 App 持续 unknown，'
                            '执行仅一次受限系统返回恢复，并重新识别页面。'
                        )
                        driver.back()
                        try:
                            recovered_state = wait_known_state(timeout=10)
                            print(
                                '  [阶段二入口] 受限系统返回后页面恢复为：'
                                f'{recovered_state}'
                            )
                            continue
                        except TimeoutException:
                            print(
                                '  [阶段二入口] 受限系统返回后仍未知，'
                                '继续按既有 App 激活和账号级重建策略恢复。'
                            )
                    if round_no >= max_rounds:
                        last_error = TimeoutException("页面持续 unknown")
                        continue
                    print("  [阶段二入口] 页面持续未知，重新激活货运 App 后再识别。")
                    activate_tizi_and_wait(
                        list(locators.values()),
                        timeout=10,
                    )
                continue
            try:
                next_state = wait_state_change(last_state, timeout=8)
                reset_stalled_state()
                print(f"  [阶段二入口] 页面已切换为：{next_state}")
            except TimeoutException:
                print(
                    f"  ⚠️ 阶段二入口点击后页面状态暂未离开 {last_state}，"
                    "重新识别当前页面，不执行系统返回。"
                )
                if current_state() == last_state:
                    stalled = register_stalled_state(last_state)
                    if stalled >= MODULE_ENTRY_STALLED_MAX_ATTEMPTS:
                        raise SalesEntryError(
                            f"销售入口停留在 {last_state}，连续 {stalled} 次点击后未跳转"
                        )
                else:
                    reset_stalled_state()
        except TRANSIENT_UI_ERRORS as exc:
            raise_if_appium_session_lost(exc, "进入销售单页面")
            last_error = exc
            print(
                f"  ⚠️ 阶段二入口第 {round_no}/{max_rounds} 轮未完成："
                f"{type(exc).__name__}: {exc}"
            )
            try:
                failed_state = current_state()
            except Exception:
                failed_state = last_state
            if failed_state == last_state and failed_state != "unknown":
                stalled = register_stalled_state(failed_state)
                if stalled >= MODULE_ENTRY_STALLED_MAX_ATTEMPTS:
                    raise SalesEntryError(
                        f"销售入口停留在 {failed_state}，连续 {stalled} 次操作失败"
                    ) from exc
            else:
                reset_stalled_state()
            time.sleep(0.6)
    else:
        error_text = f"；最后错误：{last_error}" if last_error else ""
        message = (
            f"  ❌ 无法进入销售单查询页，最后页面状态：{last_state}"
            f"{error_text}"
        )
        print(message)
        raise SalesEntryError(message)

    try:
        click_step(
            locators["search"],
            "查询按钮 search",
            timeout=8,
            retries=2
        )
        global_car_no = wait_nonempty_text(
            locators["car_list"],
            timeout=12
        )
    except Exception as e:
        raise_if_appium_session_lost(e, "销售单查询和车牌读取")
        raise SalesEntryError(
            f"销售单入口页面未稳定就绪：{type(e).__name__}: {e}"
        ) from e

    print(f"  ✅ 当前作业车牌号: {global_car_no}")
    return global_car_no

#===============================================================




def wait_selected_car(car_no, timeout=10):
    """等待当前页面车牌下拉框显示指定车辆。"""
    locator = (AppiumBy.ID, "com.ibb.tizi:id/car_list")

    def condition(d):
        try:
            value = (d.find_element(*locator).text or "").strip()
            return value if value == car_no else False
        except (NoSuchElementException, StaleElementReferenceException):
            return False

    ensure_native_context()
    return fast_wait(timeout).until(condition)


def select_car_from_spinner(car_no, timeout=10):
    """
    在采购页或销售页选择指定车辆。
    使用 text_view_spinner + 精确文本，避免误点页面上方的 car_list 文本。
    """
    current = ""
    try:
        current = wait_nonempty_text(
            (AppiumBy.ID, "com.ibb.tizi:id/car_list"),
            timeout=3
        )
    except Exception:
        pass

    if current == car_no:
        print(f"  ✅ 当前已选车辆：{car_no}")
        return True

    click_step(
        (AppiumBy.ID, "com.ibb.tizi:id/car_list"),
        f"打开车辆下拉框（目标 {car_no}）",
        next_locator=(AppiumBy.ID, "com.ibb.tizi:id/text_view_spinner"),
        timeout=timeout,
        retries=2
    )

    option_locator = (
        AppiumBy.ANDROID_UIAUTOMATOR,
        'new UiSelector().resourceId("com.ibb.tizi:id/text_view_spinner").text("' + car_no + '")'
    )

    click_step(
        option_locator,
        f"选择车辆 {car_no}",
        timeout=timeout,
        retries=2
    )

    selected = wait_selected_car(car_no, timeout=timeout)
    print(f"  ✅ 车辆切换完成：{selected}")
    return True


def configure_purchase_date_filters():
    """设置采购单开始和结束日期；一个页面会话只需设置一次。"""
    print(f"正在设置开始时间: {TARGET_START_DATE}")
    wait_clickable((AppiumBy.ID, "com.ibb.tizi:id/start_date"), timeout=10).click()
    set_smart_date(driver, TARGET_START_DATE)
    wait_clickable((AppiumBy.ID, "com.ibb.tizi:id/btnSubmit"), timeout=10).click()

    print(f"正在设置结束时间: {TARGET_END_DATE}")
    wait_clickable((AppiumBy.ID, "com.ibb.tizi:id/end_date"), timeout=10).click()
    set_smart_date(driver, TARGET_END_DATE)
    wait_clickable((AppiumBy.ID, "com.ibb.tizi:id/btnSubmit"), timeout=10).click()


def ensure_purchase_date_filters():
    """每辆车查询前校验日期；日期已保留则不重复操作，被重置时才重新设置。"""
    try:
        start_text = wait_nonempty_text(
            (AppiumBy.ID, "com.ibb.tizi:id/start_date"),
            timeout=3
        )
        end_text = wait_nonempty_text(
            (AppiumBy.ID, "com.ibb.tizi:id/end_date"),
            timeout=3
        )
        if TARGET_START_DATE in start_text and TARGET_END_DATE in end_text:
            print(f"  ✅ 日期筛选已保留：{TARGET_START_DATE} 至 {TARGET_END_DATE}")
            return
    except Exception:
        pass

    print("  ℹ️ 日期筛选未设置或已被重置，重新设置。")
    configure_purchase_date_filters()


def query_current_vehicle(desc, timeout=12):
    """
    点击当前页面查询按钮。
    某些车辆可能没有运单，空结果页不一定立即暴露 rv，因此这里不把 rv 作为硬性成功条件。
    """
    click_step(
        (AppiumBy.ID, "com.ibb.tizi:id/search"),
        desc,
        timeout=timeout,
        retries=2
    )

    # 有数据时 rv 会很快出现；无数据时允许继续，由后面的遍历结束逻辑判空。
    try:
        wait_visible((AppiumBy.ID, "com.ibb.tizi:id/rv"), timeout=5)
    except Exception as error:
        if wireless_link.is_enabled():
            if not isinstance(error, TimeoutException) or is_appium_session_lost(error):
                raise
            if _ui_pause_if_external("query_result_empty"):
                wait_visible((AppiumBy.ID, "com.ibb.tizi:id/rv"), timeout=5)
        print("  ℹ️ 当前车辆查询后未发现列表容器，按空结果继续检查。")

    # 切车后列表数据可能异步刷新；保留短稳定窗口，避免读取上一辆车的旧卡片。
    time.sleep(1.0)


def detect_purchase_pagination_replay(
    visible_processed_waybills,
    processed_waybill_order,
    saw_unprocessed_waybill,
):
    '''
    检测采购列表分页后是否回放了前面已经处理过的卡片。

    正常滚动时，当前屏可能仍保留最后一条或相邻上一条，不能据此结束。
    只有当前屏没有任何新单号，并且重新出现更早的已处理单号，才判定分页回放。
    '''
    if saw_unprocessed_waybill or len(processed_waybill_order) < 3:
        return ()

    processed_index = {
        wb_no: index
        for index, wb_no in enumerate(processed_waybill_order)
    }
    latest_index = len(processed_waybill_order) - 1
    replayed = [
        wb_no
        for wb_no in visible_processed_waybills
        if processed_index.get(wb_no, latest_index) <= latest_index - 2
    ]
    return tuple(dict.fromkeys(replayed))


def process_purchase_current_vehicle(sheet_name, pdf_dir, selected_car):
    """处理采购页当前已选车辆的全部运单。业务逻辑与河北采购单保持一致。"""
    print(f"\n========== 采购车辆 {selected_car} 开始 ==========")

    # 特殊账号切换车辆后 RecyclerView 会保留上一辆车的滚动位置，必须先回顶。
    reset_recycler_to_top(
        "com.ibb.tizi:id/waybill_no",
        desc=f"采购车辆 {selected_car} 列表"
    )

    processed_waybills = set()
    processed_waybill_order = []
    last_page_source = ""
    written_count = 0

    while True:
        cards = driver.find_elements(AppiumBy.XPATH, '//*[@resource-id="com.ibb.tizi:id/rv"]/*')
        processed_any_in_this_loop = False
        visible_processed_waybills = []
        saw_unprocessed_waybill = False

        for card in cards:
            waybill_els = card.find_elements(AppiumBy.ID, "com.ibb.tizi:id/waybill_no")
            if not waybill_els:
                continue

            wb_no = (waybill_els[0].text or "").strip()
            if not wb_no:
                continue
            if wb_no in processed_waybills:
                visible_processed_waybills.append(wb_no)
                continue
            saw_unprocessed_waybill = True

            btn_els = card.find_elements(AppiumBy.ID, "com.ibb.tizi:id/btn_view_pounds")
            if not btn_els:
                print(f"  [-] 单号 {wb_no} 按钮未露出，等待滑动...")
                continue

            def get_text(id_str):
                els = card.find_elements(AppiumBy.ID, id_str)
                return "\n".join([el.text for el in els]) if els else "无"

            info = {
                "单号": wb_no,
                "状态": get_text("com.ibb.tizi:id/state"),
                "货主名称": get_text("com.ibb.tizi:id/hz_name"),
                "结算运费": get_text("com.ibb.tizi:id/wcost"),
                "结算单价": get_text("com.ibb.tizi:id/wcost_unit"),
                "亏吨单价": get_text("com.ibb.tizi:id/lost_tone_unit"),
                "亏吨量": get_text("com.ibb.tizi:id/lost_tone_num"),
                "扣油/其他": f"{get_text('com.ibb.tizi:id/lost_tone_amount')} {get_text('com.ibb.tizi:id/lost_oil')}",
                "物料": get_text("com.ibb.tizi:id/gName"),
                "车牌号": get_text("com.ibb.tizi:id/wCarNoA"),
                "承运商": get_text("com.ibb.tizi:id/shipName"),
                "装车毛重": get_text("com.ibb.tizi:id/wUploadRoughWeight"),
                "卸车毛重": get_text("com.ibb.tizi:id/wUnloadRoughWeight"),
                "装车皮重": get_text("com.ibb.tizi:id/wUploadTareWeight"),
                "卸车皮重": get_text("com.ibb.tizi:id/wUnloadTareWeight"),
                "装车净重": get_text("com.ibb.tizi:id/wUploadWeight"),
                "卸车净重": get_text("com.ibb.tizi:id/wUnloadWeight"),
                "接单时间": get_text("com.ibb.tizi:id/creatTime"),
                "卸货库房": get_text("com.ibb.tizi:id/warehouseName")
            }

            print(f"\n✅ [{selected_car}] 提取采购单 [{wb_no}] -> 写入 {sheet_name}")
            register_expected_pdf_slots(pdf_dir, wb_no, info["\u8f66\u724c\u53f7"], 1, indexed=False)
            write_data_to_excel(
                info,
                sheet_name,
                is_sales=False,
                source_kind="selected_car",
                source_value=selected_car,
            )
            written_count += 1

            print("  -> 点击详情执行下载...")
            btn_els[0].click()

            pounds_text_label = wait_visible(
                (AppiumBy.ID, "com.ibb.tizi:id/tv_pounds_pic"),
                timeout=12
            )
            require_single_pdf_download(
                driver,
                wait,
                wb_no,
                info["车牌号"],
                pounds_text_label,
                pdf_dir,
                source_kind="selected_car",
                source_value=selected_car,
                sheet_name=sheet_name,
            )

            print("  <- 返回主列表...")
            click_back_to_list(timeout=10)

            processed_waybills.add(wb_no)
            processed_waybill_order.append(wb_no)
            processed_any_in_this_loop = True
            break

        if processed_any_in_this_loop:
            print("\n  [加速滚动] 大跨步上移...")
            swipe_with_brake(driver, 1800, 650)
        else:
            replayed_waybills = detect_purchase_pagination_replay(
                visible_processed_waybills,
                processed_waybill_order,
                saw_unprocessed_waybill,
            )
            if replayed_waybills:
                print(
                    "\n✅ 采购车辆分页出现重复旧卡片，判定处理完毕："
                    f"{', '.join(replayed_waybills)} 已在全局集合中处理过。"
                )
                break

            current_page_source = driver.page_source
            if current_page_source == last_page_source:
                print(f"\n✅ 采购车辆 {selected_car} 处理完毕，共写入 {written_count} 条。")
                break
            last_page_source = current_page_source
            print("\n  [微调滚动] 卡片半遮挡，进行小碎步对齐...")
            swipe_with_brake(driver, 1800, 1350)

    return written_count


SHANGHAI_SALES_ENTRY_LOCATORS = {
    'sales_search': (AppiumBy.ID, 'com.ibb.tizi:id/search'),
    'car_list': (AppiumBy.ID, 'com.ibb.tizi:id/car_list'),
    'purchase_start_date': (AppiumBy.ID, 'com.ibb.tizi:id/start_date'),
    'purchase_end_date': (AppiumBy.ID, 'com.ibb.tizi:id/end_date'),
    'park_tab': (AppiumBy.ID, 'com.ibb.tizi:id/rd_user_yq'),
    'park_icon': (AppiumBy.ID, 'com.ibb.tizi:id/icon_yuanqu_1'),
    'back': (AppiumBy.ID, 'com.ibb.tizi:id/iv_left'),
}


class ShanghaiSalesEntryError(AccountSessionSuspectedError):
    """上海销售阶段入口经过页面状态恢复后仍未就绪。"""


class ShanghaiSalesTechnicalError(StageRecoveryExhaustedError):
    """上海销售当前车辆经过阶段级恢复后仍未完成。"""


def locator_visible_now(locator):
    """立即判断控件是否可见；会话死亡不能被当成“控件不存在”。"""
    try:
        return any(el.is_displayed() for el in driver.find_elements(*locator))
    except (NoSuchElementException, StaleElementReferenceException):
        return False
    except Exception as exc:
        raise_if_appium_session_lost(exc, f'识别页面控件 {locator}')
        return False


def diagnose_current_app_page(label, max_resource_ids=20):
    '''记录持续 unknown 页面的包名、Activity、结构签名和资源 ID，不输出业务文本。'''
    ensure_native_context()
    package = ''
    activity = ''
    source = ''

    try:
        package = str(driver.current_package or '')
        activity = str(driver.current_activity or '')
        source = str(driver.page_source or '')
    except Exception as exc:
        raise_if_appium_session_lost(exc, '采集 unknown 页面诊断')

    resource_ids = []
    seen = set()
    for resource_id in re.findall(r'resource-id=\x22([^\x22]+)\x22', source):
        value = str(resource_id or '').strip()
        if not value or value in seen:
            continue
        seen.add(value)
        resource_ids.append(value)
        if len(resource_ids) >= max_resource_ids:
            break

    signature = hashlib.sha256(source.encode('utf-8')).hexdigest()[:12] if source else 'empty'
    package_label = package or 'unknown'
    activity_label = activity or 'unknown'
    resource_id_labels = resource_ids or ['<none>']
    print(
        f'  [页面诊断] {label}：package={package_label}，'
        f'activity={activity_label}，source_sha256={signature}，'
        f'resource_ids={resource_id_labels}'
    )
    return {
        'package': package,
        'activity': activity,
        'source_signature': signature,
        'resource_ids': resource_ids,
    }


def get_shanghai_sales_entry_state():
    """识别销售页、采购页、园区主页或内层页面，避免盲目重复点击。"""
    ensure_native_context()
    locators = SHANGHAI_SALES_ENTRY_LOCATORS
    has_search = locator_visible_now(locators['sales_search'])
    has_car = locator_visible_now(locators['car_list'])
    has_purchase_dates = (
        locator_visible_now(locators['purchase_start_date'])
        or locator_visible_now(locators['purchase_end_date'])
    )

    # 采购页同样有 search/car_list，必须用采购日期控件排除误判。
    if has_search and has_car and not has_purchase_dates:
        return 'sales_ready'
    if locator_visible_now(locators['park_icon']):
        return 'park_icon_ready'
    if locator_visible_now(locators['park_tab']):
        return 'park_home'
    if has_purchase_dates:
        return 'purchase_page'
    if locator_visible_now(locators['back']):
        return 'inner_page'
    return 'unknown'


def wait_for_shanghai_sales_state(expected_states, timeout=15):
    expected_states = set(expected_states)

    def condition(_driver):
        state = get_shanghai_sales_entry_state()
        return state if state in expected_states else False

    return fast_wait(timeout).until(condition)


def wait_for_shanghai_sales_ready(timeout=25, stable_rounds=3):
    """销售页标志连续稳定多轮才算就绪，避开采购页退出时的旧控件残影。"""
    stable_count = 0

    def condition(_driver):
        nonlocal stable_count
        if get_shanghai_sales_entry_state() == 'sales_ready':
            stable_count += 1
            return 'sales_ready' if stable_count >= stable_rounds else False
        stable_count = 0
        return False

    return fast_wait(timeout).until(condition)


def enter_sales_page_without_query(max_rounds=6):
    """按当前页面状态幂等进入上海销售页，不重复点击已经离开的旧页面。"""
    print('\n================ 阶段二：上海宾迪销售单逐车处理 ================')
    locators = SHANGHAI_SALES_ENTRY_LOCATORS
    last_error = None
    last_failed_state = None
    failed_state_attempts = 0

    for round_no in range(1, max_rounds + 1):
        try:
            ensure_native_context()
            state = get_shanghai_sales_entry_state()
            print(
                f'  [上海销售入口] 第 {round_no}/{max_rounds} 轮，'
                f'当前页面状态：{state}'
            )

            if state == 'sales_ready':
                wait_for_shanghai_sales_ready(timeout=20)
                current_car = wait_nonempty_text(locators['car_list'], timeout=20)
                print(f'  ✅ 上海销售页已就绪，当前车辆筛选：{current_car}')
                return True

            if state == 'park_icon_ready':
                entry = wait_clickable(locators['park_icon'], timeout=12)
                entry.click()
                print('  [上海销售入口] 园区入口点击已发送，等待销售页面真实就绪...')
                wait_for_shanghai_sales_ready(timeout=25)
                current_car = wait_nonempty_text(locators['car_list'], timeout=20)
                print(f'  ✅ 已进入上海销售页，当前车辆筛选：{current_car}')
                return True

            if state == 'park_home':
                park_tab = wait_clickable(locators['park_tab'], timeout=12)
                park_tab.click()
                print('  [上海销售入口] 已点击园区页签，等待园区入口出现...')
                wait_for_shanghai_sales_state(
                    {'park_icon_ready', 'sales_ready'},
                    timeout=15,
                )
                continue

            if state == 'purchase_page':
                back_button = wait_clickable(locators['back'], timeout=12)
                back_button.click()
                print('  [上海销售入口] 已从采购页返回，等待园区主页...')
                wait_for_shanghai_sales_state(
                    {'park_home', 'park_icon_ready', 'sales_ready'},
                    timeout=15,
                )
                continue

            if state == 'inner_page':
                back_button = wait_clickable(locators['back'], timeout=12)
                back_button.click()
                print('  [上海销售入口] 已退出内层页面，重新识别所在页面...')
                wait_for_shanghai_sales_state(
                    {'purchase_page', 'park_home', 'park_icon_ready', 'sales_ready'},
                    timeout=15,
                )
                continue

            # 页面正在加载时先等，不立刻返回或重复点击；持续未知才用系统返回恢复。
            print('  [上海销售入口] 页面标志暂未绑定，先等待稳定...')
            try:
                wait_for_shanghai_sales_state(
                    {'purchase_page', 'park_home', 'park_icon_ready', 'sales_ready', 'inner_page'},
                    timeout=8,
                )
            except TimeoutException:
                print('  [上海销售入口] 页面持续未知，尝试系统返回键恢复层级...')
                driver.back()
                time.sleep(1.0)

        except Exception as exc:
            raise_if_appium_session_lost(exc, '恢复上海销售入口')
            last_error = exc
            try:
                failed_state = get_shanghai_sales_entry_state()
            except Exception:
                failed_state = '无法识别'
            print(
                f'  ⚠️ [上海销售入口] 第 {round_no}/{max_rounds} 轮未完成；'
                f'当前状态：{failed_state}；{type(exc).__name__}: {exc}'
            )
            if failed_state == last_failed_state:
                failed_state_attempts += 1
            else:
                last_failed_state = failed_state
                failed_state_attempts = 1
            if (
                failed_state != '无法识别'
                and failed_state_attempts >= MODULE_ENTRY_STALLED_MAX_ATTEMPTS
            ):
                raise ShanghaiSalesEntryError(
                    f'上海销售入口停留在 {failed_state}，'
                    f'连续 {failed_state_attempts} 次操作后未跳转'
                ) from exc
            if round_no < max_rounds:
                try:
                    driver.activate_app('com.ibb.tizi')
                    ensure_native_context()
                except Exception as activate_error:
                    raise_if_appium_session_lost(activate_error, '重新激活货运 App')
                time.sleep(1.0)

    raise ShanghaiSalesEntryError(
        f'上海销售入口连续 {max_rounds} 轮恢复失败'
    ) from last_error


def process_sales_current_vehicle(sheet_name, pdf_dir, selected_car):
    """处理销售页当前已选车辆的全部运单。业务规则与河北销售单保持一致。"""
    print(f"\n========== 销售车辆 {selected_car} 开始 ==========")

    # 特殊账号切换车辆后 RecyclerView 会保留上一辆车的滚动位置，必须先回顶。
    reset_recycler_to_top(
        "com.ibb.tizi:id/item_way_history_num",
        desc=f"销售车辆 {selected_car} 列表"
    )

    sales_processed = set()
    last_page_source = ""
    sales_cutoff_date = SALES_START_DATE_VALUE
    sales_end_date = SALES_END_DATE_VALUE
    written_count = 0


    while True:
        cards = driver.find_elements(AppiumBy.XPATH, '//*[@resource-id="com.ibb.tizi:id/rv"]/*')
        processed_in_loop = False
        time_exceeded = False

        for card in cards:
            num_els = card.find_elements(AppiumBy.ID, "com.ibb.tizi:id/item_way_history_num")
            if not num_els:
                continue

            order_no = (num_els[0].text or "").strip()
            if not order_no or order_no in sales_processed:
                continue

            export_time_str = read_first_nonempty_child_text(
                card,
                "com.ibb.tizi:id/item_way_history_expected_export_time",
                retries=4,
                interval=0.25
            )

            # 预计出场时间仅用于时间范围和“当天无附件”判断，
            # 不再作为“有附件销售单是否写入 Excel”的前置条件。
            dt = None
            time_is_valid = False

            if not export_time_str:
                # 同一卡片内部已做短轮询；仍为空时不再滑动重抓，
                # 直接检查当前运单的附件，避免列表位置变化后串到下一运单。
                print(
                    f"  ⚠️ [{selected_car}] 单号 {order_no} 预计出场时间为空，"
                    "继续检查该运单磅单附件；如有附件仍下载并写入 Excel。"
                )
            else:
                try:
                    dt = datetime.datetime.strptime(export_time_str, "%Y-%m-%d %H:%M:%S")
                    time_is_valid = True
                    print(f"  [时间校验][{selected_car}] 单号 {order_no} 预计出场时间: {dt}")

                    if dt.date() > sales_end_date:
                        print(
                            f'  ⏭️ [{selected_car}] 单号 {order_no} 晚于销售结束日期 '
                            f'{sales_end_date.isoformat()}，跳过当前卡片。'
                        )
                        sales_processed.add(order_no)
                        processed_in_loop = True
                        continue

                    if dt.date() < sales_cutoff_date:
                        print(
                            f"  🛑 [{selected_car}] 单号 {order_no} 早于销售开始日期 "
                            f"(最早有效日期: {sales_cutoff_date.strftime('%Y-%m-%d')})，当前车辆遍历终止！"
                        )
                        time_exceeded = True
                        break
                except Exception as e:
                    print(
                        f"  ⚠️ [{selected_car}] 时间解析失败: '{export_time_str}'，"
                        f"继续进入详情检查磅单附件；如有附件仍写入 Excel。错误: {e}"
                    )

            is_today = bool(time_is_valid and dt.date() == ANCHOR_DATE_VALUE)

            btn_els = card.find_elements(AppiumBy.ID, "com.ibb.tizi:id/btn_view_pounds")
            if not btn_els:
                print(f"  [-] [{selected_car}] 单号 {order_no} 显示不全，等滑动...")
                continue

            def get_card_text(id_str):
                els = card.find_elements(AppiumBy.ID, id_str)
                return "\n".join([el.text for el in els]) if els else "无"

            register_pdf_enumeration_pending(pdf_dir, order_no, selected_car)
            raw_address = get_card_text("com.ibb.tizi:id/item_way_history_company_name")
            goods_name = get_card_text("com.ibb.tizi:id/item_way_history_goods")
            load_net_weight = get_card_text("com.ibb.tizi:id/item_way_history_load_net_weight")

            print("  -> 进入详情检查电子磅单附件...")
            btn_els[0].click()
            time.sleep(3)

            try:
                pic_links = driver.find_elements(AppiumBy.ID, "com.ibb.tizi:id/tv_pounds_pic")
                total_pics = len(pic_links)
                print(f"  ✅ [{selected_car}] 该单发现 {total_pics} 个磅单附件！")
            except Exception as e:
                if isinstance(e, (AppiumSessionOwnershipError, wireless_link.ConnectionUnavailable)):
                    raise
                print(f"  ⚠️ [{selected_car}] 详情页磅单附件统计异常: {e}")
                raise PdfDownloadIncompleteError("pdf_attachment_enumeration_failed") from e

            register_expected_pdf_slots(pdf_dir, order_no, selected_car, total_pics, indexed=True)

            # 时间无效且没有附件时，仍保持原先“不写入”的处理。
            # 但只要发现附件，就必须继续下载并写入 Excel。
            if not time_is_valid and total_pics == 0:
                print(
                    f"  ⏭️ [{selected_car}] 单号 {order_no} 预计出场时间无效且无磅单附件，"
                    "按原逻辑跳过且不写入 Excel。"
                )
                print("  <- 返回列表...")
                click_back_to_list(timeout=10)
                sales_processed.add(order_no)
                processed_in_loop = True
                break

            if is_today and total_pics == 0:
                print(f"  ⏭️ [{selected_car}] 单号 {order_no} 是当天单据且无磅单附件，跳过且不写入 Excel。")
                print("  <- 返回列表...")
                click_back_to_list(timeout=10)
                sales_processed.add(order_no)
                processed_in_loop = True
                break

            print(f"  [AI输入地址原文] {raw_address!r}")
            print("  [AI] 正在分析复杂地址...")
            clean_addr = extract_address_with_llm(raw_address)
            print(f"  [AI输出地址] {clean_addr!r}")

            info = {
                "单号": order_no,
                "车牌号": selected_car,
                "货物名称": goods_name,
                "卸货地": clean_addr,
                # 销售表“装货日期”暂时留空，预计出场时间仅用于内部判断。
                "装货日期": "",
                "装货重量": load_net_weight,
                "备注2": ""
            }

            if total_pics == 0:
                info["备注2"] = "缺失磅单信息"
                print(f"\n✅ [{selected_car}] 销售单 [{order_no}] 缺失磅单 -> 写入 {sheet_name} 并写备注2")
                target_row = write_data_to_excel(
                    info,
                    sheet_name,
                    is_sales=True,
                    source_kind="selected_car",
                    source_value=selected_car,
                )
                persist_sales_freight_target(
                    sheet_name=sheet_name,
                    source_kind="selected_car",
                    source_value=selected_car,
                    waybill=order_no,
                    car=selected_car,
                    app_export_time=export_time_str,
                    excel_row=target_row,
                )
                written_count += 1

                print("  <- 返回列表...")
                click_back_to_list(timeout=10)
                sales_processed.add(order_no)
                processed_in_loop = True
                break

            download_success_count = 0
            print(f"  -> 开始下载 {total_pics} 个电子磅单附件...")

            for idx in range(total_pics):
                refreshed_links = driver.find_elements(AppiumBy.ID, "com.ibb.tizi:id/tv_pounds_pic")
                if idx < len(refreshed_links):
                    print(f"    [下载器] 正在处理第 {idx + 1}/{total_pics} 个附件...")
                    ok = execute_single_download(
                        driver,
                        wait,
                        order_no,
                        selected_car,
                        refreshed_links[idx],
                        pdf_dir,
                        file_index=str(idx + 1),
                        source_kind="selected_car",
                        source_value=selected_car,
                        sheet_name=sheet_name,
                    )
                    if ok:
                        download_success_count += 1

            require_pdf_downloads_complete(total_pics, download_success_count)
            complete_expected_pdf_slots(pdf_dir, order_no, selected_car)

            print(f"\n✅ [{selected_car}] 销售单 [{order_no}] -> 写入 {sheet_name}")
            target_row = write_data_to_excel(
                info,
                sheet_name,
                is_sales=True,
                source_kind="selected_car",
                source_value=selected_car,
            )
            persist_sales_freight_target(
                sheet_name=sheet_name,
                source_kind="selected_car",
                source_value=selected_car,
                waybill=order_no,
                car=selected_car,
                app_export_time=export_time_str,
                excel_row=target_row,
            )
            written_count += 1

            print("  <- 返回列表...")
            click_back_to_list(timeout=10)
            sales_processed.add(order_no)
            processed_in_loop = True
            break


        if time_exceeded:
            break

        if processed_in_loop:
            print("\n  [加速滚动] 大跨步上移...")
            swipe_with_brake(driver, 1800, 700)
        else:
            current_page_source = driver.page_source
            if current_page_source == last_page_source:
                print(f"\n✅ 销售车辆 {selected_car} 处理完毕，共写入 {written_count} 条。")
                break
            last_page_source = current_page_source
            print("\n  [微调滚动] 卡片半遮挡，小碎步对齐...")
            swipe_with_brake(driver, 1800, 1300)

    return written_count


SHANGHAI_SALES_VEHICLE_MAX_ATTEMPTS = 3


def run_shanghai_sales_stage_with_recovery(phone):
    """逐车处理上海销售；入口或当前车辆失败时在本阶段内恢复，不重跑采购。"""
    sales_total = 0
    completed_cars = []
    sales_page_ready = False
    rebuild_before_next_attempt = False

    for car_index, car_no in enumerate(SHANGHAI_CARS, start=1):
        last_error = None

        for attempt in range(1, SHANGHAI_SALES_VEHICLE_MAX_ATTEMPTS + 1):
            try:
                if rebuild_before_next_attempt:
                    print(
                        f'  [上海销售断点] 为车辆 {car_no} 重建 Appium 会话并重新登录；'
                        f'已完成车辆不会重跑：{completed_cars or "无"}'
                    )
                    recreate_appium_session(
                        f'上海销售车辆 {car_no} 第 {attempt} 次尝试前恢复'
                    )
                    restart_app_keep_state()
                    login_with_account(phone)
                    rebuild_before_next_attempt = False
                    sales_page_ready = False

                if not sales_page_ready:
                    enter_sales_page_without_query()
                    sales_page_ready = True

                print(
                    f'\n######## 上海销售第 {car_index}/{len(SHANGHAI_CARS)} 辆：'
                    f'{car_no}（尝试 {attempt}/{SHANGHAI_SALES_VEHICLE_MAX_ATTEMPTS}） ########'
                )
                select_car_from_spinner(car_no, timeout=15)
                query_current_vehicle(f'上海销售车辆 {car_no} 查询', timeout=15)
                written_count = process_sales_current_vehicle(
                    '销售单-上海宾迪',
                    PDF_SALES_SHANGHAI_DIR,
                    car_no,
                )
                sales_total += written_count
                completed_cars.append(car_no)
                print(
                    f'  ✅ [上海销售断点] 车辆 {car_no} 已完成；'
                    f'当前已完成 {len(completed_cars)}/{len(SHANGHAI_CARS)} 辆。'
                )
                sales_page_ready = True
                break

            except Exception as exc:
                if isinstance(exc, (AppiumSessionOwnershipError, wireless_link.ConnectionUnavailable)):
                    raise
                last_error = exc
                relogin_required = is_account_relogin_error(exc)
                print(
                    f'  ⚠️ [上海销售断点] 车辆 {car_no} 第 '
                    f'{attempt}/{SHANGHAI_SALES_VEHICLE_MAX_ATTEMPTS} 次未完成；'
                    f'{type(exc).__name__}: {exc}'
                )

                if attempt >= SHANGHAI_SALES_VEHICLE_MAX_ATTEMPTS:
                    break

                sales_page_ready = False
                # 入口状态机已经自行恢复多轮；它仍失败时直接换新会话。
                # 其他普通 UI 抖动先在原会话恢复一次，连续第二次再换会话。
                rebuild_before_next_attempt = (
                    relogin_required
                    or attempt >= 2
                )

                if not rebuild_before_next_attempt:
                    try:
                        driver.activate_app('com.ibb.tizi')
                        ensure_native_context()
                        print('  [上海销售断点] 已重新激活货运 App，下次从当前车辆恢复。')
                    except Exception as activate_error:
                        if isinstance(activate_error, (AppiumSessionOwnershipError, wireless_link.ConnectionUnavailable)):
                            raise
                        print(
                            f'  ⚠️ [上海销售断点] 重新激活 App 失败，将改为重建会话：'
                            f'{activate_error}'
                        )
                        rebuild_before_next_attempt = True
                time.sleep(1.0)
        if car_no not in completed_cars:
            raise ShanghaiSalesTechnicalError(
                f'上海销售车辆 {car_no} 连续 '
                f'{SHANGHAI_SALES_VEHICLE_MAX_ATTEMPTS} 次恢复失败；'
                f'已完成车辆：{completed_cars or "无"}'
            ) from last_error

    return sales_total


def run_shanghai_special_flow(phone):
    """15830152319：采购和销售均按 10 辆车逐车循环。"""
    print(f"\n\n================ 上海宾迪特殊账号：{phone} ================")
    restart_app_keep_state()
    login_with_account(phone)

    print("\n================ 阶段一：上海宾迪采购单逐车处理 ================")
    purchase_total = 0

    for car_index, car_no in enumerate(SHANGHAI_CARS, start=1):
        print(f"\n######## 上海采购第 {car_index}/{len(SHANGHAI_CARS)} 辆：{car_no} ########")
        select_car_from_spinner(car_no, timeout=10)

        # 每辆车先校验日期。正常保留时不会重复操作；若 App 切车后重置日期则自动补设。
        ensure_purchase_date_filters()

        query_current_vehicle(f"上海采购车辆 {car_no} 查询", timeout=12)
        purchase_total += process_purchase_current_vehicle(
            "采购单-上海宾迪",
            PDF_PURCHASE_SHANGHAI_DIR,
            car_no
        )

    sales_total = run_shanghai_sales_stage_with_recovery(phone)
    freight_total = run_shanghai_freight_stage_with_recovery(phone)

    print(
        f"✅ 上海宾迪账号 {phone} 完整流程执行完成："
        f"采购写入 {purchase_total} 条，销售写入 {sales_total} 条，"
        f"运费规格采集 {freight_total} 条。"
    )


def run_full_flow_for_account(phone):
    print(f"\n\n================ 当前账号：{phone} 开始完整流程 ================")

    # 先强制回到登录页
    restart_app_keep_state()

    # 登录当前账号
    login_with_account(phone)

    # -------------------------------------------------------------
    # === 阶段一：采购单处理 ===
    # -------------------------------------------------------------
    print("\n================ 阶段一：采购单处理 ================")
    print(f"正在设置开始时间: {TARGET_START_DATE}")
    wait_clickable((AppiumBy.ID, "com.ibb.tizi:id/start_date"), timeout=10).click()
    set_smart_date(driver, TARGET_START_DATE)
    wait_clickable((AppiumBy.ID, "com.ibb.tizi:id/btnSubmit"), timeout=10).click()

    print(f"正在设置结束时间: {TARGET_END_DATE}")
    wait_clickable((AppiumBy.ID, "com.ibb.tizi:id/end_date"), timeout=10).click()
    set_smart_date(driver, TARGET_END_DATE)
    wait_clickable((AppiumBy.ID, "com.ibb.tizi:id/btnSubmit"), timeout=10).click()

    print("点击查询...")
    # 查询成功后，空结果页可能不会创建 RecyclerView；不能把“没有 rv”误判为点击失败。
    # 复用特殊账号已验证的查询逻辑：按钮成功点击即视为查询动作完成，列表为空则继续销售阶段。
    query_current_vehicle("采购单查询按钮", timeout=12)

    initial_purchase_cards = []
    for _ in range(6):
        initial_purchase_cards = driver.find_elements(
            AppiumBy.XPATH,
            '//*[@resource-id="com.ibb.tizi:id/rv"]/*'
        )
        if initial_purchase_cards:
            break
        time.sleep(0.5)
    purchase_result_empty = not initial_purchase_cards
    if wireless_link.is_enabled() and purchase_result_empty:
        while _ui_pause_if_external("purchase_result_empty"):
            initial_purchase_cards = driver.find_elements(
                AppiumBy.XPATH, '//*[@resource-id="com.ibb.tizi:id/rv"]/*')
            purchase_result_empty = not initial_purchase_cards
            if initial_purchase_cards:
                break
    if purchase_result_empty:
        print("  ℹ️ 当前账号在指定时间段内没有采购运单，跳过采购遍历，继续执行销售单流程。")

    processed_waybills = set()
    processed_waybill_order = []
    last_page_source = ""

    while not purchase_result_empty:
        cards = driver.find_elements(AppiumBy.XPATH, '//*[@resource-id="com.ibb.tizi:id/rv"]/*')
        processed_any_in_this_loop = False
        visible_processed_waybills = []
        saw_unprocessed_waybill = False

        for card in cards:
            waybill_els = card.find_elements(AppiumBy.ID, "com.ibb.tizi:id/waybill_no")
            if not waybill_els:
                continue

            wb_no = (waybill_els[0].text or "").strip()
            if not wb_no:
                continue
            if wb_no in processed_waybills:
                visible_processed_waybills.append(wb_no)
                continue
            saw_unprocessed_waybill = True

            btn_els = card.find_elements(AppiumBy.ID, "com.ibb.tizi:id/btn_view_pounds")
            if not btn_els:
                print(f"  [-] 单号 {wb_no} 按钮未露出，等待滑动...")
                continue

            def get_text(id_str):
                els = card.find_elements(AppiumBy.ID, id_str)
                return "\n".join([el.text for el in els]) if els else "无"

            info = {
                "单号": wb_no,
                "状态": get_text("com.ibb.tizi:id/state"),
                "货主名称": get_text("com.ibb.tizi:id/hz_name"),
                "结算运费": get_text("com.ibb.tizi:id/wcost"),
                "结算单价": get_text("com.ibb.tizi:id/wcost_unit"),
                "亏吨单价": get_text("com.ibb.tizi:id/lost_tone_unit"),
                "亏吨量": get_text("com.ibb.tizi:id/lost_tone_num"),
                "扣油/其他": f"{get_text('com.ibb.tizi:id/lost_tone_amount')} {get_text('com.ibb.tizi:id/lost_oil')}",
                "物料": get_text("com.ibb.tizi:id/gName"),
                "车牌号": get_text("com.ibb.tizi:id/wCarNoA"),
                "承运商": get_text("com.ibb.tizi:id/shipName"),
                "装车毛重": get_text("com.ibb.tizi:id/wUploadRoughWeight"),
                "卸车毛重": get_text("com.ibb.tizi:id/wUnloadRoughWeight"),
                "装车皮重": get_text("com.ibb.tizi:id/wUploadTareWeight"),
                "卸车皮重": get_text("com.ibb.tizi:id/wUnloadTareWeight"),
                "装车净重": get_text("com.ibb.tizi:id/wUploadWeight"),
                "卸车净重": get_text("com.ibb.tizi:id/wUnloadWeight"),
                "接单时间": get_text("com.ibb.tizi:id/creatTime"),
                "卸货库房": get_text("com.ibb.tizi:id/warehouseName")
            }

            print(f"\n✅ 提取新单据 [{wb_no}] -> 写入 Excel")
            register_expected_pdf_slots(PDF_PURCHASE_DIR, wb_no, info["\u8f66\u724c\u53f7"], 1, indexed=False)
            write_data_to_excel(
                info,
                "采购单-河北众沃",
                is_sales=False,
                source_kind="login_account",
                source_value=phone,
            )

            print(f"  -> 点击详情执行下载...")
            btn_els[0].click()

            pounds_text_label = wait_visible(
                (AppiumBy.ID, "com.ibb.tizi:id/tv_pounds_pic"),
                timeout=12
            )
            require_single_pdf_download(
                driver,
                wait,
                wb_no,
                info["车牌号"],
                pounds_text_label,
                PDF_PURCHASE_DIR,
                source_kind="login_account",
                source_value=phone,
                sheet_name="采购单-河北众沃",
            )

            print("  <- 返回主列表...")
            click_back_to_list(timeout=10)

            processed_waybills.add(wb_no)
            processed_waybill_order.append(wb_no)
            processed_any_in_this_loop = True
            break

        if processed_any_in_this_loop:
            print("\n  [加速滚动] 大跨步上移...")
            swipe_with_brake(driver, 1800, 650)
        else:
            if wireless_link.is_enabled() and _ui_pause_if_external("purchase_page_end"):
                last_page_source = ""
                continue
            replayed_waybills = detect_purchase_pagination_replay(
                visible_processed_waybills,
                processed_waybill_order,
                saw_unprocessed_waybill,
            )
            if replayed_waybills:
                print(
                    "\n✅采购单分页出现重复旧卡片，判定处理完毕："
                    f"{', '.join(replayed_waybills)} 已在全局集合中处理过。"
                )
                break

            current_page_source = driver.page_source
            if wireless_link.is_enabled() and _ui_pause_if_external("purchase_page_end"):
                last_page_source = ""
                continue
            if current_page_source == last_page_source:
                print("\n✅采购单处理完毕！")
                break
            last_page_source = current_page_source if wireless_link.is_enabled() else driver.page_source
            print("\n  [微调滚动] 卡片半遮挡，进行小碎步对齐...")
            swipe_with_brake(driver, 1800, 1350)

    # -------------------------------------------------------------
    # === 阶段二：销售单处理 ===
    # -------------------------------------------------------------
    # driver.find_element(AppiumBy.ID, "com.ibb.tizi:id/iv_left").click()
    # time.sleep(1)
    # driver.find_element(AppiumBy.ID, "com.ibb.tizi:id/rd_user_yq").click()
    # time.sleep(1)
    # driver.find_element(AppiumBy.ID, "com.ibb.tizi:id/icon_yuanqu_1").click()
    # time.sleep(1)
    # driver.find_element(AppiumBy.ID, "com.ibb.tizi:id/search").click()
    # time.sleep(2)

    # global_car_no = driver.find_element(AppiumBy.ID, "com.ibb.tizi:id/car_list").text
    # print(f"✅ 当前作业车牌号: {global_car_no}")
    global_car_no = enter_sales_page()

    if not global_car_no:
        raise SalesEntryError("销售单入口未返回当前作业车牌号")

    sales_processed = set()
    last_page_source = ""
    sales_cutoff_date = SALES_START_DATE_VALUE
    sales_end_date = SALES_END_DATE_VALUE

    while True:
        cards = driver.find_elements(AppiumBy.XPATH, '//*[@resource-id="com.ibb.tizi:id/rv"]/*')
        processed_in_loop = False
        time_exceeded = False

        for card in cards:
            num_els = card.find_elements(AppiumBy.ID, "com.ibb.tizi:id/item_way_history_num")
            if not num_els:
                continue

            order_no = num_els[0].text
            if order_no in sales_processed:
                continue

            export_time_str = read_first_nonempty_child_text(
                card,
                "com.ibb.tizi:id/item_way_history_expected_export_time",
                retries=4,
                interval=0.25
            )

            # 预计出场时间仅用于时间范围和“当天无附件”判断，
            # 不再作为“有附件销售单是否写入 Excel”的前置条件。
            dt = None
            time_is_valid = False

            if not export_time_str:
                # 同一卡片内部已做短轮询；仍为空时直接检查当前运单附件。
                # 不再滑动后重新抓取 cards，避免目标运单离开视野后串到下一条。
                print(
                    f"  ⚠️ 单号 {order_no} 预计出场时间为空，"
                    "继续检查该运单磅单附件；如有附件仍下载并写入 Excel。"
                )
            else:
                try:
                    dt = datetime.datetime.strptime(export_time_str, "%Y-%m-%d %H:%M:%S")
                    time_is_valid = True
                    print(f"  [时间校验] 单号 {order_no} 预计出场时间: {dt}")

                    if dt.date() > sales_end_date:
                        print(
                            f'  ⏭️ 单号 {order_no} 晚于销售结束日期 '
                            f'{sales_end_date.isoformat()}，跳过当前卡片。'
                        )
                        sales_processed.add(order_no)
                        processed_in_loop = True
                        continue

                    if dt.date() < sales_cutoff_date:
                        print(
                            f"  🛑 单号 {order_no} 早于销售开始日期 "
                            f"(最早有效日期: {sales_cutoff_date.strftime('%Y-%m-%d')})，销售单遍历终止！"
                        )
                        time_exceeded = True
                        break
                except Exception as e:
                    print(
                        f"  ⚠️ 时间解析失败: '{export_time_str}'，"
                        f"继续进入详情检查磅单附件；如有附件仍写入 Excel。错误: {e}"
                    )

            is_today = bool(time_is_valid and dt.date() == ANCHOR_DATE_VALUE)

            btn_els = card.find_elements(AppiumBy.ID, "com.ibb.tizi:id/btn_view_pounds")
            if not btn_els:
                print(f"  [-] 单号 {order_no} 显示不全，等滑动...")
                continue

            def get_card_text(id_str):
                els = card.find_elements(AppiumBy.ID, id_str)
                return "\n".join([el.text for el in els]) if els else "无"

            # 先采集基础字段，但暂时不写 Excel
            register_pdf_enumeration_pending(PDF_SALES_DIR, order_no, global_car_no)
            raw_address = get_card_text("com.ibb.tizi:id/item_way_history_company_name")
            goods_name = get_card_text("com.ibb.tizi:id/item_way_history_goods")
            load_net_weight = get_card_text("com.ibb.tizi:id/item_way_history_load_net_weight")

            print(f"  -> 进入详情检查电子磅单附件...")
            btn_els[0].click()
            time.sleep(3)

            total_pics = 0
            pic_links = []

            try:
                pic_links = driver.find_elements(AppiumBy.ID, "com.ibb.tizi:id/tv_pounds_pic")
                total_pics = len(pic_links)
                print(f"  ✅ 该单发现 {total_pics} 个磅单附件！")
            except Exception as e:
                if isinstance(e, (AppiumSessionOwnershipError, wireless_link.ConnectionUnavailable)):
                    raise
                print(f"  ⚠️ 详情页磅单附件统计异常: {e}")
                raise PdfDownloadIncompleteError("pdf_attachment_enumeration_failed") from e

            register_expected_pdf_slots(PDF_SALES_DIR, order_no, global_car_no, total_pics, indexed=True)

            # 时间无效且没有附件时，仍保持原先“不写入”的处理。
            # 但只要发现附件，就必须继续下载并写入 Excel。
            if not time_is_valid and total_pics == 0:
                print(
                    f"  ⏭️ 单号 {order_no} 预计出场时间无效且无磅单附件，"
                    "按原逻辑跳过该单，不写入 Excel。"
                )
                print("  <- 返回列表...")
                click_back_to_list(timeout=10)
                sales_processed.add(order_no)
                processed_in_loop = True
                break

            # 规则2-3：当天单据 + 0 个磅单附件，跳过，不写 Excel
            if is_today and total_pics == 0:
                print(f"  ⏭️ 单号 {order_no} 是当天单据且无磅单附件，跳过该单，不写入 Excel。")

                print("  <- 返回列表...")
                click_back_to_list(timeout=10)

                sales_processed.add(order_no)
                processed_in_loop = True
                break

            # 到这里说明该单需要写入 Excel，这时再调用 AI
            print(f"  [AI输入地址原文] {raw_address!r}")
            print(f"  [AI] 正在分析复杂地址...")
            clean_addr = extract_address_with_llm(raw_address)
            print(f"  [AI输出地址] {clean_addr!r}")

            info = {
                "单号": order_no,
                "车牌号": global_car_no,
                "货物名称": goods_name,
                "卸货地": clean_addr,
                # 销售表“装货日期”暂时留空，预计出场时间仅用于内部判断。
                "装货日期": "",
                "装货重量": load_net_weight,
                "备注2": ""
            }

            # 规则3：不是当天 + 0 个磅单附件，正常写入，但备注2写“缺失磅单信息”
            if total_pics == 0:
                info["备注2"] = "缺失磅单信息"

                print(f"\n✅ 销售单提取成功 [{order_no}]，但缺失磅单 -> 写入 Excel 并写备注2")
                target_row = write_data_to_excel(
                    info,
                    "销售单-河北众沃",
                    is_sales=True,
                    source_kind="login_account",
                    source_value=phone,
                )
                persist_sales_freight_target(
                    sheet_name="销售单-河北众沃",
                    source_kind="login_account",
                    source_value=phone,
                    waybill=order_no,
                    car=global_car_no,
                    app_export_time=export_time_str,
                    excel_row=target_row,
                )

                print("  <- 返回列表...")
                click_back_to_list(timeout=10)

                sales_processed.add(order_no)
                processed_in_loop = True
                break

            # 有磅单附件：正常下载
            download_success_count = 0

            print(f"  -> 开始下载 {total_pics} 个电子磅单附件...")
            for idx in range(total_pics):
                refreshed_links = driver.find_elements(AppiumBy.ID, "com.ibb.tizi:id/tv_pounds_pic")
                if idx < len(refreshed_links):
                    print(f"    [下载器] 正在处理第 {idx + 1}/{total_pics} 个附件...")
                    ok = execute_single_download(
                        driver,
                        wait,
                        order_no,
                        global_car_no,
                        refreshed_links[idx],
                        PDF_SALES_DIR,
                        file_index=str(idx + 1),
                        source_kind="login_account",
                        source_value=phone,
                        sheet_name="销售单-河北众沃",
                    )
                    if ok:
                        download_success_count += 1

            require_pdf_downloads_complete(total_pics, download_success_count)
            complete_expected_pdf_slots(PDF_SALES_DIR, order_no, global_car_no)

            print(f"\n✅ 销售单提取成功 [{order_no}] -> 写入 Excel")
            target_row = write_data_to_excel(
                info,
                "销售单-河北众沃",
                is_sales=True,
                source_kind="login_account",
                source_value=phone,
            )
            persist_sales_freight_target(
                sheet_name="销售单-河北众沃",
                source_kind="login_account",
                source_value=phone,
                waybill=order_no,
                car=global_car_no,
                app_export_time=export_time_str,
                excel_row=target_row,
            )

            print("  <- 返回列表...")
            click_back_to_list(timeout=10)

            sales_processed.add(order_no)
            processed_in_loop = True
            break


        if time_exceeded:
            break

        if processed_in_loop:
            print("\n  [加速滚动] 大跨步上移...")
            swipe_with_brake(driver, 1800, 700)
        else:
            if wireless_link.is_enabled() and _ui_pause_if_external("sales_page_end"):
                last_page_source = ""
                continue
            current_page_source = driver.page_source
            if wireless_link.is_enabled() and _ui_pause_if_external("sales_page_end"):
                last_page_source = ""
                continue
            if current_page_source == last_page_source:
                print("\n✅销售单处理完毕！")
                break
            last_page_source = current_page_source if wireless_link.is_enabled() else driver.page_source
            print("\n  [微调滚动] 卡片半遮挡，小碎步对齐...")
            swipe_with_brake(driver, 1800, 1300)

    freight_total = run_hebei_freight_stage_with_recovery(phone, global_car_no)
    print(
        f"✅ 账号 {phone} 完整流程执行完成，"
        f"运费规格采集 {freight_total} 条。"
    )

MAX_ACCOUNT_RECOVERY_RETRIES = 2


def should_retry_account_after_error(exc, account_attempt):
    """账号级最多三次；模块内部已耗尽的恢复不得再嵌套放大。"""
    return (
        account_attempt <= MAX_ACCOUNT_RECOVERY_RETRIES
        and not isinstance(exc, StageRecoveryExhaustedError)
        and is_account_relogin_error(exc)
    )


_account_failures = []
_account_results = []
_freight_critical_failures = []
_sales_critical_failures = []
try:
    print("应用启动，开始执行多账号循环流程...", flush=True)

    for idx, phone in enumerate(ACCOUNT_LIST, start=1):
        print(f"\n\n########## 第 {idx}/{len(ACCOUNT_LIST)} 个账号：{phone} ##########", flush=True)

        for account_attempt in range(1, MAX_ACCOUNT_RECOVERY_RETRIES + 2):
            try:
                # 长流程不再让 11 个账号共用同一条 instrumentation。
                # 每个账号以及会话崩溃/登录入口失败后的重试都从一条实测新会话开始。
                recreate_appium_session(
                    f'账号 {phone} 第 {account_attempt} 次执行前刷新'
                )

                if phone == SHANGHAI_SPECIAL_ACCOUNT:
                    run_shanghai_special_flow(phone)
                else:
                    run_full_flow_for_account(phone)
                _account_results.append({
                    'account': phone,
                    'status': 'SUCCESS',
                    'stage': '完整流程',
                    'attempts': account_attempt,
                })
                break
            except Exception as e:
                if isinstance(e, (AppiumSessionOwnershipError, wireless_link.ConnectionUnavailable)):
                    raise
                session_lost = is_appium_session_lost(e)
                login_failed = isinstance(e, AccountLoginError)
                sales_entry_failed = isinstance(e, SalesEntryError)
                can_retry = should_retry_account_after_error(e, account_attempt)
                if can_retry:
                    recovery_reason = (
                        'UiAutomator2 会话已崩溃'
                        if session_lost
                        else (
                            '销售单入口页面未就绪'
                            if sales_entry_failed
                            else (
                                '登录或业务入口页面未就绪'
                                if login_failed
                                else '采购、销售或运费页面操作连续未就绪'
                            )
                        )
                    )
                    print(
                        f'⚠️ 账号 {phone} 的{recovery_reason}：{e}',
                        flush=True,
                    )
                    print(
                        f'  [账号恢复] 将重建 Appium 会话、重新登录，并从该账号开头重跑；'
                        f'Excel 与已归档 PDF 会自动去重。',
                        flush=True,
                    )
                    continue

                _account_failures.append(f"{phone}: {e}")
                if isinstance(e, FreightTechnicalError):
                    _freight_critical_failures.append(f"{phone}: {e}")
                if isinstance(e, (SalesEntryError, ShanghaiSalesTechnicalError)):
                    _sales_critical_failures.append(f"{phone}: {e}")
                if isinstance(e, FreightTechnicalError):
                    failure_stage = '运费确认'
                elif isinstance(e, (SalesEntryError, ShanghaiSalesTechnicalError)):
                    failure_stage = '销售单'
                elif login_failed:
                    failure_stage = '登录或业务入口'
                elif session_lost:
                    failure_stage = 'Appium 会话'
                else:
                    failure_stage = '账号完整流程'
                _account_results.append({
                    'account': phone,
                    'status': 'FAILED',
                    'stage': failure_stage,
                    'attempts': account_attempt,
                })
                if isinstance(
                    e,
                    (FreightTechnicalError, SalesEntryError, ShanghaiSalesTechnicalError),
                ):
                    print(
                        f"❌ 账号 {phone} 关键阶段连续恢复失败；"
                        "继续采集后续账号，但整条流水线最终返回失败并禁止平台上传。"
                        f"错误: {e}",
                        flush=True,
                    )
                else:
                    print(
                        f"❌ 账号 {phone} 执行失败，进入下一个账号。错误: {e}",
                        flush=True,
                    )
                break

    assert_all_pdf_batches_complete((PDF_PURCHASE_DIR, PDF_SALES_DIR, PDF_PURCHASE_SHANGHAI_DIR, PDF_SALES_SHANGHAI_DIR))
    if _account_failures:
        print("STATUS:YYB_DONE_WITH_WARNINGS|" + " | ".join(_account_failures), flush=True)
    else:
        print("STATUS:YYB_DONE", flush=True)
    print("\n🎉 所有账号已全部循环完成！", flush=True)

finally:
    _session_release_confirmed = False
    try:
        if driver is not None and not _session_cleanup_uncertain:
            if wireless_link.is_enabled():
                _close_owned_appium_session(driver)
            else:
                driver.quit()
        _session_release_confirmed = not _session_creation_outcome_uncertain and not _session_cleanup_uncertain
    finally:
        print(
            "🚀 所有自动化任务已结束，Appium 会话已释放。" if _session_release_confirmed
            else "STATUS:YYB_SESSION_RELEASE_UNCONFIRMED|未创建替代会话，需核实本次归属会话。",
            flush=True,
        )

print(
    'STATUS:YYB_ACCOUNT_SUMMARY|'
    + json.dumps(_account_results, ensure_ascii=False, separators=(',', ':')),
    flush=True,
)

if _freight_critical_failures:
    print(
        "FATAL_ERROR:运费确认技术阶段未完整完成|"
        + " | ".join(_freight_critical_failures),
        flush=True,
    )
if _sales_critical_failures:
    print(
        "FATAL_ERROR:销售单技术阶段未完整完成|"
        + " | ".join(_sales_critical_failures),
        flush=True,
    )
if _account_failures:
    raise SystemExit(1)
