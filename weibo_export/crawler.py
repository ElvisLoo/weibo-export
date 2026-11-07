"""抓取层：新浪微博移动端 m.weibo.cn 的博文抓取。

关键设计
--------
1. 鉴权：通过用户从浏览器复制的 Cookie（含 SUB / SUBP / MLOGIN 等字段）模拟登录态，
   比内置账号密码登录更稳、更不易触发验证码。同时支持「无 Cookie 公开抓取」降级模式。
2. 限速：默认每条请求间隔 20 秒（稳定部落实测的安全值，新浪对爬虫限流严格）。
   可通过 ``REQUEST_INTERVAL`` 环境变量或 ``interval`` 参数覆盖。
3. 断点续传：抓取任务写入 SQLite（tasks 表记录 max_page），重启后自动从下一页继续，
   已抓到的博文因 PRIMARY KEY 去重不会重复入库。
4. 进度回调：每抓完一页调用 ``progress`` 回调，便于前端实时展示。
5. 可替换抓取器：实现 ``BaseCrawler`` 接口即可接入 mock 抓取器用于自测导出链路。

本项目自行实现，未复制任何第三方项目源码（如 weibo-crawler 无许可证，需规避版权）。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import db

DEFAULT_INTERVAL = float(os.environ.get("WEIBO_REQUEST_INTERVAL", "20"))
CONTAINER_TEMPLATE = "100505{user_id}"
BASE_URL = "https://m.weibo.cn"

ProgressCb = Optional[Callable[[Dict[str, Any]], None]]


@dataclass
class CrawlResult:
    user: Dict[str, Any] = field(default_factory=dict)
    task_id: int = 0
    total_posts: int = 0
    pages_fetched: int = 0
    stopped_reason: str = ""


class CrawlerError(Exception):
    pass


class BaseCrawler:
    """抓取器接口。子类实现 fetch_page 与 get_user。"""

    name = "base"

    def __init__(self, cookie: str = "", interval: float = DEFAULT_INTERVAL,
                 progress: ProgressCb = None, stop_flag: Optional[Callable[[], bool]] = None,
                 **kwargs: Any) -> None:
        self.cookie = cookie.strip() if cookie else ""
        self.interval = float(interval)
        self.progress = progress
        self.stop_flag = stop_flag or (lambda: False)
        self.logged_in = bool(self.cookie)

    def _emit(self, payload: Dict[str, Any]) -> None:
        if self.progress:
            try:
                self.progress(payload)
            except Exception:
                pass

    def _sleep(self) -> None:
        if self.interval > 0:
            time.sleep(self.interval)

    def get_user(self, user_id: str) -> Dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def fetch_page(self, user_id: str, page: int) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def run(self, conn, user_id: str, max_pages: int = 0,
            resume: bool = True, mode: str = "user") -> CrawlResult:
        """主循环：抓取 → 入库 → 断点续传 → 进度回调。"""
        user_id = str(user_id).strip()
        user = self.get_user(user_id)
        db.upsert_user(conn, user)

        prev = db.latest_task_for(conn, user_id, self.name) if resume else None
        start_page = 1
        if resume and prev and prev.get("status") == "running" and prev.get("max_page"):
            start_page = int(prev["max_page"]) + 1

        total_pages = max_pages if max_pages else int(user.get("statuses_count", 0) or 0)
        task_id = db.create_task(conn, user_id, self.name, total_pages)

        pages_fetched = 0
        total_new = 0
        empty_streak = 0
        reason = ""
        page = start_page
        while True:
            if self.stop_flag():
                reason = "用户请求停止（断点已保存，可续传）"
                break
            if max_pages and pages_fetched >= max_pages:
                reason = "已达到本次设置的最大页数"
                break
            self._emit({"type": "page_start", "user_id": user_id, "page": page})
            data = self.fetch_page(user_id, page)
            posts = data.get("posts", [])
            self._sleep()  # 限速：每条请求间隔约 interval 秒
            if not posts:
                empty_streak += 1
                if empty_streak >= 2:
                    reason = "连续两页无新博文，判定到底"
                    break
            else:
                empty_streak = 0
                total_new += db.upsert_posts(conn, posts)
                pages_fetched += 1
                db.update_task(conn, task_id, max_page=page, post_count=db.count_posts(conn, user_id))
                self._emit({
                    "type": "page_done", "user_id": user_id, "page": page,
                    "new_posts": len(posts), "total_in_db": db.count_posts(conn, user_id),
                })
                page += 1
                continue
            page += 1

        db.update_task(conn, task_id, status="done", message=reason,
                       post_count=db.count_posts(conn, user_id))
        self._emit({"type": "done", "user_id": user_id, "reason": reason})
        return CrawlResult(user=user, task_id=task_id, total_posts=db.count_posts(conn, user_id),
                           pages_fetched=pages_fetched, stopped_reason=reason)


def _parse_time(ts: Any) -> str:
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
        except Exception:
            return ""
    return str(ts)


def _strip_html(html: str) -> str:
    txt = re.sub(r"<br\s*/?>", "\n", html or "")
    txt = re.sub(r"</p>", "\n", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    return txt.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


class WeiboCrawler(BaseCrawler):
    """真实抓取器（m.weibo.cn）。需浏览器 Cookie 才能抓全量；无 Cookie 仅公开可见部分。"""

    name = "weibo"

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        import requests  # 延迟导入，便于 mock 环境无 requests 也能跑其它模块

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"),
            "Referer": BASE_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        })
        if self.cookie:
            self.session.headers["Cookie"] = self.cookie

    def get_user(self, user_id: str) -> Dict[str, Any]:
        url = f"{BASE_URL}/api/container/getIndex?containerid=100505{user_id}"
        try:
            r = self.session.get(url, timeout=15)
            data = r.json()
        except Exception as e:  # 网络/解析失败时返回最小信息，不阻断主流程
            return {"user_id": user_id, "screen_name": f"user_{user_id}", "description": "",
                    "statuses_count": 0, "followers_count": 0, "_error": str(e)}
        info = (data.get("data") or {}).get("userInfo") or {}
        return {
            "user_id": user_id,
            "screen_name": info.get("screen_name") or f"user_{user_id}",
            "description": info.get("description", ""),
            "statuses_count": info.get("statuses_count", 0),
            "followers_count": info.get("followers_count", 0),
        }

    def fetch_page(self, user_id: str, page: int) -> Dict[str, Any]:
        url = (f"{BASE_URL}/api/container/getIndex?containerid="
               f"107603{user_id}&page_type=01&page={page}")
        r = self.session.get(url, timeout=15)
        if r.status_code == 418:
            raise CrawlerError("被限流(HTTP 418)，请增大请求间隔或稍后重试")
        data = r.json()
        cards = (data.get("data") or {}).get("cards", []) or []
        posts: List[Dict[str, Any]] = []
        for card in cards:
            mb = card.get("mblog")
            if not mb:
                for g in card.get("card_group", []) or []:
                    if g.get("mblog"):
                        mb = g["mblog"]
                        break
            if not mb:
                continue
            pics = []
            for p in mb.get("pics", []) or []:
                pics.append(p.get("large", {}).get("url") or p.get("url", ""))
            posts.append({
                "mblog_id": str(mb.get("id") or mb.get("mid")),
                "user_id": user_id,
                "created_at": _parse_time(mb.get("created_at")),
                "text": mb.get("text", ""),
                "text_raw": mb.get("text_raw") or _strip_html(mb.get("text", "")),
                "reposts_count": mb.get("reposts_count", 0),
                "comments_count": mb.get("comments_count", 0),
                "attitudes_count": mb.get("attitudes_count", 0),
                "source": mb.get("source", ""),
                "region_name": mb.get("region_name", ""),
                "pic_urls": [u for u in pics if u],
                "raw": mb,
            })
        return {"posts": posts, "ok": data.get("ok")}


def build_crawler(kind: str = "weibo", **kwargs: Any) -> BaseCrawler:
    """工厂：按 kind 返回真实/模拟抓取器。"""
    if kind == "mock":
        from . import mock_crawler
        return mock_crawler.MockCrawler(**kwargs)
    return WeiboCrawler(**kwargs)
