# -*- mode: python ; coding: utf-8 -*-
"""极客港 · 微博导出工具（桌面版）—— PyInstaller 打包配置。

打包命令（在 weibo-export/ 目录下）：
    pyinstaller desktop_gui.spec --noconfirm
产物：dist/极客港微博导出/极客港微博导出.exe（onedir，启动快、不易被杀软误报）

v1.1.0 架构变更：
    - 移除 FastAPI + uvicorn + pywebview（不再依赖 WebView2 运行时）
    - 改用 tkinter 原生 GUI（Python 自带，Win10/11 开箱即用）
    - 数据目录：~/Documents/微博导出/（weibo.db + 导出文件）
"""

import os

a = Analysis(
    ['desktop_app.py'],
    pathex=[os.path.abspath('.')],
    binaries=[],
    datas=[
        # 应用图标
        (os.path.join('assets', 'app.ico'), '.'),
    ],
    hiddenimports=[
        # 只保留核心引擎依赖（tkinter 由 PyInstaller 自动检测）
        'reportlab',
        # v1.2.0：可选增强——内置浏览器一键登录取 Cookie（无 WebView2 时自动降级为手动粘贴）
        'webview',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 排除本项目用不到的巨型依赖，显著减小产物体积
        'scipy', 'matplotlib', 'pandas', 'numpy',
        'PyQt5', 'PySide2', 'PyQt6', 'PySide6',
        'IPython', 'notebook', 'jupyter',
        'pytest', 'setuptools', 'pip', 'wheel',
        'scrapy',
        # v1.1.0 起不再依赖（保留 webview 作为 v1.2.0 可选增强）
        'uvicorn', 'fastapi', 'pydantic',
        'starlette', 'anyio', 'h11',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='极客港微博导出',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join('assets', 'app.ico'),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='极客港微博导出',
)
