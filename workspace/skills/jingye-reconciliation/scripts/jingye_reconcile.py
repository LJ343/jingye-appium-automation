#!/usr/bin/env python3
# coding: utf-8
'''Run reconciliation from the durable local fact store without Appium crawling.'''

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
SNAPSHOT_SCRIPT = SCRIPT_DIR / 'build_reconciliation_snapshot.py'
RESULT_SCRIPT = SCRIPT_DIR / 'result_check.py'
SHARED_ENV_FILE = SCRIPT_DIR.parent.parent / 'jingye-platform-sync' / '.env'
DEFAULT_BASE_DIR = Path(r'D:\openclaw_data\data\jingyeapp')
SHANGHAI_TZ = dt.timezone(dt.timedelta(hours=8))


def load_shared_env_file(path: Path = SHARED_ENV_FILE) -> Path | None:
    '''Load the shared Jingye UTF-8 .env without overriding process variables.'''
    if not path.is_file():
        return None
    for line_number, raw_line in enumerate(
        path.read_text(encoding='utf-8-sig').splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        if '=' not in line:
            raise RuntimeError(f'.env第{line_number}行缺少等号')
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip()
        if not key or key[0].isdigit() or not key.replace('_', '').isalnum():
            raise RuntimeError(f'.env第{line_number}行变量名无效')
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return path


def resolve_email_to(explicit: str) -> str:
    return (
        explicit.strip()
        or os.getenv('JINGYE_RECONCILIATION_EMAIL_TO', '').strip()
        or os.getenv('SMTP_RECEIVER', '').strip()
    )


def parse_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'日期必须为 YYYY-MM-DD：{value}') from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='从本地数据库生成敬业对账报告并发送邮件')
    parser.add_argument('--base-dir', type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--purchase-start', '--start', dest='purchase_start', type=parse_date, required=True)
    parser.add_argument('--purchase-end', '--end', dest='purchase_end', type=parse_date, required=True)
    parser.add_argument('--anchor-date', type=parse_date, default=None)
    parser.add_argument('--email-to', default='')
    parser.add_argument('--run-id', default='')
    parser.add_argument('--snapshot-only', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--no-vlm', action='store_true')
    parser.add_argument('--max-workers', type=int, default=0)
    return parser.parse_args()


def new_run_id() -> str:
    timestamp = dt.datetime.now(SHANGHAI_TZ).strftime('%Y%m%dT%H%M%S%f')[:-3]
    return f'reconcile-{timestamp}-{uuid.uuid4().hex[:8]}'


@contextlib.contextmanager
def single_instance_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b'0')
        stream.flush()
    stream.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise RuntimeError('已有敬业对账任务正在运行') from exc
    try:
        yield
    finally:
        try:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def run_streaming(name: str, command: list[str]) -> list[str]:
    print(f'STATUS:RECONCILIATION_STEP_START|NAME:{name}', flush=True)
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
    lines: list[str] = []
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip('\r\n')
        lines.append(line)
        print(f'[{name}] {line}', flush=True)
    return_code = process.wait()
    print(
        f'STATUS:RECONCILIATION_STEP_DONE|NAME:{name}|RETURN_CODE:{return_code}',
        flush=True,
    )
    if return_code != 0:
        raise RuntimeError(f'步骤 {name} 失败，返回码 {return_code}')
    return lines


def main() -> int:
    load_shared_env_file()
    args = parse_args()
    email_to = resolve_email_to(args.email_to)
    if args.purchase_start > args.purchase_end:
        raise SystemExit('采购开始日期不能晚于结束日期')
    anchor = args.anchor_date or dt.datetime.now(SHANGHAI_TZ).date()
    sales_end = anchor - dt.timedelta(days=1)
    sales_start = anchor - dt.timedelta(days=9)
    if args.purchase_end > sales_end:
        raise SystemExit(
            f'采购截止日期不能晚于对账锚点日的前一天：{sales_end}'
        )
    run_id = re.sub(
        r'[^0-9A-Za-z_.-]+', '_', args.run_id or new_run_id()
    ).strip('._')
    if not run_id:
        raise ValueError('run-id 不能为空')
    base_dir = args.base_dir.resolve()
    db_path = (
        args.db.resolve()
        if args.db
        else base_dir / 'platform_sync' / 'jingye_facts.sqlite3'
    )
    snapshot_root = base_dir / 'reconciliation_runs' / run_id
    lock_path = base_dir / 'reconciliation_runs' / 'jingye_reconciliation.lock'
    print(
        'STATUS:RECONCILIATION_WINDOWS|'
        f'PURCHASE:{args.purchase_start}..{args.purchase_end}|'
        f'SALES:{sales_start}..{sales_end}|ANCHOR:{anchor}',
        flush=True,
    )
    with single_instance_lock(lock_path):
        snapshot_command = [
            sys.executable, '-X', 'utf8', str(SNAPSHOT_SCRIPT),
            '--base-dir', str(base_dir),
            '--db', str(db_path),
            '--run-id', run_id,
            '--purchase-start', args.purchase_start.isoformat(),
            '--purchase-end', args.purchase_end.isoformat(),
            '--sales-start', sales_start.isoformat(),
            '--sales-end', sales_end.isoformat(),
            '--snapshot-root', str(snapshot_root),
        ]
        lines = run_streaming('snapshot', snapshot_command)
        manifest: dict[str, object] | None = None
        for line in lines:
            if line.startswith('SNAPSHOT_JSON:'):
                manifest = json.loads(line.split(':', 1)[1])
        if manifest is None:
            raise RuntimeError('快照脚本未返回 SNAPSHOT_JSON')
        if args.snapshot_only:
            print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
            print(
                f'STATUS:RECONCILIATION_SNAPSHOT_ONLY_DONE|RUN_ID:{run_id}',
                flush=True,
            )
            return 0

        result_command = [
            sys.executable, '-X', 'utf8', str(RESULT_SCRIPT),
            '--base-dir', str(snapshot_root),
            '--excel-input', str(manifest['excel_path']),
            '--run-id', run_id,
            '--output-dir', str(base_dir / 'excel_check'),
            '--cache-dir', str(base_dir / 'result_check_cache'),
            '--start', args.purchase_start.isoformat(),
            '--end', args.purchase_end.isoformat(),
            '--sales-start', sales_start.isoformat(),
            '--sales-end', sales_end.isoformat(),
        ]
        if email_to:
            result_command.extend(('--email-to', email_to))
        if args.force:
            result_command.append('--force')
        if args.no_vlm:
            result_command.append('--no-vlm')
        if args.max_workers > 0:
            result_command.extend(('--max-workers', str(args.max_workers)))
        run_streaming('result_check', result_command)
    print(f'STATUS:RECONCILIATION_DONE|RUN_ID:{run_id}', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(
            f'STATUS:RECONCILIATION_FATAL|{type(exc).__name__}:{exc}',
            file=sys.stderr,
        )
        raise SystemExit(1)
