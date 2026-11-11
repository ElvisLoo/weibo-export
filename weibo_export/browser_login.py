"""Optional built-in browser login (pywebview) — lets beginners get a Cookie
without opening DevTools.

Design (Option A: optional enhancement):
    - The core app stays pure tkinter; this module is imported lazily and only
      when the user clicks "Login to get Cookie".
    - If pywebview or WebView2 runtime is unavailable, we degrade gracefully:
      the caller shows a message telling the user to paste the Cookie manually.
    - Runs the browser in a background thread so the tkinter main loop keeps
      running; results are marshalled back to the tkinter thread via
      ``root.after``.

Security note: the Cookie is account credentials. It is only held in memory /
returned to the app for the current session and never sent anywhere.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

WEIBO_LOGIN_URL = "https://m.weibo.cn/"

# These Cookie names indicate a logged-in session. SUB / SUBP / MLOGIN are the
# essential login-state fields weibo sets (they are HttpOnly, which is exactly
# why we need a real browser engine to read them).
KEY_COOKIES = {"SUB", "SUBP", "MLOGIN", "SSOLoginState"}


def is_available() -> bool:
    """Return True if the optional browser backend can be used right now.

    Checks that pywebview imports. (WebView2 runtime availability is only
    confirmed when we actually start the engine; if that fails we report it
    through on_error rather than here.)
    """
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def _cookie_header(cookies) -> str:
    """Build a request-header style cookie string 'k=v; k2=v2' from pywebview
    Cookie objects (which expose .name / .value)."""
    pairs = []
    for c in cookies or []:
        try:
            name = c.name
            value = c.value
        except AttributeError:
            d = c if isinstance(c, dict) else getattr(c, "__dict__", {})
            name = d.get("name", "")
            value = d.get("value", "")
        if name:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _login_keys_found(cookies) -> set:
    found = set()
    for c in cookies or []:
        try:
            name = c.name
        except AttributeError:
            d = c if isinstance(c, dict) else getattr(c, "__dict__", {})
            name = d.get("name", "")
        if name in KEY_COOKIES:
            found.add(name)
    return found


def launch_login(parent_root,
                 on_success: Callable[[str], None],
                 on_error: Callable[[str], None]) -> bool:
    """Open a built-in browser for weibo login and capture the Cookie.

    Args:
        parent_root: tkinter root, used to marshal callbacks onto the GUI
            thread via ``root.after``.
        on_success: called (on the tkinter thread) with the cookie header string.
        on_error: called (on the tkinter thread) with a human-readable reason.

    Returns:
        True if a login window was launched, False if the backend is
        unavailable (in which case on_error is scheduled with guidance).
    """
    if not is_available():
        parent_root.after(0, on_error,
                          "当前系统未安装 WebView2 运行时，无法打开内置浏览器。\n"
                          "请改用手动方式：在浏览器登录微博后，按 F12 复制 Cookie 粘贴进来。")
        return False

    def worker():
        import webview

        state = {"captured": False}

        def try_capture(win):
            if state["captured"]:
                return
            try:
                cookies = win.get_cookies()
            except Exception:
                return
            if _login_keys_found(cookies):
                state["captured"] = True
                header = _cookie_header(cookies)
                # Capture again shortly after to catch late-set cookies, then close.
                def finalize():
                    try:
                        final = _cookie_header(win.get_cookies()) or header
                    except Exception:
                        final = header
                    try:
                        win.destroy()
                    except Exception:
                        pass
                    parent_root.after(0, on_success, final)
                threading.Thread(target=lambda: (time.sleep(1.0), finalize()),
                                 daemon=True).start()

        def on_loaded():
            try_capture(win_ref["w"])

        win_ref = {}

        def opener():
            try:
                win = webview.create_window(
                    "微博登录（登录成功后自动获取 Cookie）",
                    WEIBO_LOGIN_URL,
                    width=430,
                    height=740,
                )
                win_ref["w"] = win
                win.events.loaded += on_loaded

                def watcher():
                    # Safety: if the user closes the window without logging in,
                    # notify the app once the GUI loop exits.
                    time.sleep(0.3)
                    if not state["captured"]:
                        # Loop exits when window closes; wait then report if nothing captured.
                        while win.events and not _window_closed(win):
                            time.sleep(0.5)
                        if not state["captured"]:
                            parent_root.after(0, on_error,
                                              "已关闭登录窗口，未捕获到登录 Cookie。\n"
                                              "请重新点击登录，完成登录后再关闭窗口。")

                threading.Thread(target=watcher, daemon=True).start()
                webview.start(private_mode=False, storage_path=_storage_path())
            except Exception as e:
                # WebView2 运行时缺失、引擎初始化失败等，统一回传错误信息
                err_msg = str(e)
                if "WebView2" in err_msg or "CoreWebView2" in err_msg or "edge" in err_msg.lower():
                    err_msg = (
                        "WebView2 运行时不可用，无法打开内置浏览器。\n\n"
                        "【自动登录】需要安装 Microsoft Edge WebView2 运行时（一次性安装）。\n"
                        "  下载地址：https://developer.microsoft.com/en-us/microsoft-edge/webview2/\n\n"
                        "【手动方式】在浏览器登录微博后，按 F12 → 网络/Network → 刷新 → "
                        "找任一请求 → 复制 Cookie 请求头粘贴到 Cookie 栏。"
                    )
                else:
                    err_msg = f"登录窗口启动失败：{err_msg}\n\n请改用手动粘贴 Cookie。"
                parent_root.after(0, on_error, err_msg)

        threading.Thread(target=opener, daemon=True).start()

    worker()
    return True


def _storage_path() -> str:
    import os
    path = os.path.join(os.path.expanduser("~"), "Documents", "微博导出", "_login_browser")
    os.makedirs(path, exist_ok=True)
    return path


def _window_closed(win) -> bool:
    """Best-effort check whether the pywebview window has been destroyed."""
    try:
        return not win.native.Handle  # type: ignore[attr-defined]
    except Exception:
        # Fallback: consider it closed when it's no longer in webview.windows
        try:
            import webview
            return win not in webview.windows
        except Exception:
            return False
