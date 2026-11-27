"""Reconcile a batch of parking exports and stream verified records into Excel."""

import argparse
import json
import os
import re
import sys
from collections import Counter
from contextlib import closing, contextmanager
from datetime import date, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from parking_workbook import validate_export


EXCEL_MAX_ROWS = 1_048_576
DETAIL_METADATA = (("名单车场", "source_name", 38), ("平台项目编码", "code", 16), ("来源报表", "file_name", 46))
PROVENANCE = tuple(title for title, _, _ in DETAIL_METADATA)
AUDIT_HEADERS = ("序号", "名单车场", "查找方式", "实际搜索词", "平台项目名称", "项目编码",
                 "处理结果", "平台查询条数", "汇总条数", "原始报表", "备注")
MATCH_LABELS = {
    "code": "项目编码", "exact_code": "项目编码", "项目编号": "项目编码",
    "exact": "完整名称", "exact_name": "完整名称", "full_name": "完整名称",
    "normalized": "规范化名称", "normalized_name": "规范化名称",
    "keyword": "关键词", "prefix": "关键词", "suffix": "关键词", "shortened": "关键词", "关键字": "关键词",
    "截短名称": "关键词",
    "none": "", "unattempted": "", "ambiguous": "",
}
STATE_LABELS = {
    "not_found": "未找到", "missing": "未找到", "unmatched": "未找到", "无匹配": "未找到",
    "ambiguous": "候选待核对", "multiple": "候选待核对", "有歧义": "候选待核对",
    "unattempted": "未查询", "not_attempted": "未查询", "unsearched": "未查询",
    "matched": "已匹配待导出", "found": "已匹配待导出", "resolved": "已匹配待导出",
    "empty": "查询为零", "zero": "查询为零", "no_records": "查询为零",
    "failed": "导出失败", "error": "导出失败", "export_failed": "导出失败",
    "pending": "待处理", "exporting": "待下载", "exported": "已导出待核对",
    "downloaded": "已下载待核对", "verified": "已下载待核对", "ok": "已下载待核对",
}
UNRESOLVED = {"未找到", "候选待核对", "未查询"}
HEADER_FILL = PatternFill("solid", fgColor="18364B")
HEADER_FONT = Font(name="微软雅黑", color="FFFFFF", bold=True)
TITLE_FONT = Font(name="微软雅黑", color="18364B", bold=True, size=17)
META_FONT = Font(name="微软雅黑", color="526678")
WARNING_FILL = PatternFill("solid", fgColor="FBE8CD")
GOOD_FILL = PatternFill("solid", fgColor="DAF0E4")


def label(value):
    return str(value).strip() if value is not None else ""


def count(value):
    if isinstance(value, bool):
        return None
    text = label(value).replace(",", "")
    return int(text) if re.fullmatch(r"[0-9]+", text) else None


@contextmanager
def export_rows(path):
    with path.open("rb") as stream, closing(load_workbook(stream, read_only=True, data_only=True)) as workbook:
        sheet = workbook.active
        sheet.reset_dimensions()
        rows = sheet.iter_rows(min_row=4, values_only=True)
        first = next(rows, ())
        if not first or any(not label(value) for value in first):
            raise ValueError("报表含有空白表头。")
        headers = tuple(label(value) for value in first)
        if len(headers) != len(set(headers)) or any(name in PROVENANCE for name in headers):
            raise ValueError("报表表头存在重名。")
        yield headers, rows


