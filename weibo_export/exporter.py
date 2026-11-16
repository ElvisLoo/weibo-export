"""导出层：把 SQLite 中的博文导出为 HTML / Markdown / PDF / JSON / TXT。

增强（相对初版）：
- 图片：pic_urls 渲染为真正的 <img>（HTML/PDF）或 Markdown 图片语法，而非仅链接文字。
- 转发：从 raw.retweeted_status 提取被转原文（作者/时间/正文/图片），渲染为引用块。
- 正文内嵌 <img>：微博正文 text 里有时带 <img> 表情/图，HTML 模式原样保留。

其它：
- HTML：Jinja2 渲染，内联 CSS，中文 UTF-8。
- Markdown / TXT：纯文本拼接。
- PDF：reportlab 生成，注册 Windows 中文字体，自动 fallback。
- JSON：结构化完整数据（含 retweeted 与 pic_urls）。
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import db

SUPPORTED_FORMATS = ("html", "md", "pdf", "json", "txt")

# 常见 Windows / 跨平台中文字体候选（按优先级）
_CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",       # 微软雅黑
    r"C:\Windows\Fonts\simsun.ttc",     # 宋体
    r"C:\Windows\Fonts\simhei.ttf",     # 黑体
    r"C:\Windows\Fonts\msyhbd.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]

_FONT_STATUS: Dict[str, Any] = {"registered": None, "file": None, "fallback": False}


def _register_cjk_font() -> str:
    """注册一个中文字体并返回其 reportlab 字体名。失败则返回内置 CJK 字体名。"""
    if _FONT_STATUS["registered"]:
        return _FONT_STATUS["registered"]
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    for path in _CJK_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("WeiboCJK", path))
                _FONT_STATUS["registered"] = "WeiboCJK"
                _FONT_STATUS["file"] = path
                return "WeiboCJK"
            except Exception:
                continue
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        _FONT_STATUS["registered"] = "STSong-Light"
        _FONT_STATUS["fallback"] = True
        return "STSong-Light"
    except Exception:
        _FONT_STATUS["registered"] = "Helvetica"
        _FONT_STATUS["fallback"] = True
        return "Helvetica"


def font_status() -> Dict[str, Any]:
    return dict(_FONT_STATUS)


# ---------------- 正文/图片/转发的统一提取 ----------------

def _clean_text(text: str) -> str:
    return (text or "").strip()


def _html_to_text(text: str) -> str:
    txt = re.sub(r"<br\s*/?>", "\n", text or "")
    txt = re.sub(r"</p>", "\n", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    return html.unescape(txt).strip()


def _post_text(p: Dict[str, Any]) -> str:
    return _clean_text(p.get("text_raw") or _html_to_text(p.get("text", "")))


def _parse_pic_urls(p: Dict[str, Any]) -> List[str]:
    """pic_urls 已在入库时存为 JSON 数组字符串。"""
    v = p.get("pic_urls")
    if isinstance(v, list):
        return [u for u in v if u]
    if isinstance(v, str) and v:
        try:
            arr = json.loads(v)
            if isinstance(arr, list):
                return [u for u in arr if u]
        except Exception:
            return [v] if v.startswith("http") else []
    return []


def _parse_retweet(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 raw.retweeted_status 提取被转原文，返回 {author, time, text, pic_urls}。"""
    raw = p.get("raw")
    if not raw:
        return None
    try:
        j = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    # 双重序列化兼容：db 里 raw 有时被多套一层 JSON
    if isinstance(j, str):
        try:
            j = json.loads(j)
        except Exception:
            return None
    if not isinstance(j, dict):
        return None
    rt = j.get("retweeted_status")
    if not isinstance(rt, dict):
        return None
    author = (rt.get("user") or {}).get("screen_name", "")
    text = _strip_tags_for_retweet(rt.get("text") or rt.get("text_raw") or "")
    pics = []
    for pic in rt.get("pics", []) or []:
        u = (pic.get("large") or {}).get("url") or pic.get("url")
        if u:
            pics.append(u)
    if not pics and rt.get("pic_urls"):
        pics = [u for u in rt["pic_urls"] if u]
    return {
        "author": author or "未知用户",
        "time": rt.get("created_at", ""),
        "text": text,
        "pic_urls": pics,
    }


