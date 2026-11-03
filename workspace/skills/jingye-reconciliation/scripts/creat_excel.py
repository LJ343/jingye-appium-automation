# coding: utf-8
"""创建敬业运输对账初始 Excel。

该脚本只创建四个业务工作表及表头，不写入任何模拟数据。
支持由 OpenClaw 流水线传入起止日期和唯一输出路径。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import uuid
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

DEFAULT_BASE_DIR = Path(r"D:\openclaw_data\data\jingyeapp")

PURCHASE_HEADERS = [
    "序号", "运单号", "车牌号", "货物名称", "承运商", "卸货地", "装货日期", "卸货日期",
    "装货重量", "卸货重量", "结算重量", "上游不含税运价", "上游含税运价",
    "下游含税运价", "亏吨", "亏损金额", "预计应结算金额", "已结算金额", "备注1", "备注2",
]

SALES_HEADERS = [
    "序号", "运单号", "车牌号", "货物名称", "卸货地", "装货日期", "装货重量",
    "磅票净重", "规格", "理重", "结算量", "单价", "上游不含税运价", "上游含税运价",
    "下游含税运价", "预计应结算金额", "已结算金额", "备注1", "备注2",
]

PURCHASE_GREEN_COLS = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 19}
SALES_GREEN_COLS = {2, 3, 4, 5, 6, 7, 8, 9, 10,11,12,18}

PURCHASE_WIDTHS = {
    1: 8, 2: 32, 3: 16, 4: 16, 5: 32, 6: 32, 7: 14, 8: 14, 9: 12, 10: 12,
    11: 12, 12: 15, 13: 15, 14: 15, 15: 10, 16: 12, 17: 16, 18: 16, 19: 22, 20: 22,
}
SALES_WIDTHS = {
    1: 8, 2: 32, 3: 16, 4: 16, 5: 32, 6: 14, 7: 12, 8: 12, 9: 24,
    10: 12, 11: 12, 12: 12, 13: 15, 14: 15, 15: 15, 16: 16, 17: 16,
    18: 22, 19: 22,
}

SHEET_NAMES = (
    "销售单-河北众沃",
    "采购单-河北众沃",
    "销售单-上海宾迪",
    "采购单-上海宾迪",
)


def parse_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD：{value}") from exc


def safe_filename_part(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")


def make_unique_filename(start: dt.date, end: dt.date) -> str:
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    random_id = uuid.uuid4().hex[:8]
    return (
        f"远行_敬业运输明细_{start:%Y-%m-%d}_至_{end:%Y-%m-%d}_"
        f"{timestamp}_{random_id}.xlsx"
    )


def build_workbook(start: dt.date, end: dt.date) -> openpyxl.Workbook:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    wb.properties.title = "远行敬业运输明细"
    wb.properties.subject = f"对账时间段：{start:%Y-%m-%d} 至 {end:%Y-%m-%d}"
    wb.properties.creator = "jingye-reconciliation"

    font_title = Font(name="微软雅黑", size=16, bold=True, color="FFFFFF")
    font_header = Font(name="微软雅黑", size=11, bold=True, color="000000")

    fill_title_purchase = PatternFill("solid", fgColor="365F91")
    fill_title_sales = PatternFill("solid", fgColor="954F4D")
    fill_green_header = PatternFill("solid", fgColor="C6EFCE")
    fill_normal_header = PatternFill("solid", fgColor="EAEAEA")

    thin_side = Side(style="thin", color="D9D9D9")
    border_all = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

    for name in SHEET_NAMES:
        ws = wb.create_sheet(title=name)
        is_purchase = "采购单" in name
        headers = PURCHASE_HEADERS if is_purchase else SALES_HEADERS
        green_cols = PURCHASE_GREEN_COLS if is_purchase else SALES_GREEN_COLS
        widths = PURCHASE_WIDTHS if is_purchase else SALES_WIDTHS
        title_text = "采购运单汇总表" if is_purchase else "销售运单汇总表"
        title_fill = fill_title_purchase if is_purchase else fill_title_sales

        max_col_letter = get_column_letter(len(headers))
        ws.merge_cells(f"A1:{max_col_letter}1")
        title_cell = ws["A1"]
        title_cell.value = title_text
        title_cell.font = font_title
        title_cell.fill = title_fill
        title_cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 45

        ws.row_dimensions[2].height = 30
        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(row=2, column=col_idx, value=header)
            cell.font = font_header
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = border_all
            cell.fill = fill_green_header if col_idx in green_cols else fill_normal_header

        for col_idx, width in widths.items():
            ws.column_dimensions[get_column_letter(col_idx)].width = width

        # 初始表不含模拟数据。筛选范围仅覆盖表头，后续写入数据后仍可正常使用。
        ws.auto_filter.ref = f"A2:{max_col_letter}2"
        ws.freeze_panes = "A3"
        ws.sheet_view.showGridLines = False

    return wb


def save_atomic(wb: openpyxl.Workbook, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp.xlsx")
    try:
        wb.save(temp_path)
        os.replace(temp_path, output_path)
    finally:
        wb.close()
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="创建敬业运输对账初始Excel")
    parser.add_argument("--start", required=True, type=parse_date, help="开始日期，YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=parse_date, help="结束日期，YYYY-MM-DD")
    parser.add_argument(
        "--base-dir",
        default=str(DEFAULT_BASE_DIR),
        help="敬业对账数据根目录",
    )
    parser.add_argument(
        "--output-path",
        default="",
        help="完整输出路径；省略时自动在 base-dir/excel_ori 下生成唯一文件名",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.start > args.end:
        raise SystemExit(f"开始日期不能晚于结束日期：{args.start} > {args.end}")

    base_dir = Path(args.base_dir)
    output_path = (
        Path(args.output_path)
        if args.output_path
        else base_dir / "excel_ori" / make_unique_filename(args.start, args.end)
    )
    if output_path.suffix.lower() != ".xlsx":
        output_path = output_path.with_suffix(".xlsx")
    output_path = output_path.with_name(safe_filename_part(output_path.stem) + ".xlsx")

    print(f"STATUS:CREATE_EXCEL_START|START:{args.start}|END:{args.end}", flush=True)
    wb = build_workbook(args.start, args.end)
    save_atomic(wb, output_path)
    print(f"EXCEL_PATH:{output_path}", flush=True)
    print("STATUS:CREATE_EXCEL_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
