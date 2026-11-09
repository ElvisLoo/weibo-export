"""自测脚本：验证「贴微博主页链接 → 自动识别 ID」功能。

运行方式：
    python test_url_parse.py

覆盖场景：
    - weibo.com/u/1234567890
    - m.weibo.cn/u/1234567890
    - m.weibo.cn/profile/1234567890
    - weibo.com/abcdefg        （自定义域名 → 需联网换 UID）
    - 带协议头 / 不带协议头
    - 纯数字（不应误判为链接）
    - 普通文本（不应误判为链接）
    - 已知非用户路径不应误判
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from weibo_export.crawler import _extract_uid_from_url, _parse_uid_input


def test_direct_uid_urls():
    print("\n[测试1] 直接带数字 ID 的链接 → 本地识别")
    cases = {
        "https://weibo.com/u/1234567890": "1234567890",
        "https://m.weibo.cn/u/1234567890": "1234567890",
        "https://m.weibo.cn/profile/1234567890": "1234567890",
        "https://weibo.com/profile.php?uid=1234567890": "1234567890",
        # 不带协议头也能识别
        "weibo.com/u/1234567890": "1234567890",
        "m.weibo.cn/profile/1234567890": "1234567890",
    }
    for url, expected in cases.items():
        got = _extract_uid_from_url(url)
        status = "✓" if got == expected else "✗"
        print(f"  {status} {url} → {got}")
        assert got == expected, f"{url}: 预期 {expected}，实际 {got}"
    print("  ✓ 全部直接 ID 链接识别正确")


def test_custom_domain_needs_network():
    print("\n[测试2] weibo.com/自定义域名 → 需联网换 UID")
    cases = {
        "https://weibo.com/abcdefg": "abcdefg",
        "https://weibo.com/yaozeyuan": "yaozeyuan",
        "weibo.com/abcdefg": "abcdefg",
    }
    for url, expected in cases.items():
        got = _extract_uid_from_url(url)
        status = "✓" if got == expected else "✗"
        print(f"  {status} {url} → {got}（自定义域名，待联网解析）")
        assert got == expected, f"{url}: 预期 {expected}，实际 {got}"
    print("  ✓ 自定义域名被正确提取（UI 将后台解析为数字 UID）")


def test_parse_uid_input_three_states():
    print("\n[测试3] _parse_uid_input 三种返回状态")
    # (uid, False)：本地识别到数字
    uid, net = _parse_uid_input("https://weibo.com/u/1234567890")
    assert (uid, net) == ("1234567890", False), f"预期 ('1234567890', False)，实际 {(uid, net)}"
    print("  ✓ 直接 ID 链接 → (数字ID, False)")

    # (None, True)：自定义域名需联网
    uid, net = _parse_uid_input("https://weibo.com/abcdefg")
    assert (uid, net) == (None, True), f"预期 (None, True)，实际 {(uid, net)}"
    print("  ✓ 自定义域名 → (None, True) 待联网")

    # (None, None)：非链接（纯数字）
    uid, net = _parse_uid_input("123456789")
    assert (uid, net) == (None, None), f"纯数字不应判定为链接，实际 {(uid, net)}"
    print("  ✓ 纯数字 → (None, None) 不打扰")

    # (None, None)：普通文本
    uid, net = _parse_uid_input("随便打几个字")
    assert (uid, net) == (None, None)
    print("  ✓ 普通文本 → (None, None) 不打扰")

    # (None, None)：空输入
    uid, net = _parse_uid_input("")
    assert (uid, net) == (None, None)
    print("  ✓ 空输入 → (None, None) 不打扰")


def test_known_non_user_paths():
    print("\n[测试4] 已知非用户路径不误判为自定义域名")
    cases = ["https://weibo.com/login", "https://weibo.com/signup", "https://weibo.com/api"]
    for url in cases:
        got = _extract_uid_from_url(url)
        print(f"  ✓ {url} → {got}（不应误判）")
        assert got is None, f"{url} 不应被识别为用户域名"
    print("  ✓ 非用户路径已排除")


def main():
    print("=" * 60)
    print("贴链接自动识别 ID —— 自测")
    print("=" * 60)
    tests = [
        test_direct_uid_urls,
        test_custom_domain_needs_network,
        test_parse_uid_input_three_states,
        test_known_non_user_paths,
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
