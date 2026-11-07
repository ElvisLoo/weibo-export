"""本地 Web 服务（FastAPI）：提供 HTTP API + serve 前端界面。

启动方式：
    python -m weibo_export.server
    # 或项目根目录
    python run.py

监听 127.0.0.1（仅本机访问，安全）。桌面版 Electron 壳与未来网页版共用本引擎。

设计：抓取在后台线程执行，进度写入内存 job 状态，前端轮询 /api/job 拿实时进度；
     抓取到的博文实时入库，因此中途停止/断点续传都成立。
"""

from __future__ import annotations

import argparse
import os
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __app_name_en__, __brand__, __brand_site__, __version__
from . import db, exporter, paths

app = FastAPI(title=f"{__brand__} 微博导出 ({__app_name_en__})", version=__version__)

_STATE: Dict[str, Any] = {
    "db_path": paths.default_db_path(),
    "export_dir": paths.default_export_dir(),
    "conn": None,
    "job": None,          # 当前抓取任务状态 dict
    "job_lock": threading.Lock(),
    "stop_flag": False,
    "progress_cb": None,  # 注入到 crawler 的回调
}


def get_conn():
    if _STATE["conn"] is None:
        _STATE["conn"] = db.connect(_STATE["db_path"])
    return _STATE["conn"]


# ---------------- 请求模型 ----------------

class CrawlReq(BaseModel):
    user_id: str = Field(..., description="微博用户 ID（纯数字）")
    cookie: str = Field("", description="浏览器复制的微博 Cookie；留空则为未登录公开抓取")
    crawler: str = Field("weibo", description="weibo=真实抓取；mock=模拟抓取（自测）")
    max_pages: int = Field(0, description="本次最多抓多少页；0=按博文总数推算/抓到底")
    interval: float = Field(20, description="每条请求间隔秒数，默认 20（限速安全值）")
    resume: bool = Field(True, description="是否断点续传")


class ExportReq(BaseModel):
    user_id: str
    formats: List[str] = Field(default_factory=lambda: ["html", "md", "pdf", "json", "txt"])
    out_dir: Optional[str] = None


class SeedReq(BaseModel):
    user_id: str = "1892838392"
    total_pages: int = 5
    per_page: int = 8


# ---------------- 前端 ----------------

@app.get("/")
def index():
    return FileResponse(os.path.join(paths.static_dir(), "index.html"))


@app.get("/static/{filename}")
def static_file(filename: str):
    """提供 static/ 目录下的界面静态资源（如 icon.png）。"""
    import re as _re
    if not _re.fullmatch(r"[\w.\-]+", filename):
        raise HTTPException(400, "非法文件名")
    path = os.path.join(paths.static_dir(), filename)
    if not os.path.exists(path):
        raise HTTPException(404, "文件不存在")
    return FileResponse(path)


@app.get("/api/status")
def status():
    from .exporter import font_status
    conn = get_conn()
    return {
        "brand": __brand__, "brand_site": __brand_site__, "tool": __app_name_en__,
        "version": __version__,
        "db_path": _STATE["db_path"],
        "export_dir": _STATE["export_dir"],
        "db_users": len(db.list_users(conn)),
        "font": font_status(),
        "busy": bool(_STATE["job"] and _STATE["job"].get("status") == "running"),
    }


# ---------------- 抓取 ----------------

def _progress_cb(payload: Dict[str, Any]) -> None:
    job = _STATE["job"]
    if not job:
        return
    t = payload.get("type")
    if t == "page_start":
        job.update(status="running", phase="抓取中", current_page=payload.get("page"),
                   last_event=f"正在抓取第 {payload.get('page')} 页")
    elif t == "page_done":
        job.update(phase="抓取中", current_page=payload.get("page"),
                   pages_done=job.get("pages_done", 0) + 1,
                   total_in_db=payload.get("total_in_db", job.get("total_in_db")),
                   last_event=f"第 {payload.get('page')} 页完成，新增 {payload.get('new_posts')} 条")
    elif t == "done":
        job.update(phase="完成", last_event=payload.get("reason") or "抓取结束")
    job["updated_at"] = time.time()


