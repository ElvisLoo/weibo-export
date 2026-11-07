"""极客港 · 微博导出工具 —— Windows 桌面版入口（双击运行）。

流程：后台启动 FastAPI 服务 -> 打开原生应用窗口加载界面。
打包后（PyInstaller frozen）资源走 paths.py 定位，数据放用户目录。

实现说明：用 pywebview 提供原生窗口（Edge WebView2 内核，Win10/11 自带），
窗口关掉时自动结束后端进程。
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser


def _wait_backend(url: str, retries: int = 60, delay: float = 0.25) -> bool:
    import urllib.request
    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(delay)
    return False


def _backend_thread(url: str):
    """在新线程里跑 uvicorn（阻塞），供桌面入口调用。"""
    from weibo_export.server import app
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=_port_of(url), log_level="warning")


def _port_of(url: str) -> int:
    from urllib.parse import urlparse
    return urlparse(url).port or 8765


def main() -> None:
    PORT = 8765
    URL = f"http://127.0.0.1:{PORT}/"

    # 1) 后台线程启动服务
    t = threading.Thread(target=_backend_thread, args=(URL,), daemon=True)
    t.start()

    # 2) 等服务就绪
    if not _wait_backend(URL):
        # 兜底：服务起不来就退回浏览器方式并提示
        webbrowser.open(URL)
        print("后端服务启动超时，已在浏览器打开；若空白请稍后刷新。")
        try:
            while t.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    # 3) 打开原生窗口
    try:
        import webview  # pywebview
        import os
        _ico = os.path.join(sys._MEIPASS, "app.ico") if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS") \
            else os.path.join(os.path.dirname(os.path.abspath(__file__)), "build", "app.ico")
        webview.create_window(
            "极客港 · 微博导出工具",
            URL,
            width=1080,
            height=900,
            min_size=(760, 600),
            icon=_ico if os.path.exists(_ico) else None,
        )
        webview.start()
    except Exception:
        # pywebview 不可用时退回默认浏览器，保证功能可用
        webbrowser.open(URL)
        print(f"已用默认浏览器打开：{URL}（关闭此窗口请结束本程序）")
        try:
            while t.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
