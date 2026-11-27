"""Export all selectable entry channels of each roster park in Scrapling."""

import argparse
import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from time import monotonic

from patchright.sync_api import Error as BrowserError
from patchright.sync_api import TimeoutError as BrowserTimeout
from patchright.sync_api import expect

from parking_batch import (
    DEFAULT_END, DEFAULT_START, ROSTER, current_projects, exclusive_output,
    read_roster, resolve_roster, save_manifest,
)
from parking_batch_match import match_catalog
from parking_channel_report import build_report
from parking_export import (
    PROFILE, WORKSPACE, RequestCounts, control, export_park, open_menu,
    open_project, open_reports, open_select, project_page, record_count,
    report_page_is_active, report_rows, set_dates,
)
from parking_scrapling_session import LoginRequiredError, authenticated_session


ENTRY_FILTER = "入场通道"
ENTRY_HINT = "通过入场通道筛选"
STATUS_HINT = "请选择出入状态"
COMPLETE = {"exported", "empty"}
TASK_ERRORS = (AssertionError, BrowserError, OSError, ValueError)
PRIOR_MATCHES = (WORKSPACE / "exports" / f"批量_{DEFAULT_START}至{DEFAULT_END}_出场_更新名单" /
                 "查找与导出进度.json")


def scope(args):
    return {"start": args.start.isoformat(), "end": args.end.isoformat(),
            "status": None, "entry_channel": "each_selectable"}


def load_progress(manifest, names, args):
    if not manifest.is_file():
        return match_catalog(names, None)
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    if saved.get("scope") != scope(args) or [entry["source_name"] for entry in saved["entries"]] != names:
        raise ValueError("已有进度的日期、筛选方式或车场名单与本次不同，请使用另一个输出目录。")
    return saved["entries"]


def combine_progress(resolved, previous):
    for entry, old in zip(resolved, previous, strict=True):
        if (entry["state"] == "matched" and entry["project_code"] == old["project_code"]
                and entry["matched_name"] == old["matched_name"]):
            entry["channels"] = old.get("channels", [])
            if old["state"] in COMPLETE and entry["channels"] and all(
                    item["state"] in COMPLETE for item in entry["channels"]):
                entry["state"] = old["state"]
    return resolved


def known_project_aliases():
    if not PRIOR_MATCHES.is_file():
        return {}
    entries = json.loads(PRIOR_MATCHES.read_text(encoding="utf-8"))["entries"]
    return {entry["source_name"]: entry for entry in entries
            if entry["state"] == "exported" and entry.get("match_method") in {"keyword", "suffix"}}


def first_error(error):
    return next((line.strip() for line in str(error).splitlines() if line.strip()), "平台页面未完成操作。")


def save_metrics(folder, metrics):
    path = folder / "请求统计.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    runs = previous.get("runs", [previous] if previous else [])
    runs.append(metrics)
    total = {
        key: round(sum(run[key] for run in runs), 1)
        for key in ("http", "api", "elapsed_seconds")
    }
    total.update({key: max(run[key] for run in runs) for key in ("http_peak", "api_peak")})
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps({"runs": runs, "total": total}, ensure_ascii=False, indent=2), encoding="utf-8")
    staging.replace(path)


def channel_control(page):
    return control(page, ENTRY_FILTER, ".//*[@role='combobox']", "[role=combobox]:visible")


def selected_text(combobox):
    select = combobox.locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-select ')][1]"
    )
    return select.inner_text().strip()


def open_record_filters(page, start, end):
    time_label = page.get_by_text("出入场时间", exact=True).filter(visible=True)
    if not time_label.is_visible():
        open_menu(page, "记录查询", "车辆进出记录")
        time_label.wait_for(state="visible", timeout=12_000)
    status = page.get_by_text(STATUS_HINT, exact=True).filter(visible=True)
    expand = page.get_by_role("button", name="展开", exact=True)
    if expand.is_visible() and not status.is_visible():
        expand.click()
    status.wait_for(state="visible", timeout=12_000)
    dates = (page.get_by_placeholder(text, exact=True).filter(visible=True)
             for text in ("请选择开始时间", "请选择结束时间"))
    actual = tuple(date_field.input_value() for date_field in dates)
    wanted = tuple(value.isoformat(sep=" ", timespec="seconds") for value in (start, end))
    if actual != wanted:
        set_dates(page, start, end)
    return channel_control(page)