def _run_crawl_thread(req: CrawlReq) -> None:
    from .crawler import build_crawler, CrawlerError

    job = _STATE["job"]
    try:
        crawler = build_crawler(
            kind=req.crawler,
            cookie=req.cookie,
            interval=req.interval,
            progress=_progress_cb,
            stop_flag=lambda: _STATE["stop_flag"],
        )
        # 抓取在独立线程运行：为本线程创建专属连接，避免跨线程复用 SQLite 连接
        conn = db.connect(_STATE["db_path"])
        try:
            result = crawler.run(conn, req.user_id, max_pages=req.max_pages, resume=req.resume)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        job.update(status="done", phase="完成",
                   total_posts=result.total_posts, pages_fetched=result.pages_fetched,
                   stopped_reason=result.stopped_reason, user=result.user,
                   finished_at=time.time(),
                   last_event=f"完成：入库 {result.total_posts} 条")
    except CrawlerError as e:
        job.update(status="error", phase="出错", error=str(e), finished_at=time.time())
    except Exception as e:  # noqa
        job.update(status="error", phase="出错", error=f"{e}",
                   traceback=traceback.format_exc(), finished_at=time.time())


@app.post("/api/crawl")
def crawl(req: CrawlReq):
    with _STATE["job_lock"]:
        if _STATE["job"] and _STATE["job"].get("status") == "running":
            raise HTTPException(409, "已有抓取任务在进行中")
        _STATE["stop_flag"] = False
        _STATE["job"] = {
            "status": "running", "phase": "启动中", "crawler": req.crawler,
            "user_id": req.user_id, "logged_in": bool(req.cookie.strip()),
            "interval": req.interval, "current_page": 0, "pages_done": 0,
            "total_in_db": 0, "started_at": time.time(), "updated_at": time.time(),
            "last_event": "任务已启动",
        }
    # 注入 stop 检查：通过 monkeypatch crawler 主循环太重，这里用轮询线程设 stop_flag
    threading.Thread(target=_run_crawl_thread, args=(req,), daemon=True).start()
    return {"ok": True, "job": _STATE["job"]}


@app.get("/api/job")
def job_status():
    return {"job": _STATE["job"]}


@app.post("/api/job/stop")
def job_stop():
    _STATE["stop_flag"] = True
    return {"ok": True, "note": "已请求停止；当前页完成后退出（尊重限速）"}


# ---------------- 数据查询 ----------------

@app.get("/api/users")
def users():
    conn = get_conn()
    out = []
    for u in db.list_users(conn):
        u["posts_in_db"] = db.count_posts(conn, u["user_id"])
        out.append(u)
    return {"users": out}


@app.get("/files/{filename}")
def download_file(filename: str):
    """下载 exports/ 目录中已生成的导出文件（白名单校验文件名，防路径穿越）。"""
    import re as _re
    if not _re.fullmatch(r"[\w.\-一-鿿]+", filename):
        raise HTTPException(400, "非法文件名")
    safe_dir = os.path.abspath(_STATE["export_dir"])
    path = os.path.abspath(os.path.join(safe_dir, filename))
    if not path.startswith(safe_dir) or not os.path.exists(path):
        raise HTTPException(404, "文件不存在")
    return FileResponse(path, filename=filename)


@app.get("/api/posts")
def posts(user_id: str, limit: int = 20, offset: int = 0):
    conn = get_conn()
    items = db.get_posts(conn, user_id, limit=limit or 0, offset=offset)
    for it in items:
        it.pop("raw", None)
    return {"user_id": user_id, "count": db.count_posts(conn, user_id), "posts": items}


# ---------------- 导出 ----------------

@app.post("/api/export")
def export(req: ExportReq):
    conn = get_conn()
    if db.count_posts(conn, req.user_id) == 0:
        raise HTTPException(404, f"用户 {req.user_id} 在数据库中暂无博文，请先抓取或灌入 mock 数据")
    out_dir = req.out_dir or _STATE["export_dir"]
    try:
        results = exporter.export_all(conn, req.user_id, out_dir, req.formats)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa
        raise HTTPException(500, f"导出失败：{e}")
    return {"ok": True, "out_dir": out_dir, "files": results}


# ---------------- Mock 数据（自测） ----------------

@app.post("/api/seed-mock")
def seed_mock(req: SeedReq):
    from .mock_crawler import seed_mock_db
    info = seed_mock_db(_STATE["db_path"], user_id=req.user_id,
                        total_pages=req.total_pages, per_page=req.per_page)
    return {"ok": True, "user": info["user"], "posts_inserted": info["posts"],
            "db_path": _STATE["db_path"]}


@app.exception_handler(Exception)
async def unhandled(request, exc):  # noqa
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# ---------------- 入口 ----------------

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=f"{__brand__} 微博导出本地服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=paths.default_db_path())
    parser.add_argument("--export-dir", default=paths.default_export_dir())
    args = parser.parse_args(argv)

    _STATE["db_path"] = args.db
    _STATE["export_dir"] = args.export_dir
    import uvicorn
    print(f"[{__brand__} · {__app_name_en__}] 服务启动：http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
