"""运行时资源与数据目录定位。

打包（PyInstaller 单文件）后，``__file__`` 指向临时解压目录，且每次启动不同，
因此必须区分两类路径：

1. 只读资源（static 界面等）——打进 exe，用 ``sys._MEIPASS`` 或源码目录定位。
2. 可写数据（weibo.db、exports/）——绝不能放临时目录（会丢），
   统一放到用户目录下的固定位置：``~/极客港微博导出/``。
"""

from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的 exe 中。"""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_dir() -> str:
    """只读资源目录（含 static/ 界面）。开发态为包目录；打包后为解压目录。"""
    if is_frozen():
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def _documents_dir() -> str:
    """定位「我的文档」目录，优先用 Windows API（可正确处理 OneDrive 重定向），
    失败则退回 ~/Documents 或用户目录。跨平台下退化为用户目录。"""
    if os.name == "nt":
        try:
            import ctypes.wintypes
            CSIDL_PERSONAL = 0x0005
            SHGFP_TYPE_CURRENT = 0
            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_PERSONAL, None,
                                                      SHGFP_TYPE_CURRENT, buf) == 0:
                d = buf.value
                if d and os.path.isdir(d):
                    return d
        except Exception:
            pass
        cand = os.path.join(os.path.expanduser("~"), "Documents")
        if os.path.isdir(cand):
            return cand
    # 非 Windows：退化为用户目录下的「文档」
    return os.path.expanduser("~")


def data_dir() -> str:
    """可写数据目录：存放 weibo.db（程序内部存档，不放进用户文档）。
    开发态用项目根，打包后用用户目录。"""
    if is_frozen():
        base = os.path.join(os.path.expanduser("~"), "极客港微博导出")
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(base, exist_ok=True)
    return base


def export_dir() -> str:
    """默认导出目录：优先 D 盘根目录的「微博导出」，D 盘不存在时回退。

    用户明确要求把默认导出目录改到 D 盘根目录，微博导出文件夹。
    """
    # 尝试 D 盘（Windows）
    if os.name == "nt":
        d_path = "D:\\微博导出"
        try:
            if os.path.exists("D:\\") or os.access("D:\\", os.W_OK):
                os.makedirs(d_path, exist_ok=True)
                return d_path
        except (OSError, PermissionError):
            pass

    # 回退到用户目录
    fallback = os.path.join(os.path.expanduser("~"), "微博导出")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def static_dir() -> str:
    """界面静态资源目录。
    打包后：_MEIPASS/weibo_export/static（与 spec 的 datas 目标一致）
    开发态：weibo_export/static（本包同级）
    """
    if is_frozen():
        return os.path.join(sys._MEIPASS, "weibo_export", "static")  # type: ignore[attr-defined]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def default_db_path() -> str:
    return os.path.join(data_dir(), "weibo.db")


def default_export_dir() -> str:
    """导出目录优先级：
    1. 环境变量 EXPORT_DIR（服务器部署用，如 /www/wwwroot/weibo-export/微博导出）
    2. Windows D 盘根目录 微博导出（用户明确要求）
    3. Windows 回退 ~/Documents/微博导出
    4. Linux 默认 ~/微博导出
    """
    # 优先级 1：环境变量（服务器 / systemd / Docker 场景）
    env_dir = os.environ.get("EXPORT_DIR", "").strip()
    if env_dir:
        try:
            os.makedirs(env_dir, exist_ok=True)
            return env_dir
        except (OSError, PermissionError):
            pass  # 环境变量路径不可写时回退到默认逻辑，避免服务启动失败

    # 优先级 2-3：Windows 默认逻辑
    if os.name == "nt":
        d_path = "D:\\微博导出"
        try:
            if os.path.exists("D:\\") or os.access("D:\\", os.W_OK):
                os.makedirs(d_path, exist_ok=True)
                return d_path
        except (OSError, PermissionError):
            pass
        fallback = os.path.join(_documents_dir(), "微博导出")
        os.makedirs(fallback, exist_ok=True)
        return fallback

    # 优先级 4：Linux / macOS 默认
    fallback = os.path.join(os.path.expanduser("~"), "微博导出")
    os.makedirs(fallback, exist_ok=True)
    return fallback
