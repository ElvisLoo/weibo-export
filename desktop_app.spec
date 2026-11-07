# -*- mode: python ; coding: utf-8 -*-
"""极客港 · 微博导出工具 —— PyInstaller 打包配置。

打包命令（在 weibo-export/ 目录下）：
    pyinstaller desktop_app.spec --noconfirm
产物：dist/极客港微博导出/极客港微博导出.exe（onedir，启动更快、不易被杀软误报）

说明：单目录模式把 static 界面等只读资源放在 _MEIPASS/weibo_export/static，
与 paths.resource_dir() 对应；weibo.db 与 exports 写入用户目录，不丢数据。
"""

import os

a = Analysis(
    ['desktop_app.py'],
    pathex=[os.path.abspath('.')],
    binaries=[],
    datas=[
        # 把界面静态资源打进包，目标路径与 paths.static_dir() 一致
        (os.path.join('weibo_export', 'static'), os.path.join('weibo_export', 'static')),
        # 应用图标（窗口标题栏 / 任务栏用）
        (os.path.join('build', 'app.ico'), '.'),
    ],
    hiddenimports=[
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'fastapi',
        'jinja2',
        'reportlab',
        'webview',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
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
    icon=os.path.join('build', 'app.ico'),
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
