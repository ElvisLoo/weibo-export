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


def _data_dir() -> str:
    """数据目录：文档/微博导出/"""
    base = os.path.join(os.path.expanduser("~"), "Documents", "微博导出")
    os.makedirs(base, exist_ok=True)
    return base


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
        root.title(f"极客港 · 微博导出工具 v{__version__}")
        root.geometry("920x800")
        root.minsize(800, 680)
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

    def _build_ui(self):
        # 全局字体：按用户偏好放大（在 v1.1.2 基础上再大一号）
        self.font_label = ("Microsoft YaHei UI", 13)
        self.font_hint = ("Microsoft YaHei UI", 11)
        self.font_btn = ("Microsoft YaHei UI", 13, "bold")
        # 日志用系统默认中文字体（Consolas 无中文字形，中文会回退渲染导致发虚）
        self.font_log = ("Microsoft YaHei UI", 12)
        self.font_progress = ("Microsoft YaHei UI", 14, "bold")

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
        frm_input.pack(fill="x", padx=14, pady=(14, 8))

        frm_input.columnconfigure(0, minsize=132)  # 标签列固定宽，短标签右对齐贴向冒号
        frm_input.columnconfigure(2, weight=1)      # 输入框列自适应

        # 列职责：0=标签(右对齐) 1=冒号(独立列,保证竖直对齐) 2=输入框 3=提示(紧贴)
        row = 0
        ttk.Label(frm_input, text="微博用户 ID", font=self.font_label).grid(
            row=row, column=0, sticky="e", pady=5)
        ttk.Label(frm_input, text="：", font=self.font_label).grid(
            row=row, column=1, sticky="w", pady=5)
        self.var_uid = tk.StringVar()
        ttk.Entry(frm_input, textvariable=self.var_uid, width=22, font=self.font_label).grid(
            row=row, column=2, sticky="w", pady=5)
        ttk.Label(frm_input, text="（纯数字，如 123456789）", font=self.font_hint,
                  foreground="gray").grid(row=row, column=3, sticky="w", pady=5, padx=(10, 0))

        row += 1
        ttk.Label(frm_input, text="Cookie（可选）", font=self.font_label).grid(
            row=row, column=0, sticky="e", pady=5)
        ttk.Label(frm_input, text="：", font=self.font_label).grid(
            row=row, column=1, sticky="w", pady=5)
        # Cookie 用多行文本框（4 行高，长 Cookie 直接铺开看，无需横向滚动箭头）
        self.cookie_text = tk.Text(frm_input, height=4, wrap="none",
                                   font=self.font_label, relief="solid", borderwidth=1)
        self.cookie_text.grid(row=row, column=2, sticky="we", pady=5)
        ttk.Label(frm_input, text="粘贴网页 Cookie", font=self.font_hint,
                  foreground="gray").grid(row=row, column=3, sticky="n", pady=7, padx=(10, 0))

        row += 1
        ttk.Label(frm_input, text="最大页数", font=self.font_label).grid(
            row=row, column=0, sticky="e", pady=5)
        ttk.Label(frm_input, text="：", font=self.font_label).grid(
            row=row, column=1, sticky="w", pady=5)
        # 页数：输入框 + 大号 −/＋ 按钮（原生 Spinbox 箭头太小，换成可点的方按钮）
        self.var_max_pages = tk.IntVar(value=0)
        pages_box = ttk.Frame(frm_input)
        pages_box.grid(row=row, column=2, sticky="w", pady=5)
        ttk.Entry(pages_box, textvariable=self.var_max_pages, width=8,
                  font=self.font_label).pack(side="left")
        # 大号 −/＋ 按钮（原生 Spinbox 箭头太小）
        ttk.Button(pages_box, text="－", width=3, style="Big.TButton",
                   command=lambda: self._nudge_pages(-1)).pack(side="left", padx=(6, 3))
        ttk.Button(pages_box, text="＋", width=3, style="Big.TButton",
                   command=lambda: self._nudge_pages(1)).pack(side="left", padx=3)
        ttk.Label(frm_input, text="0 = 抓到底", font=self.font_hint,
                  foreground="gray").grid(row=row, column=3, sticky="w", pady=5, padx=(10, 0))

        # --- 导出格式 ---
        frm_fmt = ttk.LabelFrame(self.root, text=" 2. 导出格式 ", padding=12)
        frm_fmt.pack(fill="x", padx=14, pady=8)

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

        self.btn_stop = ttk.Button(frm_btn, text="停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left")

        ttk.Label(frm_btn, text=f"导出目录: {_data_dir()}", font=self.font_hint,
                  foreground="gray").pack(side="right")

        # --- 进度 ---
        frm_progress = ttk.LabelFrame(self.root, text=" 3. 任务进度 ", padding=12)
        frm_progress.pack(fill="both", expand=True, padx=14, pady=8)

        self.var_progress = tk.StringVar(value="就绪")
        ttk.Label(frm_progress, textvariable=self.var_progress,
                  font=self.font_progress).pack(anchor="w", pady=4)

        # 进度条：让用户看到连续推进
        self.progress_bar = ttk.Progressbar(frm_progress, mode="indeterminate", length=200)
        self.progress_bar.pack(fill="x", pady=(0, 6))

        self.log_text = ScrolledText(frm_progress, height=13, state="disabled",
                                     font=self.font_log, bg="#1e1e1e", fg="#d4d4d4")
        self.log_text.pack(fill="both", expand=True, pady=(4, 0))

    # ================= 日志 =================

    def _log(self, msg: str):
        self.log_text.config(state="normal")
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        _write_log(msg)

    def _set_progress(self, text: str):
        self.var_progress.set(text)

    # ================= 抓取 =================

    def _start_crawl(self):
        uid = self.var_uid.get().strip()
        if not uid.isdigit():
            messagebox.showwarning("提示", "请输入纯数字微博用户 ID")
            return
        cookie = self._get_cookie().strip()
        max_pages = self.var_max_pages.get()

        self.btn_crawl.config(state="disabled")
        self.btn_export.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.stop_flag.clear()
        self.pages_fetched = 0
        self._set_progress("正在启动抓取...")
        self.progress_bar.start(12)

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

        try:
            db_path = os.path.join(_data_dir(), "weibo.db")
            conn = db.connect(db_path)

            crawler = crawler_mod.build_crawler(
                kind="weibo",
                cookie=cookie,
                interval=20,
                progress=progress_cb,
                stop_flag=lambda: self.stop_flag.is_set(),
            )
            result = crawler.run(conn, uid, max_pages=max_pages, resume=True)
            conn.close()

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
            self.root.after(0, lambda: self._ui_idle())

    def _update_progress_ui(self):
        with self.job_lock:
            state = dict(self.job_state)
        t = state.get("type", "")
        if t == "page_start":
            self.progress_bar.start(12)  # 进度条动起来
            self._set_progress(f"正在抓取第 {state.get('page', '?')} 页...")
        elif t == "page_done":
            self.pages_fetched += 1
            self._set_progress(
                f"已完成 {self.pages_fetched} 页（当前数据页 {state.get('page', '?')}），"
                f"新增 {state.get('new_posts', 0)} 条，累计入库 {state.get('total_in_db', 0)} 条")
            self._log(f"第 {state.get('page', '?')} 页完成：新增 {state.get('new_posts', 0)} 条")

    def _ui_idle(self):
        try:
            self.progress_bar.stop()
        except Exception:
            pass
        self.btn_crawl.config(state="normal")
        self.btn_export.config(state="normal")
        self.btn_stop.config(state="disabled")

    # ================= 导出 =================

    def _start_export(self):
        uid = self.var_uid.get().strip()
        if not uid.isdigit():
            messagebox.showwarning("提示", "请先输入微博用户 ID")
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

        try:
            db_path = os.path.join(_data_dir(), "weibo.db")
            conn = db.connect(db_path)
            if db.count_posts(conn, uid) == 0:
                self.root.after(0, lambda: messagebox.showinfo(
                    "无数据", f"用户 {uid} 在本地数据库中暂无博文，请先抓取。"))
                return
            out_dir = _data_dir()
            results = exporter.export_all(conn, uid, out_dir, fmts)
            conn.close()
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
            self.root.after(0, lambda: self._ui_idle())

    # ================= 停止 =================

    def _stop(self):
        self.stop_flag.set()
        self._log("已请求停止，等待当前操作完成...")
        self._set_progress("正在停止...")


def main() -> None:
    root = tk.Tk()
    app = WeiboExporterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