def _strip_tags_for_retweet(text: str) -> str:
    """转发正文里的 <a href=...> 话题/链接标签，剥成纯文本保留可读性。"""
    txt = re.sub(r"<br\s*/?>", "\n", text or "")
    txt = re.sub(r"</p>", "\n", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    return html.unescape(txt).strip()


def _safe_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\s]+", "_", name or "user")
    return name[:60] or "user"


# ---------------- 本地图片缓存（解决新浪防盗链，实现离线备份） ----------------

import hashlib

_MEDIA_DIR_NAME = "media"


def _download_media(urls: List[str], media_dir: str,
                    user_rel: str = _MEDIA_DIR_NAME) -> Dict[str, str]:
    """把图片下载到 media_dir，返回 {远程url: 本地相对路径}。

    新浪图床有防盗链：带微博 Referer 反而 403，实测空 User-Agent 可下载。
    并发下载提速；同一 url 只下载一次；失败则不写入映射，导出时回退为远程链接。

    user_rel 是 media_dir 相对于 out_dir 的相对路径（如 "media/张三_123"），
    返回的相对路径用它在 HTML/MD 里正确引用按用户分文件夹后的图片。
    """
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed
    os.makedirs(media_dir, exist_ok=True)

    def _name(u: str) -> str:
        ext = os.path.splitext(u.split("?")[0])[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            ext = ".jpg"
        return hashlib.md5(u.encode("utf-8")).hexdigest()[:16] + ext

    # 过滤出需要下载的（未缓存且下载成功的）
    mapping: Dict[str, str] = {}
    todo: List[tuple] = []
    for u in urls:
        if u in mapping:
            continue
        name = _name(u)
        dest = os.path.join(media_dir, name)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            mapping[u] = name
        else:
            todo.append((u, name, dest))

    if todo:
        sess = requests.Session()
        sess.headers.update({"User-Agent": ""})  # 空 UA 是图床可下载的关键

        def _fetch(item):
            u, name, dest = item
            try:
                r = sess.get(u, timeout=20)
                if r.status_code == 200 and len(r.content) > 0:
                    with open(dest, "wb") as f:
                        f.write(r.content)
                    return (u, name)
            except Exception:
                return (u, None)
            return (u, None)

        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = [ex.submit(_fetch, it) for it in todo]
            for fut in as_completed(futs):
                u, name = fut.result()
                if name:
                    mapping[u] = name

    # 把文件名转成含用户文件夹的相对路径，供导出文件正确引用
    return {u: f"{user_rel}/{name}" for u, name in mapping.items()}


def _collect_all_pic_urls(posts: List[Dict[str, Any]]) -> List[str]:
    """收集全部博文（含转发）的图片 url，去重保序。"""
    seen = set()
    ordered: List[str] = []
    for p in posts:
        for u in _parse_pic_urls(p):
            if u and u not in seen:
                seen.add(u); ordered.append(u)
        rt = _parse_retweet(p)
        if rt:
            for u in rt["pic_urls"]:
                if u and u not in seen:
                    seen.add(u); ordered.append(u)
    return ordered


def _extract_pic_live_videos(raw) -> List[Tuple[int, str]]:
    """识别「实况照片」（Live Photo）里附带的短视频地址。

    Live Photo = 静态图 + 一小段配套视频。微博接口里它可能藏在：
      - pics 元素的 video_src / videoSrc / play_url / live_photo_url 字段
      - pic 元素嵌套的 video / play_info 对象里
    我们没有用户的 Live Photo 真实样例可分析，故用防御性做法：扫描所有
    常见视频字段名，命中且值是 mp4/m3u8 直链就采集。识别不到则返回空列表，
    静态图仍正常导出（不影响普通图片）。
    """
    if not raw:
        return []
    try:
        mb = raw
        for _ in range(2):
            if isinstance(mb, str):
                mb = json.loads(mb)
            else:
                break
    except Exception:
        return []
    if not isinstance(mb, dict):
        return []

    VIDEO_KEYS = ("video_src", "videoSrc", "play_url", "playUrl",
                  "live_photo_url", "livePhotoUrl", "mp4_url")
    out: List[Tuple[int, str]] = []  # [(pics下标, 视频url)]

    def _scan_pic(idx, pic):
        if not isinstance(pic, dict):
            return
        for k in VIDEO_KEYS:
            v = pic.get(k)
            if isinstance(v, str) and v.startswith("http"):
                out.append((idx, v))
        # 嵌套对象兜底
        for sub in ("video", "play_info", "media_info"):
            subv = pic.get(sub)
            if isinstance(subv, dict):
                for k in VIDEO_KEYS:
                    v = subv.get(k)
                    if isinstance(v, str) and v.startswith("http"):
                        out.append((idx, v))

    for i, pic in enumerate(mb.get("pics") or []):
        _scan_pic(i, pic)
    return out


def _collect_all_pic_live_videos(posts: List[Dict[str, Any]]) -> List[Tuple[Any, int, str]]:
    """收集全部博文（含转发）实况照片的 (博文, 图片下标, 视频url)，去重保序。

    返回 (post_dict, pic_index, video_url) 三元组，便于把实况视频精确
    绑定到它对应的那张静态图上，而不是笼统追加到博文末尾。
    """
    seen = set()
    ordered: List[Tuple[Any, int, str]] = []
    for p in posts:
        for idx, u in _extract_pic_live_videos(p.get("raw")):
            if u and u not in seen:
                seen.add(u)
                ordered.append((p, idx, u))
    return ordered


def _extract_video_url(raw) -> Optional[str]:
    """从微博 raw（mblog 对象）提取可直接下载的视频 mp4 URL。

    微博 page_info.media_info 字段结构（公开文档常见）：
      - playback_list: [{play_info: {url: "...mp4", type: 1}}]  新字段，type=1 mp4, type=2 HLS
      - mp4_hd_url / mp4_url: 旧字段，中等清晰度 mp4
      - stream_url: HLS (.m3u8)，需 ffmpeg 合并，当前环境无 ffmpeg，跳过

    优先 mp4（避免依赖 ffmpeg）；清晰度取最高（playback_list 末尾通常最高清）。
    返回 mp4 URL 字符串，或 None（无视频 / 只有 HLS / 非视频微博）。
    """
    if not raw:
        return None
    try:
        mb = raw
        # 兼容双重编码（历史数据 raw 可能被 json.dumps 两次）：最多解析两层
        for _ in range(2):
            if isinstance(mb, str):
                mb = json.loads(mb)
            else:
                break
    except Exception:
        return None
    if not isinstance(mb, dict):
        return None

    pi = mb.get("page_info")
    if not pi or pi.get("type") != "video":
        return None
    mi = pi.get("media_info") or {}

    # 优先 playback_list：微博/yt-dlp 的标准路径，每项 play_info.url + quality_desc
    # 选 type=1（mp4）里清晰度最高的（按 quality_desc 比较更可靠，退化为取最后一个）
    best_mp4 = None
    best_q = -1
    for item in mi.get("playback_list") or []:
        play = item.get("play_info") or {}
        if play.get("type") != 1 or not play.get("url"):
            continue
        q = play.get("quality") or play.get("quality_desc") or 0
        try:
            q = int(q)
        except (ValueError, TypeError):
            q = 0
        if q >= best_q:  # >= 让后面的（通常更高清）覆盖前面的
            best_mp4 = play["url"]
            best_q = q
    if best_mp4:
        return best_mp4

    # 次选直接 mp4 字段（高清优先）。这些字段名在真实接口里都出现过，
    # 按清晰度从高到低排列：1080p > 720p > hd > 普通
    for k in ("mp4_1080p_url", "mp4_1080p_mp4", "mp4_720p_url",
              "mp4_hd_url", "mp4_url"):
        if mi.get(k):
            return mi[k]

    # 只有 HLS（.m3u8）：无 ffmpeg 无法本地合并，跳过（导出时回退为远程链接）
    return None


def _collect_all_video_urls(posts: List[Dict[str, Any]]) -> List[str]:
    """收集全部博文（含转发）的 mp4 视频 URL，去重保序。"""
    seen = set()
    ordered: List[str] = []
    for p in posts:
        u = _extract_video_url(p.get("raw"))
        if u and u not in seen:
            seen.add(u); ordered.append(u)
        rt = _parse_retweet(p)
        if rt:
            rt_raw = p.get("raw")
            if rt_raw:
                try:
                    mb = json.loads(rt_raw) if isinstance(rt_raw, str) else rt_raw
                    rt_page = (mb.get("retweeted_status") or {}).get("page_info")
                    if rt_page:
                        u = _extract_video_url(json.dumps({"page_info": rt_page}, ensure_ascii=False))
                        if u and u not in seen:
                            seen.add(u); ordered.append(u)
                except Exception:
                    pass
    return ordered


def _download_videos(urls: List[str], media_dir: str,
                     user_rel: str = _MEDIA_DIR_NAME) -> Dict[str, str]:
    """把 mp4 视频下载到 media_dir，返回 {远程url: 本地相对路径}。

    视频文件大（几十 MB），顺序下载 + 进度日志，不用并发（避免带宽争抢）。
    视频服务器一般不防盗链，带普通 UA 即可。
    """
    import requests
    os.makedirs(media_dir, exist_ok=True)
    mapping: Dict[str, str] = {}

    for u in urls:
        ext = os.path.splitext(u.split("?")[0])[1].lower()
        if ext not in (".mp4", ".mov", ".webm"):
            ext = ".mp4"
        name = hashlib.md5(u.encode("utf-8")).hexdigest()[:16] + ext
        dest = os.path.join(media_dir, name)

        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            mapping[u] = f"{user_rel}/{name}"
            continue

        try:
            sess = requests.Session()
            sess.headers.update({
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)",
                "Referer": "https://m.weibo.cn/",
            })
            r = sess.get(u, timeout=120, stream=True)
            if r.status_code == 200:
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                mapping[u] = f"{user_rel}/{name}"
        except Exception:
            pass  # 失败回退为远程链接

    return mapping


