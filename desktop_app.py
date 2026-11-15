"""极客港 · 微博导出工具 —— 纯本地桌面版（无 Web 服务、无 WebView2 依赖）。

架构：
    tkinter 原生 GUI（Python 自带，Win10/11 无需额外运行时）
    └─ 后台线程跑 crawler（20 秒限速、断点续传）
    └─ 抓取完成后自动跑 exporter 生成 5 格式到用户目录
    └─ 进度用 root.after() 从后台线程安全更新

数据目录：
    DB:    ~/Documents/微博导出/weibo.db
    导出:  ~/Documents/微博导出/

打包：
    pyinstaller desktop_gui.spec --noconfirm
    产物: dist/极客港微博导出/极客港微博导出.exe（onedir）
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText

from weibo_export.crawler import _parse_uid_input


def _data_dir() -> str:
    """数据目录：文档/微博导出/（仅存放 weibo.db 与日志）。"""
    base = os.path.join(os.path.expanduser("~"), "Documents", "微博导出")
    os.makedirs(base, exist_ok=True)
    return base


def _export_dir() -> str:
    """默认导出目录：优先 D 盘根目录「微博导出」，D 盘不存在时回退用户目录。

    独立于 _data_dir：数据库仍放在 _data_dir，导出的文件/图片走这里。
    """
    from weibo_export.paths import export_dir
    return export_dir()


def _log_dir() -> str:
    return _data_dir()


def _write_log(msg: str):
    try:
        with open(os.path.join(_log_dir(), "运行日志.txt"), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg.rstrip()}\n")
    except Exception:
        pass


class WeiboExporterGUI:
    def __init__(self, root: tk.Tk):
        from weibo_export import __version__
        self.root = root
        # 窗口几何（尺寸/位置/居中/自适应屏幕）统一在 main() 里设置，
        # 避免这里硬编码覆盖掉 main() 的自适应逻辑。
        root.title(f"极客港 · 微博导出工具 v{__version__}")
        root.minsize(800, 600)
        # 窗口图标（打包后从 _MEIPASS 加载，源码运行从 assets/ 加载）
        self._set_window_icon(root)

        # 状态
        self.crawl_thread = None
        self.export_thread = None
        self.stop_flag = threading.Event()
        self.job_state = {}
        self.job_lock = threading.Lock()
        self.pages_fetched = 0  # 已成功抓取页数（平滑进度用）

        self._build_ui()
        self._log("启动完成")

    def _set_window_icon(self, root):
        """设置窗口左上角图标（微博眼睛）。"""
        try:
            if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
                icon = os.path.join(sys._MEIPASS, "app.ico")
            else:
                icon = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "assets", "app.ico")
            if os.path.exists(icon):
                root.iconbitmap(icon)
        except Exception:
            pass  # 图标加载失败不影响主功能

    # ================= 页数调数 =================

    def _get_cookie(self) -> str:
        """从多行文本框读取 Cookie（去掉每行首尾空白后拼回单行）。"""
        raw = self.cookie_text.get("1.0", "end-1c")
        return "".join(line.strip() for line in raw.splitlines())

    def _nudge_pages(self, delta: int):
        """点击 −/＋ 按钮调整最大页数（最小 0，最大 9999）。"""
        try:
            cur = int(self.var_max_pages.get())
        except Exception:
            cur = 0
        self.var_max_pages.set(max(0, min(9999, cur + delta)))

    # ================= UI 构建 =================

    def _icon_path(self):
        """定位 app.ico（打包后从 _MEIPASS，源码从 assets/）。"""
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            return os.path.join(sys._MEIPASS, "app.ico")
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "assets", "app.ico")

    def _make_logo_photo(self, icon_path: str):
        """把 app.ico 转成 64px logo（供品牌区展示）。

        根因：图标本身是"橙色圆角方块"，但圆角以外的四个角在原图里就是**实心黑**
        （像素实测：从角到 (8,8) 一带 alpha=255 的纯黑），不是透明。所以单纯改
        透明度、加透明蒙版都治不了——黑角是被"画出来"的，不是"没抠掉"。

        正确做法（即用户最初指出的）：把圆角矩形以外的区域**填成窗口背景色**，
        让四角与浅灰窗口融为一体，彻底看不到黑角。用不透明的 RGB 图避免
        ImageTk 的 alpha 合成再引入暗边。
        """
        from PIL import Image, ImageDraw, ImageTk
        img = Image.open(icon_path)
        img.load()
        img.size = (256, 256)  # 取最大帧
        # logo 48px：品牌区紧凑些，把纵向空间让给底部进度区（日志框）
        logo = img.convert("RGBA").resize((40, 40), Image.LANCZOS)
        # 窗口背景色（与 tk 默认浅灰一致）。圆角半径按像素实测：橙色方块铺满整图、
        # 对角线第一个橙像素在 k=11(256) → R≈37.6px(256)=9.4px(64)→7px(48)，取 7。
        bg = (240, 240, 240)
        canvas = Image.new("RGBA", (40, 40), bg + (255,))
        mask = Image.new("L", (40, 40), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, 39, 39], radius=6, fill=255)
        canvas.paste(logo, (0, 0), mask)   # 只取圆角内的图标像素，角外留背景色
        return ImageTk.PhotoImage(canvas.convert("RGB"))

    def _build_brand(self, parent):
        """顶部品牌区：大 logo 图标 + 标题 + 副标题。

        logo 用 PIL 把 app.ico 的 256px 帧转成 PhotoImage 放大到 64px，
        比 16px 标题栏图标醒目得多，解决"左上角 logo 太小"。
        """
        frm = ttk.Frame(parent)
        frm.pack(fill="x")
        # 左侧大 logo（64px）
        try:
            icon = self._icon_path()
            if os.path.exists(icon):
                self._logo_photo = self._make_logo_photo(icon)  # 保引用防 GC
                if self._logo_photo is not None:
                    tk.Label(frm, image=self._logo_photo).pack(side="left", padx=(0, 12))
        except Exception as e:
            _write_log(f"加载品牌 logo 失败：{e}")
        # 右侧标题 + 副标题
        txt = ttk.Frame(frm)
        txt.pack(side="left", fill="y")
        ttk.Label(txt, text="极客港 · 微博导出工具",
                  font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        from weibo_export import __version__
        ttk.Label(txt, text=f"本地运行 · 数据不出电脑 · v{__version__}",
                  font=("Microsoft YaHei UI", 11),
                  foreground="gray").pack(anchor="w")

    def _build_ui(self):
        # 全局字体：按用户偏好放大（在 v1.1.2 基础上再大一号）
        self.font_label = ("Microsoft YaHei UI", 13)
        self.font_hint = ("Microsoft YaHei UI", 11)
        self.font_btn = ("Microsoft YaHei UI", 13, "bold")
        # 日志用系统默认中文字体（Consolas 无中文字形，中文会回退渲染导致发虚）
        self.font_log = ("Microsoft YaHei UI", 12)
        self.font_progress = ("Microsoft YaHei UI", 14, "bold")

        # --- 顶部品牌区：大 logo + 标题（让左上角标识醒目）---
        # 压缩 padding，把纵向空间让给底部进度区（进度区空间不足会被挤扁）
        frm_brand = ttk.Frame(self.root, padding=(18, 8, 18, 2))
        frm_brand.pack(fill="x")
        self._build_brand(frm_brand)

        style = ttk.Style()
        try:
            style.configure("TLabel", font=self.font_label)
            style.configure("TButton", font=self.font_btn, padding=6)
            style.configure("TCheckbutton", font=self.font_label)
            style.configure("TLabelframe.Label", font=self.font_label)
            style.configure("TEntry", font=self.font_label)
            # 页数 −/＋ 大号按钮样式
            style.configure("Big.TButton", font=("Microsoft YaHei UI", 15, "bold"), padding=2)
        except Exception:
            pass

        # --- 输入区 ---
        frm_input = ttk.LabelFrame(self.root, text=" 1. 输入信息 ", padding=12)
        frm_input.pack(fill="x", padx=14, pady=(6, 3))

        frm_input.columnconfigure(0, minsize=92)   # 标签列固定宽，右对齐贴向冒号
        frm_input.columnconfigure(2, weight=1)      # 输入框列自适应
        frm_input.columnconfigure(3, minsize=236)   # 提示列固定宽 → 三条提示词竖直对齐

        # 列职责：0=标签(右对齐) 1=冒号(独立列,竖直对齐) 2=输入框 3=提示(固定列,三条对齐)
        # 行职责（每行独占，互不冲突）：0=微博ID 1=Cookie按钮 2=Cookie文本框 3=最大页数
        row = 0
        ttk.Label(frm_input, text="微博 ID", font=self.font_label).grid(
            row=row, column=0, sticky="e", pady=5)
        ttk.Label(frm_input, text="：", font=self.font_label).grid(
            row=row, column=1, sticky="w", pady=5)
        self.var_uid = tk.StringVar()
        uid_entry = ttk.Entry(frm_input, textvariable=self.var_uid, width=22,
                              font=self.font_label)
        uid_entry.grid(row=row, column=2, sticky="w", pady=3)
        # 支持直接粘贴微博主页链接，自动识别出数字 ID（小白友好）
        uid_entry.bind("<KeyRelease>", self._try_resolve_uid)
        ttk.Label(frm_input, text="纯数字或主页链接，如：123456789", font=self.font_hint,
                  foreground="gray").grid(row=row, column=3, sticky="w", pady=3, padx=(10, 0))

        row += 1  # row=1：Cookie 标签 + 一键登录取Cookie 按钮（独占一行）
        ttk.Label(frm_input, text="Cookie", font=self.font_label).grid(
            row=row, column=0, sticky="e", pady=5)
        ttk.Label(frm_input, text="：", font=self.font_label).grid(
            row=row, column=1, sticky="w", pady=5)
        btn_holder = ttk.Frame(frm_input)
        btn_holder.grid(row=row, column=2, columnspan=2, sticky="w", pady=3)
        self.btn_login = ttk.Button(btn_holder, text="登录取Cookie",
                                    command=self._start_browser_login)
        self.btn_login.pack(side="left")
        ttk.Label(btn_holder, text="  点此在软件内登录，自动填入下方 Cookie",
                  font=self.font_hint, foreground="gray").pack(side="left")

        row += 1  # row=2：Cookie 多行文本框（独占一行，避免与最大页数冲突）
        self.cookie_text = tk.Text(frm_input, height=1, wrap="none",
                                   font=self.font_label, relief="solid", borderwidth=1)
        self.cookie_text.grid(row=row, column=2, sticky="we", pady=3)
        ttk.Label(frm_input, text="也可粘贴网页 Cookie\n不填仅抓公开博文", font=self.font_hint,
                  foreground="gray", justify="left").grid(
            row=row, column=3, sticky="nw", pady=5, padx=(10, 0))

        row += 1  # row=3：最大页数（独占一行）
        ttk.Label(frm_input, text="最大页数", font=self.font_label).grid(
            row=row, column=0, sticky="e", pady=5)
        ttk.Label(frm_input, text="：", font=self.font_label).grid(
            row=row, column=1, sticky="w", pady=5)
        # 页数：输入框 + 大号 −/＋ 按钮（原生 Spinbox 箭头太小，换成可点的方按钮）
        self.var_max_pages = tk.IntVar(value=0)
        pages_box = ttk.Frame(frm_input)
        pages_box.grid(row=row, column=2, sticky="w", pady=3)
        ttk.Entry(pages_box, textvariable=self.var_max_pages, width=8,
                  font=self.font_label).pack(side="left")
        # 大号 −/＋ 按钮（原生 Spinbox 箭头太小）
        ttk.Button(pages_box, text="－", width=3, style="Big.TButton",
                   command=lambda: self._nudge_pages(-1)).pack(side="left", padx=(6, 3))
        ttk.Button(pages_box, text="＋", width=3, style="Big.TButton",
                   command=lambda: self._nudge_pages(1)).pack(side="left", padx=3)
        ttk.Label(frm_input, text="0 = 抓取全部", font=self.font_hint,
                  foreground="gray").grid(row=row, column=3, sticky="w", pady=3, padx=(10, 0))
        # --- 导出格式 ---
        frm_fmt = ttk.LabelFrame(self.root, text=" 2. 导出格式 ", padding=12)
        frm_fmt.pack(fill="x", padx=14, pady=3)

        self.fmt_vars = {}
        for i, fmt in enumerate(["html", "md", "pdf", "json", "txt"]):
            v = tk.BooleanVar(value=True)
            self.fmt_vars[fmt] = v
            ttk.Checkbutton(frm_fmt, text=fmt.upper(), variable=v).grid(
                row=0, column=i, sticky="w", padx=10)

        # --- 操作按钮 ---
        frm_btn = ttk.Frame(self.root, padding=(14, 8))
        frm_btn.pack(fill="x")

        self.btn_crawl = ttk.Button(frm_btn, text="抓取微博", command=self._start_crawl)
        self.btn_crawl.pack(side="left", padx=(0, 10))

        self.btn_export = ttk.Button(frm_btn, text="导出文件", command=self._start_export)
        self.btn_export.pack(side="left", padx=(0, 10))

        # 补全长文：只扫库里已是截断版的长博文，逐条调全文接口补全，
        # 无需重新抓几百条。用于 v1.3.7 及更早版本抓的旧数据。
        self.btn_fill = ttk.Button(frm_btn, text="补全长文",
                                   command=self._start_fill_longtext)
        self.btn_fill.pack(side="left", padx=(0, 10))

        self.btn_stop = ttk.Button(frm_btn, text="停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left")

        # --- 导出目录：单独占一整行，完整显示路径，不被按钮挤压截断 ---
        frm_dir = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        frm_dir.pack(fill="x")
        ttk.Label(frm_dir, text="导出目录：", font=self.font_hint,
                  foreground="gray").pack(side="left")
        ttk.Label(frm_dir, text=_export_dir(), font=self.font_hint,
                  foreground="gray").pack(side="left")
        ttk.Button(frm_dir, text="打开目录", command=lambda: self._open_dir(_export_dir())
                   ).pack(side="right")

        # --- 进度 ---
        frm_progress = ttk.LabelFrame(self.root, text=" 3. 任务进度 ", padding=12)
        frm_progress.pack(fill="both", expand=True, padx=14, pady=(3, 4))
        self._frm_progress = frm_progress  # 供布局诊断（环境变量 WEIBO_LAYOUT_DEBUG）

        self.var_progress = tk.StringVar(value="就绪")
        ttk.Label(frm_progress, textvariable=self.var_progress,
                  font=self.font_progress).pack(anchor="w", pady=4)

        # 进度条：确定模式，用百分比真实反映抓取/导出进度（不再来回滑动）
        # 进度条太窄（默认很扁）→ 用自定义 style 加大厚度，视觉更清晰；
        # troughcolor（未填充轨道）默认深灰、和已填充色都偏暗 → 改成主题浅色，
        # 这样进度条不再显得「又窄又看不清」。
        try:
            style.configure("Wb.Horizontal.TProgressbar", thickness=22,
                            troughcolor="#cfd8dc", background="#2e86de",
                            bordercolor="#b0bec5", lightcolor="#2e86de",
                            darkcolor="#2e86de")
        except Exception:
            pass
        self.progress_bar = ttk.Progressbar(frm_progress, mode="determinate",
                                            style="Wb.Horizontal.TProgressbar",
                                            length=200, maximum=100, value=0)
        self.progress_bar.pack(fill="x", pady=(0, 4))

        # 黑色日志框：它是唯一 expand=True 的可伸缩控件。当「其余固定内容总高」已
        # 等于/超过窗口客户区（约 685px）时，Tkinter 会把它压到只剩一条（实测 23px）。
        # 把请求高度降到 3 行：内容总需求降到窗口高度以内，日志框就有余量正常显示，
        # 窗口更高时它仍随 expand 撑满剩余空间。
        self.log_text = ScrolledText(frm_progress, height=3, state="disabled",
                                     font=self.font_log, bg="#1e1e1e", fg="#d4d4d4")
        self.log_text.pack(fill="both", expand=True, pady=(4, 0))

    # ================= 日志 =================

    def _open_dir(self, path: str):
        """在系统文件管理器中打开目录（Windows）。"""
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)  # Windows 专用
        except Exception as e:
            self._log(f"无法打开目录：{e}")

    def _log(self, msg: str):
        self.log_text.config(state="normal")
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        _write_log(msg)

    def _set_progress(self, text: str):
        self.var_progress.set(text)

    def _try_resolve_uid(self, event=None):
        """输入框内容变化时，若看起来是微博主页链接，自动识别出数字 ID。"""
        raw = self.var_uid.get().strip()
        uid, needs_net = _parse_uid_input(raw)
        if needs_net is None:
            return  # 不是链接（纯数字或普通文本），不打扰
        if uid:
            # 本地直接识别到数字 ID，自动回填
            if self.var_uid.get() != uid:
                self.var_uid.set(uid)
                self._log(f"已从链接识别微博 ID：{uid}")
        else:
            # weibo.com/自定义域名 需联网换 UID，后台解析（不阻塞输入框）
            if not getattr(self, "_uid_resolving", False):
                self._uid_resolving = True
                self._set_progress("正在识别主页链接...")
                threading.Thread(target=self._resolve_uid_async,
                                 args=(raw,), daemon=True).start()

    def _resolve_uid_async(self, url: str):
        """后台把 weibo.com/自定义域名 解析成数字 UID。"""
        from weibo_export.crawler import build_crawler

        uid, log_msg, ok = "", "", False
        try:
            crawler = build_crawler(kind="weibo", cookie=self._get_cookie(),
                                    progress=None, stop_flag=lambda: False)
            info = crawler.get_user_for_url(url)
            uid = (info or {}).get("user_id", "")
            if uid and uid.isdigit():
                ok = True
            else:
                log_msg = "该主页链接未识别到数字 ID，请改填纯数字 ID"
        except Exception as e:
            log_msg = f"识别主页链接失败：{e}"

        def apply():
            self._uid_resolving = False
            if ok:
                self.var_uid.set(uid)
                self._log(f"已从主页链接识别微博 ID：{uid}")
                self._set_progress("就绪")
            else:
                self._log(log_msg or "无法识别该主页链接，请确认链接正确或改填纯数字 ID")
                self._set_progress("识别失败，请检查链接或改填纯数字 ID")
        self.root.after(0, apply)

    # ---------- 一键登录取 Cookie（可选增强，Option A） ----------

    def _start_browser_login(self):
        """打开内置浏览器登录微博，登录后自动把 Cookie 填入 Cookie 栏。

        依赖 pywebview + WebView2 运行时；不可用时给出手动粘贴的引导。
        """
        from weibo_export import browser_login

        def on_success(cookie_header: str):
            self.cookie_text.delete("1.0", "end")
            self.cookie_text.insert("1.0", cookie_header)
            self._log(f"✅ 已自动获取登录 Cookie（{len(cookie_header)} 字符），可直接抓取")
            self._set_progress("已自动获取 Cookie，就绪")

        def on_error(reason: str):
            self._log(f"⚠ {reason}")
            self._set_progress("自动登录不可用，可手动粘贴 Cookie")
            messagebox.showinfo("无法自动登录", reason)

        if not browser_login.is_available():
            on_error(
                "当前系统未安装 WebView2 运行时，无法打开内置浏览器。\n\n"
                "【自动登录】需要安装 Microsoft Edge WebView2 运行时（一次性安装）。\n"
                "【手动方式】在浏览器登录微博后，按 F12 → 网络/Network → 刷新 → "
                "找任一请求 → 复制 Cookie 请求头粘贴到上面 Cookie 栏。")
            return

        self._set_progress("正在打开登录窗口，请在窗口内登录微博...")
        self._log("正在打开微博登录窗口，请在弹出窗口内完成登录...")
        browser_login.launch_login(self.root, on_success, on_error)

    def _start_crawl(self):
        uid = self._resolve_uid_from_input()
        if not uid:
            return
        cookie = self._get_cookie().strip()
        max_pages = self.var_max_pages.get()

        self.btn_crawl.config(state="disabled")
        self.btn_export.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.stop_flag.clear()
        self.pages_fetched = 0
        self._set_progress("正在启动抓取...")
        self.progress_bar["value"] = 0  # 归零，等待真实进度填充

        self.crawl_thread = threading.Thread(
            target=self._crawl_worker,
            args=(uid, cookie, max_pages),
            daemon=True,
        )
        self.crawl_thread.start()

    def _crawl_worker(self, uid: str, cookie: str, max_pages: int):
        from weibo_export import db, crawler as crawler_mod

        def progress_cb(payload: dict):
            with self.job_lock:
                self.job_state.update(payload)
            # 触发 UI 更新（从后台线程安全调用）
            self.root.after(0, lambda: self._update_progress_ui())

        db_path = os.path.join(_data_dir(), "weibo.db")
        conn = None
        crawler = None
        try:
            conn = db.connect(db_path)

            crawler = crawler_mod.build_crawler(
                kind="weibo",
                cookie=cookie,
                interval=20,
                progress=progress_cb,
                stop_flag=lambda: self.stop_flag.is_set(),
            )
            result = crawler.run(conn, uid, max_pages=max_pages, resume=True)

            self.root.after(0, lambda: self._log(
                f"抓取完成：入库 {result.total_posts} 条，{result.pages_fetched} 页"
                + (f"，原因：{result.stopped_reason}" if result.stopped_reason else "")
            ))
            self.root.after(0, lambda: self._set_progress(
                f"抓取完成：{result.total_posts} 条博文已入库"))
        except Exception as e:
            self.root.after(0, lambda: self._log(f"抓取失败：{e}"))
            self.root.after(0, lambda: self._set_progress(f"抓取出错：{e}"))
            _write_log(f"抓取异常：{traceback.format_exc()}")
        finally:
            # 无论成功/异常都释放连接与底层 socket 连接池，避免长会话累积句柄
            if crawler is not None:
                try:
                    crawler.session.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self.root.after(0, lambda: self._ui_idle())

    def _update_progress_ui(self):
        with self.job_lock:
            state = dict(self.job_state)
        t = state.get("type", "")
        if t == "page_start":
            self._set_progress(f"正在抓取第 {state.get('page', '?')} 页...")
        elif t == "page_done":
            self.pages_fetched += 1
            # 抓取进度：以「已完成页数 / 用户设定的最大页数」填充。
            # 最大页数为 0（抓到底）时，用当前数据页作为估计，保持推进感。
            total = state.get("max_pages") or state.get("page") or self.pages_fetched
            total = max(1, int(total))
            pct = min(99, int(self.pages_fetched / total * 100))
            self.progress_bar["value"] = pct
            self._set_progress(
                f"已完成 {self.pages_fetched} 页（当前数据页 {state.get('page', '?')}），"
                f"新增 {state.get('new_posts', 0)} 条，累计入库 {state.get('total_in_db', 0)} 条"
                f"（{pct}%）")
            self._log(f"第 {state.get('page', '?')} 页完成：新增 {state.get('new_posts', 0)} 条")
        elif t == "retry":
            # 抓取遇到瞬时错误正在重试，给用户明确反馈（而不是看起来卡住）
            self._log(
                f"⚠ 请求失败，第 {state.get('attempt', '?')}/{state.get('max_retries', '?')} 次重试，"
                f"等待 {state.get('wait', '?')} 秒（{state.get('error', '')}）")
            self._set_progress(
                f"网络波动，第 {state.get('attempt', '?')} 次重试中...")
        elif t == "longtext_fetched":
            # 长博文全文补全成功（抓取过程中）
            self._log(
                f"📄 已补全长博文全文（{state.get('mblog_id', '')}，"
                f"{state.get('len', 0)} 字）")
        elif t == "longtext_progress":
            # 补全长文过程中：按 已处理条数 / 总条数 填充进度条
            idx = max(0, state.get("index", 0))
            total = max(1, state.get("total", 1))
            pct = min(99, int(idx / total * 100))
            self.progress_bar["value"] = pct
            self._set_progress(
                f"补全长文：{idx}/{total} 条，已补全 {state.get('filled', 0)} 条"
                f"（{pct}%）")
        elif t == "longtext_done":
            total = state.get("total", 0)
            filled = state.get("filled", 0)
            self.progress_bar["value"] = 100
            self._set_progress(
                f"长文补全完成：补全 {filled}/{total} 条")

    def _start_fill_longtext(self):
        uid = self._resolve_uid_from_input()
        if not uid:
            return
        cookie = self._get_cookie().strip()
        if not cookie:
            messagebox.showinfo(
                "需要 Cookie",
                "补全长文需要登录态 Cookie（要用登录 Cookie 才能调全文接口）。\n"
                "请先在上方 Cookie 栏粘贴，或点「登录取 Cookie」自动获取。")
            return

        self.btn_crawl.config(state="disabled")
        self.btn_export.config(state="disabled")
        self.btn_fill.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.stop_flag.clear()
        self._set_progress("正在扫描待补全长博文...")

        self.fill_thread = threading.Thread(
            target=self._fill_worker,
            args=(uid, cookie),
            daemon=True,
        )
        self.fill_thread.start()

    def _fill_worker(self, uid: str, cookie: str):
        from weibo_export import db, crawler as crawler_mod

        def progress_cb(payload: dict):
            with self.job_lock:
                self.job_state.update(payload)
            self.root.after(0, lambda: self._update_progress_ui())

        db_path = os.path.join(_data_dir(), "weibo.db")
        conn = None
        crawler = None
        try:
            conn = db.connect(db_path)
            crawler = crawler_mod.build_crawler(
                kind="weibo", cookie=cookie, interval=20,
                progress=progress_cb,
                stop_flag=lambda: self.stop_flag.is_set(),
            )
            result = crawler.fill_longtext(conn, uid)
            self.root.after(0, lambda: self._log(
                f"长文补全完成：发现 {result['total']} 条，"
                f"成功补全 {result['filled']} 条"
                + ("（其余可能是受限/已删微博，接口拿不到全文）"
                   if result['filled'] < result['total'] else "")))
            self.root.after(0, lambda: self._set_progress(
                f"长文补全完成：补全 {result['filled']}/{result['total']} 条"))
            self.root.after(0, lambda: messagebox.showinfo(
                "补全长文完成",
                f"扫描到 {result['total']} 条截断长博文\n"
                f"成功补全 {result['filled']} 条。\n\n"
                f"现在可以点「导出文件」重新导出，长博文即为完整正文。"))
        except Exception as e:
            self.root.after(0, lambda: self._log(f"长文补全失败：{e}"))
            self.root.after(0, lambda: self._set_progress(f"长文补全出错：{e}"))
            _write_log(f"长文补全异常：{traceback.format_exc()}")
        finally:
            if crawler is not None:
                try:
                    crawler.session.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self.root.after(0, lambda: self._ui_idle())

    def _ui_idle(self):
        try:
            self.progress_bar.stop()
        except Exception:
            pass
        self.btn_crawl.config(state="normal")
        self.btn_export.config(state="normal")
        self.btn_fill.config(state="normal")
        self.btn_stop.config(state="disabled")

    def _update_export_bar(self, pct: int):
        """导出进度条：线程安全地更新为确定百分比。"""
        try:
            self.progress_bar["value"] = max(0, min(100, int(pct)))
        except Exception:
            pass

    # ================= 导出 =================

    def _resolve_uid_from_input(self) -> Optional[str]:
        """从输入框解析出纯数字 ID（支持直接粘贴主页链接）。

        返回数字 ID；无法识别时弹提示并返回 None。
        """
        raw = self.var_uid.get().strip()
        if raw.isdigit():
            return raw
        uid, needs_net = _parse_uid_input(raw)
        if needs_net is None:
            messagebox.showwarning(
                "提示", "请输入纯数字微博用户 ID\n或粘贴微博主页链接（支持 weibo.com/u/…、m.weibo.cn/u/…）")
            return None
        if needs_net is False and uid:
            self.var_uid.set(uid)
            return uid
        # 自定义域名需联网换 UID（用户已明确点按钮，主线程同步解析可接受）
        from weibo_export.crawler import build_crawler
        self._set_progress("正在识别主页链接...")
        try:
            crawler = build_crawler(kind="weibo", cookie=self._get_cookie(),
                                    progress=None, stop_flag=lambda: False)
            info = crawler.get_user_for_url(raw)
            uid = (info or {}).get("user_id", "")
        except Exception as e:
            messagebox.showwarning("提示", f"识别主页链接失败：\n{e}\n\n请改填纯数字 ID")
            return None
        if not (uid and uid.isdigit()):
            messagebox.showwarning(
                "提示", f"无法识别该主页链接：\n{raw}\n\n请确认链接正确或改填纯数字 ID")
            return None
        self.var_uid.set(uid)
        self._log(f"已从主页链接识别微博 ID：{uid}")
        return uid

    def _start_export(self):
        uid = self._resolve_uid_from_input()
        if not uid:
            return
        fmts = [f for f, v in self.fmt_vars.items() if v.get()]
        if not fmts:
            messagebox.showwarning("提示", "请至少选择一种导出格式")
            return

        self.btn_crawl.config(state="disabled")
        self.btn_export.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.stop_flag.clear()

        self.export_thread = threading.Thread(
            target=self._export_worker, args=(uid, fmts), daemon=True,
        )
        self.export_thread.start()

    def _export_worker(self, uid: str, fmts: list):
        from weibo_export import db, exporter

        def export_progress(payload: dict):
            # 导出进度：按「已完成格式数 / 总格式数」填充进度条
            idx = payload.get("index", 0)
            total = max(1, payload.get("total", 1))
            pct = int(idx / total * 100)
            fmt = payload.get("fmt", "")
            ptype = payload.get("type")
            if ptype == "export_done":
                # 用默认参数固化 fmt/pct，避免 lambda 捕获可变变量
                self.root.after(0, lambda f=fmt, i=idx, t=total: self._set_progress(
                    f"已导出 {f}（{i}/{t}）"))
            self.root.after(0, lambda p=pct: self._update_export_bar(p))

        db_path = os.path.join(_data_dir(), "weibo.db")
        conn = None
        try:
            conn = db.connect(db_path)
            if db.count_posts(conn, uid) == 0:
                self.root.after(0, lambda: messagebox.showinfo(
                    "无数据", f"用户 {uid} 在本地数据库中暂无博文，请先抓取。"))
                return
            out_dir = _export_dir()
            self.root.after(0, lambda: self.progress_bar.__setitem__("value", 0))
            results = exporter.export_all(conn, uid, out_dir, fmts,
                                          progress=export_progress)
            files = [f"{r['format']}: {os.path.basename(r['path'])} ({r['size']} B)"
                     for r in results]
            self.root.after(0, lambda: self._log("导出完成：\n  " + "\n  ".join(files)))
            self.root.after(0, lambda: self._set_progress(
                f"导出完成，共 {len(results)} 个文件 → {out_dir}"))
            self.root.after(0, lambda: messagebox.showinfo(
                "导出完成", f"已生成 {len(results)} 个文件到：\n{out_dir}"))
        except Exception as e:
            self.root.after(0, lambda: self._log(f"导出失败：{e}"))
            self.root.after(0, lambda: self._set_progress(f"导出出错：{e}"))
            _write_log(f"导出异常：{traceback.format_exc()}")
        finally:
            # 无数据提前 return / 异常路径都释放连接，避免 SQLite 句柄泄漏
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self.root.after(0, lambda: self._ui_idle())

    # ================= 停止 =================

    def _stop(self):
        self.stop_flag.set()
        self._log("已请求停止，等待当前操作完成...")
        self._set_progress("正在停止...")


def main() -> None:
    # 登录浏览器子进程分支：打包后的 exe 用 --weibo-login-worker <结果文件>
    # 被再次调用时，走独立进程跑 pywebview（必须占子进程自己的主线程），
    # 不再与 tkinter 主循环抢主线程（这是 v1.2.1 "pywebview must be run on a
    # main thread" 崩溃的根因）。
    if "--weibo-login-worker" in sys.argv:
        from weibo_export.browser_login import _child_main_from_args
        sys.exit(_child_main_from_args(sys.argv[sys.argv.index("--weibo-login-worker") + 1:]))

    root = tk.Tk()
    # 让窗口自适应屏幕可用高度并居中：Windows 任务栏约占底部 ~40px，
    # 窗口若设得过高，底部「进度区」会被任务栏压住（实测屏幕 800 / 可用 760，
    # 窗口 750 时底边贴到 y=800，进度区只剩 1px）。
    # 这里取「屏幕可用高度」，并扣掉 Windows 标题栏+边框（实测约 39px）：
    # geometry() 只设客户区，系统会在顶部再加标题栏，不扣就会超出安全区压住任务栏。
    # 窗口尺寸按屏幕自动匹配：先取「屏幕可用高度」（已自动扣除 Windows 任务栏），
    # 再按各区块的真实需求高度决定窗口大小，避免写死数字在不同分辨率下错位。
    _CHROME = 45  # 标题栏 + 边框余量
    _screen_h = root.winfo_screenheight()
    _screen_w = root.winfo_screenwidth()
    try:
        _avail_h = root.winfo_screenheight() - 40  # 保守再扣 40，给标题栏+边框留量
    except Exception:
        _avail_h = 700

    _win_w = min(920, _screen_w - 40)
    # 客户区目标高度 = 屏幕可用高 − 标题栏边框。窗口整体(客户区+标题栏)落在可用高内。
    _win_h = max(600, _avail_h - 5)
    _x = (_screen_w - _win_w) // 2
    _y = max(0, (_screen_h - _win_h - _CHROME) // 2 - 15)
    root.geometry(f"{_win_w}x{_win_h}+{_x}+{_y}")
    root.minsize(800, 560)
    app = WeiboExporterGUI(root)

    # 布局诊断：仅在设 WEIBO_LAYOUT_DEBUG 时，于真实 mainloop 首帧打印各区块高度，
    # 用于定位「进度区/日志框被压扁」问题。正常使用不设该变量，完全无影响。
    if os.environ.get("WEIBO_LAYOUT_DEBUG"):
        def _dump_layout():
            root.update_idletasks()
            fp = app._frm_progress
            lt = app.log_text
            pb = app.progress_bar
            print(f"[布局诊断] 窗口客户区高 = {root.winfo_height()}px  "
                  f"全部内容需求高 = {root.winfo_reqheight()}px")
            for ch in root.winfo_children():
                print(f"  · {ch.winfo_class():<12} "
                      f"实际={ch.winfo_height():>4}px 需求={ch.winfo_reqheight():>4}px "
                      f"{ch}")
            print(f"[布局诊断] 进度区 frm_progress = {fp.winfo_height()}px  "
                  f"请求高 = {fp.winfo_reqheight()}px")
            print(f"[布局诊断] 进度条 progress_bar = {pb.winfo_height()}px")
            print(f"[布局诊断] 日志框 log_text    = {lt.winfo_height()}px  "
                  f"请求高 = {lt.winfo_reqheight()}px")
            root.destroy()
        root.after(300, _dump_layout)
        root.mainloop()
        return

    root.mainloop()


if __name__ == "__main__":
    main()
