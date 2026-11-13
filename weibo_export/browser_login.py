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
import threading
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
    """Run inside the spawned process. Opens the login browser; the user logs
    in and then clicks an injected "已登录？点此获取 Cookie" button (via the
    js_api bridge) which captures the full cookie header — including HttpOnly
    SUB/SUBP that document.cookie cannot see — and writes it to result_path.

    Why a manual button rather than auto-detection: m.weibo.cn's login signal
    is not stable across page versions/redirects, and the essential SUB/SUBP
    cookies are HttpOnly (invisible to document.cookie). Previous auto-probe
    attempts never fired reliably in the field. A single explicit click avoids
    guessing and runs get_cookies() on pywebview's own thread, where it is
    safe."""
    import webview

    window_state = {"captured": False}
    diag_path = result_path + ".diag.txt"

    def _diag(msg):
        try:
            with open(diag_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

    def _write_result(header):
        try:
            # 原子写：先写同目录临时文件，再 os.replace 原子重命名。
            # 避免父进程轮询到「已创建但还没写完」的半截文件（open("w")
            # 会先创建空文件），从而读到空/截断的 Cookie。
            tmp = result_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(header)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp, result_path)
            _diag(f"已写出 Cookie 结果（{len(header)} 字符）")
        except Exception as e:
            _diag(f"写出结果失败: {e!r}")

    class _Bridge:
        """js_api bridge. Its methods run on pywebview's own dispatch thread.

        pywebview's get_cookies() is broken on WebView2: it queries cookies
        only for the window's *current* URL domain, but login hops across
        domains and SUB/SUBP are set on .weibo.cn while the window may sit on
        another host. We instead reach the native CoreWebView2.CookieManager
        and query *every relevant weibo domain*, marshalling each GetCookies
        call onto the UI thread via webview.Invoke (CoreWebView2 is UI-thread
        only). No blocking-with-semaphore from a non-UI thread → no deadlock,
        and we capture the full set including HttpOnly SUB/SUBP."""

        def capture(self):
            try:
                header = _native_capture()
                _diag(f"原生多域抓取 header {len(header)} 字符")
                if not header:
                    return "未读取到 Cookie，请确认已在窗口内登录后再点。"
                window_state["captured"] = True
                try:
                    _win_ref.get("w").destroy()
                except Exception:
                    pass
                _write_result(header)
                return "ok"
            except Exception as e:
                _diag(f"capture 失败: {e!r}")
                return f"读取 Cookie 失败：{e}"

    def _native_capture() -> str:
        """Query the WebView2 cookie store across all weibo domains and return
        a combined 'k=v; k2=v2' header. Must be called from a pywebview
        dispatch thread (js_api method), because it uses webview.Invoke to
        marshal onto the UI thread.

        Implementation note — we use the Chrome DevTools Protocol (CDP) call
        ``Network.getCookies`` via CoreWebView2.CallDevToolsProtocolMethodAsync
        rather than the native CookieManager. The CookieManager returns a
        managed CoreWebView2Cookie LIST that pythonnet cannot marshal (the
        ICoreWebView2Cookie COM→.NET cast raises NotImplementedException on
        every WebView2 SDK we tested, including the latest). CDP returns a
        plain JSON string instead — no COM casting involved — so we parse it
        with stdlib json and get every cookie (including HttpOnly SUB/SUBP)
        across all domains in ONE call."""
        import clr
        import json as _json
        from System import Action, String
        from System.Threading.Tasks import Task

        win = _win_ref.get("w")
        native = getattr(win, "native", None)
        webview_ctl = getattr(native, "webview", None) if native else None
        if webview_ctl is None:
            _diag("原生抓取: 找不到 webview 控件")
            return ""

        collected = {"json": "", "err": ""}
        done = threading.Event()

        def ui_work():
            try:
                core = webview_ctl.CoreWebView2
                if core is None:
                    _diag("原生抓取: CoreWebView2 为空")
                    done.set()
                    return

                def cb(task):
                    try:
                        collected["json"] = str(task.Result)
                        _diag(f"CDP 返回 JSON，长度 {len(collected['json'])}")
                    except Exception as e:
                        collected["err"] = repr(e)
                        _diag(f"CDP 回调异常: {e!r}")
                    done.set()

                # No URL filter ("{}") → returns cookies for all domains the
                # engine has touched (weibo.cn, weibo.com, passport, …).
                core.CallDevToolsProtocolMethodAsync(
                    "Network.getCookies", "{}"
                ).ContinueWith(Action[Task[String]](cb))
            except Exception as e:
                _diag(f"原生抓取 UI 工作异常: {e!r}")
                done.set()

        webview_ctl.Invoke(Action(ui_work))
        if not done.wait(timeout=10):
            _diag("原生抓取超时（10s）")

        pairs = []
        try:
            data = _json.loads(collected["json"]) if collected["json"] else {}
            for c in data.get("cookies", []):
                n, v = c.get("name", ""), c.get("value", "")
                if n:
                    pairs.append(f"{n}={v}")
        except Exception as e:
            _diag(f"解析 CDP JSON 失败: {e!r}")
        _diag(f"CDP 抓取到 {len(pairs)} 条 cookie")
        return "; ".join(pairs)

    bridge = _Bridge()
    _win_ref = {}

    # A persistent floating button injected into every page. Clicking it calls
    # pywebview.api.capture(). Styled to stay visible at the bottom on both the
    # login page and the logged-in feed.
    INJECT_JS = r"""
(function(){
  if (window.__wbCookieBtn) return;
  function add(){
    if (document.getElementById('wbCookieBtn')) return;
    var b = document.createElement('button');
    b.id = 'wbCookieBtn';
    b.textContent = '✅ 已登录？点此获取 Cookie';
    b.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;'
      + 'width:100%;padding:16px;font-size:18px;font-weight:bold;'
      + 'background:#e6162d;color:#fff;border:0;border-radius:0;'
      + 'box-shadow:0 -2px 8px rgba(0,0,0,.25);cursor:pointer;';
    b.onclick = function(){
      b.textContent = '正在获取 Cookie…';
      window.pywebview.api.capture().then(function(res){
        if (res !== 'ok') { b.textContent = res; b.disabled = false; }
      }).catch(function(e){ b.textContent = '获取失败：' + e; });
    };
    document.body.appendChild(b);
    window.__wbCookieBtn = true;
  }
  add();
  new MutationObserver(add).observe(document.documentElement, {childList:true, subtree:true});
})()
"""

    def on_loaded(win):
        try:
            win.evaluate_js(INJECT_JS)
            _diag("已注入获取 Cookie 按钮")
        except Exception as e:
            _diag(f"注入按钮失败: {e!r}")

        # 自动检测：延迟探测登录 cookie（SUB/SUBP）。weibo 登录成功后会跳转
        # 并种下 HttpOnly 的 SUB/SUBP cookie。我们在后台每隔几秒用 CDP 探测
        # 一次（CDP 取 cookie 不依赖 COM cast，比 CookieManager 可靠），一旦
        # 发现 SUB 或 SUBP 就自动抓取并写结果——用户无需知道要点底部按钮。
        def auto_probe():
            import json as _aj
            for attempt in range(60):  # 最多探测 ~2 分钟
                if window_state.get("captured"):
                    return
                if webview_loop_done.is_set():
                    return
                time.sleep(2.0)
                try:
                    w = _win_ref.get("w")
                    if w is None:
                        continue
                    native = getattr(w, "native", None)
                    wc = getattr(native, "webview", None) if native else None
                    if wc is None:
                        continue
                    box = {"json": "", "err": "", "done": False}

                    def ui():
                        try:
                            core = wc.CoreWebView2
                            if core is None:
                                box["done"] = True
                                return
                            def cb(task):
                                try:
                                    box["json"] = str(task.Result)
                                except Exception as e:
                                    box["err"] = repr(e)
                                box["done"] = True
                            core.CallDevToolsProtocolMethodAsync(
                                "Network.getCookies", "{}"
                            ).ContinueWith(_Action[_Task[_String]](cb))
                        except Exception:
                            box["done"] = True

                    wc.Invoke(_Action(ui))
                    for _ in range(20):
                        if box["done"]:
                            break
                        time.sleep(0.2)
                    if not box["json"]:
                        continue
                    data = _aj.loads(box["json"])
                    names = {c.get("name", "") for c in data.get("cookies", [])}
                    _diag(f"自动探测第{attempt+1}次: cookie 名={names}")
                    if "SUB" in names or "SUBP" in names:
                        _diag("✅ 自动检测到登录 Cookie（SUB/SUBP），自动抓取")
                        result = bridge.capture()
                        _diag(f"自动抓取结果: {result}")
                        return
                except Exception as e:
                    _diag(f"自动探测异常: {e!r}")

        threading.Thread(target=auto_probe, daemon=True).start()

    def inject_worker():
        # Belt-and-suspenders: on_loaded may not fire in every environment.
        # Keep trying to inject the button until it succeeds, so the user is
        # always able to click it. Stops once the loop ends or Cookie captured.
        injected = False
        while not injected and not window_state.get("captured"):
            if webview_loop_done.is_set():
                return
            w = _win_ref.get("w")
            if w is not None:
                try:
                    w.evaluate_js(INJECT_JS)
                    injected = True
                    _diag("注入按钮成功（兜底线程）")
                    return
                except Exception:
                    pass
            time.sleep(1.0)

    webview_loop_done = threading.Event()
    import clr as _clr  # noqa: F401  确保 System 类型在 on_loaded 闭包里可用
    from System import Action as _Action, String as _String  # noqa: F401
    from System.Threading.Tasks import Task as _Task  # noqa: F401

    win = webview.create_window(
        "微博登录（登录后点底部红色按钮获取 Cookie）",
        url,
        width=460,
        height=800,
        js_api=bridge,
    )
    _win_ref["w"] = win
    def _loaded_handler(*_a):
        on_loaded(win)
    win.events.loaded += _loaded_handler
    threading.Thread(target=inject_worker, daemon=True).start()
    _diag(f"打开登录窗口 {url}")
    webview.start(private_mode=False, storage_path=_storage_path())
    webview_loop_done.set()
    # Loop ended. If the user never clicked (no result file), the parent infers
    # a failure and tells them to click the red button before closing.


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
    for path in (result_path, result_path + ".diag.txt"):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
