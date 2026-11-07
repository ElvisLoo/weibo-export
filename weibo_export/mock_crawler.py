"""模拟抓取器（MockCrawler）—— 用于在没有真实 Cookie / 不触网的情况下自测导出链路。

行为与真实 WeiboCrawler 完全一致（同样经过 run() 主循环、限速、入库、断点续传），
只是 fetch_page 返回本地生成的确定性假数据，且 interval 默认压到 0 以便秒级跑完。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from . import db
from .crawler import BaseCrawler

# 一段覆盖中文标点、emoji、英文混排、换行的示例文本，用于验证各导出格式渲染正常。
_SAMPLE_TEXTS: List[str] = [
    "今天天气真好，和小伙伴去公园散步🌿。生活需要一点绿意，也需要一点慢下来的勇气。",
    "记录一下：把旧硬盘里的照片整理完了，从 2015 到 2023，整整八年，一晃而过。\n有些瞬间当时觉得普通，回头看才明白它的重量。",
    "分享一个效率小技巧：把每天最重要的三件事写在纸上，做完划掉，比任何待办 App 都管用。",
    "读完了《人类简史》，最大的感触是——讲故事的能力，才是人类最核心的竞争力。\n公司、国家、货币，本质上都是共识的产物。",
    "写代码写到深夜，窗外的雨声很治愈🌧️。Bug 终于修好了，明天给自己放半天假。",
    "周末在家煮了一锅番茄牛腩，香到邻居敲门问配方。慢炖两小时，值得。🍅🥩",
    "极客港的每一次迭代，都是为了让工具更懂人。感谢一路同行的朋友们🙏。",
    "运动第 100 天打卡🏃。体重没怎么变，但精神状态完全不一样了，坚持本身就是意义。",
    "偶尔要允许自己什么都不做。躺平不是放弃，是给心灵充电。",
    "把过去的微博翻了一遍，青涩又真诚。导出备份这件事，越早做越好——数据在自己手里才安心。",
]


class MockCrawler(BaseCrawler):
    name = "mock"

    def __init__(self, total_pages: int = 5, per_page: int = 8,
                 user_id: str = "123456789", screen_name: str = "极客港_测试用户",
                 **kw: Any) -> None:
        kw.setdefault("interval", 0)
        super().__init__(**kw)
        self.total_pages = max(1, int(total_pages))
        self.per_page = max(1, int(per_page))
        self.user_id = str(user_id)
        self.screen_name = screen_name

    def get_user(self, user_id: str) -> Dict[str, Any]:
        return {
            "user_id": str(user_id),
            "screen_name": self.screen_name,
            "description": "这是一个用于自测导出链路的模拟用户。",
            "statuses_count": self.total_pages * self.per_page,
            "followers_count": 12345,
        }

    def fetch_page(self, user_id: str, page: int) -> Dict[str, Any]:
        if page > self.total_pages:
            return {"posts": []}
        posts: List[Dict[str, Any]] = []
        base_ts = 1700000000 - (page - 1) * self.per_page * 86400
        for i in range(self.per_page):
            idx = (page - 1) * self.per_page + i
            created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base_ts - i * 3600))
            text = _SAMPLE_TEXTS[idx % len(_SAMPLE_TEXTS)]
            mblog_id = f"mock_{user_id}_{page}_{i}"
            posts.append({
                "mblog_id": mblog_id,
                "user_id": str(user_id),
                "created_at": created,
                "text": f"<p>{text}</p>",
                "text_raw": text,
                "reposts_count": idx * 3,
                "comments_count": idx * 7,
                "attitudes_count": idx * 11,
                "source": "iPhone 15 Pro" if i % 2 == 0 else "微博网页版",
                "region_name": "发布于 北京" if i % 3 == 0 else "",
                "pic_urls": [] if i % 2 else [f"https://example.com/pic/{mblog_id}.jpg"],
                "raw": {"id": mblog_id, "page": page, "index": idx},
            })
        return {"posts": posts, "ok": 1}


def seed_mock_db(db_path: str, user_id: str = "123456789",
                 total_pages: int = 5, per_page: int = 8) -> Dict[str, Any]:
    """直接用 mock 数据灌满一个 weibo.db，用于端到端测试导出（不经抓取）。"""
    conn = db.connect(db_path)
    crawler = MockCrawler(total_pages=total_pages, per_page=per_page, user_id=user_id)
    user = crawler.get_user(user_id)
    db.upsert_user(conn, user)
    total = 0
    for page in range(1, total_pages + 1):
        total += db.upsert_posts(conn, crawler.fetch_page(user_id, page)["posts"])
    conn.close()
    return {"user": user, "posts": total}
