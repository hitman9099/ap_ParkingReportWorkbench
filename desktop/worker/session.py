"""Verified browser sessions for the portable Windows client."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
from shutil import which
from time import monotonic
from urllib.parse import urlsplit

from patchright.sync_api import Error as BrowserError

from parking_scrapling_session import (
    LOGIN_BUTTON,
    MANUAL_LOGIN_TIMEOUT_MS,
    VERIFICATION_TEXT,
    LoginRequiredError,
    check_platform_origin,
    platform_session,
    project_menu,
    site_host,
    wait_for_marker,
)

SESSION_FILE = "verified-session.dpapi"
DPAPI_NO_UI = 0x01


class Blob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect_bytes(value, *, decrypt=False):
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    fn.argtypes = [
        ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(Blob),
    ]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    array = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    source = Blob(len(value), array)
    result = Blob()
    if not fn(ctypes.byref(source), None, None, None, None, DPAPI_NO_UI, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.length)
    finally:
        kernel32.LocalFree(result.data)


def find_browser(explicit=None):
    if explicit:
        executable = Path(explicit).resolve()
        if not executable.is_file():
            raise ValueError("指定的浏览器程序不存在。")
        return executable
    relative = (
        ("Microsoft", "Edge", "Application", "msedge.exe"),
        ("Google", "Chrome", "Application", "chrome.exe"),
    )
    for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        if os.environ.get(key):
            for parts in relative:
                browser = Path(os.environ[key]).joinpath(*parts)
                if browser.is_file():
                    return browser
    for name in ("msedge.exe", "chrome.exe"):
        if executable := which(name):
            return Path(executable)
    # Patchright can also use its own Chromium when present.
    return None


def open_session(profile, browser, snapshot=None, *, headless=True, on_context=None):
    snapshot = snapshot or {}
    return platform_session(
        profile, executable_path=browser, storage_state=snapshot.get("state"),
        tab_session_state=snapshot.get("tab"), headless=headless, on_context=on_context,
    )


def require_verified(page):
    menu = project_menu(page)
    login = page.get_by_role("button", name=LOGIN_BUTTON).filter(visible=True)
    verify = page.get_by_text(VERIFICATION_TEXT).filter(visible=True)
    wait_for_marker(menu.or_(login).or_(verify), "平台未显示可验证的登录状态。")
    check_platform_origin(page)
    if not menu.first.is_visible():
        raise LoginRequiredError("登录已失效，请在客户端重新验证。")


def capture_session(context, page):
    check_platform_origin(page)
    state = context.storage_state(indexed_db=True)
    cookies = [cookie for cookie in state["cookies"] if site_host(cookie["domain"].lstrip("."))]
    origins = [origin for origin in state["origins"] if site_host(urlsplit(origin["origin"]).hostname)]
    return {"state": {"cookies": cookies, "origins": origins},
            "tab": page.evaluate("() => Object.entries(sessionStorage)")}


def load_snapshot(profile):
    try:
        payload = (profile / SESSION_FILE).read_bytes()
        return json.loads(protect_bytes(payload, decrypt=True).decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def save_snapshot(profile, snapshot):
    profile.mkdir(parents=True, exist_ok=True)
    target = profile / SESSION_FILE
    temp = target.with_suffix(".tmp")
    data = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    temp.write_bytes(protect_bytes(data))
    temp.replace(target)


def capture_interactive(profile, browser):
    with open_session(profile, browser, headless=False) as (context, page):
        deadline = monotonic() + MANUAL_LOGIN_TIMEOUT_MS / 1000
        menu = project_menu(page)
        try:
            while not menu.first.is_visible() and monotonic() < deadline:
                page.wait_for_timeout(700)
        except BrowserError:
            raise LoginRequiredError("授权窗口已关闭，登录尚未验证。") from None
        if not menu.first.is_visible():
            raise LoginRequiredError("等待浏览器验证超时，请重新验证。")
        check_platform_origin(page)
        return capture_session(context, page)