def export(conn, user_id: str, out_dir: str, fmt: str) -> Dict[str, Any]:
    """导出某用户全部博文为指定格式，返回 {path, count, size, format}。"""
    fmt = fmt.lower().lstrip(".")
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"不支持的格式: {fmt}（可选 {', '.join(SUPPORTED_FORMATS)}）")

    user_id = str(user_id)
    user = db.get_user(conn, user_id) or {"user_id": user_id, "screen_name": f"user_{user_id}"}
    posts = db.get_posts(conn, user_id)
    os.makedirs(out_dir, exist_ok=True)
    screen_name = user.get("screen_name") or f"user_{user_id}"
    # 文件名不带 weibo_ 前缀，直接用「昵称_ID」，与用户文件夹同名，便于归档
    base = f"{_safe_filename(screen_name)}_{user_id}"

    # 每个用户一个独立文件夹：<out_dir>/<昵称_ID>/，所有格式文件放里面，
    # 图片放该文件夹内的 media/。结构清晰、便于按用户归档。
    #   D:\微博导出\测试用户_123456789\测试用户_123456789.html
    #   D:\微博导出\测试用户_123456789\media\...
    user_folder = _safe_filename(f"{screen_name}_{user_id}")
    user_dir = os.path.join(out_dir, user_folder)
    os.makedirs(user_dir, exist_ok=True)
    media_dir = os.path.join(user_dir, _MEDIA_DIR_NAME)  # 用户文件夹内
    url_map: Dict[str, str] = {}
    video_map: Dict[str, str] = {}
    # LivePhoto 实况视频：{(post_id, pic_index): 本地相对路径}
    live_video_map: Dict[Tuple[Any, int], str] = {}
    if fmt in ("html", "md", "txt"):
        # 相对路径相对「文件所在目录 user_dir」，故为 media/xxx
        url_map = _download_media(_collect_all_pic_urls(posts), media_dir,
                                  user_rel=_MEDIA_DIR_NAME)
        # LivePhoto 实况视频并入与 page_info 视频同一套下载/映射：
        # 先下载到本地拿到 url→本地路径，再按 (博文,图片下标) 记录。
        live_pairs = _collect_all_pic_live_videos(posts)
        live_urls = [u for _, _, u in live_pairs]
        live_dl = _download_videos(live_urls, media_dir, user_rel=_MEDIA_DIR_NAME)
        video_map.update(live_dl)
        for p, idx, u in live_pairs:
            if u in live_dl:
                live_video_map[(p.get("mblog_id"), idx)] = live_dl[u]

        video_map.update(_download_videos(_collect_all_video_urls(posts), media_dir,
                                          user_rel=_MEDIA_DIR_NAME))

    dispatch = {
        "html": _export_html, "md": _export_md, "pdf": _export_pdf,
        "json": _export_json, "txt": _export_txt,
    }
    path = dispatch[fmt](posts, user, os.path.join(user_dir, f"{base}.{fmt}"),
                         url_map=url_map, video_map=video_map,
                         live_video_map=live_video_map)
    return {"format": fmt, "path": path, "count": len(posts),
            "size": os.path.getsize(path) if os.path.exists(path) else 0,
            "media_dir": media_dir}