def channel_dropdown(page, combo):
    open_select(combo)
    expect(combo).to_have_attribute("aria-expanded", "true")
    box_id = combo.get_attribute("aria-controls")
    if not box_id:
        raise ValueError("无法定位入场通道下拉框。")
    popup = page.locator(f'[id="{box_id}"]').locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-select-dropdown ')][1]"
    )
    popup.wait_for(state="visible", timeout=8_000)
    return popup


def read_channels(page, combo):
    if selected_text(combo) != ENTRY_HINT:
        raise ValueError("车场记录页打开时已选入场通道，无法核对完整选项。")
    popup = channel_dropdown(page, combo)
    try:
        options = popup.get_by_role("option")
        empty = popup.locator(".el-select-dropdown__empty")
        options.first.or_(empty).filter(visible=True).first.wait_for(state="visible", timeout=8_000)
        # The select renders its empty state before the park's lane list arrives.
        if not options.first.is_visible():
            try:
                options.first.wait_for(state="visible", timeout=3_000)
            except BrowserTimeout:
                pass
        values = [value.strip() for value in options.filter(visible=True).all_inner_texts()]
        disabled = [option.inner_text().strip() for option in options.filter(visible=True).all()
                    if option.is_disabled()]
        if any(not value for value in values) or len(set(values)) != len(values):
            raise ValueError("平台返回空白或同名的入场通道选项，无法按名称唯一核对。")
        return [item for item in values if item not in disabled], disabled
    finally:
        page.get_by_text("出入场时间", exact=True).filter(visible=True).click()


def query_channel(page, start, end, channel):
    combo = open_record_filters(page, start, end)
    if selected_text(combo) != channel:
        popup = channel_dropdown(page, combo)
        popup.get_by_role("option", name=channel, exact=True).click()
    expect(page.get_by_text(STATUS_HINT, exact=True).filter(visible=True)).to_be_visible()
    if selected_text(combo) != channel:
        raise ValueError(f"平台未选中入场通道「{channel}」。")
    return record_count(page)


def open_park(home, entry):
    park, code = entry["matched_name"], entry["project_code"]
    if (park, code) in current_projects(home):
        row = home.locator("tbody tr:visible").filter(has=home.get_by_text(code, exact=True))
        return open_project(home, row.locator("td").nth(2), park)
    popup, actual = project_page(home, code)
    if actual != park:
        popup.close()
        raise ValueError("项目编码打开的车场名称与目录不符。")
    return popup


def process_park(page, entry, args, checkpoint):
    park = entry["matched_name"]
    start = datetime.combine(args.start, time.min)
    end = datetime.combine(args.end, time(23, 59, 59))
    open_reports(page)
    previous_ids = [cells[1] for _, cells in report_rows(page, park)]
    combo = open_record_filters(page, start, end)
    options, disabled = read_channels(page, combo)
    if not options and not disabled:
        baseline = record_count(page)
        options, disabled = read_channels(page, combo)
        if not options:
            entry["unfiltered_rows"] = baseline
    print(f"{park}，发现 {len(options)} 个可选入场通道。", flush=True)
    keep = {item["channel"]: item for item in entry.get("channels", [])}
    if set(keep) - set(options):
        raise ValueError("已保存的入场通道与平台当前选项不同，请核对通道变更。")
    entry["channels"] = [keep.get(channel, {"channel": channel, "state": "pending"}) for channel in options]
    if not options:
        entry.update(state="no_channels", note="平台没有显示可选的入场通道。"
                     + (f"未选通道时查询到 {entry['unfiltered_rows']} 条。" if "unfiltered_rows" in entry else "")
                     + ("不可选通道：" + "、".join(disabled) if disabled else ""))
        checkpoint()
        return
    entry["note"] = "不可选通道：" + "、".join(disabled) if disabled else ""
    checkpoint()
    for item in entry["channels"]:
        if item["state"] in COMPLETE:
            continue
        channel = item["channel"]

        def record_query(expected):
            item.update(expected_rows=expected, state="pending", note="")
            checkpoint()

        def completed(count, file):
            item.update(state="exported" if file else "empty", expected_rows=count,
                        file=str(file) if file else "", note="")
            checkpoint()

        try:
            export_park(page, park, args, entry_channel=channel,
                        query_records=lambda: query_channel(page, start, end, channel),
                        previous_ids=previous_ids, on_queried=record_query, on_completed=completed)
        except TASK_ERRORS as error:
            item.update(state="export_failed", note=first_error(error))
            checkpoint()
            raise
        if report_page_is_active(page):
            previous_ids = [cells[1] for _, cells in report_rows(page, park)]
    entry["state"] = "exported" if any(item["state"] == "exported" for item in entry["channels"]) else "empty"
    checkpoint()