def reconcile(entries, source_dir, destination, start, end, status):
    seen_files = {}
    seen_parks = set()
    outcomes = []
    for entry in entries:
        method = label(entry.get("match_method"))
        state = label(entry.get("state"))
        source_name = label(entry.get("source_name"))
        matched_name = label(entry.get("matched_name"))
        code = label(entry.get("project_code"))
        expected = count(entry.get("expected_rows"))
        supplied_file = label(entry.get("file"))
        outcome = {
            "source_name": source_name,
            "method": MATCH_LABELS.get(method.lower(), method),
            "query": label(entry.get("query")),
            "matched_name": matched_name,
            "code": code,
            "state": STATE_LABELS.get(state.lower(), state) or ("已匹配待导出" if matched_name else "未查询"),
            "expected": expected,
            "note": label(entry.get("note")),
            "file_name": Path(supplied_file).name if supplied_file else "",
            "path": None,
            "headers": (),
            "merged": None,
        }
        outcomes.append(outcome)
        if not supplied_file:
            if matched_name and expected == 0 and outcome["state"] not in UNRESOLVED:
                outcome.update(state="查询为零", merged=0)
            continue

        path = (source_dir / supplied_file).resolve()
        if path == destination:
            raise ValueError("汇总文件与一份原始报表路径相同。")
        try:
            if outcome["state"] in UNRESOLVED or not source_name or not matched_name or expected is None:
                raise ValueError("缺少已确认的名单车场、平台项目名称或查询条数。")
            park_identity = (code, matched_name)
            if path in seen_files and seen_files[path] != (matched_name, expected):
                raise ValueError("同一份原始报表对应不同的平台项目或查询条数。")
            if path in seen_files or park_identity in seen_parks:
                outcome.update(state="重复项目未再次汇总", merged=0)
                continue
            validate_export(path, project=matched_name, start=start, end=end, status=status, expected_rows=expected)
            with export_rows(path) as (headers, _):
                outcome.update(state="已核对并汇总", path=path, headers=headers, merged=expected)
            seen_files[path] = (matched_name, expected)
            seen_parks.add(park_identity)
        except (OSError, ValueError, TypeError) as error:
            detail = str(error)
            outcome.update(state="报表核对未通过", note="。".join(filter(None, (outcome["note"], detail))))
    return outcomes


def cell(sheet, value, *, font=None, fill=None, wrap=False):
    item = WriteOnlyCell(sheet, value=value)
    if isinstance(value, str):
        item.data_type = "s"
    if font is not None:
        item.font = font
    if fill is not None:
        item.fill = fill
    if wrap:
        item.alignment = Alignment(wrap_text=True, vertical="center")
    return item


def add_header(sheet, headers, row=1):
    sheet.row_dimensions[row].height = 26
    sheet.append([cell(sheet, name, font=HEADER_FONT, fill=HEADER_FILL, wrap=True) for name in headers])


