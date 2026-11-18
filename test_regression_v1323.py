"""封版前回归自测：验证 v1.3.23 修复的三个真实 bug。

运行方式：
    python test_regression_v1323.py

覆盖：
    BUG-A 优雅停止后 resume 失效（原来会从头重抓，现应从断点页续抓）
    BUG-B 网络异常后任务永久卡 running（现应写 stopped/done + 停止原因）
    BUG-C GUI 与 server 导出路径分裂（export_dir 应与 default_export_dir 一致）
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from weibo_export import db as dbmod                      # noqa: E402
from weibo_export import paths                            # noqa: E402
from weibo_export.crawler import WeiboCrawler, CrawlerError  # noqa: E402


PASS = 0
FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print(f"  ✓ {msg}")


def no(msg):
    global FAIL
    FAIL += 1
    print(f"  ✗ {msg}")


def _mkpost(pid, uid):
    return {
        "mblog_id": pid, "user_id": uid, "created_at": "2023-01-01 00:00:00",
        "text": "t", "text_raw": "t", "reposts_count": 0, "comments_count": 0,
        "attitudes_count": 0, "source": "", "region_name": "",
        "pic_urls": [], "raw": {"id": pid},
    }


class _FakeCrawler(WeiboCrawler):
    """按脚本返回每页数据；可在第 N 次请求时抛错或触发优雅停止。

    stop_after：抓满该页数后，模拟用户点停止（通过 stop_flag 生效）。
    raise_on  ：第 N 次 fetch 抛 CrawlerError（模拟网络中断）。
    """

    def __init__(self, script, stop_after=None, raise_on=None):
        super().__init__(cookie="", interval=0)
        self.script = list(script)
        self.stop_after = stop_after
        self.raise_on = raise_on
        self._fetch_count = 0
        self.calls = []                       # 记录实际请求的页码
        self._stop_requested = False
        # 把内部停止标志接到基类 stop_flag：抓满 stop_after 页后视为用户点停止
        self.stop_flag = lambda: self._stop_requested

    def get_user(self, uid):
        return {"user_id": uid, "screen_name": "T", "statuses_count": 100,
                "followers_count": 0}

    def _warmup_session(self):
        pass

    def fetch_page(self, uid, page):
        self.calls.append(page)
        self._fetch_count += 1
        if self.stop_after and self._fetch_count >= self.stop_after:
            self._stop_requested = True       # 抓完本页后停止，下一轮循环顶部触发
        if self.raise_on and self._fetch_count >= self.raise_on:
            raise CrawlerError("模拟网络中断")
        posts, okv = self.script.pop(0)
        return {"posts": posts, "ok": okv}


def test_bug_a_resume_after_graceful_stop():
    """BUG-A：优雅停止写 stopped 后，resume 应从断点页续抓而非从头。"""
    print("[BUG-A] 优雅停止 → 续传从断点页继续")
    tmp = tempfile.mkdtemp()
    conn = dbmod.connect(os.path.join(tmp, "t.db"))

    # 第一轮：抓 2 页后优雅停止
    c1 = _FakeCrawler(
        [([_mkpost("a", "1")], 1), ([_mkpost("b", "1")], 1)],
        stop_after=2,
    )
    c1.run(conn, "1", resume=False)

    prev = dbmod.latest_task_for(conn, "1", c1.name)
    assert prev["status"] == "stopped", f"第一轮应写 stopped，实为 {prev['status']}"
    ok(f"第一轮停止后 status={prev['status']}（可续传态）")

    # 第二轮：resume=True，应从第 3 页开始（不是第 1 页）
    c2 = _FakeCrawler(
        [([_mkpost("c", "1")], 1), ([], 1), ([], 1), ([], 1)],
    )
    c2.run(conn, "1", resume=True)
    if c2.calls and c2.calls[0] == 3:
        ok(f"续传起始页={c2.calls[0]}（正确，从断点第 3 页继续）")
    else:
        no(f"续传起始页应为 3，实为 {c2.calls[0] if c2.calls else '无'}（从头重抓 bug 未修）")
    conn.close()


def test_bug_b_no_zombie_running():
    """BUG-B：fetch_page 抛 CrawlerError 后，task 应写终止态而非卡 running。

    v1.3.23 修复行为：run() 内部捕获循环中断，写 status=stopped + 停止原因，
    不再向上抛 CrawlerError（这样调用方拿到正常 CrawlResult，UI 能显示原因，
    且任务处于可续传态，不会永久卡 running）。
    """
    print("[BUG-B] 网络异常 → task 写终止态 + 停止原因，不卡 running")
    tmp = tempfile.mkdtemp()
    conn = dbmod.connect(os.path.join(tmp, "t.db"))

    c = _FakeCrawler(
        [([_mkpost("a", "1")], 1)],
        raise_on=2,   # 第 2 次 fetch 抛错（第 1 次成功入库）
    )
    try:
        result = c.run(conn, "1", resume=False)
        # 修复后 run() 不再抛异常，而是正常返回，stopped_reason 记录中断原因
        if "抓取中断" in (result.stopped_reason or ""):
            ok(f"run() 正常返回，stopped_reason={result.stopped_reason!r}")
        else:
            no(f"stopped_reason 未记录中断原因：{result.stopped_reason!r}")
    except CrawlerError as e:
        no(f"run() 仍向上抛 CrawlerError（{e}），调用方无法拿到结果对象")

    prev = dbmod.latest_task_for(conn, "1", c.name)
    if prev["status"] in ("stopped", "done"):
        ok(f"异常后 task.status={prev['status']}，message={prev['message']!r}（不卡 running）")
    else:
        no(f"异常后 task.status={prev['status']}，仍卡 running（bug 未修）")
    conn.close()


def test_bug_c_export_path_consistency():
    """BUG-C：GUI 用的 export_dir 与 server 用的 default_export_dir 应一致。"""
    print("[BUG-C] GUI 与 server 导出路径一致")
    a = paths.export_dir()
    b = paths.default_export_dir()
    if a == b:
        ok(f"export_dir == default_export_dir == {a}")
    else:
        no(f"路径分裂：export_dir={a} vs default_export_dir={b}")


if __name__ == "__main__":
    print("=" * 60)
    test_bug_a_resume_after_graceful_stop()
    test_bug_b_no_zombie_running()
    test_bug_c_export_path_consistency()
    print("=" * 60)
    print(f"结果：通过 {PASS}，失败 {FAIL}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
