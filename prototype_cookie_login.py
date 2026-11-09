"""原型验证：软件内登录微博 → 自动抓取 Cookie → 存盘。

用途：验证「内置微浏览器让小白登录、软件自动拿 Cookie」这条路是否走得通。
运行方式（在 weibo-export/ 目录下）：
    python prototype_cookie_login.py

流程：
    1. 弹出一个内嵌浏览器窗口，打开微博登录页
    2. 你在窗口里正常登录（扫码 / 账号密码 / 验证码都行）
    3. 登录成功后，脚本自动抓取 weibo.com 域下的 Cookie
    4. 把 Cookie 存到 「微博Cookie_原型.txt」，可直接粘贴回主工具的 Cookie 栏
    5. 脚本窗口自动关闭

注意：
    - 这是独立原型，不改主程序
    - Cookie 等同账号凭证，仅存本机，勿外传
"""

import webview
import threading
import time
import os

WEIBO_LOGIN_URL = "https://m.weibo.cn/"
WEIBO_DOMAIN = ".weibo.cn"
# 关键登录态字段，抓到这些才算登录成功
KEY_COOKIES = {"SUB", "SUBP", "MLOGIN", "SSOLoginState"}
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "微博Cookie_原型.txt")


def make_header_cookie(cookies) -> str:
    """把 pywebview 返回的 Cookie 对象列表拼成请求头用的 'k=v; k2=v2' 字符串。"""
    pairs = []
    for c in cookies:
        try:
            name = c.name
            value = c.value
        except AttributeError:
            # 兼容不同 pywebview 版本的 Cookie 对象
            d = c if isinstance(c, dict) else getattr(c, "__dict__", {})
            name = d.get("name", "")
            value = d.get("value", "")
        if name:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def on_loaded(window):
    """页面加载完成时，尝试抓一次 Cookie（登录后 weibo.cn 域会有登录态）。"""
    cookies = window.get_cookies()
    got_keys = set()
    for c in cookies:
        try:
            if c.name in KEY_COOKIES:
                got_keys.add(c.name)
        except AttributeError:
            pass
    # 抓到关键登录态就保存并关闭
    if got_keys:
        header = make_header_cookie(cookies)
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(header)
        print(f"\n✅ 登录成功！已抓到登录态 Cookie：{sorted(got_keys)}")
        print(f"   共 {len(cookies)} 个 Cookie，已存到：{OUTPUT_FILE}")
        print(f"   Cookie 长度：{len(header)} 字符")
        print("   → 可直接粘贴回主工具的 Cookie 栏使用")
        threading.Thread(target=lambda: (time.sleep(1), window.destroy()), daemon=True).start()


def main():
    print("=" * 60)
    print("微博 Cookie 登录取回 —— 原型验证")
    print("=" * 60)
    print(f"即将打开微博登录窗口，请在窗口内正常登录。")
    print(f"登录成功后会自动抓取 Cookie 存到：{OUTPUT_FILE}")
    print("-" * 60)

    # storage_path 让 Cookie 持久化（非必须，但便于调试）
    storage = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_proto_webview_data")
    os.makedirs(storage, exist_ok=True)

    window = webview.create_window(
        "微博登录（原型）",
        WEIBO_LOGIN_URL,
        width=420,
        height=720,
    )
    # loaded 事件：每次页面加载完成（含登录跳转）都尝试抓 Cookie
    window.events.loaded += on_loaded
    # private_mode=False 让 Cookie 保留；storage_path 指定持久化目录
    webview.start(private_mode=False, storage_path=storage)

    if os.path.exists(OUTPUT_FILE):
        print("\n" + "=" * 60)
        print("✅ 原型验证成功：软件内登录 → 自动取 Cookie → 存盘，整条链路跑通")
        print("=" * 60)
    else:
        print("\n未检测到登录（可能未完成登录就关闭了窗口）。可重新运行再试。")


if __name__ == "__main__":
    main()
