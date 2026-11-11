"""Optional built-in browser login (pywebview) — lets beginners get a Cookie
without opening DevTools.

Why a separate PROCESS (not a thread):
    pywebview's GUI engine must run on the main thread of a process. The main
    tkinter app already owns the main thread, so launching webview from a
    background thread raises "pywebview must be run on a main thread". We
    therefore run the login browser in a spawned child process (which has its
    own main thread), and hand the captured Cookie back to the tkinter app via
    a small temp file that the parent polls.

Design (Option A: optional enhancement):
    - The core app stays pure tkinter; this module is imported lazily and only
      when the user clicks "Login to get Cookie".
    - If pywebview or WebView2 runtime is unavailable, we degrade gracefully:
      the caller shows a message telling the user to paste the Cookie manually.
    - Works both from source and from the PyInstaller onedir bundle: the child
      re-launches the same frozen interpreter via multiprocessing.

Security note: the Cookie is account credentials. It only lives in memory and
in a short-lived temp file on this machine, and is never sent anywhere.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import time
import uuid
from typing import Callable

WEIBO_LOGIN_URL = "https://m.weibo.cn/"

# These Cookie names indicate a logged-in session. SUB / SUBP / MLOGIN are the
# essential login-state fields weibo sets (they are HttpOnly, which is exactly
# why we need a real browser engine to read them).
KEY_COOKIES = {"SUB", "SUBP", "MLOGIN", "SSOLoginState"}


def is_available() -> bool:
    """Return True if the optional browser backend can be used right now.

    Only checks that pywebview imports. (WebView2 runtime availability is
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


def _storage_path() -> str:
    path = os.path.join(os.path.expanduser("~"), "Documents", "微博导出", "_login_browser")
    os.makedirs(path, exist_ok=True)
    return path


def _result_dir() -> str:
    path = os.path.join(_storage_path(), "results")
    os.makedirs(path, exist_ok=True)
    return path


# =====================================================================
# 子进程入口：拥有自己「主线程」，pywebview 在这里可以正常启动
# =====================================================================

def _login_child(url: str, result_path: str) -> None:
    """Run inside the spawned process. Opens the login browser and, once a
    logged-in session is detected, writes the Cookie header to ``result_path``
    and exits. Exits with code 1 if the engine cannot start."""
    import webview

    window_state = {"captured": False}

    def capture_and_close(win):
        if window_state["captured"]:
            return
        try:
            cookies = win.get_cookies()
        except Exception:
            return
        if not _login_keys_found(cookies):
            return
        window_state["captured"] = True
        header = _cookie_header(cookies)

        def finalize():
            try:
                final = _cookie_header(win.get_cookies()) or header
            except Exception:
                final = header
            try:
                win.destroy()
            except Exception:
                pass
            try:
                with open(result_path, "w", encoding="utf-8") as f:
                    f.write(final)
            except Exception:
                pass

        import threading
        threading.Thread(
            target=lambda: (time.sleep(1.0), finalize()),
            daemon=True,
        ).start()

    def on_loaded(win):
        capture_and_close(win)

    def on_shown(win):
        # Poll periodically in case cookies are set without a fresh load event.
        import threading

        def poller():
            while not window_state["captured"]:
                try:
                    if _window_closed(win):
                        return
                except Exception:
                    return
                capture_and_close(win)
                time.sleep(0.8)

        threading.Thread(target=poller, daemon=True).start()

    win = webview.create_window(
        "微博登录（登录成功后自动获取 Cookie，登录完可关闭本窗口）",
        url,
        width=430,
        height=760,
    )
    win.events.loaded += on_loaded
    win.events.shown += on_shown
    webview.start(private_mode=False, storage_path=_storage_path())


def _window_closed(win) -> bool:
    """Best-effort check whether the pywebview window has been destroyed."""
    try:
        return not win.native.Handle  # type: ignore[attr-defined]
    except Exception:
        try:
            import webview
            return win not in webview.windows
        except Exception:
            return False


# =====================================================================
# 父进程侧：启动子进程 + 轮询结果文件回传给 tkinter 线程
# =====================================================================

def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _spawn_login_process(result_path: str) -> object:
    """Spawn the login-browser child process.

    Returns the ``subprocess.Popen`` handle. From a PyInstaller bundle we
    re-invoke the same exe with a magic argv so the frozen app branches into
    the child worker instead of opening the GUI.
    """
    import subprocess

    if _is_frozen():
        cmd = [sys.executable, "--weibo-login-worker", result_path]
    else:
        cmd = [
            sys.executable,
            "-c",
            "import sys; from weibo_export.browser_login import _login_child; "
            "_login_child(sys.argv[1], sys.argv[2])",
            WEIBO_LOGIN_URL,
            result_path,
        ]
    return subprocess.Popen(cmd)


def _child_main_from_args(argv: list) -> int:
    """Entry point used when the frozen exe is re-invoked as the login worker.

    ``argv`` is the slice starting right after ``--weibo-login-worker``, so
    ``argv[0]`` is the result-file path the child should write the Cookie to.
    """
    if len(argv) < 1:
        return 1
    result_path = argv[0]
    try:
        _login_child(WEIBO_LOGIN_URL, result_path)
    except Exception:
        return 1
    return 0


def launch_login(parent_root,
                 on_success: Callable[[str], None],
                 on_error: Callable[[str], None]) -> bool:
    """Open a built-in browser (in a child process) for weibo login and capture
    the Cookie.

    Args:
        parent_root: tkinter root, used to marshal callbacks onto the GUI
            thread via ``root.after``.
        on_success: called (on the tkinter thread) with the cookie header string.
        on_error: called (on the tkinter thread) with a human-readable reason.

    Returns:
        True if a login process was launched, False if the backend is
        unavailable (in which case on_error is scheduled with guidance).
    """
    if not is_available():
        parent_root.after(0, on_error,
                          "内置浏览器组件（pywebview）缺失，无法打开登录窗口。\n"
                          "请改用手动方式：在浏览器登录微博后，按 F12 复制 Cookie 粘贴进来。")
        return False

    result_path = os.path.join(_result_dir(), f"cookie_{uuid.uuid4().hex}.txt")
    try:
        proc = _spawn_login_process(result_path)
    except Exception as e:
        parent_root.after(0, on_error,
                          f"无法启动登录窗口进程：{e}\n\n请改用手动粘贴 Cookie。")
        return False

    def poll():
        # 1) 子进程写出 Cookie 结果文件 → 成功
        if os.path.exists(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    cookie = f.read().strip()
            except Exception:
                cookie = ""
            if cookie:
                on_success(cookie)
            else:
                on_error("登录窗口已结束，但未取到有效 Cookie。\n请重试或手动粘贴 Cookie。")
            _cleanup(result_path, proc)
            return

        # 2) 子进程已退出但没写出 Cookie → 失败（可能用户直接关窗）
        if proc.poll() is not None:
            on_error(
                "已关闭登录窗口，未捕获到登录 Cookie。\n"
                "请重新点击登录，完成登录后再关闭窗口；若窗口打不开，"
                "请确认已安装 Microsoft Edge WebView2 运行时。")
            _cleanup(result_path, proc)
            return

        parent_root.after(400, poll)

    parent_root.after(400, poll)
    return True


def _cleanup(result_path: str, proc) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    try:
        if os.path.exists(result_path):
            os.remove(result_path)
    except Exception:
        pass