def write_reconciliation(book, outcomes, start, end, status):
    sheet = book.create_sheet("车场查找与导出")
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "C6"
    for index, width in enumerate((8, 38, 15, 32, 40, 14, 23, 15, 15, 48, 65), start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.row_dimensions[1].height = 27
    statuses = Counter(item["state"] for item in outcomes)
    matched = [item for item in outcomes if item["matched_name"] and item["state"] not in UNRESOLVED]
    keyword = sum("关键词" in item["method"] or "关键字" in item["method"] for item in matched)
    merged_total = sum(item["merged"] or 0 for item in outcomes)
    measures = {
        "listed": len(outcomes), "matched": len(matched), "keyword": keyword,
        "not_found": statuses["未找到"], "ambiguous": statuses["候选待核对"],
        "unattempted": statuses["未查询"], "merged_rows": merged_total,
    }
    sheet.append([cell(sheet, f"全车场{status}记录汇总", font=TITLE_FONT)])
    sheet.append([cell(sheet, f"查询区间 {start:%Y-%m-%d} 至 {end:%Y-%m-%d}，含首尾两日", font=META_FONT)])
    sheet.append([cell(sheet, (f"名单 {len(outcomes)} 个，已匹配 {len(matched)} 个，关键词匹配 {keyword} 个，"
                               f"未找到 {statuses['未找到']} 个，候选待核对 {statuses['候选待核对']} 个，"
                               f"未查询 {statuses['未查询']} 个，已汇总 {merged_total:,} 条"), font=META_FONT)])
    sheet.append([])
    add_header(sheet, AUDIT_HEADERS, row=5)
    for index, item in enumerate(outcomes, start=1):
        fill = GOOD_FILL if item["state"] in ("已核对并汇总", "查询为零") else WARNING_FILL
        values = (index, item["source_name"], item["method"], item["query"], item["matched_name"], item["code"],
                  item["state"], item["expected"], item["merged"], item["file_name"], item["note"])
        row = [cell(sheet, value, fill=fill if column == 7 else None, wrap=column in (2, 4, 5, 7, 10, 11))
               for column, value in enumerate(values, start=1)]
        row[7].number_format = row[8].number_format = "#,##0"
        sheet.row_dimensions[index + 5].height = 34 if item["note"] else 28
        sheet.append(row)
    sheet.auto_filter.ref = f"A5:K{max(5, 5 + len(outcomes))}"
    return measures


def write_details(book, outcomes, *, metadata=DETAIL_METADATA, tally_column=None):
    inputs = [item for item in outcomes if item["path"]]
    headers = tuple(dict.fromkeys(name for item in inputs for name in item["headers"]))
    if not headers:
        return 0
    full_headers = tuple(title for title, _, _ in metadata) + headers
    if len(full_headers) > 16_384:
        raise ValueError("原始报表合并后的字段数超过 Excel 上限。")
    if len(full_headers) != len(set(full_headers)):
        raise ValueError("明细来源字段与原始表头重名。")
    sheet = None
    sheet_index = current_row = total = 0
    last_column = get_column_letter(len(full_headers))
    for item in inputs:
        written = 0
        with export_rows(item["path"]) as (current_headers, rows):
            if current_headers != item["headers"]:
                raise ValueError(f"汇总时检测到原始报表表头发生变化。{item['file_name']}")
            positions = {value: index for index, value in enumerate(current_headers)}
            indices = [positions.get(name) for name in headers]
            source = tuple(item[key] for _, key, _ in metadata)
            tally_index = positions[tally_column] if tally_column else None
            if tally_index is not None:
                item["detail_counts"] = Counter()
            for values in rows:
                if all(value in (None, "") for value in values):
                    continue
                if any(value not in (None, "") for value in values[len(current_headers):]):
                    raise ValueError(f"原始报表含未定义的列，无法完整汇总。{item['file_name']}")
                if sheet is None or current_row == EXCEL_MAX_ROWS:
                    if sheet is not None:
                        sheet.auto_filter.ref = f"A1:{last_column}{current_row}"
                        sheet.close()
                    sheet_index += 1
                    sheet = book.create_sheet(f"车辆明细{sheet_index:02d}")
                    sheet.freeze_panes = f"{get_column_letter(len(metadata) + 1)}2"
                    widths = tuple(width for _, _, width in metadata) + (22,) * len(headers)
                    for column, width in enumerate(widths, start=1):
                        sheet.column_dimensions[get_column_letter(column)].width = width
                    add_header(sheet, full_headers)
                    current_row = 1
                detail = [values[index] if index is not None and index < len(values) else None for index in indices]
                # Keep formula-like strings as platform data when opened in Excel.
                output = [cell(sheet, value) if isinstance(value, str) and value.startswith(("=", "#")) else value
                          for value in (*source, *detail)]
                sheet.append(output)
                if tally_index is not None:
                    item["detail_counts"][label(values[tally_index])] += 1
                current_row += 1
                written += 1
        if written != item["merged"]:
            raise ValueError(f"汇总时检测到原始报表条数发生变化。{item['file_name']}")
        total += written
    if sheet is not None:
        sheet.auto_filter.ref = f"A1:{last_column}{current_row}"
        sheet.close()
    return total


def build_report(manifest, output, start, end, status):
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise ValueError("名单进度须为 JSON 对象列表，或包含 entries 列表的 JSON 对象。")
    start_time = datetime.combine(start, time.min)
    end_time = datetime.combine(end, time(23, 59, 59))
    output = output.resolve()
    outcomes = reconcile(entries, manifest.resolve().parent, output, start_time, end_time, status)
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook(write_only=True)
    measures = write_reconciliation(workbook, outcomes, start, end, status)
    if write_details(workbook, outcomes) != measures["merged_rows"]:
        raise ValueError("明细条数与汇总条数不一致。")
    with TemporaryDirectory(dir=output.parent) as staging:
        ready = Path(staging) / "report.xlsx"
        workbook.save(ready)
        os.replace(ready, output)
    return measures


def main():
    parser = argparse.ArgumentParser(description="生成车场查找与记录汇总 Excel")
    parser.add_argument("manifest", type=Path, help="逐车场进度 JSON")
    parser.add_argument("--output", type=Path, required=True, help="汇总 Excel 路径")
    parser.add_argument("--start", default="2026-10-20", type=date.fromisoformat)
    parser.add_argument("--end", default="2026-11-20", type=date.fromisoformat)
    parser.add_argument("--status", default="出场", choices=("出场", "入场"))
    args = parser.parse_args()
    if args.start > args.end or args.output.suffix.lower() != ".xlsx":
        parser.error("开始日期须不晚于结束日期，且输出文件须使用 .xlsx 后缀。")
    try:
        measures = build_report(args.manifest, args.output, args.start, args.end, args.status)
    except (ValueError, OSError) as error:
        sys.exit(f"生成汇总失败。{error}")
    print(json.dumps(measures, ensure_ascii=False))


if __name__ == "__main__":
    main()
