"""空页处理逻辑自测：区分「真到底」与「限流/网络失败」，避免刚抓几条就被误判成到底。

运行方式：
    python test_empty_streak.py

覆盖三种场景：
    1. 连续多页 ok=1 且无数据  → 判定到底（正常结束）
    2. 中间某页 ok=0/解析失败 → 退避重试同一页，重试成功后继续（不误判到底）
    3. 连续多次取不到数据（限流）→ 重试耗尽后停止，断点保留可续传
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from weibo_export import db as dbmod                      # noqa: E402
from weibo_export.crawler import WeiboCrawler             # noqa: E402


def _mkpost(pid: str, uid: str) -> dict:
    """构造一条最小可用博文（upsert_posts 需要的字段）。"""
    return {
        "mblog_id": pid,
        "user_id": uid,
        "created_at": "2023-01-01 00:00:00",
        "text": "t",
        "text_raw": "t",
        "reposts_count": 0,
        "comments_count": 0,
        "attitudes_count": 0,
        "source": "",
        "region_name": "",
        "pic_urls": [],
        "raw": {"id": pid},
    }


class _FakeCrawler(WeiboCrawler):
    """不真正发请求，按脚本返回每页数据。script 是 [(posts, ok), ...]。"""

    def __init__(self, script):
        super().__init__(cookie="", interval=0)
        self.script = list(script)
        self.calls = []

    def get_user(self, uid):
        return {"user_id": uid, "screen_name": "T", "statuses_count": 100,
                "followers_count": 0}

    def _warmup_session(self):
        pass

    def fetch_page(self, uid, page):
        self.calls.append(page)
        posts, ok = self.script.pop(0)
        return {"posts": posts, "ok": ok}


def main() -> int:
    failures = 0
    tmp = tempfile.mkdtemp(prefix="wbtest_")
    dbp = os.path.join(tmp, "t.db")
    conn = dbmod.connect(dbp)

    # 缩短退避，避免测试变慢
    import weibo_export.crawler as cm
    orig_sleep = cm.time.sleep
    cm.time.sleep = lambda s: None

    try:
        # ---------- 测试1：连续 ok=1 空页 → 判定到底 ----------
        print("\n[测试1] 连续 ok=1 空页 → 判定到底")
        c1 = _FakeCrawler([
            ([_mkpost("a", "u1")], 1),
            ([_mkpost("b", "u1")], 1),
            ([], 1), ([], 1), ([], 1),
        ])
        r1 = c1.run(conn, "u1", resume=False)
        ok1 = ("到底" in r1.stopped_reason) and r1.total_posts == 2
        print(f"  reason={r1.stopped_reason!r} total={r1.total_posts} calls={c1.calls}")
        print(f"  {'✓ 通过' if ok1 else '✗ 失败'}")
        if not ok1:
            failures += 1

        # ---------- 测试2：限流空页 → 重试同一页成功 ----------
        print("\n[测试2] ok=0 空页 → 退避重试同一页，重试成功后继续")
        c2 = _FakeCrawler([
            ([_mkpost("x", "u2")], 1),   # page1
            ([], 0),                      # page2 首次：限流
            ([], 0),                      # page2 二次：仍限流
            ([_mkpost("y", "u2")], 1),   # page2 三次：成功
            ([], 1), ([], 1), ([], 1),   # page3 到底
        ])
        r2 = c2.run(conn, "u2", resume=False)
        retried = c2.calls.count(2)
        ok2 = ("到底" in r2.stopped_reason) and r2.total_posts == 2 and retried == 3
        print(f"  reason={r2.stopped_reason!r} total={r2.total_posts} "
              f"page2重试次数={retried} calls={c2.calls}")
        print(f"  {'✓ 通过' if ok2 else '✗ 失败'}")
        if not ok2:
            failures += 1

        # ---------- 测试3：连续限流 → 重试耗尽后停止 ----------
        print("\n[测试3] 连续限流空页 → 重试耗尽后停止（断点保留）")
        c3 = _FakeCrawler([
            ([_mkpost("m", "u3")], 1),
        ] + [([], 0)] * 10)            # page2 起一直限流
        r3 = c3.run(conn, "u3", resume=False)
        ok3 = ("限流" in r3.stopped_reason or "重试" in r3.stopped_reason) and r3.total_posts == 1
        print(f"  reason={r3.stopped_reason!r} total={r3.total_posts}")
        print(f"  {'✓ 通过' if ok3 else '✗ 失败'}")
        if not ok3:
            failures += 1

        print("\n" + "=" * 60)
        print(f"结果：通过 {3 - failures} / 3，失败 {failures}")
        print("=" * 60)
        return 1 if failures else 0
    finally:
        cm.time.sleep = orig_sleep
        conn.close()
        try:
            os.remove(dbp)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
