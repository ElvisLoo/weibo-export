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

# 主页链接里直接带数字 ID 的形式（本地即可识别，无需联网）
_UID_URL_PATTERNS = [
    re.compile(r"weibo\.com/u/(\d+)"),
    re.compile(r"m\.weibo\.cn/u/(\d+)"),
    re.compile(r"m\.weibo\.cn/profile/(\d+)"),
    re.compile(r"weibo\.com/profile\.php\?.*?uid=(\d+)"),
]
# weibo.com/<自定义域名> 形式（需联网请求换数字 UID）
_CUSTOM_DOMAIN_RE = re.compile(r"weibo\.com/([A-Za-z0-9_\-]+)", re.I)
_URL_RE = re.compile(r"^https?://", re.I)


def _extract_uid_from_url(url: str) -> Optional[str]:
    """从微博主页链接里提取数字 UID。

    返回：
        数字 ID 字符串（本地识别或自定义域名）；
        无法识别时返回 None。
    """
    if not url:
        return None
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url  # 允许省略协议头
    for pat in _UID_URL_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    # weibo.com/自定义域名 → 由调用方联网解析
    m = _CUSTOM_DOMAIN_RE.search(url)
    if m:
        name = m.group(1)
        # 排除已知的非用户路径（如 weibo.com/u 本身、api 等）
        if name.lower() not in {"u", "profile", "api", "login", "signup", "home"}:
            return m.group(1)
    return None


