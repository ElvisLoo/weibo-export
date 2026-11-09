"""自测脚本：验证长博文全文补全功能。

运行方式：
    python test_longtext.py

测试场景：
    1. 长博文（isLong=True）→ 调 longText 接口 → text_raw 被替换为全文
    2. 长博文但 longText 接口失败 → 优雅回退到截断版正文，不抛错
    3. 短博文（isLong=False）→ 不触发 longText 请求
    4. 通过 "...全文" 后缀识别长博文（无 isLong 标记时）
"""

import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent))

from weibo_export.crawler import WeiboCrawler, CrawlerError


def make_crawler(get_side_effects):
    """构造一个带 mock session 的抓取器。

    get_side_effects: 一个列表，依次作为 session.get 的返回值或异常。
    """
    import requests
    crawler = WeiboCrawler.__new__(WeiboCrawler)
    crawler.name = "weibo"
    crawler.cookie = ""
    crawler.logged_in = False
    crawler.interval = 0.0
    crawler.stop_flag = lambda: False
    crawler.progress = None
    crawler._emit = Mock()
    crawler._sleep = Mock()
    crawler._requests = Mock()
    crawler._requests.exceptions = requests.exceptions
    crawler.session = Mock()
    crawler.session.get = Mock(side_effect=get_side_effects)
    return crawler


def resp(status=200, payload=None):
    r = Mock()
    r.status_code = status
    r.json = Mock(return_value=payload or {})
    return r


SHORT_POST = {
    "id": "100", "isLong": False,
    "text": "<p>这是一条短博文</p>",
    "text_raw": "这是一条短博文",
}

LONG_POST = {
    "id": "200", "isLong": True,
    "text": "<p>这是一条很长很长...(被截断)...<a href='#'>全文</a></p>",
    "text_raw": "这是一条很长很长...(被截断)...全文",
}
LONG_FULL = "这是一条很长很长的博文全文内容，总共好几百字。" * 5


def test_long_post_fetches_full_text():
    print("\n[测试1] 长博文 → 调 longText → 全文替换成功")
    crawler = make_crawler([
        resp(200, {"data": {"cards": [{"mblog": dict(LONG_POST)}]}}),  # fetch_page
        resp(200, {"data": {"longTextContent": LONG_FULL}}),           # longText
    ])
    data = crawler.fetch_page("123", 1)
    post = data["posts"][0]
    assert post["text_raw"] == LONG_FULL, f"text_raw 未被全文替换：{post['text_raw'][:40]}"
    # 应发起了 longText 请求（session.get 被调 2 次）
    assert crawler.session.get.call_count == 2, f"预期调 2 次，实际 {crawler.session.get.call_count}"
    # 应 emit longtext_fetched 事件
    types = [c[0][0].get("type") for c in crawler._emit.call_args_list]
    assert "longtext_fetched" in types, f"未发送 longtext_fetched 事件：{types}"
    print(f"  ✓ text_raw 已替换为全文（{len(LONG_FULL)} 字），发起 longText 请求")


def test_long_post_api_fail_falls_back():
    print("\n[测试2] 长博文 → longText 接口失败 → 回退到截断版，不抛错")
    crawler = make_crawler([
        resp(200, {"data": {"cards": [{"mblog": dict(LONG_POST)}]}}),  # fetch_page
        resp(500),  # longText 接口失败
        resp(500),
        resp(500),
        resp(500),  # 重试耗尽（给足重试次数）
    ])
    data = crawler.fetch_page("123", 1)
    post = data["posts"][0]
    # 应回退到截断版原文，且仍成功返回这一页
    assert post["text_raw"] == LONG_POST["text_raw"], f"应回退到截断版：{post['text_raw'][:40]}"
    assert len(data["posts"]) == 1
    print(f"  ✓ longText 失败后回退到截断版，整页抓取未中断")


def test_short_post_no_longtext_call():
    print("\n[测试3] 短博文 → 不触发 longText 请求")
    crawler = make_crawler([
        resp(200, {"data": {"cards": [{"mblog": dict(SHORT_POST)}]}}),
    ])
    data = crawler.fetch_page("123", 1)
    post = data["posts"][0]
    assert post["text_raw"] == SHORT_POST["text_raw"]
    # 短博文只调 1 次（fetch_page），不应调 longText
    assert crawler.session.get.call_count == 1, f"短博文不应调 longText，实际调 {crawler.session.get.call_count} 次"
    print("  ✓ 短博文不触发 longText 请求")


def test_fulltext_suffix_detection():
    print("\n[测试4] 无 isLong 标记但有'全文'后缀 → 仍识别为长博文")
    post_no_flag = {"id": "300", "text": "<p>长博文内容...<a>全文</a></p>", "text_raw": "长博文内容...全文"}
    crawler = make_crawler([
        resp(200, {"data": {"cards": [{"mblog": post_no_flag}]}}),
        resp(200, {"data": {"longTextContent": LONG_FULL}}),
    ])
    data = crawler.fetch_page("123", 1)
    assert data["posts"][0]["text_raw"] == LONG_FULL, "未通过'全文'后缀识别长博文"
    print("  ✓ 通过'全文'后缀识别并补全")


def main():
    print("=" * 60)
    print("长博文全文补全 —— 自测")
    print("=" * 60)
    tests = [
        test_long_post_fetches_full_text,
        test_long_post_api_fail_falls_back,
        test_short_post_no_longtext_call,
        test_fulltext_suffix_detection,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t(); passed += 1
        except AssertionError as e:
            print(f"  ✗ 失败：{e}"); failed += 1
        except Exception as e:
            print(f"  ✗ 异常：{type(e).__name__}: {e}")
            import traceback; traceback.print_exc(); failed += 1
    print("\n" + "=" * 60)
    print(f"结果：通过 {passed}/{len(tests)}，失败 {failed}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
