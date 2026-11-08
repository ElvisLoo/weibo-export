"""自测脚本：验证抓取稳定性改进（自动重试 + 指数退避 + 抖动）。

运行方式：
    python test_crawler_retry.py

测试场景：
    1. **首次超时，第二次成功**：验证 fetch_page 遇 Timeout 会重试并最终成功
    2. **首次 418 限流，第二次成功**：验证 fetch_page 遇 HTTP 418 会重试
    3. **连续超时到重试耗尽**：验证 fetch_page 在 max_retries 次后抛 CrawlerError
    4. **用户点停止立即退出**：验证 stop_flag 被设置时重试循环立刻终止
    5. **成功路径不重试**：验证首次成功时不进入重试分支

所有测试用 mock 对象模拟 session.get()，不触真实网络。
"""

import sys
import time
from pathlib import Path
from unittest.mock import Mock, MagicMock

sys.path.insert(0, str(Path(__file__).parent))

from weibo_export.crawler import WeiboCrawler, CrawlerError


# ---------------------------------------------------------------------------
# Mock 构造器
# ---------------------------------------------------------------------------

def make_mock_response(status_code=200, json_data=None):
    """构造一个假的 requests.Response。"""
    resp = Mock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json = Mock(return_value=json_data)
    else:
        resp.json = Mock(return_value={"data": {"cards": []}, "ok": 1})
    return resp


def make_mock_crawler(session_get_side_effect, stop_flag=lambda: False, max_retries=3):
    """构造一个带 mock session.get 的 WeiboCrawler。"""
    crawler = WeiboCrawler.__new__(WeiboCrawler)
    crawler.name = "weibo"
    crawler.cookie = ""
    crawler.logged_in = False
    crawler.interval = 0.0  # 不 sleep 限速
    crawler.stop_flag = stop_flag
    crawler.progress = None
    crawler._emit = Mock()
    crawler._sleep = Mock()

    # 模拟 requests 模块和 session
    crawler._requests = Mock()
    crawler._requests.exceptions = __import__("requests").exceptions
    crawler.session = Mock()
    crawler.session.get = Mock(side_effect=session_get_side_effect)
    crawler._max_retries = max_retries
    return crawler


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

def test_timeout_retry_then_success():
    """场景1：首次超时，第二次成功。预期：不抛错，返回正常响应。"""
    import requests
    print("\n[测试1] 首次超时 → 重试 → 成功")

    # 第一次抛 Timeout，第二次返回成功
    side_effect = [
        requests.exceptions.Timeout("连接超时"),
        make_mock_response(200),
    ]
    crawler = make_mock_crawler(side_effect)

    # 临时改小 backoff（真实是 3s），避免测试太慢
    import weibo_export.crawler as cw
    original_sleep = time.sleep
    time.sleep = Mock()  # 跳过真实 sleep

    try:
        resp = crawler._get_with_retry("https://fake.url")
        assert resp.status_code == 200, f"预期 200，实际 {resp.status_code}"
        assert crawler.session.get.call_count == 2, f"预期调用 2 次，实际 {crawler.session.get.call_count} 次"
        assert crawler._emit.called, "预期发送了 retry 事件"
        retry_event = crawler._emit.call_args[0][0]
        assert retry_event["type"] == "retry", f"事件类型不对：{retry_event}"
        assert retry_event["attempt"] == 1, f"第一次重试，attempt 应为 1：{retry_event}"
        print(f"  ✓ 通过：重试 1 次后成功，重试事件 attempt={retry_event['attempt']}, wait={retry_event['wait']}s")
    finally:
        time.sleep = original_sleep


def test_rate_limit_418_retry_then_success():
    """场景2：首次 418 限流，第二次成功。预期：不抛错。"""
    print("\n[测试2] 首次 418 限流 → 重试 → 成功")

    side_effect = [
        make_mock_response(418),
        make_mock_response(200),
    ]
    crawler = make_mock_crawler(side_effect)

    original_sleep = time.sleep
    time.sleep = Mock()
    try:
        resp = crawler._get_with_retry("https://fake.url")
        assert resp.status_code == 200, f"预期 200，实际 {resp.status_code}"
        assert crawler.session.get.call_count == 2, f"预期调用 2 次，实际 {crawler.session.get.call_count} 次"
        print(f"  ✓ 通过：418 限流重试 1 次后成功")
    finally:
        time.sleep = original_sleep


