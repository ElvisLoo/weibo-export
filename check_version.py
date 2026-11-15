"""打包前版本号一致性检查。

用途：防止出现「release tag 升了、代码里的 __version__ 忘了改」导致的
界面显示版本与实际发布版本不一致（v1.3.8/v1.3.9 就出过这个疏漏）。

用法：
    python check_version.py            # 用当前 HEAD 对比的 tag 检查
    python check_version.py v1.3.9     # 显式指定要发布的 tag

规则：
- tag 形如 v1.3.9，代码里的 __version__ 应为 1.3.9（去掉前导 v）。
- 检查通过退出码 0；不一致退出码 1 并打印修复提示。
打包脚本应在 PyInstaller 之前调用本脚本，不一致立即中止。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INIT_PY = ROOT / "weibo_export" / "__init__.py"


def read_code_version() -> str:
    """从 weibo_export/__init__.py 读取 __version__。"""
    text = INIT_PY.read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        print(f"❌ 在 {INIT_PY.name} 里找不到 __version__ 定义")
        sys.exit(1)
    return m.group(1).strip()


def current_tag() -> str | None:
    """取当前 HEAD 指向的 tag（若有）。打包通常在打 tag 之后。"""
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--exact-match", "HEAD"],
            cwd=ROOT, capture_output=True, text=True,
        )
        tag = out.stdout.strip()
        return tag or None
    except Exception:
        return None


def normalize(tag: str) -> str:
    """v1.3.9 -> 1.3.9；容忍带/不带前导 v。"""
    t = tag.strip()
    return t[1:] if t.startswith("v") else t


def main() -> int:
    arg_tag = sys.argv[1] if len(sys.argv) > 1 else None
    tag = arg_tag or current_tag()

    code_ver = read_code_version()

    if not tag:
        print("⚠️  当前 HEAD 没有精确匹配的 tag（可能还没打 tag）。")
        print(f"   代码里的 __version__ = {code_ver}")
        print("   若要检查某个 tag，运行: python check_version.py v1.3.9")
        return 0

    tag_ver = normalize(tag)
    print(f"发布 tag      : {tag}  (版本号部分 {tag_ver})")
    print(f"代码 __version__: {code_ver}")

    if tag_ver == code_ver:
        print("✅ 版本号一致，可以打包发布。")
        return 0

    print()
    print("❌ 版本号不一致！release tag 与代码 __version__ 对不上。")
    print(f"   tag 是 {tag}，但代码显示 {code_ver}。")
    print(f"   请先把 {INIT_PY.relative_to(ROOT)} 里的 __version__ 改成 \"{tag_ver}\"，")
    print("   提交并确保该提交位于 tag 指向的 commit 上，再重新打包。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
