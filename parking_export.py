"""Export a filtered vehicle report through the parking platform's browser UI."""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import urlsplit

from patchright.sync_api import Error as BrowserError
from patchright.sync_api import expect

from parking_scrapling_session import LoginRequiredError, authenticated_session
from parking_workbook import ExportValidationError, validate_export


SITE = "https://s.qcyuns.com/"
REPORT_ROUTE = "/park/parkReportManager/reportExportManager"
CHINA_TIME = timezone(timedelta(hours=8))
WORKSPACE = Path(__file__).resolve().parent
PROFILE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ANPING1" / "ParkingExportBrowser"
REPORT_NAME = re.compile(r"车辆进出记录\d{14}\.xlsx")
REPORT_CHECK_DELAYS = (4, 8, 16, 24, 24)


class ReportCountMismatch(ValueError):
    def __init__(self, expected, reported):
        self.reported = reported
        super().__init__(f"车辆查询显示 {expected} 条，报表任务显示 {reported} 条。")


def site_request(request):
    host = urlsplit(request.url).hostname or ""
    return host == "qcyuns.com" or host.endswith(".qcyuns.com")


class RequestCounts:
    def __init__(self, context):
        self.total = self.peak = self.api = self.api_peak = 0
        self.pending = set()
        self.pending_api = set()
        context.on("request", self.start)
        context.on("requestfinished", self.finish)
        context.on("requestfailed", self.finish)

    def start(self, request):
        if site_request(request):
            self.total += 1
            self.pending.add(request)
            self.peak = max(self.peak, len(self.pending))
            if request.resource_type in ("xhr", "fetch"):
                self.api += 1
                self.pending_api.add(request)
                self.api_peak = max(self.api_peak, len(self.pending_api))

    def finish(self, request):
        self.pending.discard(request)
        self.pending_api.discard(request)


def site_action(page, button, timeout_ms=15_000):
    deadline = monotonic() + timeout_ms / 1_000
    started = set()

    def track(request):
        if request.resource_type in ("xhr", "fetch") and site_request(request):
            started.add(request)

    page.on("request", track)
    try:
        with page.expect_event("requestfinished", predicate=lambda request: request in started, timeout=timeout_ms) as event:
            button.click(timeout=timeout_ms)
    finally:
        page.remove_listener("request", track)
    response = event.value.response()
    if response is None or not response.ok:
        raise ValueError("平台查询或提交请求失败，请检查页面提示。")
    remaining_ms = max(1, int((deadline - monotonic()) * 1_000))
    expect(page.locator(".el-loading-mask:visible")).to_have_count(0, timeout=remaining_ms)


def open_menu(page, parent, child):
    item = page.get_by_role("menu").get_by_role("menuitem", name=child, exact=True).first
    if not item.is_visible():
        page.get_by_role("menuitem", name=re.compile(parent + r"\s*$")).first.click()
    item.click()


def query(page, timeout_ms=15_000):
    site_action(page, page.get_by_role("button", name="查询", exact=True), timeout_ms)


def control(page, label, xpath_test, selector):
    return (
        page.get_by_text(label, exact=True)
        .filter(visible=True)
        .locator(f"xpath=ancestor::*[{xpath_test}][1]")
        .locator(selector)
        .first
    )


def open_select(combobox):
    wrapper = combobox.locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-select__wrapper ')][1]"
    )
    if wrapper.count():
        wrapper.click()
    else:
        combobox.click()


def project_page(page, keyword):
    open_menu(page, "项目管理", "项目管理")
    control(page, "项目/编码", ".//input", "input:visible").fill(keyword)
    query(page)
    candidates = []
    for row in page.locator("tbody tr:visible").all():
        cells = row.locator("td")
        if cells.count() < 3:
            continue
        code, name = (cells.nth(i).inner_text().strip() for i in (1, 2))
        if keyword == code or keyword in name:
            candidates.append((cells.nth(2), name))
    if len(candidates) != 1:
        raise ValueError(f"找到 {len(candidates)} 个匹配项目，请使用唯一的项目名称或编码。")
    cell, park = candidates[0]
    return open_project(page, cell, park), park


def open_project(page, cell, park):
    with page.expect_popup(timeout=20_000) as new_page:
        cell.get_by_text(park, exact=True).click()
    return new_page.value


def set_dates(page, start, end, label="出入场时间"):
    fields = (("请选择开始时间", start), ("请选择结束时间", end))
    for placeholder, value in fields:
        field = page.get_by_placeholder(placeholder, exact=True).filter(visible=True)
        field.fill(value.isoformat(sep=" ", timespec="seconds"))
        field.press("Tab")
    page.get_by_text(label, exact=True).filter(visible=True).click()
    for placeholder, value in fields:
        field = page.get_by_placeholder(placeholder, exact=True).filter(visible=True)
        expect(field).to_have_value(value.isoformat(sep=" ", timespec="seconds"))


