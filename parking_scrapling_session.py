"""Open an authenticated headless session for the parking platform."""

import os
import re
import sys
import warnings
from contextlib import contextmanager
from getpass import GetPassWarning, getpass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from patchright.sync_api import Error as BrowserError
from patchright.sync_api import TimeoutError as BrowserTimeout
from scrapling.fetchers import StealthySession


SITE = "https://s.qcyuns.com/"
LOCAL_SESSION_HANDOFF = SITE + "__local_scrapling_session_handoff__"
LOGIN_TIMEOUT_MS = 20_000
LOGIN_BUTTON = re.compile(r"^登\s*录$")
VERIFICATION_TEXT = re.compile(r"验证码|人机验证|滑块验证|滑动完成验证|滑动验证|安全验证|二次验证|Verify you are human|Just a moment")
LOGIN_ERROR_TEXT = re.compile(r"登录失败|用户名或密码错误|账号或密码错误|账户已锁定|登录过于频繁")
AGREEMENT_TEXT = re.compile(r"请先阅读并勾选服务协议|请.*同意.*服务协议")
FORM_ERROR_TEXT = re.compile(r"密码不能少于6位|用户名不能为空|密码不能为空|请输入用户名|请输入密码")
QR_LOGIN_TIMEOUT_MS = 120_000
MANUAL_LOGIN_TIMEOUT_MS = 360_000


class LoginRequiredError(RuntimeError):
    """The browser session needs user-provided authentication."""


def check_platform_origin(page):
    destination = urlsplit(page.url)
    if (destination.scheme != "https" or destination.hostname != "s.qcyuns.com"
            or destination.port not in (None, 443)):
        raise LoginRequiredError("停车平台停在非预期的页面，未输入或重试登录信息。")


def wait_for_marker(marker, message):
    try:
        marker.first.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS)
    except BrowserTimeout:
        raise LoginRequiredError(message) from None


def project_menu(page):
    return page.get_by_role("menuitem", name=re.compile("项目管理")).filter(visible=True)


def login_values(prompt_login):
    account = os.environ.get("QCYUN_ACCOUNT")
    password = os.environ.get("QCYUN_PASSWORD")
    if prompt_login and not (account and password):
        if not sys.stdin.isatty():
            raise LoginRequiredError("请从本机交互式终端运行登录授权。")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                account = account or input("停车平台账号：").strip()
                password = password or getpass("停车平台密码（不回显）：")
        except (EOFError, KeyboardInterrupt, GetPassWarning):
            raise LoginRequiredError("本机登录输入已取消或终端不支持隐藏密码。") from None
    if not account or not password:
        raise LoginRequiredError("无界面会话尚未登录。请提供已登录的浏览器配置或会话文件，或在本机完成登录授权。")
    return account, password