def test_all_retries_exhausted():
    """场景3：连续超时到重试耗尽。预期：抛 CrawlerError。"""
    import requests
    print("\n[测试3] 连续超时 → 重试耗尽 → 抛错")

    side_effect = requests.exceptions.Timeout("一直超时")  # 每次都超时
    crawler = make_mock_crawler(side_effect, max_retries=2)  # 最多重试 2 次（共调 3 次）

    original_sleep = time.sleep
    time.sleep = Mock()
    try:
        try:
            crawler._get_with_retry("https://fake.url", max_retries=2)
            assert False, "预期抛 CrawlerError，但没抛"
        except CrawlerError as e:
            assert "多次重试仍失败" in str(e), f"错误消息不对：{e}"
            assert crawler.session.get.call_count == 3, f"预期调用 3 次（1+2重试），实际 {crawler.session.get.call_count} 次"
            assert crawler._emit.call_count >= 2, f"预期至少发送 2 次 retry 事件，实际 {crawler._emit.call_count} 次"
            print(f"  ✓ 通过：重试 2 次后抛错，共调用 3 次，发送 retry 事件 {crawler._emit.call_count} 次")
    finally:
        time.sleep = original_sleep


def test_user_stop_during_retry():
    """场景4：用户在重试等待时点停止。预期：立刻抛 CrawlerError，不继续重试。"""
    import requests
    print("\n[测试4] 用户点停止 → 立即退出重试")

    # 第 1 次超时，第 2 次超时（但第 2 次前用户已点停止）
    side_effect = [
        requests.exceptions.Timeout("超时"),
        requests.exceptions.Timeout("超时（不会走到这里，应被 stop_flag 拦住）"),
    ]
    stop_after_first = {"count": 0}
    def stop_flag():
        stop_after_first["count"] += 1
        return stop_after_first["count"] >= 2  # 第 2 次调用返回 True

    crawler = make_mock_crawler(side_effect, stop_flag=stop_flag, max_retries=3)

    original_sleep = time.sleep
    time.sleep = Mock()
    try:
        try:
            crawler._get_with_retry("https://fake.url", max_retries=3)
            assert False, "预期抛 CrawlerError，但没抛"
        except CrawlerError as e:
            assert "用户请求停止" in str(e), f"错误消息不对：{e}"
            # 应在第 2 次重试前检测到 stop 并立刻退出，只调用 1 次 get
            assert crawler.session.get.call_count == 1, f"预期只调用 1 次（用户停止后立即退出），实际 {crawler.session.get.call_count} 次"
            print(f"  ✓ 通过：用户停止后立即退出，仅调用 1 次 get")
    finally:
        time.sleep = original_sleep


def test_success_first_attempt_no_retry():
    """场景5：首次成功。预期：不进入重试分支，只调用 1 次。"""
    print("\n[测试5] 首次成功 → 不重试")

    side_effect = [make_mock_response(200)]
    crawler = make_mock_crawler(side_effect)

    resp = crawler._get_with_retry("https://fake.url")
    assert resp.status_code == 200
    assert crawler.session.get.call_count == 1, f"预期只调用 1 次，实际 {crawler.session.get.call_count} 次"
    assert not crawler._emit.called, "预期不发送 retry 事件"
    print(f"  ✓ 通过：首次成功，只调用 1 次，不发送 retry 事件")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("微博抓取稳定性改进 —— 重试机制自测")
    print("=" * 60)

    tests = [
        test_timeout_retry_then_success,
        test_rate_limit_418_retry_then_success,
        test_all_retries_exhausted,
        test_user_stop_during_retry,
        test_success_first_attempt_no_retry,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  ✗ 失败：{e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ 异常：{type(e).__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"结果：通过 {passed} / {len(tests)}，失败 {failed}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