def record_count(page):
    query(page)
    total = page.locator(".el-pagination__total:visible").inner_text()
    match = re.search(r"共\s*([\d,]+)\s*条", total)
    if match is None:
        raise ValueError("无法识别车辆进出记录的查询总条数。")
    return int(match[1].replace(",", ""))


def filter_records(page, start, end, status):
    open_menu(page, "记录查询", "车辆进出记录")
    state = page.get_by_text("出入状态", exact=True).filter(visible=True)
    if not state.is_visible():
        page.get_by_role("button", name="展开", exact=True).click()
    set_dates(page, start, end)
    dropdown = control(page, "出入状态", ".//*[@role='combobox']", "[role=combobox]:visible")
    open_select(dropdown)
    page.get_by_role("option", name=status, exact=True).click()
    select = dropdown.locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' el-select ')][1]")
    expect(select).to_contain_text(status)
    return record_count(page)


def report_rows(page, park):
    for row in page.locator("tbody tr:visible").all():
        cells = [value.strip() for value in row.locator("td").all_inner_texts()]
        if len(cells) < 10 or cells[2] != park or cells[4:6] != ["车辆进出记录", "xlsx"]:
            continue
        if cells[1].isdigit() and REPORT_NAME.fullmatch(cells[3]):
            yield row, cells


def open_reports(page):
    open_menu(page, "报表汇总", "报表导出下载")
    page.get_by_text("文件名称", exact=True).filter(visible=True).wait_for(timeout=15_000)
    expect(page.locator(".el-loading-mask:visible")).to_have_count(0, timeout=15_000)


def report_page_is_active(page):
    return (not page.is_closed() and urlsplit(page.url).fragment.split("?")[0] == REPORT_ROUTE
            and page.evaluate("document.visibilityState") == "visible")


def wait_for_report(page, park, job):
    open_reports(page)
    submitted = datetime.fromisoformat(job["submitted"])
    recent_after = submitted - timedelta(minutes=1)
    today = datetime.now(CHINA_TIME).date()
    if recent_after.date() != today:
        start = datetime.combine(recent_after.date(), time.min)
        end = datetime.combine(min(today, submitted.date() + timedelta(days=1)), time(23, 59, 59))
        set_dates(page, start, end, label="时间")
        query(page)
    old_ids = set(job["previous_ids"])
    refreshes = iter(REPORT_CHECK_DELAYS)
    deadline = monotonic() + 120
    while True:
        if not report_page_is_active(page):
            raise ValueError("已离开报表下载页，已停止刷新。")
        recent = []
        for row, cells in report_rows(page, park):
            if cells[1] in old_ids:
                continue
            try:
                created = datetime.fromisoformat(cells[9])
            except ValueError:
                continue
            if created >= recent_after:
                recent.append((row, cells))
        if len(recent) > 1:
            raise ValueError("同一车场出现多份新报表，无法唯一匹配本次任务。")
        if recent:
            row, cells = recent[0]
            if "失败" in cells[7]:
                raise ValueError("平台显示本次报表生成失败。")
            if cells[7] == "已生成":
                count = cells[6].replace(",", "")
                if not count.isdecimal():
                    raise ValueError("报表任务没有提供可核对的数据条数。")
                if int(count) != job["expected"]:
                    raise ReportCountMismatch(job["expected"], int(count))
                return row, cells[3]
        delay = next(refreshes, None)
        if delay is None or monotonic() + delay >= deadline:
            raise ValueError("等待报表生成超时。任务可能仍在后台运行，请在报表下载页查看。")
        page.wait_for_timeout(delay * 1_000)
        if not report_page_is_active(page):
            raise ValueError("已离开报表下载页，已停止刷新。")
        remaining_ms = max(1, int((deadline - monotonic()) * 1_000))
        query(page, min(15_000, remaining_ms))


def export(page, args):
    park_page, park = project_page(page, args.project)
    try:
        return export_park(park_page, park, args)
    finally:
        park_page.close()


