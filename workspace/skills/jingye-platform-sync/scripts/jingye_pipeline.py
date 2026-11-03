#!/usr/bin/env python3
# coding: utf-8
'''One-shot Jingye crawl, durable import, attachment and platform-sync driver.'''

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import json
import os
import re
import smtplib
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, TextIO

from jingye_store import (
    DEFAULT_BASE_DIR,
    DEFAULT_SALES_LOADING_POINT_NAME,
    connect_db,
    export_pending_freight_seed,
    iter_candidates,
    now_text,
    status_summary,
)
from jingye_sync import load_env_file, single_instance_lock


SCRIPT_DIR = Path(__file__).resolve().parent
PLATFORM_SKILL_DIR = SCRIPT_DIR.parent
RECONCILIATION_SCRIPT_DIR = PLATFORM_SKILL_DIR.parent / 'jingye-reconciliation' / 'scripts'
CREATE_EXCEL_SCRIPT = RECONCILIATION_SCRIPT_DIR / 'creat_excel.py'
CRAWLER_SCRIPT = RECONCILIATION_SCRIPT_DIR / 'yyb_search.py'
SYNC_SCRIPT = SCRIPT_DIR / 'jingye_sync.py'
SHANGHAI_TZ = dt.timezone(dt.timedelta(hours=8))


def parse_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'日期必须为 YYYY-MM-DD：{value}') from exc


def shanghai_today() -> dt.date:
    return dt.datetime.now(SHANGHAI_TZ).date()


@dataclass(frozen=True)
class Windows:
    mode: str
    anchor: dt.date
    purchase_start: dt.date
    purchase_end: dt.date
    sales_start: dt.date
    sales_end: dt.date

    def as_dict(self) -> dict[str, str]:
        return {
            'mode': self.mode,
            'anchor_date': self.anchor.isoformat(),
            'purchase_start': self.purchase_start.isoformat(),
            'purchase_end': self.purchase_end.isoformat(),
            'sales_start': self.sales_start.isoformat(),
            'sales_end': self.sales_end.isoformat(),
        }


@dataclass
class StepResult:
    name: str
    command: list[str]
    started_at: str
    finished_at: str
    elapsed_seconds: float
    return_code: int
    output_tail: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            'name': self.name,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'elapsed_seconds': round(self.elapsed_seconds, 3),
            'return_code': self.return_code,
            'output_tail': self.output_tail,
        }


class TeeStream:
    '''Mirror text to the durable task log while preserving console output.'''

    def __init__(self, console: TextIO, log_stream: TextIO) -> None:
        self.console = console
        self.log_stream = log_stream

    @property
    def encoding(self) -> str:
        return str(getattr(self.console, 'encoding', None) or 'utf-8')

    def write(self, value: str) -> int:
        self.log_stream.write(value)
        self.log_stream.flush()
        try:
            self.console.write(value)
            self.console.flush()
        except (OSError, ValueError):
            pass
        return len(value)

    def flush(self) -> None:
        self.log_stream.flush()
        try:
            self.console.flush()
        except (OSError, ValueError):
            pass

    def isatty(self) -> bool:
        return False