def sign_in_if_needed(page, *, prompt_login=False):
    menu = project_menu(page)
    login = page.get_by_role("button", name=LOGIN_BUTTON).filter(visible=True)
    verification = page.get_by_text(VERIFICATION_TEXT).or_(page.locator(
        ".verifybox, .verifybox-top, input[placeholder*='验证码'], input[autocomplete='one-time-code'], "
        "iframe[src*='challenges.cloudflare.com'], iframe[src*='recaptcha'], iframe[src*='hcaptcha']"
    )).filter(visible=True)
    failure = page.get_by_text(LOGIN_ERROR_TEXT).or_(page.locator(
        ".el-message--error, .el-message--warning, .el-form-item__error"
    )).filter(visible=True)

    wait_for_marker(menu.or_(login).or_(verification), "无法确认平台的登录状态，请检查会话是否有效。")
    check_platform_origin(page)
    if verification.first.is_visible():
        raise LoginRequiredError("停车平台需要验证码或人工登录验证。请提供已验证的会话后重试。")
    if menu.first.is_visible():
        return

    account, password = login_values(prompt_login)
    try:
        check_platform_origin(page)
        page.get_by_placeholder("请输入用户名", exact=True).fill(account)
        check_platform_origin(page)
        page.get_by_placeholder("请输入密码", exact=True).fill(password)
        check_platform_origin(page)
        if verification.first.is_visible():
            raise LoginRequiredError("停车平台需要验证码或人工登录验证。请提供已验证的会话后重试。")
        login.click()
        wait_for_marker(menu.or_(verification).or_(failure), "未确认登录成功。请检查会话或登录条件后重试。")
        check_platform_origin(page)
        if verification.first.is_visible():
            raise LoginRequiredError("停车平台需要验证码或人工登录验证。请提供已验证的会话后重试。")
        if not menu.first.is_visible():
            if failure.filter(has_text=AGREEMENT_TEXT).first.is_visible():
                raise LoginRequiredError("平台要求先由你查看并确认服务协议。请通过平台的人工授权流程登录。")
            if failure.filter(has_text=FORM_ERROR_TEXT).first.is_visible():
                raise LoginRequiredError("平台表单校验未通过，请在本机核对账号和密码的输入。")
            raise LoginRequiredError("平台未接受本次登录。请在本机核对平台的登录提示。")
    except BrowserError:
        raise LoginRequiredError("登录页面未完成本次提交。请在本机核对登录状态。") from None


def sign_in_with_qr(page, show_qr):
    menu = project_menu(page)
    tab = page.get_by_role("tab", name="扫码登录", exact=True)
    wait_for_marker(menu.or_(tab), "平台没有显示扫码登录入口。")
    check_platform_origin(page)
    if menu.first.is_visible():
        return

    tab.click()
    image_selector = ".wx-qrcode img.el-image__inner"
    image = page.locator(image_selector).filter(visible=True)
    try:
        image.wait_for(timeout=LOGIN_TIMEOUT_MS)
        page.wait_for_function(
            "selector => Array.from(document.querySelectorAll(selector)).some(img => "
            "img.complete && img.naturalWidth > 0 && img.getClientRects().length > 0)",
            arg=image_selector, timeout=LOGIN_TIMEOUT_MS,
        )
    except BrowserTimeout:
        raise LoginRequiredError("平台未生成可读的登录二维码。") from None

    failure = page.get_by_text(re.compile(r"扫码登录失败|二维码已失效|二维码已过期")).filter(visible=True)
    with TemporaryDirectory(prefix="parking-platform-login-") as working:
        qr_path = Path(working) / "登录二维码.png"
        image.screenshot(path=qr_path)
        show_qr(qr_path)
        try:
            menu.or_(failure).first.wait_for(timeout=QR_LOGIN_TIMEOUT_MS)
        except BrowserTimeout:
            raise LoginRequiredError("没有收到平台扫码登录成功的确认。二维码已关闭。") from None

    check_platform_origin(page)
    if not menu.first.is_visible():
        raise LoginRequiredError("扫码未登录到可访问项目管理的账号。")