def export_park(park_page, park, args, *, on_completed=None, entry_channel=None, query_records=None,
                previous_ids=None, on_queried=None):
    start = datetime.combine(args.start, time.min)
    end = datetime.combine(args.end, time(23, 59, 59))
    parts = [park, str(start), str(end), args.status]
    if entry_channel is not None:
        parts.append(entry_channel)
    scope = json.dumps(parts, ensure_ascii=False)
    key = hashlib.sha256(scope.encode()).hexdigest()
    pending_dir = getattr(args, "pending_dir", PROFILE.parent / "pending-exports")
    pending_job = pending_dir / f"{key}.json"
    if pending_job.exists() and not args.new_export:
        job = json.loads(pending_job.read_text(encoding="utf-8"))
        print("找到尚未完成的导出，继续查询原任务。", flush=True)
    else:
        if previous_ids is None:
            open_reports(park_page)
            previous_ids = [cells[1] for _, cells in report_rows(park_page, park)]
        expected = query_records() if query_records else filter_records(park_page, start, end, args.status)
        if on_queried:
            on_queried(expected)
        description = f"入场通道「{entry_channel}」" if entry_channel is not None else args.status
        print(f"{park}，{description}，平台查询到 {expected} 条。", flush=True)
        if expected == 0:
            if on_completed:
                on_completed(0, None)
            return 0, None
        park_page.get_by_role("button", name=re.compile(r"导出\s*$")).click()
        confirm = park_page.locator(".el-message-box:visible").get_by_role("button", name="确定", exact=True)
        confirm.wait_for(state="visible")
        job = {"previous_ids": previous_ids, "expected": expected,
               "submitted": datetime.now(CHINA_TIME).replace(tzinfo=None, microsecond=0).isoformat()}
        pending_job.parent.mkdir(parents=True, exist_ok=True)
        save_pending(pending_job, job)
        site_action(park_page, confirm)
        expect(confirm).to_be_hidden()

    try:
        row, filename = wait_for_report(park_page, park, job)
    except ReportCountMismatch as mismatch:
        refreshed = query_records() if query_records else filter_records(park_page, start, end, args.status)
        if refreshed != mismatch.reported:
            raise ReportCountMismatch(refreshed, mismatch.reported) from None
        job["expected"] = refreshed
        save_pending(pending_job, job)
        row, filename = wait_for_report(park_page, park, job)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    safe_park = re.sub(r'[<>:"/\\|?*]', "_", park).rstrip(" .")
    if entry_channel is not None:
        safe_channel = re.sub(r'[<>:"/\\|?*]', "_", entry_channel).rstrip(" .") or "通道"
        label = f"入场通道_{safe_channel[:45]}_{key[:8]}"
    else:
        label = args.status
    target = args.output_dir / f"{safe_park}_{label}_{args.start}_{args.end}_{filename}"
    filters = dict(project=park, start=start, end=end, status=args.status, expected_rows=job["expected"])
    if entry_channel is not None:
        filters["entry_channel"] = entry_channel
    if target.exists():
        result = validate_export(target, **filters)
    else:
        with park_page.expect_download(timeout=45_000) as event:
            row.get_by_text("下载", exact=True).click()
        with TemporaryDirectory(dir=args.output_dir) as temporary:
            pending = Path(temporary) / filename
            event.value.save_as(pending)
            result = validate_export(pending, **filters)
            pending.rename(target)
    if on_completed:
        on_completed(result.row_count, target)
    pending_job.unlink()
    print(f"已核对 {result.row_count} 条，{result.min_timestamp} 至 {result.max_timestamp}。")
    print(f"Excel 已保存到 {target}")
    return result.row_count, target


def save_pending(path, job):
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps(job), encoding="utf-8")
    staging.replace(path)


def main():
    parser = argparse.ArgumentParser(description="从利民停车管理平台下载并核对车辆进出记录 Excel")
    parser.add_argument("--project", default="北运河西地铁", help="项目名称关键词或编码")
    parser.add_argument("--start", type=date.fromisoformat, default="2026-10-20", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, default="2026-11-20", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--status", choices=("入场", "出场"), default="出场", help="出入状态")
    parser.add_argument("--output-dir", type=Path, default=WORKSPACE / "exports", help="本地 Excel 保存目录")
    parser.add_argument("--new-export", action="store_true", help="确认平台已无可用旧任务后，重新提交导出")
    args = parser.parse_args()
    args.project = args.project.strip()
    args.output_dir = args.output_dir.resolve()
    if not args.project or args.start > args.end:
        parser.error("请输入项目关键词或编码，并确保开始日期不晚于结束日期。")

    with authenticated_session(PROFILE) as (context, page):
        context.set_default_timeout(12_000)
        counts = RequestCounts(context)
        started = monotonic()
        try:
            export(page, args)
        finally:
            print(f"平台请求 {counts.total} 次，峰值并发 {counts.peak}；"
                  f"其中接口请求 {counts.api} 次，峰值并发 {counts.api_peak}；"
                  f"查询与导出耗时 {monotonic() - started:.1f} 秒。")


if __name__ == "__main__":
    try:
        main()
    except ExportValidationError as error:
        sys.exit(f"Excel 核对未通过：{error}")
    except (AssertionError, BrowserError, LoginRequiredError, OSError, ValueError) as error:
        sys.exit(f"导出未完成：{error}")