def main():
    parser = argparse.ArgumentParser(description="逐入口通道导出车辆进出记录，出入状态保留未选择。")
    parser.add_argument("--list", type=Path, default=ROSTER)
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--profile", type=Path, default=PROFILE)
    parser.add_argument("--manual-login-once", action="store_true")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("开始日期不能晚于结束日期。")
    args.status = None
    args.new_export = False
    folder = (args.output_dir or WORKSPACE / "exports" /
              f"批量_{args.start}至{args.end}_逐入场通道_不限出入状态").resolve()
    args.output_dir = folder / "原始报表"
    args.pending_dir = folder / ".pending"
    manifest = folder / "入场通道导出进度.json"
    summary = folder / "车场与入场通道导出汇总.xlsx"
    names = read_roster(args.list)
    with exclusive_output(folder):
        entries = load_progress(manifest, names, args)

        def checkpoint():
            save_manifest(manifest, entries, scope(args))

        checkpoint()
        failed = False
        try:
            with authenticated_session(args.profile, manual_login_once=args.manual_login_once) as (context, home):
                counts = RequestCounts(context)
                started = monotonic()
                print("后台登录会话已就绪，读取车场目录。", flush=True)
                try:
                    resolved = resolve_roster(home, names, known_aliases=known_project_aliases())
                    entries = combine_progress(resolved, entries)
                    checkpoint()
                    consecutive_failures = 0
                    for entry in entries:
                        if entry["state"] != "matched":
                            continue
                        page = None
                        try:
                            page = open_park(home, entry)
                            process_park(page, entry, args, checkpoint)
                            consecutive_failures = 0
                        except TASK_ERRORS as error:
                            entry.update(state="export_failed", note=first_error(error))
                            print(f"{entry['source_name']}：{entry['note']}", file=sys.stderr, flush=True)
                            checkpoint()
                            consecutive_failures += 1
                        finally:
                            if page is not None and not page.is_closed():
                                page.close()
                        if consecutive_failures >= 2:
                            raise ValueError("连续两个车场导出失败，已保存核验进度。")
                finally:
                    metrics = {"http": counts.total, "http_peak": counts.peak, "api": counts.api,
                               "api_peak": counts.api_peak, "elapsed_seconds": round(monotonic() - started, 1)}
                    save_metrics(folder, metrics)
                    print(json.dumps(metrics, ensure_ascii=False), flush=True)
        except (LoginRequiredError, *TASK_ERRORS) as error:
            print(f"后台任务暂未完成：{first_error(error)}", file=sys.stderr, flush=True)
            failed = True
        measures = build_report(manifest, summary, args.start, args.end)
        print(json.dumps(measures, ensure_ascii=False), flush=True)
        print(f"汇总文件 {summary}", flush=True)
        unfinished = failed or measures["failed_channels"] or any(entry["state"] not in COMPLETE for entry in entries)
        return 2 if unfinished else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (LoginRequiredError, *TASK_ERRORS) as error:
        sys.exit(f"无法完成：{first_error(error)}")
