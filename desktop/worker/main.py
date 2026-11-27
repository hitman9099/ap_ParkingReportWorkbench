"""JSON-lines sidecar for the Windows parking ledger client."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stdout
from copy import deepcopy
from datetime import date, datetime, time
from errno import ENOENT, ENOSPC
from itertools import count
import json
from pathlib import Path
from queue import Empty, Queue
import re
import sys
from tempfile import TemporaryDirectory
from threading import Lock, get_ident, local
from time import monotonic
from types import SimpleNamespace
from unicodedata import normalize
from zipfile import BadZipFile

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from patchright.sync_api import Error as BrowserError
from patchright.sync_api import TimeoutError as BrowserTimeout

from parking_batch import exclusive_output, resolve_roster, save_manifest
from parking_channel_batch import COMPLETE, TASK_ERRORS, combine_progress, load_progress, open_park, process_park, scope
from parking_channel_report import FINAL_CHANNELS, HEADERS, KNOWN_CHANNEL_STATES, build_report, reconcile
from parking_export import RequestCounts
from parking_scrapling_session import LOCAL_SESSION_HANDOFF, LoginRequiredError
from session import (
    capture_interactive,
    capture_session,
    find_browser,
    load_snapshot,
    open_session,
    require_verified,
    save_snapshot,
)

ALIASES = (
    ("春和景明小区北侧", "春和景明北停车场", "108924", "春和景明"),
    ("苏坨花海北侧", "苏坨北花海停车场", "106959", "苏坨"),
    ("南大街2号停车场（大汉写字楼）", "南大街2号停车场", "96499", "南大街2号"),
    ("南大街1号停车场（煤炭）", "南大街1号停车场", "94215", "南大街1号"),
    ("BOBO自由城西北三角地", "BOBO自由城西北角停车场", "99125", "自由城"),
    ("环北派对2号", "环北派对2号停车场", "99289", "环北派对2号"),
    ("环北花园派对2号停车场", "环北派对2号停车场", "99289", "环北派对2号"),
    ("焦王庄消防站南", "焦王庄消防站南停车场", "112975", "焦王庄消防站南"),
    ("焦王庄消防站北", "焦王庄消防站北停车场", "112966", "焦王庄消防站北"),
    ("滨惠南三街停车场", "滨惠南三街路外停车场", "113757", "滨惠南三街"),
    ("北京市通州区青少年活动中心停车场", "通州区青少年活动中心停车场", "112825", "青少年活动中心"),
)
CONFIRMED_ISSUES = {"missing", "ambiguous", "no_channels", "export_failed", "validation_failed"}
RUN_ERRORS = (*TASK_ERRORS, BadZipFile, LoginRequiredError, TypeError, KeyError)
SECRET_FIELD = re.compile(
    r"(?i)\b(?:authorization|(?:set[-_ ]?)?cookie|(?:access[-_ ]?|refresh[-_ ]?)?token|"
    r"password|passwd|(?:api[-_ ]?)?secret|credentials?)\b[\"']?\s*[:=]|\bbearer\s+\S+"
)


class Cancelled(RuntimeError):
    pass


class Events:
    def __init__(self):
        self.stream = sys.stdout
        self.lock = Lock()
        self.last_issues = None

    def send(self, event_type, **payload):
        with self.lock:
            if event_type == "issues":
                if self.last_issues == payload["issues"]:
                    return
                self.last_issues = payload["issues"]
            print(json.dumps({"type": event_type, **payload}, ensure_ascii=False), file=self.stream, flush=True)


class PlatformLogs:
    def __init__(self, events):
        self.events = events
        self.pending = local()

    def write(self, text):
        lines = (getattr(self.pending, "text", "") + text).split("\n")
        self.pending.text = lines.pop()
        for line in lines:
            if line.strip():
                self.events.send("log", level="info", message=clean_log(line))
        return len(text)

    def flush(self):
        if message := getattr(self.pending, "text", "").strip():
            self.events.send("log", level="info", message=clean_log(message))
        self.pending.text = ""


class CombinedCounts(RequestCounts):
    def __init__(self):
        self.lock = Lock()
        self.total = self.peak = self.api = self.api_peak = 0
        self.pending = set()
        self.pending_api = set()

    def attach(self, context):
        pending = set()

        def start(request):
            if request.url == LOCAL_SESSION_HANDOFF:
                return
            with self.lock:
                self.start(request)
                if request in self.pending:
                    pending.add(request)

        def finish(request):
            with self.lock:
                self.finish(request)
                pending.discard(request)

        def close(*_):
            with self.lock:
                self.pending.difference_update(pending)
                self.pending_api.difference_update(pending)
                pending.clear()

        context.on("request", start)
        context.on("requestfinished", finish)
        context.on("requestfailed", finish)
        context.on("close", close)


class ParkActivity:
    def __init__(self):
        self.lock = Lock()
        self.workers = set()
        self.active = 0
        self.peak = 0

    @contextmanager
    def park(self):
        with self.lock:
            self.workers.add(get_ident())
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1


def clean_log(message):
    if SECRET_FIELD.search(message):
        return "平台返回了会话信息，无法展示具体内容。"
    return re.sub(r"https?://\S+", "[平台链接]", message.strip())[:900]


def clean_reason(message, fallback="任务未能完成。"):
    line = next((line.strip() for line in message.splitlines() if line.strip()), "")
    if not line or line.startswith(("{", "[")) or SECRET_FIELD.search(line):
        return fallback
    line = re.sub(r"(?i)(?:\b[a-z]:[\\/]|\\\\)[^\"'<>|，。；]*", "[本地路径]", line)
    return clean_log(line)


def reason(error):
    if isinstance(error, BrowserTimeout):
        return "等待平台页面响应超时，未能完成当前操作。"
    if isinstance(error, BrowserError):
        detail = str(error).lower()
        if "closed" in detail:
            return "浏览器窗口或平台页面已关闭，当前操作中断。"
        if "err_" in detail:
            return "浏览器未能连接停车平台，请检查网络或平台状态。"
        return "浏览器未能完成平台页面操作。"
    if isinstance(error, AssertionError):
        return "页面当前显示的内容未通过核验。"
    if isinstance(error, BadZipFile):
        return "文件不是有效的 Excel 工作簿，或文件已损坏。"
    if isinstance(error, json.JSONDecodeError):
        return "任务参数或进度记录的格式无效。"
    if isinstance(error, (KeyError, TypeError)):
        return "任务参数、平台数据或进度记录的内容不完整。"
    if isinstance(error, OSError):
        if getattr(error, "winerror", None) in {32, 33}:
            return "文件正被其他程序使用，无法读取或保存。"
        if isinstance(error, PermissionError):
            return "读取或保存文件时权限不足，请检查文件和目录权限。"
        if error.errno == ENOENT:
            return "找不到所需的本地文件或目录。"
        if error.errno == ENOSPC:
            return "输出目录所在磁盘空间不足。"
        return "读取或保存本地文件失败。"
    return clean_reason(str(error))


def alias_key(name):
    return re.sub(r"\s+", "", normalize("NFKC", name)).casefold()


def project_aliases(names):
    known = {alias_key(source): {"matched_name": matched, "project_code": code, "match_method": "keyword", "query": query}
             for source, matched, code, query in ALIASES}
    return {name: known[key] for name in names if (key := alias_key(name)) in known}


def verify(events, profile, browser, interactive):
    profile.mkdir(parents=True, exist_ok=True)
    with exclusive_output(profile):
        events.send("auth", status="verifying", message="正在验证浏览器登录。")
        snapshot = load_snapshot(profile)
        try:
            with open_session(profile, browser, snapshot) as (context, page):
                require_verified(page)
                save_snapshot(profile, capture_session(context, page))
        except LoginRequiredError as error:
            if not interactive:
                events.send("auth", status="needs_login", message=reason(error))
                return 3
            events.send("auth", status="verifying", message="请在弹出的浏览器中输入账号密码并完成人工验证。")
            snapshot = capture_interactive(profile, browser)
            with open_session(profile, browser, snapshot) as (_, page):
                require_verified(page)
            save_snapshot(profile, snapshot)
        events.send("auth", status="verified", message="浏览器账号验证通过。")
        return 0


def load_request(path):
    content = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(content, dict):
        raise ValueError("导出请求格式不正确。")
    try:
        start, end = (date.fromisoformat(content[key]) for key in ("start", "end"))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("请选择有效的起止日期。") from error
    if start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    workers = content.get("workers", 1)
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("并行车场数须为 1 至 4。")
    ledger = Path(content["ledger"]).resolve(strict=True)
    if not ledger.is_file() or ledger.suffix.lower() != ".xlsx":
        raise ValueError("请选择 .xlsx 台账。")
    folder = Path(content["outputDir"]).resolve(strict=True)
    if not folder.is_dir():
        raise ValueError("输出文件夹不存在。")
    profile = Path(content["profile"]).resolve()
    cancel = Path(content.get("cancelFile") or (folder / ".cancel")).resolve()
    if cancel.parent != folder:
        raise ValueError("取消标记必须在当前任务文件夹。")
    return ledger, folder, profile, start, end, workers, find_browser(content.get("browser")), cancel


def check_cancelled(marker):
    if marker.is_file():
        raise Cancelled("导出已取消。已保存的数据可用于续跑。")


def manifest_counts(entries):
    channels = [lane for entry in entries for lane in entry.get("channels", []) if lane["state"] in COMPLETE]
    return dict(completed=sum(entry["state"] in COMPLETE for entry in entries), total=len(entries),
                records=sum(lane.get("expected_rows", 0) for lane in channels), channels=len(channels))


def manifest_issues(entries, *, confirmed_only=False):
    issues = []
    for entry in entries:
        state = entry["state"]
        if state in COMPLETE or (confirmed_only and state not in CONFIRMED_ISSUES):
            continue
        detail = {
            "matched": "已匹配平台项目，尚未完成导出。",
            "missing": "当前账号的项目目录中未找到可唯一确认的项目。",
            "ambiguous": "项目匹配待确认，请核对地块名称和项目编码。",
            "no_channels": "平台未提供可选的入场通道。",
            "unattempted": "尚未检查项目目录。",
            "validation_failed": "车场报表未通过通道汇总核验。",
        }.get(state, "该车场尚未完成导出。")
        if entry.get("note") and state not in {"matched", "unattempted"}:
            detail = clean_reason(entry["note"], detail)
        lane_reasons = []
        for lane in entry.get("channels", []):
            if lane["state"] in {"export_failed", "validation_failed"}:
                lane_reason = clean_reason(lane.get("note") or "", detail)
                lane_reasons.append(f"{lane['channel']}：{lane_reason}")
        if lane_reasons:
            detail = "；".join(lane_reasons)
        issues.append({"name": entry["source_name"], "state": state, "reason": detail})
    return issues


def merge_progress(resolved, previous, args):
    merged = combine_progress(resolved, previous)
    claimed = {entry["project_code"] for entry in merged if entry["state"] in COMPLETE | {"matched"}}
    raw_folder = args.output_dir.resolve()
    run_folder = raw_folder.parent
    start = datetime.combine(args.start, time.min)
    end = datetime.combine(args.end, time(23, 59, 59))
    for index, (current, old) in enumerate(zip(merged, previous, strict=True)):
        channels = old.get("channels", [])
        code = old["project_code"]
        if current["state"] not in {"missing", "ambiguous"} or old["state"] not in COMPLETE or not code or code in claimed:
            continue
        if not channels or any(lane["state"] not in COMPLETE or
                               (lane.get("file") and not (run_folder / lane["file"]).resolve().is_relative_to(raw_folder))
                               for lane in channels):
            continue
        outcomes = reconcile([old], run_folder, run_folder / "车场与入场通道导出汇总.xlsx", start, end)
        if len(outcomes) == len(channels) and all(outcome["state"] in FINAL_CHANNELS for outcome in outcomes):
            merged[index] = deepcopy(old)
            claimed.add(code)
    return merged


def open_park_with_retry(events, page, entry, marker):
    try:
        return open_park(page, entry)
    except (BrowserTimeout, ValueError) as error:
        empty_lookup = str(error) == "找到 0 个匹配项目，请使用唯一的项目名称或编码。"
        if (not isinstance(error, BrowserTimeout) and not empty_lookup) or page.is_closed():
            raise
    check_cancelled(marker)
    events.send("log", level="warning", message=f"{entry['source_name']} · 首次未能打开项目，正在按已确认编码重试。")
    return open_park(page, entry)


def process_queue(events, queue, entries, args, manifest, page, marker, manifest_lock, activity=None):
    consecutive_errors = 0
    while not marker.is_file():
        try:
            index, entry = queue.get_nowait()
        except Empty:
            return
        popup = None

        def checkpoint(phase="export"):
            with manifest_lock:
                previous_state = entries[index]["state"]
                entries[index] = deepcopy(entry)
                save_manifest(manifest, entries, scope(args))
                if previous_state != entry["state"] and {previous_state, entry["state"]} & CONFIRMED_ISSUES:
                    events.send("issues", issues=manifest_issues(entries, confirmed_only=True))
                events.send("progress", phase=phase, current=entry["source_name"],
                            message="正在核对通道导出。" if phase == "export" else "正在查找车场。",
                            **manifest_counts(entries))
            check_cancelled(marker)

        with activity.park() if activity else nullcontext():
            try:
                checkpoint("search")
                popup = open_park_with_retry(events, page, entry, marker)
                process_park(popup, entry, args, checkpoint)
                consecutive_errors = 0
            except TASK_ERRORS as error:
                detail = reason(error)
                original_note = next((line.strip() for line in str(error).splitlines() if line.strip()), "")
                for lane in entry.get("channels", []):
                    if lane["state"] == "export_failed" and lane.get("note") == original_note:
                        lane["note"] = detail
                entry.update(state="export_failed", note=detail)
                events.send("log", level="warning", message=f"{entry['source_name']} · {entry['note']}")
                checkpoint()
                consecutive_errors += 1
            finally:
                if popup is not None and not popup.is_closed():
                    try:
                        popup.close()
                    except BrowserError:
                        pass
        if consecutive_errors >= 2:
            events.send("log", level="warning", message="有两处连续导出失败，已保存当前进度。")
            return


def export_with_sessions(events, profile, browser, entries, args, manifest, workers, marker):
    check_cancelled(marker)
    started = monotonic()
    counts = CombinedCounts()
    activity = ParkActivity()
    snapshot = load_snapshot(profile)
    events.send("progress", phase="catalog", current="", message="正在核对平台项目目录。",
                **manifest_counts(entries))
    with exclusive_output(profile), open_session(profile, browser, snapshot, on_context=counts.attach) as (context, page):
        require_verified(page)
        snapshot = capture_session(context, page)
        save_snapshot(profile, snapshot)
        names = [entry["source_name"] for entry in entries]
        resolved = resolve_roster(page, names, known_aliases=project_aliases(names))
        entries[:] = merge_progress(resolved, entries, args)
        save_manifest(manifest, entries, scope(args))
        events.send("issues", issues=manifest_issues(entries, confirmed_only=True))
        queue = Queue()
        for index, entry in enumerate(entries):
            if entry["state"] == "matched":
                queue.put((index, deepcopy(entry)))
            elif entry["state"] not in COMPLETE:
                events.send("log", level="warning", message=f"{entry['source_name']} · {entry['note']}")
        worker_count = min(queue.qsize(), workers)
        manifest_lock = Lock()
        startup_lock = Lock()

        def extra_worker():
            with ExitStack() as session:
                with startup_lock:
                    if marker.is_file() or queue.empty():
                        return
                    try:
                        temp = session.enter_context(TemporaryDirectory(
                            prefix="export-worker-", dir=profile, ignore_cleanup_errors=True))
                        _, other_page = session.enter_context(open_session(
                            Path(temp), browser, snapshot, on_context=counts.attach))
                        require_verified(other_page)
                    except (LoginRequiredError, BrowserError, OSError) as error:
                        events.send("log", level="warning", message=f"一个并行车场会话未能启动。{reason(error)}")
                        return
                process_queue(events, queue, entries, args, manifest, other_page, marker, manifest_lock, activity)

        def first_worker():
            process_queue(events, queue, entries, args, manifest, page, marker, manifest_lock, activity)

        try:
            if worker_count > 1:
                with ThreadPoolExecutor(max_workers=worker_count - 1, thread_name_prefix="Parking") as executor:
                    extra = [executor.submit(extra_worker) for _ in range(worker_count - 1)]
                    first_worker()
                    for worker in extra:
                        worker.result()
            else:
                first_worker()
        except Cancelled:
            pass
    return {"http": counts.total, "httpPeak": counts.peak, "api": counts.api, "apiPeak": counts.api_peak,
            "elapsedSeconds": round(monotonic() - started, 1), "workersRequested": workers,
            "workers": len(activity.workers), "parksPeak": activity.peak}


def next_result_paths(folder, source):
    for revision in count(1):
        suffix = "" if revision == 1 else "_续跑" if revision == 2 else f"_续跑_{revision - 1:02}"
        report = folder / f"车场与入场通道导出汇总{suffix}.xlsx"
        ledger = folder / f"{source.stem}_时长汇总{suffix}.xlsx"
        metrics = folder / f"请求统计{suffix}.json"
        if not any(path.exists() for path in (report, ledger, metrics)):
            return report, ledger, metrics


def write_atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def publish_result(events, folder, status, result, *, manifest_ready):
    result.update(status=status, resumable=manifest_ready and status != "complete")
    try:
        write_atomic_json(folder / "任务结果.json", {"type": "result", **result})
    except OSError as error:
        detail = reason(error)
        result["issues"].append({"name": "任务结果记录", "state": "task_failed", "reason": detail})
        if status == "complete":
            result.update(status="partial", message=f"报表和台账已保存，任务结果记录写入失败。{detail}")
    events.send("result", **result)
    return 2 if result["status"] == "error" else 0


def mark_validation_failures(report, entries):
    """Read the audit tab only on failures, without revalidating all raw reports."""
    from openpyxl import load_workbook

    book = load_workbook(report, read_only=True, data_only=True)
    failed = defaultdict(dict)
    try:
        sheet = book["车场与通道核对"]
        sheet.reset_dimensions()
        for values in sheet.iter_rows(min_row=6, max_col=len(HEADERS), values_only=True):
            row = dict(zip(HEADERS, values))
            channel = row["查询入场通道"]
            state = row["通道处理结果"]
            if channel and state and state not in KNOWN_CHANNEL_STATES:
                failed[row["名单车场"]][channel] = clean_reason(row["备注"] or "", state)
    finally:
        book.close()
    changed = False
    for entry in entries:
        for lane in entry.get("channels", []):
            note = failed[entry["source_name"]].get(lane["channel"])
            if note and lane["state"] in COMPLETE:
                lane.update(state="validation_failed", note=note)
                if entry["state"] in COMPLETE:
                    entry.update(state="validation_failed", note="至少一个入场通道未通过汇总核验。")
                changed = True
    return changed


def finish_message(status, result, run_issue):
    if status == "cancelled":
        return "任务已停止，已下载的报表仍可在任务目录查看。"
    if run_issue:
        return f"{run_issue['name']}失败。{run_issue['reason']}"
    if status == "complete":
        return "所有车场和通道的导出与台账核验已完成。"
    if status == "error":
        return "本次导出未取得可核验的车场报表，原因见下方问题明细。"
    remaining = result["total"] - result["completed"]
    return (f"本次执行已完成，其中 {remaining} 个车场有未完成项。"
            if remaining else "本次执行已完成，请查看下方问题明细。")


def run(events, request_path):
    from ledger import read_ledger, write_ledger

    source, folder, profile, start, end, workers, browser, marker = load_request(request_path)
    run_started = monotonic()
    args = SimpleNamespace(start=start, end=end, status=None, new_export=False,
                           output_dir=folder / "原始报表", pending_dir=folder / ".pending")
    manifest = folder / "入场通道导出进度.json"
    with exclusive_output(folder):
        entries = []
        result = dict(summaryPath="", ledgerPath="", metrics={})
        manifest_ready = False
        measures = None
        metrics_path = None
        run_issues = []
        status = "partial"
        stage, issue_state = "读取台账", "task_failed"
        try:
            names = list(read_ledger(source).names)
            if not names:
                raise ValueError("台账未提供可查询的地块名称。")
            stage = "载入任务进度"
            entries = load_progress(manifest, names, args)
            save_manifest(manifest, entries, scope(args))
            manifest_ready = True
            stage, issue_state = "平台导出", "export_failed"
            try:
                result["metrics"] = export_with_sessions(events, profile, browser, entries, args, manifest, workers, marker)
            except Cancelled:
                pass
            except TASK_ERRORS as error:
                counts = manifest_counts(entries)
                if not counts["completed"] and not counts["channels"]:
                    raise
                run_issues.append({"name": stage, "state": issue_state, "reason": reason(error)})
                events.send("log", level="error", message=finish_message("partial", result, run_issues[-1]))
            stage, issue_state = "通道汇总", "report_failed"
            report, ledger_path, metrics_path = next_result_paths(folder, source)
            events.send("progress", phase="summary", current="", message="正在生成和核验 Excel，保留已完成记录。",
                        **manifest_counts(entries))
            measures = build_report(manifest, report, start, end)
            result["summaryPath"] = str(report)
            if measures["failed_channels"]:
                if mark_validation_failures(report, entries):
                    save_manifest(manifest, entries, scope(args))
                if not any(entry["state"] in {"export_failed", "validation_failed"} for entry in entries):
                    run_issues.append({"name": "报表核验", "state": "validation_failed",
                                       "reason": f"有 {measures['failed_channels']} 个通道未通过核验，详见通道汇总。"})
            if marker.is_file():
                status = "cancelled"
            else:
                stage, issue_state = "台账生成", "ledger_failed"
                summary = write_ledger(source, report, ledger_path, start, end)
                result.update(ledgerPath=str(ledger_path), **({"ledgerSummary": summary} if isinstance(summary, dict) else {}))
                if not run_issues and not measures["failed_channels"] and all(entry["state"] in COMPLETE for entry in entries):
                    status = "complete"
                elif not measures["validated_channels"] and all(entry["state"] not in COMPLETE for entry in entries) and any(
                    entry["state"] in {"export_failed", "validation_failed"} for entry in entries
                ):
                    status = "error"
        except LoginRequiredError as error:
            detail = reason(error)
            events.send("auth", status="needs_login", message=detail)
            run_issues.append({"name": "浏览器登录", "state": "auth_failed", "reason": detail})
            status = "error"
        except RUN_ERRORS as error:
            run_issues.append({"name": stage, "state": issue_state, "reason": reason(error)})
            events.send("log", level="error", message=finish_message("error", result, run_issues[-1]))
            status = "partial" if stage == "台账生成" and result["summaryPath"] else "error"
        if result["metrics"] and metrics_path is not None:
            result["metrics"]["totalElapsedSeconds"] = round(monotonic() - run_started, 1)
            try:
                write_atomic_json(metrics_path, result["metrics"])
            except OSError as error:
                detail = reason(error)
                run_issues.append({"name": "请求统计", "state": "task_failed", "reason": detail})
                events.send("log", level="warning", message=f"请求统计未能保存。{detail}")
                if status == "complete":
                    status = "partial"
        result.update(manifest_counts(entries))
        if measures is not None:
            result.update(records=measures["merged_rows"], channels=measures["validated_channels"])
        result["issues"] = manifest_issues(entries) + run_issues
        result["message"] = finish_message(status, result, run_issues[-1] if run_issues else None)
        return publish_result(events, folder, status, result, manifest_ready=manifest_ready)


def main():
    for encoding_stream in (sys.stdout, sys.stderr):
        if hasattr(encoding_stream, "reconfigure"):
            encoding_stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="停车报表导出和台账汇总")
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("verify", help="验证客户端浏览器登录")
    auth.add_argument("--profile", type=Path, required=True)
    auth.add_argument("--browser")
    auth.add_argument("--interactive", action="store_true")
    task = commands.add_parser("run", help="执行导出请求")
    task.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    events = Events()
    output = PlatformLogs(events)
    with redirect_stdout(output):
        try:
            if args.command == "verify":
                return verify(events, args.profile.resolve(), find_browser(args.browser), args.interactive)
            return run(events, args.request)
        except RUN_ERRORS as error:
            events.send("error", message=reason(error))
            return 2
        finally:
            output.flush()


if __name__ == "__main__":
    sys.exit(main())
