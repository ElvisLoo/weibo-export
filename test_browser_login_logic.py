"""内置浏览器登录取 Cookie —— 登录态判定逻辑自测。

运行方式：
    python test_browser_login_logic.py

背景：微博未登录（游客态）也会种下 SUB/SUBP，导致旧逻辑误判为「已登录」、
自动抓取并关窗。修复后判定必须以「是否存在真登录字段」为准。

覆盖：
    1. 只有 SUB/SUBP（游客态）          → 判定未登录（False）
    2. 含 SSOLoginState（真登录）        → 判定已登录（True）
    3. 含 MLOGIN（真登录）               → 判定已登录（True）
    4. 空 / None / 无关字段             → 判定未登录（False）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from weibo_export.browser_login import _is_really_logged_in   # noqa: E402


def main() -> int:
    cases = [
        ("游客态：只有 SUB/SUBP", {"SUB", "SUBP", "XSRF-TOKEN", "_T_WM"}, False),
        ("真登录：含 SSOLoginState", {"SUB", "SUBP", "SSOLoginState"}, True),
        ("真登录：含 MLOGIN", {"SUB", "MLOGIN"}, True),
        ("真登录：全字段", {"SUB", "SUBP", "MLOGIN", "SSOLoginState"}, True),
        ("空集合", set(), False),
        ("None", None, False),
        ("无关字段", {"FOO", "BAR"}, False),
        ("只有游客标识 SUB", {"SUB"}, False),
    ]

    failures = 0
    for name, names, expected in cases:
        got = _is_really_logged_in(names)
        ok = got is expected
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}: names={names} → {got}（期望 {expected}）")
        if not ok:
            failures += 1

    print("\n" + "=" * 60)
    print(f"结果：通过 {len(cases) - failures} / {len(cases)}，失败 {failures}")
    print("=" * 60)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
