"""Read the account project catalog and export a parking roster in one hidden session."""

import argparse
from contextlib import contextmanager
import json
import msvcrt
import re
import sys
from datetime import date
from pathlib import Path
from time import monotonic

from patchright.sync_api import Error as BrowserError
from patchright.sync_api import expect

from parking_batch_match import match_catalog
from parking_batch_report import build_report
from parking_export import (
    PROFILE, WORKSPACE, RequestCounts, control, export_park, open_menu,
    open_project, open_select, project_page, query, site_action,
)
from parking_scrapling_session import LoginRequiredError, authenticated_session


ROSTER = Path(r"D:\Projects\ANPING\停车场&项目&编号.txt")
CATALOG_PAGE_MAX = 100
CATALOG_REQUEST_MAX = 30
DEFAULT_START = date(2026, 10, 20)
DEFAULT_END = date(2026, 11, 20)
PRIOR_PARK = "北运河西地铁站停车场"


def read_roster(path):
    names = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not names or len(set(names)) != len(names):
        raise ValueError("名单为空或含有重复车场，请核对名单文件。")
    return names


@contextmanager
def exclusive_output(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".batch.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ValueError("这个输出目录已有批量任务在运行。") from error
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def query_scope(args):
    return {"start": args.start.isoformat(), "end": args.end.isoformat(), "status": args.status}


def save_manifest(path, entries, scope):
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps({"scope": scope, "entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    staging.replace(path)


def initial_manifest(names, manifest, args):
    if manifest.is_file():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if payload.get("scope") != query_scope(args):
            raise ValueError("这个输出目录的日期或状态与本次不同，请为本次查询指定另一输出目录。")
        previous = payload["entries"]
        if [entry["source_name"] for entry in previous] != names:
            raise ValueError("当前名单与上次运行不同，请为新名单指定另一输出目录。")
        return previous

    entries = match_catalog(names, None)
    for entry in entries:
        entry.update(query="", note="尚未读取账号项目目录。")
    if (args.start, args.end, args.status) == (DEFAULT_START, DEFAULT_END, "出场"):
        existing = WORKSPACE / "exports" / f"{PRIOR_PARK}_出场_{DEFAULT_START}至{DEFAULT_END}.xlsx"
        if existing.is_file():
            for entry in entries:
                if entry["source_name"] == PRIOR_PARK:
                    entry.update(matched_name=PRIOR_PARK, project_code="92302", match_method="keyword",
                                 query="北运河西地铁", state="exported", expected_rows=6978,
                                 file=str(existing), note="复用上次已核验的同范围平台报表。")
    return entries


def project_control(page):
    return control(page, "项目/编码", ".//input", "input:visible")


def current_projects(page):
    values = page.locator("tbody tr:visible").evaluate_all(
        "rows => rows.map(row => [...row.cells].slice(1,3).map(cell => cell.innerText.trim()))"
    )
    return [(name, code) for code, name in (item for item in values if len(item) == 2) if code and name]


def catalog_total(page):
    pagination = page.locator(".el-pagination__total:visible")
    text = pagination.inner_text()
    match = re.search(r"共\s*([\d,]+)\s*条", text)
    if not match:
        raise ValueError("平台未提供可核对的项目总数。")
    return int(match[1].replace(",", ""))


def enlarge_pages(page):
    sizes = page.locator(".el-pagination__sizes:visible")
    if not sizes.is_visible():
        return
    current = sizes.inner_text()
    try:
        open_select(sizes.get_by_role("combobox"))
        options = page.get_by_role("option", name=re.compile(r"^\d+\s*条\/页\s*$"))
        options.first.wait_for(timeout=4000)
        available = [(int(re.match(r"\d+", text)[0]), text)
                     for text in (value.strip() for value in options.all_inner_texts()) if re.match(r"\d+", text)]
        allowed = [(size, text) for size, text in available if size <= CATALOG_PAGE_MAX]
        if allowed and max(allowed)[1] not in current:
            site_action(page, page.get_by_role("option", name=max(allowed)[1], exact=True))
    except BrowserError:
        print("本页条数无法切换，继续按当前每页条数读取项目目录。", flush=True)
    finally:
        page.keyboard.press("Escape")


def read_catalog(page):
    open_menu(page, "项目管理", "项目管理")
    search = project_control(page)
    if search.input_value().strip():
        search.fill("")
        query(page)
    expect(page.locator(".el-loading-mask:visible")).to_have_count(0, timeout=15_000)
    total = catalog_total(page)
    if total > 10:
        enlarge_pages(page)
    active = page.locator(".el-pagination .el-pager .is-active:visible")
    if active.count() == 1 and active.inner_text().strip() != "1":
        first = page.locator(".el-pagination .el-pager .number:visible").filter(has_text=re.compile(r"^\s*1\s*$"))
        site_action(page, first)

    index = {}
    for _ in range(CATALOG_REQUEST_MAX):
        old_size = len(index)
        index.update((code, name) for name, code in current_projects(page))
        if len(index) == total:
            return [(name, code) for code, name in index.items()]
        if len(index) == old_size or len(index) > total:
            break
        next_page = page.locator(".el-pagination .btn-next:visible")
        if not next_page.is_enabled():
            break
        site_action(page, next_page)
    raise ValueError(f"项目列表读到 {len(index)} 个编码，平台显示 {total} 个项目，暂未核对完整。")


def match_known_projects(names, catalog, known_aliases):
    entries = match_catalog(names, catalog)
    if not known_aliases:
        return entries
    identities = set(catalog)
    claimed = {entry["project_code"] for entry in entries if entry["state"] == "matched"}
    for entry in entries:
        known = known_aliases.get(entry["source_name"])
        if entry["state"] != "missing" or not known:
            continue
        name, code = known["matched_name"], known["project_code"]
        if code not in claimed and (name, code) in identities:
            entry.update({key: known[key] for key in ("matched_name", "project_code", "match_method", "query")})
            entry.update(state="matched", note="复核了此前确认的平台项目名称和编码。")
            claimed.add(code)
    return entries


def resolve_roster(page, names, *, known_aliases=None):
    catalog = read_catalog(page)
    first = match_known_projects(names, catalog, known_aliases)
    for entry in first:
        if entry["state"] != "missing":
            continue
        for term in dict.fromkeys((entry["source_name"], entry["query"])):
            project_control(page).fill(term)
            query(page)
            hits = current_projects(page)
            if len(hits) != catalog_total(page):
                continue
            catalog.extend(hits)
            if match_catalog([entry["source_name"]], hits)[0]["state"] != "missing":
                break
    return match_known_projects(names, catalog, known_aliases)


def reuse_completed(resolved, previous):
    for current, old in zip(resolved, previous, strict=True):
        if (current["state"] == "matched" and old["state"] in {"exported", "empty"}
                and current["project_code"] == old["project_code"]
                and current["matched_name"] == old["matched_name"]):
            current.update((key, old[key]) for key in ("state", "expected_rows", "file", "note") if key in old)
    return resolved


def export_matched(page, entries, manifest, args):
    failures = 0
    for entry in entries:
        if entry["state"] != "matched":
            continue
        park, code = entry["matched_name"], entry["project_code"]
        popup = None

        def completed(record_count, file):
            entry.update(state="exported" if file else "empty", expected_rows=record_count,
                         file=str(file) if file else "", note="")
            save_manifest(manifest, entries, query_scope(args))

        try:
            if (park, code) in current_projects(page):
                row = page.locator("tbody tr:visible").filter(has=page.get_by_text(code, exact=True))
                popup = open_project(page, row.locator("td").nth(2), park)
            else:
                popup, actual = project_page(page, code)
                if actual != park:
                    raise ValueError("项目编码打开的车场名称与目录不符。")
            export_park(popup, park, args, on_completed=completed)
            failures = 0
        except (AssertionError, BrowserError, ValueError, OSError) as error:
            if entry["state"] not in {"exported", "empty"}:
                entry.update(state="export_failed", note=next((line for line in str(error).splitlines() if line),
                                                             "未能完成导出。"))
                save_manifest(manifest, entries, query_scope(args))
            failures += 1
        finally:
            if popup is not None and not popup.is_closed():
                popup.close()
        if failures >= 2:
            raise ValueError("连续两项导出遇到错误，已保存进度。")


def show_platform_qr(path):
    print(f"LOGIN_QR_IMAGE={path}", flush=True)
    print("请在 120 秒内用绑定停车账号的微信扫描平台二维码。", flush=True)


def main():
    parser = argparse.ArgumentParser(description="使用 Scrapling 无头导出停车场名单")
    parser.add_argument("--list", type=Path, default=ROSTER, help="每行一个车场名称的 UTF-8 文件")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument("--status", choices=("入场", "出场"), default="出场")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--profile", type=Path, default=PROFILE, help="Scrapling 持久登录配置目录")
    parser.add_argument("--browser-executable", type=Path, help="已有独立配置对应的 Chromium 程序")
    parser.add_argument("--storage-state", type=Path, help="用户已准备的本地浏览器会话状态文件")
    login_mode = parser.add_mutually_exclusive_group()
    login_mode.add_argument("--authorize-only", action="store_true", help="只在本地终端授权无头会话")
    login_mode.add_argument("--qr-login", action="store_true", help="在后台生成官方登录二维码，扫码后直接开始批量导出")
    login_mode.add_argument("--manual-login-once", action="store_true", help="仅打开一次人工授权窗口，成功后在后台批量导出")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("开始日期不能晚于结束日期。")

    profile = args.profile.resolve()
    session_options = dict(executable_path=args.browser_executable, storage_state=args.storage_state,
                           show_qr=show_platform_qr if args.qr_login else None,
                           manual_login_once=args.manual_login_once)
    if args.authorize_only:
        with authenticated_session(profile, **session_options, prompt_login=True):
            print("已授权无头会话，可以继续执行批量导出。")
        return 0

    folder = (args.output_dir or WORKSPACE / "exports" / f"批量_{args.start}至{args.end}_{args.status}").resolve()
    with exclusive_output(folder):
        return run_batch(args, folder, profile, session_options)


def run_batch(args, folder, profile, session_options):
    manifest = folder / "查找与导出进度.json"
    args.output_dir = folder / "原始报表"
    args.pending_dir = folder / ".pending"
    args.new_export = False
    names = read_roster(args.list)
    entries = initial_manifest(names, manifest, args)
    save_manifest(manifest, entries, query_scope(args))
    exit_code = 0
    try:
        with authenticated_session(profile, **session_options) as (context, page):
            context.set_default_timeout(12_000)
            counts = RequestCounts(context)
            started = monotonic()
            try:
                entries = reuse_completed(resolve_roster(page, names), entries)
                save_manifest(manifest, entries, query_scope(args))
                export_matched(page, entries, manifest, args)
            finally:
                print(f"平台 HTTP {counts.total} 次（峰值 {counts.peak}），"
                      f"接口 {counts.api} 次（峰值 {counts.api_peak}），累计 {monotonic()-started:.1f} 秒。")
    except (AssertionError, BrowserError, LoginRequiredError, OSError, ValueError) as error:
        print(f"批量任务暂未完成。{error}", file=sys.stderr, flush=True)
        exit_code = 2
    summary = folder / f"车场查找与{args.status}记录汇总.xlsx"
    measures = build_report(manifest, summary, args.start, args.end, args.status)
    print(json.dumps(measures, ensure_ascii=False))
    print(f"汇总文件 {summary}")
    expected_total = sum(entry.get("expected_rows", 0) for entry in entries if entry["state"] == "exported")
    complete = all(entry["state"] in {"exported", "empty"} for entry in entries)
    return exit_code if complete and expected_total == measures["merged_rows"] else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BrowserError, LoginRequiredError, OSError, ValueError) as error:
        sys.exit(f"无法完成。{error}")