def export_all(conn, user_id: str, out_dir: str, fmts: Iterable[str],
               progress: Optional[callable] = None) -> List[Dict[str, Any]]:
    """导出某用户多个格式。progress(payload) 逐个格式回调，用于进度条。

    payload: {"type": "export_start"|"export_done", "index": i, "total": n,
              "fmt": 当前格式, "media": 图片数(仅 start)}
    """
    fmts = list(fmts)
    total = len(fmts)
    if progress:
        progress({"type": "export_start", "index": 0, "total": total, "fmt": fmts[0]})
    results = []
    for i, f in enumerate(fmts):
        r = export(conn, user_id, out_dir, f)
        results.append(r)
        if progress:
            progress({"type": "export_done", "index": i + 1, "total": total, "fmt": f})
    return results


# ---------------- 视图模型：把博文整理成各格式都好用的结构 ----------------

def _build_view(posts: List[Dict[str, Any]], url_map: Optional[Dict[str, str]] = None,
                video_map: Optional[Dict[str, str]] = None,
                live_video_map: Optional[Dict[Tuple[Any, int], str]] = None) -> List[Dict[str, Any]]:
    """为每条博文准备 pic_urls(list)、video_urls(list)、retweet(dict|None)、text(纯文本)。

    url_map 不为空时（本地已下载图片），把图片 url 替换为本地相对路径
    "media/<用户文件夹>/xxx"，使导出文件离线也能显示图片、不受新浪防盗链影响。
    video_map 同理，把视频 mp4 url 替换为本地路径。
    live_video_map：{(mblog_id, 图片下标): 本地路径}，把「实况照片 Live Photo」
    的配套短视频绑定到它对应的那张静态图上；对应图渲染为可悬停/点击播放的实况图。
    图片已按用户分文件夹，避免多用户混杂无法分辨。
    """
    def _resolve(u: str) -> str:
        if url_map and u in url_map:
            return url_map[u]  # 已是含用户文件夹的相对路径
        return u

    def _resolve_video(u: str) -> str:
        if video_map and u in video_map:
            return video_map[u]
        return u

    out = []
    for p in posts:
        d = dict(p)
        mid = p.get("mblog_id")
        # pic_urls 由「字符串列表」升级为「{url, live_video} 对象列表」，
        # live_video 为该图的实况视频本地路径（无则 None）。兼容各渲染器。
        pics: List[Dict[str, Optional[str]]] = []
        for i, u in enumerate(_parse_pic_urls(p)):
            lv = live_video_map.get((mid, i)) if live_video_map else None
            pics.append({"url": _resolve(u), "live_video": lv})
        d["pic_items"] = pics
        d["pic_urls"] = [it["url"] for it in pics]  # 兼容 md/txt/pdf 旧用法
        v = _extract_video_url(p.get("raw"))
        d["video_url"] = _resolve_video(v) if v else None
        rt = _parse_retweet(p)
        if rt:
            rt["pic_urls"] = [_resolve(u) for u in rt["pic_urls"]]
        d["retweet"] = rt
        d["text"] = _post_text(p)
        out.append(d)
    return out