def _parse_uid_input(raw: str):
    """解析用户输入框内容，供 UI 使用。

    返回 (uid, needs_net)：
        - 非链接（纯数字或普通文本）：(None, None) → UI 不处理
        - 链接且本地可识别到数字 ID：(数字ID, False) → UI 直接回填
        - 链接但需联网换 UID（weibo.com/自定义域名）：(None, True) → UI 后台解析
    """
    raw = (raw or "").strip()
    if not _URL_RE.search(raw):
        return (None, None)
    uid = _extract_uid_from_url(raw)
    if uid and uid.isdigit():
        return (uid, False)
    return (None, True)


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

    def _fetch_longtext(self, mblog_id: str) -> Optional[str]:
        """抓取长博文全文（微博列表接口会把超长正文截断，需调 longText 接口补全）。

        成功返回纯文本全文；任何失败（无网/解析失败/无全文）返回 None，
        由调用方回退到截断版正文，绝不因单篇长博文拖垮整页抓取。
        """
        url = f"{BASE_URL}/statuses/longtext?id={mblog_id}"
        try:
            # 补全全文是"锦上添花"，失败立即回退即可，不重试
            # （否则一页多条长博文持续失败时，25 秒重试会严重拖慢整页抓取）
            r = self._get_with_retry(url, max_retries=0)
            data = r.json()
        except Exception:
            return None
        try:
            long_data = (data.get("data") or {}).get("longTextContent", "")
            return long_data or None
        except Exception:
            return None

    def _warmup_session(self) -> None:
        """Session 预热（no-op 基类默认；WeiboCrawler 覆写实际逻辑）。"""

    def fetch_page(self, user_id: str, page: int) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def run(self, conn, user_id: str, max_pages: int = 0,
            resume: bool = True, mode: str = "user") -> CrawlResult:
        """主循环：抓取 → 入库 → 断点续传 → 进度回调。"""
        # 抓取前先做 Session 预热（有 cookie 时），提升首次请求成功率
        self._warmup_session()
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

        self._requests = requests
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

    def get_user_for_url(self, url: str) -> Dict[str, Any]:
        """把微博主页链接解析成用户信息（供 UI 从链接自动识别 ID）。

        支持链接：
            https://weibo.com/u/1234567890
            https://weibo.com/abcdefg      （自定义域名，需请求换 UID）
            https://m.weibo.cn/profile/1234567890
            https://m.weibo.cn/u/1234567890
        返回 {'user_id': '...', 'screen_name': '...'}；失败抛 CrawlerError。
        """
        uid = _extract_uid_from_url(url)
        if uid is None:
            raise CrawlerError(f"无法识别的微博链接：{url}")
        return self.get_user(uid)

    def _get_with_retry(self, url: str, max_retries: int = 3) -> Any:
        """带指数退避 + 随机抖动的请求，抗瞬时网络错误与限流。

        可重试：超时 / 连接错误 / HTTP 5xx / HTTP 418（新浪限流）。
        不重试：HTTP 404 等客户端错误（非瞬时问题）。
        每次重试前检查 stop_flag，用户点停止能立刻退出。
        """
        import random
        last_err: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            if self.stop_flag():
                raise CrawlerError("用户请求停止")
            try:
                r = self.session.get(url, timeout=15)
            except self._requests.exceptions.Timeout as e:
                last_err = e
            except self._requests.exceptions.ConnectionError as e:
                last_err = e
            else:
                if r.status_code == 418:
                    last_err = CrawlerError("被限流(HTTP 418)")
                elif 500 <= r.status_code < 600:
                    last_err = CrawlerError(f"服务器错误(HTTP {r.status_code})")
                else:
                    return r  # 成功（含 4xx 等非瞬时错误，交给上层判断）
            # 走到这里说明本次失败；若还有重试机会则退避
            if attempt < max_retries:
                backoff = 3 * (2 ** attempt) + random.uniform(0, 2)  # 3,7,15 +抖动
                self._emit({"type": "retry", "attempt": attempt + 1,
                            "max_retries": max_retries, "wait": round(backoff, 1),
                            "error": str(last_err)})
                if self.stop_flag():
                    raise CrawlerError("用户请求停止")
                time.sleep(backoff)
        raise CrawlerError(f"多次重试仍失败：{last_err}")

    def _warmup_session(self) -> None:
        """抓取前预热 Session（仅在有 Cookie 时）。

        只带核心 Cookie（尤其 SUB）请求一次 m.weibo.cn 首页，让服务器
        下发适配的其它指纹（_T_WM、XSRF-TOKEN 等）。这是参考 weibo-crawler
        的公开做法（仅学习思路、自行实现）：微博在登录后往往只需 SUB 一个
        核心字段，其余反爬指纹可由服务器在预热时补齐，从而提升后续接口
        抓取的成功率、降低被限流(418)概率。

        设计原则：预热失败绝不阻断抓取——它只是"锦上添花"。网络不通或
        超时都静默跳过，由后续正式抓取流程自己重试。响应里若 Set-Cookie
        了新指纹，requests.Session 会自动合并进 cookie jar，但因为我们把
        Cookie 放在 header 里，这里需手动把新指纹并回 Cookie header。
        """
        if not self.cookie:
            return
        try:
            r = self.session.get(BASE_URL, timeout=10)
            # 把服务器下发的新 cookie 合并进 Cookie header（保留原有字段）
            new = r.cookies.get_dict() if r.cookies else {}
            if new:
                existing = {}
                for pair in self.cookie.split(";"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        existing[k.strip()] = v.strip()
                existing.update(new)  # 服务器新指纹覆盖旧值
                merged = "; ".join(f"{k}={v}" for k, v in existing.items())
                self.cookie = merged
                self.session.headers["Cookie"] = merged
                self._emit({"type": "info", "message":
                            f"会话预热完成，已更新 {len(new)} 项会话指纹"})
            else:
                self._emit({"type": "info", "message": "会话预热完成"})
        except Exception:
            # 预热失败不阻断：交给正式抓取流程按需重试
            self._emit({"type": "info", "message": "会话预热跳过（网络未就绪）"})

    def get_user(self, user_id: str) -> Dict[str, Any]:
        url = f"{BASE_URL}/api/container/getIndex?containerid=100505{user_id}"
        try:
            r = self._get_with_retry(url)
            data = r.json()
        except CrawlerError:
            raise  # 重试耗尽/用户停止：向上抛，让主流程记录
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
        try:
            r = self._get_with_retry(url)
            data = r.json()
        except CrawlerError:
            raise  # 重试耗尽/用户停止：向上抛，让主循环记录
        except Exception as e:
            # 新浪偶发返回空 body/HTML → r.json() 抛错。不中断整次抓取，
            # 返回空结果交由主循环的 empty_streak 判到底逻辑处理。
            self._emit({"type": "info", "message": f"第 {page} 页返回非 JSON，跳过（{type(e).__name__}）"})
            return {"posts": [], "ok": 0}
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
            raw_id = mb.get("id") or mb.get("mid")
            if raw_id is None:
                # 既无 id 也无 mid 的脏数据：跳过，避免 str(None)="None" 互相覆盖主键静默丢数据
                continue
            mblog_id = str(raw_id)
            text = mb.get("text", "")
            text_raw = mb.get("text_raw") or _strip_html(text)
            pics = []
            for p in mb.get("pics", []) or []:
                pics.append(p.get("large", {}).get("url") or p.get("url", ""))

            # 长博文全文补全：列表接口会把超长正文截断，需再调 longText 接口
            is_long = mb.get("isLong", False) or "全文" in _strip_html(text)[-20:]
            if is_long:
                full_text = self._fetch_longtext(mblog_id)
                if full_text:
                    text_raw = full_text
                    self._emit({"type": "longtext_fetched", "mblog_id": mblog_id,
                                "len": len(full_text)})

            posts.append({
                "mblog_id": mblog_id,
                "user_id": user_id,
                "created_at": _parse_time(mb.get("created_at")),
                "text": text,
                "text_raw": text_raw,
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