@contextlib.contextmanager
def tee_task_output(log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8', buffering=1) as log_stream:
        with contextlib.redirect_stdout(TeeStream(sys.stdout, log_stream)):
            with contextlib.redirect_stderr(TeeStream(sys.stderr, log_stream)):
                yield


def task_log_path(runtime_dir: Path, run_id: str) -> Path:
    return runtime_dir / 'logs' / f'jingye_pipeline_{run_id}.log'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='完整触发敬业数据爬取、本地去重、附件处理和平台同步'
    )
    parser.add_argument('--base-dir', type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--profile', choices=('production', 'test'), default='production')
    parser.add_argument('--anchor-date', type=parse_date, default=None)
    parser.add_argument('--project-name', default='')
    parser.add_argument('--email-to', default='')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    subparsers = parser.add_subparsers(dest='command', required=True)

    subparsers.add_parser('sync', help='采购和销售均抓取锚点日前两天至锚点日')
    backfill = subparsers.add_parser('backfill', help='人工补抓指定采购和销售日期')
    backfill.add_argument('--purchase-start', type=parse_date, required=True)
    backfill.add_argument('--purchase-end', type=parse_date, required=True)
    sales_group = backfill.add_mutually_exclusive_group(required=True)
    sales_group.add_argument('--sales-days', type=int)
    sales_group.add_argument('--sales-start', type=parse_date)
    backfill.add_argument('--sales-end', type=parse_date)
    return parser.parse_args()


def resolve_windows(args: argparse.Namespace) -> Windows:
    anchor = args.anchor_date or shanghai_today()
    if args.command == 'sync':
        start = anchor - dt.timedelta(days=2)
        return Windows('AUTO', anchor, start, anchor, start, anchor)
    if args.purchase_start > args.purchase_end:
        raise ValueError('采购开始日期不能晚于结束日期')
    if args.sales_days is not None:
        if args.sales_days < 1:
            raise ValueError('--sales-days 必须大于等于 1')
        if args.sales_end is not None:
            raise ValueError('--sales-days 不能和 --sales-end 同时使用')
        sales_end = anchor
        sales_start = anchor - dt.timedelta(days=args.sales_days - 1)
    else:
        if args.sales_end is None:
            raise ValueError('使用 --sales-start 时必须同时提供 --sales-end')
        sales_start = args.sales_start
        sales_end = args.sales_end
    if sales_start > sales_end:
        raise ValueError('销售开始日期不能晚于结束日期')
    return Windows(
        'BACKFILL', anchor, args.purchase_start, args.purchase_end,
        sales_start, sales_end,
    )


def effective_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    base_dir = args.base_dir.resolve()
    runtime_dir = base_dir / 'platform_sync'
    if args.db:
        db_path = args.db.resolve()
    elif args.profile == 'test':
        db_path = runtime_dir / os.getenv('JINGYE_TEST_DB_NAME', 'jingye_facts.test.sqlite3')
    else:
        db_path = runtime_dir / 'jingye_facts.sqlite3'
    return base_dir, runtime_dir, db_path


def resolve_project_name(args: argparse.Namespace) -> str:
    if args.project_name.strip():
        return args.project_name.strip()
    if args.profile == 'test':
        return os.getenv('JINGYE_TEST_PROJECT_NAME', '612演示项目').strip()
    return os.getenv('JINGYE_PROJECT_NAME', '敬业钢铁').strip()


def new_run_id(mode: str) -> str:
    timestamp = dt.datetime.now(SHANGHAI_TZ).strftime('%Y%m%dT%H%M%S%f')[:-3]
    return f'{timestamp}-{mode.lower()}-{uuid.uuid4().hex[:8]}'


def excel_path_for(base_dir: Path, windows: Windows, run_id: str) -> Path:
    name = (
        f'远行_敬业运输明细_{windows.purchase_start.isoformat()}_至_'
        f'{windows.purchase_end.isoformat()}_{run_id}.xlsx'
    )
    return base_dir / 'excel_ori' / name


def python_command(script: Path, *arguments: str) -> list[str]:
    return [sys.executable, '-X', 'utf8', str(script), *arguments]


def build_commands(
    args: argparse.Namespace,
    windows: Windows,
    run_id: str,
    base_dir: Path,
    db_path: Path,
    project_name: str,
) -> list[tuple[str, list[str]]]:
    excel_path = excel_path_for(base_dir, windows, run_id)
    common_sync = [
        '--base-dir', str(base_dir), '--db', str(db_path), '--profile', args.profile,
    ]
    if args.verbose:
        common_sync.append('--verbose')
    commands = [
        (
            'create_excel',
            python_command(
                CREATE_EXCEL_SCRIPT,
                '--start', windows.purchase_start.isoformat(),
                '--end', windows.purchase_end.isoformat(),
                '--base-dir', str(base_dir),
                '--output-path', str(excel_path),
            ),
        ),
        (
            'crawl',
            python_command(
                CRAWLER_SCRIPT,
                '--anchor-date', windows.anchor.isoformat(),
                '--purchase-start', windows.purchase_start.isoformat(),
                '--purchase-end', windows.purchase_end.isoformat(),
                '--sales-start', windows.sales_start.isoformat(),
                '--sales-end', windows.sales_end.isoformat(),
                '--base-dir', str(base_dir),
                '--excel-path', str(excel_path),
                '--run-id', run_id,
            ),
        ),
        ('refresh', python_command(SYNC_SCRIPT, *common_sync, 'refresh')),
        (
            'attachments',
            python_command(SYNC_SCRIPT, *common_sync, 'attachments', '--format', 'jpg'),
        ),
        (
            'oss_upload',
            python_command(SYNC_SCRIPT, *common_sync, 'oss-upload', '--execute'),
        ),
        (
            'validate_upload',
            python_command(
                SYNC_SCRIPT, *common_sync, 'dry-run',
                '--project-name', project_name, '--require-attachment',
            ),
        ),
        (
            'platform_upload',
            python_command(
                SYNC_SCRIPT, *common_sync, 'upload',
                '--project-name', project_name,
                '--confirm-project', project_name,
                '--require-attachment', '--allow-batch', '--execute',
            ),
        ),
    ]
    return commands


def run_step(name: str, command: list[str]) -> StepResult:
    print(f'STATUS:PIPELINE_STEP_START|NAME:{name}', flush=True)
    started_at = now_text()
    started = time.monotonic()
    environment = os.environ.copy()
    environment.setdefault('PYTHONUTF8', '1')
    process = subprocess.Popen(
        command,
        cwd=str(SCRIPT_DIR.parent.parent.parent),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    tail: list[str] = []
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip('\r\n')
        print(f'[{name}] {line}', flush=True)
        tail.append(line)
        if len(tail) > 80:
            del tail[0]
    return_code = process.wait()
    result = StepResult(
        name=name,
        command=command,
        started_at=started_at,
        finished_at=now_text(),
        elapsed_seconds=time.monotonic() - started,
        return_code=return_code,
        output_tail=tail,
    )
    print(
        f'STATUS:PIPELINE_STEP_DONE|NAME:{name}|RETURN_CODE:{return_code}',
        flush=True,
    )
    return result


def seed_pending_freight(
    db_path: Path, base_dir: Path, run_id: str, anchor_date: dt.date
) -> StepResult:
    name = 'seed_freight'
    started_at = now_text()
    started = time.monotonic()
    output_path = base_dir / 'freight_confirmation' / f'{run_id}.jsonl'
    print(f'STATUS:PIPELINE_STEP_START|NAME:{name}', flush=True)
    connection = connect_db(db_path)
    try:
        stats = export_pending_freight_seed(
            connection,
            output_path,
            run_id,
            anchor_date=anchor_date,
        )
    finally:
        connection.close()
    output_line = json.dumps(stats, ensure_ascii=False, sort_keys=True)
    print(f'[{name}] {output_line}', flush=True)
    result = StepResult(
        name=name,
        command=['internal', 'export_pending_freight_seed', str(output_path)],
        started_at=started_at,
        finished_at=now_text(),
        elapsed_seconds=time.monotonic() - started,
        return_code=0,
        output_tail=[output_line],
    )
    print(f'STATUS:PIPELINE_STEP_DONE|NAME:{name}|RETURN_CODE:0', flush=True)
    return result


def record_run_start(
    db_path: Path, run_id: str, windows: Windows
) -> None:
    connection = connect_db(db_path)
    try:
        values = windows.as_dict()
        connection.execute(
            '''
            INSERT INTO crawl_runs(
                run_id, mode, anchor_date, purchase_start, purchase_end,
                sales_start, sales_end, started_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING')
            ''',
            (
                run_id, values['mode'], values['anchor_date'],
                values['purchase_start'], values['purchase_end'],
                values['sales_start'], values['sales_end'], now_text(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def record_run_finish(
    db_path: Path,
    run_id: str,
    status: str,
    summary: dict[str, Any],
    error_message: str,
) -> None:
    connection = connect_db(db_path)
    try:
        connection.execute(
            '''
            UPDATE crawl_runs
            SET finished_at = ?, status = ?, summary_json = ?, error_message = ?
            WHERE run_id = ?
            ''',
            (
                now_text(), status,
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                error_message, run_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def update_crawl_scopes(db_path: Path, run_id: str, windows: Windows) -> None:
    connection = connect_db(db_path)
    try:
        rows = connection.execute(
            '''
            SELECT sheet_name, source_kind, source_value,
                   COUNT(DISTINCT waybill_no || X'1F' || plate_no) AS records_seen
            FROM source_context_events
            WHERE run_id = ? AND waybill_no != '' AND LOWER(event_name) = 'row'
            GROUP BY sheet_name, source_kind, source_value
            ''',
            (run_id,),
        ).fetchall()
        for row in rows:
            direction = 'PURCHASE' if str(row['sheet_name']).startswith('采购单-') else 'SALES'
            window_start = (
                windows.purchase_start if direction == 'PURCHASE' else windows.sales_start
            )
            window_end = windows.purchase_end if direction == 'PURCHASE' else windows.sales_end
            connection.execute(
                '''
                INSERT INTO crawl_scopes(
                    run_id, sheet_name, source_kind, source_value, direction,
                    window_start, window_end, records_seen, status, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'SUCCESS', ?)
                ON CONFLICT(run_id, sheet_name, source_kind, source_value, direction)
                DO UPDATE SET
                    records_seen = excluded.records_seen,
                    status = excluded.status,
                    finished_at = excluded.finished_at
                ''',
                (
                    run_id, row['sheet_name'], row['source_kind'], row['source_value'],
                    direction, window_start.isoformat(), window_end.isoformat(),
                    int(row['records_seen']), now_text(),
                ),
            )
        freight_rows = connection.execute(
            '''
            SELECT sheet_name, source_kind, source_value, target_count, recorded_at
            FROM freight_scope_runs WHERE run_id = ?
            ''',
            (run_id,),
        ).fetchall()
        for row in freight_rows:
            connection.execute(
                '''
                INSERT INTO crawl_scopes(
                    run_id, sheet_name, source_kind, source_value, direction,
                    window_start, window_end, records_seen, status, finished_at
                ) VALUES (?, ?, ?, ?, 'FREIGHT', ?, ?, ?, 'SUCCESS', ?)
                ON CONFLICT(run_id, sheet_name, source_kind, source_value, direction)
                DO UPDATE SET
                    records_seen = excluded.records_seen,
                    status = excluded.status,
                    finished_at = excluded.finished_at
                ''',
                (
                    run_id, row['sheet_name'], row['source_kind'], row['source_value'],
                    windows.sales_start.isoformat(), windows.sales_end.isoformat(),
                    int(row['target_count']), row['recorded_at'],
                ),
            )
        connection.commit()
    finally:
        connection.close()


def read_status(db_path: Path) -> dict[str, Any]:
    connection = connect_db(db_path)
    try:
        return status_summary(connection)
    finally:
        connection.close()


def read_upload_issue_details(
    db_path: Path, project_name: str
) -> list[dict[str, str]]:
    '''Return the exact currently rejected candidates for the human email report.'''
    connection = connect_db(db_path)
    try:
        plate_by_waybill_id = {
            int(row['id']): str(row['plate_no'] or '').strip()
            for row in connection.execute('SELECT id, plate_no FROM waybills')
        }
        issues: list[dict[str, str]] = []
        for candidate in iter_candidates(
            connection,
            os.getenv('JINGYE_SOURCE_SYSTEM', 'agent').strip() or 'agent',
            project_name,
            sales_loading_point_name=(
                os.getenv(
                    'JINGYE_SALES_LOADING_POINT_NAME',
                    DEFAULT_SALES_LOADING_POINT_NAME,
                ).strip()
                or DEFAULT_SALES_LOADING_POINT_NAME
            ),
            require_attachment=True,
        ):
            if candidate.payload is not None:
                continue
            issues.append(
                {
                    'direction': candidate.direction,
                    'waybill_no': candidate.waybill_no,
                    'plate_no': plate_by_waybill_id.get(candidate.waybill_id, ''),
                    'reason': candidate.rejection_reason or '资料未通过校验',
                }
            )
        return issues
    finally:
        connection.close()


def load_reconciliation_email_defaults() -> dict[str, Any]:
    module_path = RECONCILIATION_SCRIPT_DIR / 'result_check.py'
    module_name = '_jingye_result_check_email_defaults'
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError('无法加载现有对账邮件配置')
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    original_path = list(sys.path)
    try:
        sys.path.insert(0, str(RECONCILIATION_SCRIPT_DIR))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
    return {
        'server': module.SMTP_SERVER,
        'port': int(module.SMTP_PORT),
        'auth_code': module.SMTP_AUTH_CODE,
        'sender': module.SMTP_SENDER,
        'receiver': module.SMTP_RECEIVER,
    }


def email_settings(email_to: str) -> dict[str, Any]:
    values = {
        'server': os.getenv('SMTP_SERVER', '').strip(),
        'port': int(os.getenv('SMTP_PORT', '465')),
        'auth_code': os.getenv('SMTP_AUTH_CODE', '').strip(),
        'sender': os.getenv('SMTP_SENDER', '').strip(),
        'receiver': (
            email_to
            or os.getenv('JINGYE_PLATFORM_EMAIL_TO', '')
            or os.getenv('SMTP_RECEIVER', '')
        ).strip(),
    }
    if not all(values[key] for key in ('server', 'auth_code', 'sender', 'receiver')):
        defaults = load_reconciliation_email_defaults()
        for key in ('server', 'auth_code', 'sender', 'receiver'):
            if not values[key]:
                values[key] = defaults[key]
        if not os.getenv('SMTP_PORT'):
            values['port'] = defaults['port']
    if email_to.strip():
        values['receiver'] = email_to.strip()
    return values


def parse_recipients(value: str) -> list[str]:
    recipients: list[str] = []
    for item in re.split(r'[,;，；\s]+', value.strip()):
        address = item.strip()
        if not address:
            continue
        if '@' not in address:
            raise ValueError(f'无效邮件地址：{address}')
        if address not in recipients:
            recipients.append(address)
    if not recipients:
        raise ValueError('邮件收件人不能为空')
    return recipients


def extract_crawl_account_results(summary: dict[str, Any]) -> list[dict[str, Any]]:
    '''读取爬虫结构化账号结果，仅返回可用于邮件的脱敏字段。'''
    prefix = 'STATUS:YYB_ACCOUNT_SUMMARY|'
    allowed_stages = {
        '完整流程', '销售单', '运费确认', '登录或业务入口',
        'Appium 会话', '账号完整流程',
    }

    for step in summary.get('steps', []):
        if step.get('name') != 'crawl':
            continue
        for raw_line in reversed(step.get('output_tail') or []):
            line = str(raw_line or '').strip()
            if not line.startswith(prefix):
                continue
            try:
                raw_results = json.loads(line[len(prefix):])
            except json.JSONDecodeError:
                return []
            if not isinstance(raw_results, list):
                return []

            results: list[dict[str, Any]] = []
            for item in raw_results:
                if not isinstance(item, dict):
                    continue
                account = str(item.get('account') or '').strip()
                if not re.fullmatch(r'1\d{10}', account):
                    masked_account = '未知账号'
                else:
                    masked_account = f'*******{account[-4:]}'
                status = str(item.get('status') or '').upper()
                if status not in {'SUCCESS', 'FAILED'}:
                    continue
                stage = str(item.get('stage') or '').strip()
                if stage not in allowed_stages:
                    stage = '账号完整流程'
                try:
                    attempts = max(1, int(item.get('attempts') or 1))
                except (TypeError, ValueError):
                    attempts = 1
                results.append({
                    'account': masked_account,
                    'status': status,
                    'stage': stage,
                    'attempts': attempts,
                })
            return results
    return []


def pipeline_status_label(status: Any) -> str:
    return {
        'SUCCESS': '全部完成',
        'PARTIAL': '部分完成',
        'FAILED': '执行失败',
        'RUNNING': '正在执行',
    }.get(str(status or '').upper(), str(status or '状态未知'))


def build_email_body(summary: dict[str, Any]) -> str:
    windows = summary['windows']
    before = summary.get('status_before') or {}
    after = summary.get('status_after') or {}
    status = str(summary.get('status') or '').upper()
    status_label = pipeline_status_label(status)
    mode_label = {
        'AUTO': '自动定时同步',
        'BACKFILL': '人工补录',
    }.get(str(windows.get('mode') or '').upper(), str(windows.get('mode') or '未知'))
    status_explanation = {
        'SUCCESS': '本次采集、入库和平台处理均已完成。',
        'PARTIAL': '资料完整的运单已正常处理；仍有待完善运单保留在数据库中，本轮没有上传这些不完整运单。',
        'FAILED': '任务未能完整结束；已执行步骤、失败环节和原因见下文。',
    }.get(status, '')
    lines = [
        f'敬业运输数据同步已结束。本次结果：{status_label}。',
        status_explanation,
        '',
        '任务信息：',
        f'- 执行方式：{mode_label}',
        f'- 任务编号：{summary["run_id"]}',
        f'- 采购采集日期：{windows["purchase_start"]} 至 {windows["purchase_end"]}',
        f'- 销售采集日期：{windows["sales_start"]} 至 {windows["sales_end"]}',
        f'- 执行锚点日期：{windows["anchor_date"]}',
        f'- 详细日志：{summary.get("log_path", "")}',
        '',
        '执行过程：',
    ]
    step_labels = {
        'create_excel': '创建本轮采集表',
        'seed_freight': '补入仍需查询的历史运费目标',
        'crawl': '从货运 App 采集采购、销售、PDF 和运费数据',
        'refresh': '把本轮数据刷新到本地事实库',
        'attachments': '生成平台所需附件',
        'oss_upload': '把附件上传到 OSS',
        'validate_upload': '检查平台上传数据是否完整',
        'platform_upload': '向运输平台提交完整运单',
    }
    for step in summary.get('steps', []):
        return_code = int(step.get('return_code', 1))
        outcome = '完成' if return_code == 0 else f'失败（返回码 {return_code}）'
        lines.append(
            f'- {step_labels.get(step.get("name"), step.get("name", "未知步骤"))}：'
            f'{outcome}，用时 {step.get("elapsed_seconds", 0)} 秒。'
        )
    account_results = (
        summary.get('account_results')
        or extract_crawl_account_results(summary)
    )
    if account_results:
        successful_accounts = [
            item for item in account_results if item.get('status') == 'SUCCESS'
        ]
        failed_accounts = [
            item for item in account_results if item.get('status') == 'FAILED'
        ]
        lines.extend(
            [
                '',
                '账号采集结果（手机号已脱敏）：',
                f'- 本次共处理 {len(account_results)} 个账号：'
                f'{len(successful_accounts)} 个完成，{len(failed_accounts)} 个失败。',
            ]
        )
        success_labels = '、'.join(
            str(item.get('account') or '未知账号')
            for item in successful_accounts
        ) or '无'
        lines.append(f'- 完成账号：{success_labels}。')
        if failed_accounts:
            lines.append('- 失败账号：')
            for item in failed_accounts:
                account_label = str(item.get('account') or '未知账号')
                stage_label = str(item.get('stage') or '账号完整流程')
                attempt_count = int(item.get('attempts') or 1)
                lines.append(
                    f'  - {account_label}：在{stage_label}阶段失败，'
                    f'共执行 {attempt_count} 次。'
                )
        else:
            lines.append('- 没有账号执行失败。')

    validation = summary.get('upload_validation') or {}
    if validation:
        selected = int(validation.get('selected', 0))
        accepted = int(validation.get('accepted', 0))
        rejected = int(validation.get('rejected', 0))
        already_successful = int(validation.get('accepted_successful', 0))
        waiting_at_validation = int(validation.get('accepted_without_submission', 0))
        successful_before = int(before.get('successful', 0))
        successful_after = int(after.get('successful', successful_before))
        newly_successful = max(0, successful_after - successful_before)
        lines.extend(
            [
                '',
                '平台校验与上传结果：',
                f'- 共检查 {selected} 条运单，其中 {accepted} 条资料完整，'
                f'{rejected} 条资料仍需完善。',
                f'- 资料完整的运单中，有 {already_successful} 条此前已经上传成功，'
                '本轮没有重复提交。',
                f'- 校验时有 {waiting_at_validation} 条完整运单尚无平台提交记录，'
                '已交给本轮平台上传步骤处理。',
                f'- 本轮新增成功上传 {newly_successful} 条；'
                f'数据库累计记录 {successful_after} 条上传成功。',
            ]
        )
        current_retryable = int(after.get('retryable', 0))
        current_rejected = int(after.get('rejected', 0))
        if current_retryable or current_rejected:
            lines.append(
                f'- 当前另有 {current_retryable} 条平台请求等待重试，'
                f'{current_rejected} 条被平台明确拒绝。'
            )
        else:
            lines.append('- 当前没有平台请求等待重试，也没有新增的平台拒绝记录。')

    issues = summary.get('upload_issues') or []
    if issues:
        lines.extend(
            [
                '',
                f'需要后续完善的运单（共 {len(issues)} 条，本轮未上传）：',
            ]
        )
        direction_labels = {'PURCHASE': '采购单', 'SALES': '销售单'}
        for item in issues:
            direction_label = direction_labels.get(
                str(item.get('direction') or '').upper(), '运单'
            )
            plate_no = str(item.get('plate_no') or '').strip()
            plate_text = f'，车辆 {plate_no}' if plate_no else ''
            lines.append(
                f'- {direction_label} {item.get("waybill_no") or "未知单号"}'
                f'{plate_text}：{item.get("reason") or "资料未通过校验"}。'
            )
        if any(str(item.get('reason') or '') == '未找到PDF' for item in issues):
            lines.append(
                '- 缺少 PDF 的运单数据已经保留。以后人工补录或定时任务取得并'
                '完整解析该 PDF 后，系统会更新同一运单，重新生成并上传附件，'
                '随后把该运单上传平台；不需要删除现有数据库记录。'
            )
    elif validation and int(validation.get('rejected', 0)):
        reasons = validation.get('rejection_reasons') or {}
        reason_text = '、'.join(
            f'{reason} {int(count)} 条' for reason, count in sorted(reasons.items())
        ) or '原因明细读取失败'
        lines.extend(
            [
                '',
                f'需要后续完善的运单：{reason_text}。这些运单本轮没有上传。',
            ]
        )

    if before or after:
        added_waybills = int(after.get('waybills', 0)) - int(before.get('waybills', 0))
        added_purchase = int(after.get('purchase_waybills', 0)) - int(
            before.get('purchase_waybills', 0)
        )
        added_sales = int(after.get('sales_waybills', 0)) - int(
            before.get('sales_waybills', 0)
        )
        added_documents = int(after.get('unique_documents', 0)) - int(
            before.get('unique_documents', 0)
        )
        added_freight_records = int(after.get('freight_records', 0)) - int(
            before.get('freight_records', 0)
        )
        added_artifacts = int(after.get('attachment_artifacts', 0)) - int(
            before.get('attachment_artifacts', 0)
        )
        added_oss_urls = int(after.get('oss_urls', 0)) - int(
            before.get('oss_urls', 0)
        )
        lines.extend(
            [
                '',
                '本地数据结果：',
                f'- 本轮新增 {max(0, added_waybills)} 条唯一运单：'
                f'采购单 {max(0, added_purchase)} 条，销售单 {max(0, added_sales)} 条；'
                f'数据库现有运单共 {int(after.get("waybills", 0))} 条。',
                f'- 本轮新增识别 {max(0, added_documents)} 份 PDF，'
                f'数据库现有 PDF 共 {int(after.get("unique_documents", 0))} 份。',
                f'- 本轮新增生成 {max(0, added_artifacts)} 个平台附件，'
                f'其中 {max(0, added_oss_urls)} 个在本轮取得 OSS 地址。',
                f'- 本轮新增 {max(0, added_freight_records)} 条运费明细；'
                f'目前有 {int(after.get("freight_complete", 0))} 个运费目标已完整，'
                f'{int(after.get("freight_pending", 0))} 个仍会按规则继续查询。',
            ]
        )

    error_message = str(summary.get('error_message') or '').strip()
    if error_message and status == 'FAILED':
        lines.extend(['', f'任务未完成原因：{error_message}'])
    lines.extend(['', '本邮件由敬业数据流水线自动发送，请勿直接回复。'])
    return '\n'.join(lines)


def send_summary_email(summary: dict[str, Any], email_to: str) -> dict[str, Any]:
    settings = email_settings(email_to)
    recipients = parse_recipients(str(settings['receiver']))
    message = EmailMessage()
    message['From'] = f'德创数字AI数据助手 <{settings["sender"]}>'
    message['To'] = ', '.join(recipients)
    message['Subject'] = (
        f'敬业平台同步【{pipeline_status_label(summary["status"])}】 '
        f'{summary["windows"]["anchor_date"]} {summary["run_id"]}'
    )
    message.set_content(build_email_body(summary))
    with smtplib.SMTP_SSL(
        str(settings['server']), int(settings['port']), timeout=30
    ) as smtp:
        smtp.login(str(settings['sender']), str(settings['auth_code']))
        smtp.send_message(
            message, from_addr=str(settings['sender']), to_addrs=recipients
        )
    return {'sent': True, 'recipients': recipients}


def require_success(result: StepResult) -> None:
    if result.return_code != 0:
        raise RuntimeError(
            f'步骤 {result.name} 失败，返回码 {result.return_code}'
        )


def parse_step_json(result: StepResult) -> dict[str, Any]:
    lines = result.output_tail
    for index, line in enumerate(lines):
        if not line.lstrip().startswith('{'):
            continue
        try:
            value = json.loads('\n'.join(lines[index:]))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def execute_pipeline(
    args: argparse.Namespace,
    windows: Windows,
    base_dir: Path,
    runtime_dir: Path,
    db_path: Path,
    project_name: str,
    run_id: str,
    commands: list[tuple[str, list[str]]],
    log_path: Path,
) -> int:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / 'jingye_pipeline.lock'
    with single_instance_lock(lock_path):
        record_run_start(db_path, run_id, windows)
        results: list[StepResult] = []
        error_message = ''
        status = 'RUNNING'
        status_before: dict[str, Any] = {}
        status_after: dict[str, Any] = {}
        email_result: dict[str, Any] = {'sent': False}
        validation_report: dict[str, Any] = {}
        command_map = dict(commands)
        try:
            status_before = read_status(db_path)
            create_result = run_step('create_excel', command_map['create_excel'])
            results.append(create_result)
            require_success(create_result)

            seed_result = seed_pending_freight(
                db_path,
                base_dir,
                run_id,
                windows.anchor,
            )
            results.append(seed_result)

            crawl_result = run_step('crawl', command_map['crawl'])
            results.append(crawl_result)

            refresh_result = run_step('refresh', command_map['refresh'])
            results.append(refresh_result)
            require_success(refresh_result)
            update_crawl_scopes(db_path, run_id, windows)
            require_success(crawl_result)

            for name in (
                'attachments', 'oss_upload', 'validate_upload', 'platform_upload'
            ):
                result = run_step(name, command_map[name])
                results.append(result)
                require_success(result)
                if name == 'validate_upload':
                    validation_report = parse_step_json(result)
            status = 'SUCCESS'
        except BaseException as exc:
            status = 'FAILED'
            error_message = f'{type(exc).__name__}: {exc}'
            if isinstance(exc, KeyboardInterrupt):
                error_message = 'KeyboardInterrupt: 用户中断任务'

        try:
            status_after = read_status(db_path)
        except Exception as exc:
            status = 'FAILED'
            suffix = f'{type(exc).__name__}: {exc}'
            error_message = f'{error_message}; {suffix}'.strip('; ')

        if status == 'SUCCESS':
            local_rejected = int(validation_report.get('rejected', 0))
            new_rejected = int(status_after.get('rejected', 0)) - int(
                status_before.get('rejected', 0)
            )
            new_retryable = int(status_after.get('retryable', 0)) - int(
                status_before.get('retryable', 0)
            )
            if local_rejected > 0 or new_rejected > 0 or new_retryable > 0:
                status = 'PARTIAL'
                error_message = (
                    f'本地待完善候选={local_rejected}，'
                    f'本次新增 rejected={max(0, new_rejected)}，'
                    f'retryable={max(0, new_retryable)}'
                )

        summary: dict[str, Any] = {
            'run_id': run_id,
            'status': status,
            'windows': windows.as_dict(),
            'project_name': project_name,
            'db_path': str(db_path),
            'excel_path': str(excel_path_for(base_dir, windows, run_id)),
            'log_path': str(log_path),
            'steps': [result.as_dict() for result in results],
            'status_before': status_before,
            'status_after': status_after,
            'upload_validation': validation_report,
            'error_message': error_message,
        }
        if validation_report:
            try:
                summary['upload_issues'] = read_upload_issue_details(
                    db_path, project_name
                )
            except Exception as exc:
                summary['upload_issues'] = []
                summary['upload_issue_detail_error'] = (
                    f'{type(exc).__name__}: {exc}'
                )
        summary['account_results'] = extract_crawl_account_results(summary)
        try:
            email_result = send_summary_email(summary, args.email_to)
            print(
                f'STATUS:PIPELINE_EMAIL_SENT|TO:{",".join(email_result["recipients"])}',
                flush=True,
            )
        except Exception as exc:
            email_error = f'{type(exc).__name__}: {exc}'
            email_result = {'sent': False, 'error': email_error}
            status = 'FAILED'
            error_message = f'{error_message}; 邮件发送失败：{email_error}'.strip('; ')
        summary['status'] = status
        summary['error_message'] = error_message
        summary['email'] = email_result
        record_run_finish(db_path, run_id, status, summary, error_message)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        print(f'STATUS:PIPELINE_DONE|RUN_ID:{run_id}|STATUS:{status}', flush=True)
        if status == 'SUCCESS':
            return 0
        if status == 'PARTIAL':
            return 2
        return 1


def main() -> int:
    load_env_file()
    args = parse_args()
    windows = resolve_windows(args)
    base_dir, runtime_dir, db_path = effective_paths(args)
    project_name = resolve_project_name(args)
    run_id = new_run_id(windows.mode)
    commands = build_commands(
        args, windows, run_id, base_dir, db_path, project_name
    )
    for required in (CREATE_EXCEL_SCRIPT, CRAWLER_SCRIPT, SYNC_SCRIPT):
        if not required.is_file():
            raise FileNotFoundError(f'缺少流水线脚本：{required}')

    if not args.execute:
        print(
            json.dumps(
                {
                    'dry_run': True,
                    'external_writes': 0,
                    'run_id': run_id,
                    'windows': windows.as_dict(),
                    'project_name': project_name,
                    'db_path': str(db_path),
                    'excel_path': str(excel_path_for(base_dir, windows, run_id)),
                    'steps': (
                        [{'name': commands[0][0], 'command': commands[0][1]}]
                        + [{
                            'name': 'seed_freight',
                            'command': ['internal', 'export_pending_freight_seed'],
                        }]
                        + [
                            {'name': name, 'command': command}
                            for name, command in commands[1:]
                        ]
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    log_path = task_log_path(runtime_dir, run_id)
    with tee_task_output(log_path):
        print(
            f'STATUS:PIPELINE_LOG|RUN_ID:{run_id}|PATH:{log_path}',
            flush=True,
        )
        try:
            return execute_pipeline(
                args,
                windows,
                base_dir,
                runtime_dir,
                db_path,
                project_name,
                run_id,
                commands,
                log_path,
            )
        except KeyboardInterrupt:
            print('STATUS:PIPELINE_FATAL|KeyboardInterrupt:用户中断任务', file=sys.stderr)
            return 130
        except Exception as exc:
            print(
                f'STATUS:PIPELINE_FATAL|{type(exc).__name__}:{exc}',
                file=sys.stderr,
                flush=True,
            )
            return 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f'STATUS:PIPELINE_FATAL|{type(exc).__name__}:{exc}', file=sys.stderr)
        raise SystemExit(1)