@contextmanager
def platform_session(profile, *, executable_path=None, storage_state=None, tab_session_state=None, headless=True,
                     on_context=None):
    profile = Path(profile).expanduser().resolve()
    options = {
        "headless": headless,
        "max_pages": 1,
        "user_data_dir": str(profile),
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "solve_cloudflare": False,
        "additional_args": {
            "accept_downloads": True,
            "ignore_https_errors": False,
            "permissions": [],
            "locale": "zh-CN",
            "viewport": {"width": 1600, "height": 900},
        },
    }
    options["extra_flags"] = ["--restore-last-session"] if headless else ["--window-size=1600,1000"]
    if executable_path is not None:
        options["executable_path"] = str(Path(executable_path).expanduser().resolve())
    if storage_state is not None and not isinstance(storage_state, dict):
        storage_state = Path(storage_state).expanduser().resolve()
        if not storage_state.is_file():
            raise LoginRequiredError("指定的登录会话文件不存在。")

    with StealthySession(**options) as session:
        context = session.context
        if on_context is not None:
            on_context(context)
        if storage_state is not None:
            try:
                context.set_storage_state(storage_state)
            except (BrowserError, OSError, ValueError, RuntimeError):
                raise LoginRequiredError("无法载入指定的登录会话文件。") from None
        context.set_default_timeout(12_000)
        existing = context.pages
        platform_tabs = [tab for tab in existing if urlsplit(tab.url).hostname == "s.qcyuns.com"]
        provider_tab = next((tab for tab in platform_tabs
                             if urlsplit(tab.url).fragment.startswith("/serviceProvider/")), None)
        page = provider_tab or next(iter(platform_tabs), None)
        if page is None:
            page = existing[0] if existing else context.new_page()
        if tab_session_state:
            restore_tab_session(page, tab_session_state)
        try:
            if page is not provider_tab or tab_session_state:
                page.goto(SITE, wait_until="domcontentloaded", timeout=30_000)
            else:
                page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except BrowserError:
            raise LoginRequiredError("会话未能打开停车平台。请检查网络、证书或平台状态。") from None
        check_platform_origin(page)
        yield context, page


def restore_tab_session(page, state):
    # This response is fulfilled inside the browser. The saved values never leave the local process.
    page.route(LOCAL_SESSION_HANDOFF, lambda route: route.fulfill(
        status=200, content_type="text/html", body="<!doctype html><title>Local session handoff</title>",
        headers={"x-scrapling-local-handoff": "1"},
    ), times=1)
    try:
        response = page.goto(LOCAL_SESSION_HANDOFF, wait_until="domcontentloaded", timeout=12_000)
        if response is None or response.header_value("x-scrapling-local-handoff") != "1":
            raise LoginRequiredError("未能在本机恢复停车平台的标签页会话。")
        check_platform_origin(page)
        page.evaluate("values => { for (const [key, value] of values) sessionStorage.setItem(key, value); }", state)
    except BrowserError:
        raise LoginRequiredError("未能在本机恢复停车平台的标签页会话。") from None


def manual_platform_login(profile, *, executable_path=None, storage_state=None):
    with platform_session(profile, executable_path=executable_path, storage_state=storage_state,
                          headless=False) as (context, page):
        print("授权窗口已打开。请在浏览器内输入账号密码并手动完成验证。", flush=True)
        try:
            project_menu(page).first.wait_for(timeout=MANUAL_LOGIN_TIMEOUT_MS)
        except BrowserError:
            raise LoginRequiredError("未确认人工登录成功，请在授权窗口内完成登录后再继续。") from None
        check_platform_origin(page)
        state = context.storage_state(indexed_db=True)
        tab_state = page.evaluate("() => Object.entries(window.sessionStorage)")
        cookies = [cookie for cookie in state["cookies"] if site_host(cookie["domain"].lstrip("."))]
        origins = [origin for origin in state["origins"] if site_host(urlsplit(origin["origin"]).hostname)]
        return {"cookies": cookies, "origins": origins}, tab_state


def site_host(host):
    return bool(host and (host == "qcyuns.com" or host.endswith(".qcyuns.com")))


@contextmanager
def authenticated_session(profile, *, executable_path=None, storage_state=None, prompt_login=False, show_qr=None,
                          manual_login_once=False):
    """Yield the authorized platform page from a hidden Scrapling browser."""
    tab_session_state = None
    if manual_login_once:
        storage_state, tab_session_state = manual_platform_login(
            profile, executable_path=executable_path, storage_state=storage_state,
        )
        print("平台已登录，授权窗口已关闭。正在切换到后台继续。", flush=True)

    with platform_session(profile, executable_path=executable_path, storage_state=storage_state,
                          tab_session_state=tab_session_state) as (context, page):
        if show_qr is None:
            sign_in_if_needed(page, prompt_login=prompt_login)
        else:
            sign_in_with_qr(page, show_qr)
        yield context, page