# ---------------- HTML ----------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ user.screen_name }} 的微博备份</title>
<style>
 body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;max-width:720px;margin:0 auto;
      padding:24px;background:#f5f6f7;color:#1a1a1a;line-height:1.7}
 .head{background:#fff;border-radius:12px;padding:20px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
 .head h1{margin:0 0 8px;font-size:22px}
 .meta{color:#888;font-size:13px}
 .post{background:#fff;border-radius:12px;padding:18px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
 .post .time{color:#888;font-size:12px;margin-bottom:8px}
 .post .txt{white-space:pre-wrap;word-wrap:break-word}
 .post .stats{margin-top:10px;color:#888;font-size:12px}
 .post .src{color:#aaa;font-size:12px;margin-top:4px}
 .pics{margin-top:10px;display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
 .pics .pic-item{position:relative;margin:0}
 .pics .pic-item img{width:100%;height:auto;border-radius:6px;display:block;cursor:pointer}
 .pics .pic-item video.live{display:none;position:absolute;inset:0;width:100%;height:100%;
   object-fit:cover;border-radius:6px;pointer-events:none}
 .pics .pic-item:hover video.live,.pics .pic-item.playing video.live{display:block}
 .pics .pic-item .live-badge{position:absolute;top:4px;right:4px;background:rgba(0,0,0,.55);
   color:#fff;font-size:10px;padding:1px 6px;border-radius:3px;pointer-events:none;letter-spacing:.5px}
 .videos{margin-top:10px}
 .videos video{width:100%;max-width:480px;border-radius:6px;display:block;background:#000}
 .videos .v-note{color:#aaa;font-size:12px;margin-top:4px}
 .retweet{margin-top:10px;background:#f7f8fa;border-left:3px solid #e6162d;
          border-radius:6px;padding:10px 12px;font-size:14px}
 .retweet .rt-head{color:#888;font-size:12px;margin-bottom:6px}
 .retweet .rt-text{white-space:pre-wrap;word-wrap:break-word}
 .retweet .rt-pics{margin-top:8px;display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
 .retweet .rt-pics img{width:100%;height:auto;border-radius:6px;display:block}
 footer{text-align:center;color:#aaa;font-size:12px;padding:20px}
</style></head>
<body>
<div class="head">
  <h1>{{ user.screen_name }} 的微博备份</h1>
  <div class="meta">
    用户ID：{{ user.user_id }}　·　微博数：{{ posts|length }}　·　
    导出时间：{{ exported_at }}
    {% if user.description %}<div style="margin-top:8px">{{ user.description }}</div>{% endif %}
  </div>
</div>
{% for p in posts %}
<div class="post">
  <div class="time">{{ p.created_at }}</div>
  <div class="txt">{{ p.text_render }}</div>
  {% if p.pic_items %}
  <div class="pics">{% for it in p.pic_items %}
    <figure class="pic-item">
      <img src="{{ it.url }}" loading="lazy" alt="微博图片">
      {% if it.live_video %}<video class="live" src="{{ it.live_video }}" muted loop playsinline preload="metadata"></video><span class="live-badge" title="实况照片">实况</span>{% endif %}
    </figure>
  {% endfor %}</div>
  {% endif %}
  {% if p.video_url %}
  <div class="videos">
    <video src="{{ p.video_url }}" controls preload="metadata" poster="">您的浏览器不支持 video 标签</video>
  </div>
  {% endif %}
  {% if p.retweet %}
  <div class="retweet">
    <div class="rt-head">@{{ p.retweet.author }}{% if p.retweet.time %} · {{ p.retweet.time }}{% endif %} 转发自：</div>
    <div class="rt-text">{{ p.retweet.text_render }}</div>
    {% if p.retweet.pic_urls %}
    <div class="rt-pics">{% for u in p.retweet.pic_urls %}<img src="{{ u }}" loading="lazy" alt="转发图片">{% endfor %}</div>
    {% endif %}
  </div>
  {% endif %}
  <div class="stats">🔁 {{ p.reposts_count }}　💬 {{ p.comments_count }}　👍 {{ p.attitudes_count }}</div>
  {% if p.source %}<div class="src">来自 {{ p.source }}{% if p.region_name %} · {{ p.region_name }}{% endif %}</div>{% endif %}
</div>
{% endfor %}
<footer>由 极客港 (jikegang.com) · jikegang-weibo-export 生成 · 仅限个人备份</footer>
<script>
// 实况照片(Live Photo)：点击静态图 → 播放配套视频；再点暂停并回到静态图
document.addEventListener('click', function (e) {
  var item = e.target.closest && e.target.closest('.pic-item');
  if (!item) return;
  var v = item.querySelector('video.live');
  if (!v) return;
  e.preventDefault();
  if (item.classList.contains('playing')) {
    v.pause(); v.currentTime = 0;
    item.classList.remove('playing');
  } else {
    v.play().catch(function(){});
    item.classList.add('playing');
  }
});
</script>
</body></html>"""


def _export_html(posts: List[Dict[str, Any]], user: Dict[str, Any], path: str,
                 url_map: Optional[Dict[str, str]] = None,
                 video_map: Optional[Dict[str, str]] = None,
                 live_video_map: Optional[Dict[Tuple[Any, int], str]] = None) -> str:
    from jinja2 import Template
    enriched = _build_view(posts, url_map, video_map, live_video_map)
    for d in enriched:
        d["text_render"] = d["text"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        if d["retweet"]:
            d["retweet"]["text_render"] = d["retweet"]["text"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    html_str = Template(_HTML_TEMPLATE).render(
        user=user, posts=enriched,
        exported_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_str)
    return path


# ---------------- Markdown ----------------

def _export_md(posts: List[Dict[str, Any]], user: Dict[str, Any], path: str,
               url_map: Optional[Dict[str, str]] = None,
               video_map: Optional[Dict[str, str]] = None,
               live_video_map: Optional[Dict[Tuple[Any, int], str]] = None) -> str:
    lines = [f"# {user.get('screen_name','')} 的微博备份", ""]
    lines.append(f"> 用户ID：{user.get('user_id','')}　微博数：{len(posts)}　导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    for p in _build_view(posts, url_map, video_map, live_video_map):
        lines.append(f"## {p.get('created_at','')}")
        lines.append("")
        lines.append(p["text"])
        lines.append("")
        for it in p["pic_items"]:
            lines.append(f"![微博图片]({it['url']})")
            if it.get("live_video"):
                lines.append(f"  [📷 实况照片 · 视频]({it['live_video']})")
        if p["pic_urls"]:
            lines.append("")
        if p.get("video_url"):
            lines.append(f"[🎬 视频]({p['video_url']})")
            lines.append("")
        if p["retweet"]:
            rt = p["retweet"]
            lines.append(f"> 🔁 转发自 @{rt['author']}" + (f" · {rt['time']}" if rt["time"] else ""))
            for rl in rt["text"].split("\n"):
                lines.append(f"> {rl}")
            for u in rt["pic_urls"]:
                lines.append(f"> ![转发图片]({u})")
            lines.append("")
        lines.append(
            f"- 转发 {p.get('reposts_count',0)} · 评论 {p.get('comments_count',0)} · "
            f"点赞 {p.get('attitudes_count',0)}"
        )
        if p.get("source"):
            lines.append(f"- 来自 {p['source']}")
        lines.append("")
        lines.append("---")
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ---------------- TXT ----------------

def _export_txt(posts: List[Dict[str, Any]], user: Dict[str, Any], path: str,
                url_map: Optional[Dict[str, str]] = None,
                video_map: Optional[Dict[str, str]] = None,
                live_video_map: Optional[Dict[Tuple[Any, int], str]] = None) -> str:
    lines = [f"{user.get('screen_name','')} 的微博备份", f"用户ID：{user.get('user_id','')}",
             f"微博数：{len(posts)}", "=" * 40, ""]
    for p in _build_view(posts, url_map, video_map, live_video_map):
        lines.append(f"[{p.get('created_at','')}]")
        lines.append(p["text"])
        for u in p["pic_urls"]:
            lines.append(f"  [图片] {u}")
        if p["retweet"]:
            rt = p["retweet"]
            lines.append(f"  >> 转发自 @{rt['author']}: {rt['text']}")
            for u in rt["pic_urls"]:
                lines.append(f"     [转发图片] {u}")
        lines.append(f"转发{p.get('reposts_count',0)} 评论{p.get('comments_count',0)} 点赞{p.get('attitudes_count',0)}")
        if p.get("source"):
            lines.append(f"来自 {p['source']}")
        lines.append("-" * 40)
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ---------------- JSON ----------------

def _export_json(posts: List[Dict[str, Any]], user: Dict[str, Any], path: str,
                 url_map: Optional[Dict[str, str]] = None,
                 video_map: Optional[Dict[str, str]] = None,
                 live_video_map: Optional[Dict[Tuple[Any, int], str]] = None) -> str:
    payload = {
        "brand": "极客港 jikegang.com",
        "tool": "jikegang-weibo-export",
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "user": {k: user.get(k) for k in ("user_id", "screen_name", "description",
                                          "statuses_count", "followers_count")},
        "post_count": len(posts),
        "posts": [
            {
                "mblog_id": p.get("mblog_id"),
                "user_id": p.get("user_id"),
                "created_at": p.get("created_at"),
                "text": _post_text(p),
                "reposts_count": p.get("reposts_count"),
                "comments_count": p.get("comments_count"),
                "attitudes_count": p.get("attitudes_count"),
                "source": p.get("source"),
                "region_name": p.get("region_name"),
                "pic_urls": _parse_pic_urls(p),
                "live_photos": [
                    {"pic_index": i, "video": lv}
                    for i, lv in _extract_pic_live_videos(p.get("raw"))
                ],
                "video_url": _extract_video_url(p.get("raw")),
                "retweet": _parse_retweet(p),
            }
            for p in posts
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


# ---------------- PDF ----------------

def _export_pdf(posts: List[Dict[str, Any]], user: Dict[str, Any], path: str,
                url_map: Optional[Dict[str, str]] = None,
                video_map: Optional[Dict[str, str]] = None,
                live_video_map: Optional[Dict[Tuple[Any, int], str]] = None) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Image,
                                    ListFlowable, ListItem, KeepTogether)
    from reportlab.lib.enums import TA_LEFT

    font = _register_cjk_font()
    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"{user.get('screen_name','')} 微博备份")
    h1 = ParagraphStyle("h1", fontName=font, fontSize=18, leading=24, spaceAfter=8)
    sub = ParagraphStyle("sub", fontName=font, fontSize=9, leading=13,
                         textColor="#888888", spaceAfter=14)
    post_h = ParagraphStyle("post_h", fontName=font, fontSize=9, leading=13,
                            textColor="#888888", spaceBefore=6, spaceAfter=3)
    body = ParagraphStyle("body", fontName=font, fontSize=10.5, leading=17,
                          alignment=TA_LEFT, spaceAfter=4, wordWrap="CJK")
    stat = ParagraphStyle("stat", fontName=font, fontSize=8.5, leading=12,
                          textColor="#999999", spaceAfter=2)
    rt_head = ParagraphStyle("rt_head", fontName=font, fontSize=9, leading=13,
                             textColor="#666666", spaceAfter=2)
    rt_body = ParagraphStyle("rt_body", fontName=font, fontSize=9.5, leading=15,
                             textColor="#444444", spaceAfter=2, wordWrap="CJK")

    def esc(s: str) -> str:
        return html.escape(s or "")

    max_img_w = 70 * mm

    def img_flowables(urls: List[str]) -> List[Any]:
        """把图片 URL 转成 PDF 图片元素。

        注意：PDF 里嵌入远程图需要联网且格式受限（reportlab 不支持 GIF，
        部分环境也无法下载新浪图床）。失败时降级为图片链接文字，保证导出不中断。
        """
        flows: List[Any] = []
        for u in urls[:9]:
            # 本地化图片：把相对路径 media/xxx 解析成绝对路径供 reportlab 读取
            local = u
            if u.startswith(_MEDIA_DIR_NAME + "/") and url_map:
                cand = os.path.join(os.path.dirname(path), u)
                if os.path.exists(cand):
                    local = cand
            if not re.search(r"\.(jpe?g|png)(\?|$)", u, re.I):
                # GIF 等不支持的格式：记为链接
                flows.append(Paragraph(esc(f"[动图] {u}"), stat))
                flows.append(Spacer(1, 2))
                continue
            try:
                img = Image(local, width=max_img_w, height=max_img_w * 0.75)
                img.hAlign = "LEFT"
                flows.append(img)
                flows.append(Spacer(1, 3))
            except Exception:
                flows.append(Paragraph(esc(f"[图片] {u}"), stat))
                flows.append(Spacer(1, 2))
        return flows

    story = [Paragraph(esc(f"{user.get('screen_name','')} 的微博备份"), h1),
             Paragraph(esc(f"用户ID：{user.get('user_id','')}　微博数：{len(posts)}"), sub),
             Spacer(1, 4)]
    for p in _build_view(posts, url_map, video_map, live_video_map):
        story.append(Paragraph(esc(p.get("created_at", "")), post_h))
        story.append(Paragraph(esc(p["text"]).replace("\n", "<br/>"), body))
        if p["pic_urls"]:
            story.extend(img_flowables(p["pic_urls"]))
        if p["retweet"]:
            rt = p["retweet"]
            rt_block = [Paragraph(esc(f"🔁 转发自 @{rt['author']}" +
                                       (f" · {rt['time']}" if rt["time"] else "")), rt_head),
                        Paragraph(esc(rt["text"]).replace("\n", "<br/>"), rt_body)]
            rt_block.extend(img_flowables(rt["pic_urls"]))
            story.append(KeepTogether(rt_block))
        story.append(Paragraph(esc(f"🔁 {p.get('reposts_count',0)}　💬 "
                                   f"{p.get('comments_count',0)}　👍 "
                                   f"{p.get('attitudes_count',0)}"), stat))
        if p.get("source"):
            region = f" · {p['region_name']}" if p.get("region_name") else ""
            story.append(Paragraph(esc(f"来自 {p['source']}{region}"), stat))
        story.append(Spacer(1, 6))
    doc.build(story)
    return path
