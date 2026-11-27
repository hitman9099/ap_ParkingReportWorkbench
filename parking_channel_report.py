"""Build an Excel reconciliation of individual entrance-channel reports."""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from parking_batch_report import (
    DETAIL_METADATA,
    GOOD_FILL,
    MATCH_LABELS,
    META_FONT,
    STATE_LABELS,
    TITLE_FONT,
    UNRESOLVED,
    WARNING_FILL,
    add_header,
    cell,
    count,
    export_rows,
    label,
    write_details,
)
from parking_workbook import validate_export


CHANNEL_METADATA = (*DETAIL_METADATA[:2], ("查询入场通道", "channel", 29), DETAIL_METADATA[2])
HEADERS = ("名录序号", "名单车场", "查找方式", "实际搜索词", "平台项目名称", "项目编码",
           "查询入场通道", "车场查找结果", "通道处理结果", "平台查询条数", "报表条数", "核验条数",
           "报表状态分布", "原始报表", "备注")
CHANNEL_STATES = {
    "query_failed": "查询失败",
    "download_failed": "下载失败",
    "validation_failed": "报表核对未通过",
    "no_channels": "未取得通道",
    "skipped": "未处理",
    "queued": "待下载",
}
FINAL_CHANNELS = {"已核对并汇总", "查询为零"}
PENDING_CHANNELS = {"未查询", "未处理", "待处理", "待下载", "已匹配待导出"}
KNOWN_CHANNEL_STATES = FINAL_CHANNELS | PENDING_CHANNELS


def combined_note(*notes):
    return "。".join(dict.fromkeys(note.strip("。") for note in notes if note))


def load_entries(manifest):
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list) or not all(isinstance(park, dict) for park in entries):
        raise ValueError("名单进度须为 JSON 对象列表，或包含 entries 列表的 JSON 对象。")
    for entry in entries:
        channels = entry.get("channels")
        if channels is not None and (not isinstance(channels, list) or not all(isinstance(channel, dict) for channel in channels)):
            raise ValueError("每个车场的 channels 须为 JSON 对象列表。")
    return entries


def reconcile(entries, source_dir, output, start, end):
    outcomes = []
    seen_reports = set()
    seen_options = set()
    for index, entry in enumerate(entries, start=1):
        method = label(entry.get("match_method"))
        matched_name = label(entry.get("matched_name"))
        park_state = label(entry.get("state"))
        park_state = CHANNEL_STATES.get(park_state.lower(), STATE_LABELS.get(park_state.lower(), park_state))
        park_note = label(entry.get("note"))
        origin = {
            "park_index": index,
            "source_name": label(entry.get("source_name")),
            "method": "关键词" if method.lower() == "suffix" else MATCH_LABELS.get(method.lower(), method),
            "query": label(entry.get("query")),
            "matched_name": matched_name,
            "code": label(entry.get("project_code")),
            "park_state": "已匹配" if matched_name and park_state not in UNRESOLVED else (park_state or "未查询"),
        }
        channels = entry.get("channels") or []
        if not channels:
            outcomes.append({**origin, "channel": "", "state": "", "expected": None, "exported": None,
                             "merged": None, "file_name": "", "path": None, "headers": (),
                             "note": combined_note(park_note, (park_state if park_state == "未取得通道" else
                                                               "尚未确认具体入场通道") if matched_name else "")})
            continue
        for record in channels:
            name = label(record.get("channel"))
            expected = count(record.get("expected_rows"))
            exported = count(record.get("exported_rows", record.get("reported_rows")))
            state = label(record.get("state"))
            state = CHANNEL_STATES.get(state.lower(), STATE_LABELS.get(state.lower(), state)) or "未查询"
            raw_file = label(record.get("file"))
            note = combined_note(park_note, label(record.get("note")))
            item = {**origin, "channel": name, "state": state, "expected": expected, "exported": exported,
                    "merged": None, "file_name": Path(raw_file).name if raw_file else "", "path": None,
                    "headers": (), "note": note, "option": True}
            outcomes.append(item)
            if not name or not matched_name or origin["park_state"] in UNRESOLVED:
                item.update(state="未确认入场通道", note=combined_note(note, "缺少已确认的车场或入场通道"))
                continue
            channel_id = label(record.get("channel_id"))
            identity = (origin["code"] or matched_name, ("id", channel_id) if channel_id else ("name", name))
            if identity in seen_options:
                item.update(state="重复通道未再次汇总", note=combined_note(note, "本次名单已有同一个车场和通道"))
                continue
            seen_options.add(identity)
            if not raw_file:
                if expected == 0 and state not in {"查询失败", "导出失败", "下载失败", "报表核对未通过"}:
                    item.update(state="查询为零", merged=0)
                elif state in {"已导出待核对", "已下载待核对"}:
                    item.update(state="未取得报表", note=combined_note(note, "缺少原始报表文件路径"))
                continue
            raw = (source_dir / raw_file).resolve()
            if raw == output:
                raise ValueError("汇总文件与一份原始报表路径相同。")
            item["downloaded"] = raw.is_file()
            try:
                if expected is None:
                    raise ValueError("缺少平台查询条数")
                if raw in seen_reports:
                    raise ValueError("同一原始报表已对应另一个查询通道")
                if exported is not None and exported != expected:
                    raise ValueError("平台查询条数与报表条数不一致")
                validation = validate_export(raw, project=matched_name, start=start, end=end,
                                             status=None, entry_channel=name, expected_rows=expected)
                with export_rows(raw) as (headers, _):
                    item.update(state="已核对并汇总" if expected else "查询为零", path=raw, headers=headers,
                                exported=validation.row_count, merged=validation.row_count)
                seen_reports.add(raw)
            except (OSError, ValueError, TypeError) as error:
                item.update(state="报表核对未通过", note=combined_note(note, str(error)))
    return outcomes


