#!/usr/bin/env python3
# coding: utf-8
'''Materialize one reconciliation-only run from the durable SQLite fact store.'''

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Border, Side
from openpyxl.utils import get_column_letter

from creat_excel import build_workbook, save_atomic


DEFAULT_BASE_DIR = Path(r'D:\openclaw_data\data\jingyeapp')
SHEET_NAMES = (
    '销售单-河北众沃',
    '采购单-河北众沃',
    '销售单-上海宾迪',
    '采购单-上海宾迪',
)
PDF_FOLDER_BY_SHEET = {
    '采购单-河北众沃': 'pdf_caigouhebei',
    '采购单-上海宾迪': 'pdf_caigoushanghai',
    '销售单-河北众沃': 'pdf_xiaoshouhebei',
    '销售单-上海宾迪': 'pdf_xiaoshoushanghai',
}
PDF_NAME_RE = re.compile(
    r'^(?P<prefix>.+?_[^_]+_\d{8}_\d{6})(?:_(?P<index>\d+))?$',
    re.IGNORECASE,
)


def parse_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'日期必须为 YYYY-MM-DD：{value}') from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='从敬业本地事实库生成一次隔离的对账快照')
    parser.add_argument('--base-dir', type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--purchase-start', type=parse_date, required=True)
    parser.add_argument('--purchase-end', type=parse_date, required=True)
    parser.add_argument('--sales-start', type=parse_date, required=True)
    parser.add_argument('--sales-end', type=parse_date, required=True)
    parser.add_argument('--snapshot-root', type=Path, default=None)
    return parser.parse_args()


def latest_observations(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    has_purchase_baselines = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='purchase_observation_baselines'"
    ).fetchone() is not None
    baseline_join = (
        '''LEFT JOIN purchase_observation_baselines baseline
             ON baseline.waybill_id = ranked.waybill_id
            AND baseline.sheet_name = ranked.sheet_name'''
        if has_purchase_baselines else ''
    )
    selected_observation = (
        '''CASE WHEN ranked.direction = 'PURCHASE'
                      AND baseline.observation_id IS NOT NULL
                THEN ranked.id = baseline.observation_id
                ELSE ranked.rank_no = 1 END'''
        if has_purchase_baselines else 'ranked.rank_no = 1'
    )
    baseline_row = 'baseline.row_json' if has_purchase_baselines else 'NULL'
    return connection.execute(
        f'''
        WITH ranked AS (
            SELECT o.*, w.direction, w.waybill_no, w.plate_no,
                   w.source_loading_date,
                   ROW_NUMBER() OVER (
                       PARTITION BY o.waybill_id, o.sheet_name
                       ORDER BY o.observed_at DESC, o.id DESC
                   ) AS rank_no
            FROM waybill_observations o
            JOIN waybills w ON w.id = o.waybill_id
        )
        SELECT ranked.*, {baseline_row} AS baseline_row_json FROM ranked
        {baseline_join}
        WHERE {selected_observation}
        ORDER BY sheet_name, source_loading_date, waybill_no, plate_no
        ''',
    ).fetchall()


def purchase_carrier_for_sheet(
    connection: sqlite3.Connection, waybill_id: int, sheet_name: str
) -> str:
    """补充承运商时保留原观察，且只接受同工作表的属性来源。"""
    row = connection.execute(
        '''
        SELECT attribute.value_text
        FROM waybill_attributes attribute
        JOIN waybill_observations source
          ON source.id = attribute.source_observation_id
         AND source.waybill_id = attribute.waybill_id
        WHERE attribute.waybill_id = ? AND attribute.field_key = 'excel.承运商'
          AND source.sheet_name = ?
        ''',
        (waybill_id, sheet_name),
    ).fetchone()
    return str(row['value_text'] or '').strip() if row else ''


def sales_dates_for_waybill(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> list[str]:
    values = connection.execute(
        '''
        SELECT DISTINCT SUBSTR(app_export_time, 1, 10) AS business_date
        FROM freight_targets
        WHERE waybill_no = ? AND plate_no = ? AND sheet_name = ?
          AND LENGTH(app_export_time) >= 10
        ORDER BY business_date
        ''',
        (row['waybill_no'], row['plate_no'], row['sheet_name']),
    ).fetchall()
    return [str(value['business_date']) for value in values if value['business_date']]


def select_rows(
    connection: sqlite3.Connection,
    purchase_start: dt.date,
    purchase_end: dt.date,
    sales_start: dt.date,
    sales_end: dt.date,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    selected: list[dict[str, Any]] = []
    stats = Counter()
    for row in latest_observations(connection):
        if row['sheet_name'] not in SHEET_NAMES:
            continue
        if row['direction'] == 'PURCHASE':
            business_date = str(row['source_loading_date'] or '')[:10]
            if not business_date:
                stats['purchase_missing_date'] += 1
                continue
            if not purchase_start.isoformat() <= business_date <= purchase_end.isoformat():
                continue
        else:
            dates = sales_dates_for_waybill(connection, row)
            matching = [
                value for value in dates
                if sales_start.isoformat() <= value <= sales_end.isoformat()
            ]
            if not matching:
                if not dates:
                    stats['sales_missing_date'] += 1
                continue
            business_date = matching[-1]
        try:
            row_json = (
                row['baseline_row_json']
                if row['direction'] == 'PURCHASE' and row['baseline_row_json'] is not None
                else row['row_json']
            )
            row_data = json.loads(row_json)
        except Exception:
            stats['invalid_row_json'] += 1
            continue
        if not isinstance(row_data, dict):
            stats['invalid_row_json'] += 1
            continue
        if row['direction'] == 'PURCHASE' and not str(
            row_data.get('承运商') or ''
        ).strip():
            carrier = purchase_carrier_for_sheet(
                connection, int(row['waybill_id']), str(row['sheet_name'])
            )
            if carrier:
                row_data['承运商'] = carrier
        selected.append(
            {
                'waybill_id': int(row['waybill_id']),
                'sheet_name': str(row['sheet_name']),
                'waybill_no': str(row['waybill_no']),
                'plate_no': str(row['plate_no']),
                'business_date': business_date,
                'source_excel_path': str(row['excel_path']),
                'source_excel_row': int(row['excel_row']),
                'row_data': row_data,
            }
        )
        stats[f'{str(row["direction"]).lower()}_selected'] += 1
    selected.sort(
        key=lambda item: (
            item['sheet_name'], item['business_date'],
            item['waybill_no'], item['plate_no'],
        )
    )
    stats['selected_total'] = len(selected)
    return selected, dict(stats)


def write_snapshot_workbook(
    selected: list[dict[str, Any]],
    output_path: Path,
    purchase_start: dt.date,
    purchase_end: dt.date,
    sales_start: dt.date,
    sales_end: dt.date,
) -> dict[tuple[int, str], int]:
    workbook = build_workbook(purchase_start, purchase_end)
    workbook.properties.subject = (
        f'采购：{purchase_start} 至 {purchase_end}；'
        f'销售：{sales_start} 至 {sales_end}'
    )
    output_rows: dict[tuple[int, str], int] = {}
    thin = Side(style='thin', color='D9D9D9')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in selected:
        grouped[item['sheet_name']].append(item)
    for sheet_name in SHEET_NAMES:
        worksheet = workbook[sheet_name]
        headers = [
            str(worksheet.cell(row=2, column=index).value or '')
            for index in range(1, worksheet.max_column + 1)
        ]
        columns = {header: column for column, header in enumerate(headers, start=1)}
        for sequence, item in enumerate(grouped.get(sheet_name, []), start=1):
            row_number = sequence + 2
            row_data = item['row_data']
            for column, header in enumerate(headers, start=1):
                value = sequence if column == 1 else row_data.get(header, '')
                cell = worksheet.cell(row=row_number, column=column, value=value)
                cell.border = border
                cell.alignment = Alignment(
                    horizontal='center' if column == 1 else 'left',
                    vertical='center',
                    wrap_text=True,
                )
            settlement = row_data.get('结算重量')
            if sheet_name.startswith('采购单-') and (
                settlement in (None, '')
                or (isinstance(settlement, str) and settlement.startswith('='))
            ):
                load_col = get_column_letter(columns['装货重量'])
                unload_col = get_column_letter(columns['卸货重量'])
                worksheet.cell(
                    row=row_number, column=columns['结算重量'],
                    value=(
                        f'=IF(COUNT({load_col}{row_number}:{unload_col}{row_number})<2,'
                        f'"",MIN({load_col}{row_number},{unload_col}{row_number}))'
                    ),
                )
            output_rows[(item['waybill_id'], sheet_name)] = row_number
        last_row = max(2, 2 + len(grouped.get(sheet_name, [])))
        worksheet.auto_filter.ref = f'A2:{get_column_letter(len(headers))}{last_row}'
    save_atomic(workbook, output_path)
    return output_rows


def exact_sources(
    connection: sqlite3.Connection,
    item: dict[str, Any],
) -> list[tuple[str, str]]:
    rows = connection.execute(
        '''
        SELECT DISTINCT source_kind, source_value
        FROM source_context_events
        WHERE waybill_id = ? AND sheet_name = ?
          AND excel_path = ? AND CAST(excel_row AS INTEGER) = ?
          AND TRIM(source_kind) != '' AND TRIM(source_value) != ''
        ORDER BY recorded_at, source_kind, source_value
        ''',
        (
            item['waybill_id'], item['sheet_name'], item['source_excel_path'],
            item['source_excel_row'],
        ),
    ).fetchall()
    if not rows:
        rows = connection.execute(
            '''
            SELECT DISTINCT source_kind, source_value
            FROM waybill_sources
            WHERE waybill_id = ? AND sheet_name = ? AND plate_no = ?
            ORDER BY last_seen_at DESC, source_kind, source_value
            ''',
            (item['waybill_id'], item['sheet_name'], item['plate_no']),
        ).fetchall()
    return [
        (str(row['source_kind']), str(row['source_value']))
        for row in rows
        if row['source_kind'] and row['source_value']
    ]


def row_source_events(
    selected: list[dict[str, Any]],
    source_map: dict[tuple[int, str], list[tuple[str, str]]],
    output_rows: dict[tuple[int, str], int],
    excel_path: Path,
    run_id: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in selected:
        key = (item['waybill_id'], item['sheet_name'])
        for source_kind, source_value in source_map.get(key, []):
            events.append(
                {
                    'version': 1,
                    'event': 'row',
                    'run_id': run_id,
                    'excel_path': str(excel_path.resolve()),
                    'sheet': item['sheet_name'],
                    'excel_row': output_rows[key],
                    'waybill': item['waybill_no'],
                    'car': item['plate_no'],
                    'source_kind': source_kind,
                    'source_value': source_value,
                    'recorded_at': dt.datetime.now().isoformat(timespec='seconds'),
                    'materialized_from_database': True,
                }
            )
    return events


def freight_events(
    connection: sqlite3.Connection,
    selected: list[dict[str, Any]],
    source_map: dict[tuple[int, str], list[tuple[str, str]]],
    output_rows: dict[tuple[int, str], int],
    run_id: str,
    sales_start: dt.date,
    sales_end: dt.date,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    events: list[dict[str, Any]] = []
    stats = Counter()
    seen_targets: set[str] = set()
    seen_records: set[str] = set()
    seen_failures: set[str] = set()
    for item in selected:
        if not item['sheet_name'].startswith('销售单-'):
            continue
        key = (item['waybill_id'], item['sheet_name'])
        exact = set(source_map.get(key, []))
        targets = connection.execute(
            '''
            SELECT * FROM freight_targets
            WHERE sheet_name = ? AND waybill_no = ? AND plate_no = ?
              AND SUBSTR(app_export_time, 1, 10) BETWEEN ? AND ?
            ORDER BY source_kind, source_value, target_key
            ''',
            (
                item['sheet_name'], item['waybill_no'], item['plate_no'],
                sales_start.isoformat(), sales_end.isoformat(),
            ),
        ).fetchall()
        for target in targets:
            source_pair = (str(target['source_kind']), str(target['source_value']))
            if exact and source_pair not in exact:
                continue
            target_key = str(target['target_key'])
            if target_key not in seen_targets:
                seen_targets.add(target_key)
                events.append(
                    {
                        'version': 1,
                        'event': 'sales_target',
                        'run_id': run_id,
                        'sheet': target['sheet_name'],
                        'source_kind': target['source_kind'],
                        'source_value': target['source_value'],
                        'waybill': target['waybill_no'],
                        'car': target['plate_no'],
                        'app_export_time': target['app_export_time'],
                        'excel_row': output_rows[key],
                        'target_key': target_key,
                        'recorded_at': target['last_seen_at'],
                        'materialized_from_database': True,
                        'database_status': target['status'],
                    }
                )
                stats['targets'] += 1
            records = connection.execute(
                'SELECT * FROM freight_records WHERE target_key = ? ORDER BY record_key',
                (target_key,),
            ).fetchall()
            for record in records:
                record_key = str(record['record_key'])
                if record_key in seen_records:
                    continue
                seen_records.add(record_key)
                events.append(
                    {
                        'version': 1,
                        'event': 'freight_success',
                        'run_id': run_id,
                        'sheet': record['sheet_name'],
                        'source_kind': record['source_kind'],
                        'source_value': record['source_value'],
                        'car': record['plate_no'],
                        'waybill': record['waybill_no'],
                        'outbound_datetime': record['outbound_datetime'],
                        'raw_spec': record['raw_spec'],
                        'normalized_spec': record['normalized_spec'],
                        'settlement_quantity': record['settlement_quantity'],
                        'unit_price': record['unit_price'],
                        'card_key': record['card_key'],
                        'record_key': record_key,
                        'card_occurrence': record['card_occurrence'],
                        'record_occurrence': record['record_occurrence'],
                        'recorded_at': record['recorded_at'],
                        'materialized_from_database': True,
                    }
                )
                stats['records'] += 1
            failures = connection.execute(
                'SELECT * FROM freight_failures WHERE target_key = ? ORDER BY failure_key',
                (target_key,),
            ).fetchall()
            for failure in failures:
                failure_key = str(failure['failure_key'])
                card_key = str(failure['card_key'])
                if failure_key in seen_failures:
                    continue
                seen_failures.add(failure_key)
                events.append(
                    {
                        'version': 1,
                        'event': 'freight_failure',
                        'run_id': run_id,
                        'sheet': failure['sheet_name'],
                        'source_kind': failure['source_kind'],
                        'source_value': failure['source_value'],
                        'car': failure['plate_no'],
                        'waybill': failure['waybill_no'],
                        'outbound_datetime': failure['outbound_datetime'],
                        'raw_spec': failure['raw_spec'],
                        'error': failure['error_message'],
                        'attempt': failure['attempt'],
                        'card_key': card_key,
                        'failure_key': failure_key,
                        'card_occurrence': failure['card_occurrence'],
                        'recorded_at': failure['recorded_at'],
                        'materialized_from_database': True,
                    }
                )
                stats['failures'] += 1
    return events, dict(stats)


def collision_safe_destination(
    directory: Path,
    source_name: str,
    sha256: str,
    used: dict[Path, str],
) -> Path:
    candidate = directory / source_name
    if candidate not in used or used[candidate] == sha256:
        used[candidate] = sha256
        return candidate
    match = PDF_NAME_RE.match(Path(source_name).stem)
    if not match:
        raise RuntimeError(f'同名且无法安全改名的 PDF：{source_name}')
    prefix = match.group('prefix')
    index = max(2, int(match.group('index') or 1) + 1)
    while True:
        candidate = directory / f'{prefix}_{index}.pdf'
        if candidate not in used or used[candidate] == sha256:
            used[candidate] = sha256
            return candidate
        index += 1


def link_or_copy(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return 'reused'
    try:
        os.link(source, destination)
        return 'hardlinked'
    except OSError:
        shutil.copy2(source, destination)
        return 'copied'


def materialize_pdfs(
    connection: sqlite3.Connection,
    selected: list[dict[str, Any]],
    source_map: dict[tuple[int, str], list[tuple[str, str]]],
    snapshot_root: Path,
    excel_path: Path,
    run_id: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    events: list[dict[str, Any]] = []
    stats = Counter()
    used: dict[Path, str] = {}
    materialized: dict[tuple[str, str], Path] = {}
    for item in selected:
        sheet_name = item['sheet_name']
        owner = sheet_name.split('-', 1)[1]
        direction = 'PURCHASE' if sheet_name.startswith('采购单-') else 'SALES'
        folder = snapshot_root / PDF_FOLDER_BY_SHEET[sheet_name] / run_id
        rows = connection.execute(
            '''
            SELECT wd.sha256, dl.path, dl.direction, dl.source_owner
            FROM waybill_documents wd
            JOIN document_locations dl ON dl.sha256 = wd.sha256
            WHERE wd.waybill_id = ?
            ORDER BY wd.attachment_index, dl.mtime_ns DESC, dl.path
            ''',
            (item['waybill_id'],),
        ).fetchall()
        by_hash: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            by_hash[str(row['sha256'])].append(row)
        for sha256, locations in by_hash.items():
            matching = [
                row for row in locations
                if row['direction'] == direction
                and row['source_owner'] == owner
                and Path(str(row['path'])).is_file()
            ]
            if not matching:
                stats['missing_exact_pdf_location'] += 1
                continue
            source = Path(str(matching[0]['path']))
            cache_key = (str(folder), sha256)
            destination = materialized.get(cache_key)
            if destination is None:
                destination = collision_safe_destination(
                    folder, source.name, sha256, used
                )
                method = link_or_copy(source, destination)
                materialized[cache_key] = destination
                stats[method] += 1
                stats['pdfs'] += 1
            source_key = (item['waybill_id'], sheet_name)
            for source_kind, source_value in source_map.get(source_key, []):
                events.append(
                    {
                        'version': 1,
                        'event': 'pdf',
                        'run_id': run_id,
                        'excel_path': str(excel_path.resolve()),
                        'sheet': sheet_name,
                        'waybill': item['waybill_no'],
                        'car': item['plate_no'],
                        'source_kind': source_kind,
                        'source_value': source_value,
                        'pdf_path': str(destination.resolve()),
                        'recorded_at': dt.datetime.now().isoformat(timespec='seconds'),
                        'materialized_from_database': True,
                    }
                )
    return events, dict(stats)


def write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        temporary.write_text(
            ''.join(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + '\n'
                for event in events
            ),
            encoding='utf-8',
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    if args.purchase_start > args.purchase_end:
        raise SystemExit('采购开始日期不能晚于结束日期')
    if args.sales_start > args.sales_end:
        raise SystemExit('销售开始日期不能晚于结束日期')
    base_dir = args.base_dir.resolve()
    db_path = (
        args.db.resolve()
        if args.db
        else base_dir / 'platform_sync' / 'jingye_facts.sqlite3'
    )
    if not db_path.is_file():
        raise FileNotFoundError(f'本地事实库不存在：{db_path}')
    snapshot_root = (
        args.snapshot_root.resolve()
        if args.snapshot_root
        else base_dir / 'reconciliation_runs' / args.run_id
    )
    manifest_path = snapshot_root / 'manifest.json'
    if manifest_path.exists():
        raise FileExistsError(f'对账快照 run-id 已存在：{args.run_id}')
    snapshot_root.mkdir(parents=True, exist_ok=True)
    excel_path = snapshot_root / 'input' / (
        f'敬业数据库对账_{args.purchase_start}_至_{args.purchase_end}_{args.run_id}.xlsx'
    )
    excel_path.parent.mkdir(parents=True, exist_ok=True)
    uri = f'file:{db_path.as_posix()}?mode=ro'
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        selected, selection_stats = select_rows(
            connection,
            args.purchase_start,
            args.purchase_end,
            args.sales_start,
            args.sales_end,
        )
        output_rows = write_snapshot_workbook(
            selected,
            excel_path,
            args.purchase_start,
            args.purchase_end,
            args.sales_start,
            args.sales_end,
        )
        source_map = {
            (item['waybill_id'], item['sheet_name']): exact_sources(connection, item)
            for item in selected
        }
        row_events = row_source_events(
            selected, source_map, output_rows, excel_path, args.run_id
        )
        pdf_events, pdf_stats = materialize_pdfs(
            connection, selected, source_map, snapshot_root, excel_path, args.run_id
        )
        freight, freight_stats = freight_events(
            connection, selected, source_map, output_rows, args.run_id,
            args.sales_start, args.sales_end,
        )
    finally:
        connection.close()
    source_path = snapshot_root / 'source_context' / f'{args.run_id}.jsonl'
    freight_path = snapshot_root / 'freight_confirmation' / f'{args.run_id}.jsonl'
    write_jsonl(source_path, row_events + pdf_events)
    write_jsonl(freight_path, freight)
    missing_sources = sum(
        not source_map.get((item['waybill_id'], item['sheet_name']))
        for item in selected
    )
    manifest = {
        'run_id': args.run_id,
        'created_at': dt.datetime.now().isoformat(timespec='seconds'),
        'database_read_only': True,
        'db_path': str(db_path),
        'snapshot_root': str(snapshot_root),
        'excel_path': str(excel_path),
        'source_context_path': str(source_path),
        'freight_confirmation_path': str(freight_path),
        'purchase_start': args.purchase_start.isoformat(),
        'purchase_end': args.purchase_end.isoformat(),
        'sales_start': args.sales_start.isoformat(),
        'sales_end': args.sales_end.isoformat(),
        'selection': selection_stats,
        'missing_sources': missing_sources,
        'source_events': len(row_events) + len(pdf_events),
        'freight': freight_stats,
        'pdf': pdf_stats,
    }
    write_jsonl(manifest_path.with_suffix('.jsonl'), [manifest])
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding='utf-8',
    )
    print('STATUS:RECONCILIATION_SNAPSHOT_DONE', flush=True)
    print('SNAPSHOT_JSON:' + json.dumps(manifest, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