def write_audit(sheet, outcomes, start, end):
    parks = {item["park_index"]: item for item in outcomes}
    matched = [item for item in parks.values() if item["matched_name"] and item["park_state"] not in UNRESOLVED]
    options = [item for item in outcomes if item.get("option")]
    states = Counter(item["state"] for item in options)
    park_states = Counter(item["park_state"] for item in parks.values())
    status_counts = Counter()
    for item in options:
        status_counts.update(item.get("detail_counts", {}))
    measures = {
        "listed": len(parks),
        "matched": len(matched),
        "keyword": sum("关键词" in item["method"] for item in matched),
        "not_found": park_states["未找到"],
        "ambiguous": park_states["候选待核对"],
        "unattempted": park_states["未查询"],
        "park_without_channels": sum(not item.get("option") for item in matched),
        "channels": len(options),
        "queried_channels": sum(item["expected"] is not None for item in options),
        "zero_channels": states["查询为零"],
        "downloaded_channels": sum(bool(item.get("downloaded")) for item in options),
        "validated_channels": states["已核对并汇总"],
        "failed_channels": sum(item["state"] not in KNOWN_CHANNEL_STATES for item in options),
        "pending_channels": sum(item["state"] in PENDING_CHANNELS for item in options),
        "merged_rows": sum(item["merged"] or 0 for item in options),
        "status_counts": dict(status_counts),
    }
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "C6"
    widths = (10, 38, 15, 32, 38, 14, 30, 24, 28, 16, 15, 15, 29, 50, 69)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.row_dimensions[1].height = 27
    sheet.append([cell(sheet, "按入场通道汇总车辆进出记录", font=TITLE_FONT)])
    sheet.append([cell(sheet, f"查询区间 {start:%Y-%m-%d} 至 {end:%Y-%m-%d}，含首尾两日。出入状态未选。", font=META_FONT)])
    sheet.append([cell(sheet, (f"名单 {measures['listed']} 个车场，已匹配 {measures['matched']} 个。"
                               f"查询选项 {measures['channels']} 个，核验 {measures['validated_channels']} 个，"
                               f"零记录 {measures['zero_channels']} 个，待处理 {measures['pending_channels']} 个，"
                               f"未通过 {measures['failed_channels']} 个。已汇总 {measures['merged_rows']:,} 条。"),
                       font=META_FONT)])
    sheet.append([])
    add_header(sheet, HEADERS, row=5)
    for row_number, item in enumerate(outcomes, start=6):
        counts = item.get("detail_counts", {})
        observed = "，".join(f"{status or '空白'} {rows:,} 条" for status, rows in sorted(counts.items()))
        values = (item["park_index"], item["source_name"], item["method"], item["query"], item["matched_name"],
                  item["code"], item["channel"], item["park_state"], item["state"], item["expected"], item["exported"],
                  item["merged"], observed, item["file_name"], item["note"])
        fill = GOOD_FILL if item["state"] in FINAL_CHANNELS else WARNING_FILL
        row = [cell(sheet, value, fill=fill if column == 9 else None, wrap=column not in (1, 6, 10, 11, 12))
               for column, value in enumerate(values, start=1)]
        for index in (9, 10, 11):
            row[index].number_format = "#,##0"
        sheet.row_dimensions[row_number].height = 35 if item["note"] else 28
        sheet.append(row)
    sheet.auto_filter.ref = f"A5:O{5 + len(outcomes)}"
    return measures


def build_report(manifest, output, start, end):
    entries = load_entries(manifest)
    output = output.resolve()
    outcomes = reconcile(entries, manifest.resolve().parent, output,
                         datetime.combine(start, time.min), datetime.combine(end, time(23, 59, 59)))
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("车场与通道核对")
    written = write_details(workbook, outcomes, metadata=CHANNEL_METADATA, tally_column="状态")
    measures = write_audit(sheet, outcomes, start, end)
    if written != measures["merged_rows"] or written != sum(measures["status_counts"].values()):
        raise ValueError("通道审计条数与写入明细条数不一致。")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as staging:
        ready = Path(staging) / "report.xlsx"
        workbook.save(ready)
        os.replace(ready, output)
    return measures


def main():
    parser = argparse.ArgumentParser(description="生成按入场通道的车辆进出记录汇总")
    parser.add_argument("manifest", type=Path, help="分车场和通道的进度 JSON")
    parser.add_argument("--output", type=Path, required=True, help="汇总 Excel 路径")
    parser.add_argument("--start", default="2026-10-20", type=date.fromisoformat)
    parser.add_argument("--end", default="2026-11-20", type=date.fromisoformat)
    args = parser.parse_args()
    if args.start > args.end or args.output.suffix.lower() != ".xlsx":
        parser.error("开始日期须不晚于结束日期，且输出文件须使用 .xlsx 后缀。")
    try:
        measures = build_report(args.manifest, args.output, args.start, args.end)
    except (ValueError, OSError) as error:
        sys.exit(f"生成汇总失败。{error}")
    print(json.dumps(measures, ensure_ascii=False))


if __name__ == "__main__":
    main()
