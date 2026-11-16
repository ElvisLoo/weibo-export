"""自测脚本：验证视频下载功能（不触网，全 mock）。

运行方式：
    python test_video.py

测试场景：
    1. _extract_video_url：从 raw.page_info 提取 mp4 / 识别 HLS / 非视频返回 None
    2. _collect_all_video_urls：多条博文去重收集 mp4
    3. _download_videos：mock HTTP，验证 mp4 落盘并生成相对路径映射
    4. _build_view：video_url 正确解析为本地相对路径
    5. export(html)：端到端，HTML 里出现 <video> 标签
"""

import sys
import json
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent))

from weibo_export import exporter
from weibo_export import db

MP4 = "https://f.video.weibocdn.com/abc.mp4"
MP4_HD = "https://f.video.weibocdn.com/abc_hd.mp4"
HLS = "https://f.video.weibocdn.com/abc.m3u8"


def _video_post(mid, media_info, rtype="video"):
    raw = {"id": mid, "page_info": {"type": rtype, "media_info": media_info}}
    return {"mblog_id": str(mid), "raw": json.dumps(raw, ensure_ascii=False)}


def test_extract():
    print("[测试1] _extract_video_url 字段优先级")
    # playback_list 里 mp4 优先
    mi = {"playback_list": [{"play_info": {"url": MP4, "type": 1}},
                            {"play_info": {"url": HLS, "type": 2}}],
          "mp4_url": "old.mp4"}
    assert exporter._extract_video_url(_video_post(1, mi)["raw"]) == MP4, "应取 playback_list 的 mp4"
    # 只有旧 mp4_hd_url
    assert exporter._extract_video_url(_video_post(2, {"mp4_hd_url": MP4_HD})["raw"]) == MP4_HD
    # 只有 mp4_url
    assert exporter._extract_video_url(_video_post(3, {"mp4_url": MP4})["raw"]) == MP4
    # 只有 HLS → None（无 ffmpeg）
    assert exporter._extract_video_url(_video_post(4, {"stream_url": HLS})["raw"]) is None
    # 非视频
    assert exporter._extract_video_url(_video_post(5, {}, rtype="pic")["raw"]) is None
    # 空
    assert exporter._extract_video_url(None) is None
    assert exporter._extract_video_url({}) is None
    print("  ✓ mp4 优先、HLS 跳过、非视频返回 None")


def test_collect():
    print("[测试2] _collect_all_video_urls 去重收集")
    posts = [
        _video_post(1, {"mp4_url": MP4}),
        {"mblog_id": "2", "raw": json.dumps({"id": 2, "text": "纯图文"})},
        _video_post(3, {"mp4_hd_url": MP4_HD}),
        _video_post(4, {"mp4_url": MP4}),  # 与第1条同 URL，应去重
    ]
    urls = exporter._collect_all_video_urls(posts)
    assert urls == [MP4, MP4_HD], f"去重保序失败: {urls}"
    print(f"  ✓ 收集到 {len(urls)} 条 mp4（已去重）")


def test_download(tmp):
    print("[测试3] _download_videos 落盘 + 路径映射")
    import requests as _r
    media_dir = tmp / "media"
    calls = []

    class FakeResp:
        status_code = 200
        def iter_content(self, chunk_size=8192):
            yield b"FAKE_MP4_BYTES"
    def fake_get(url, timeout=None, stream=False):
        calls.append(url)
        return FakeResp()

    orig = _r.Session
    _r.Session = lambda: Mock(get=fake_get)
    try:
        mapping = exporter._download_videos([MP4], str(media_dir), user_rel="media")
    finally:
        _r.Session = orig
    assert MP4 in mapping, f"下载失败: {mapping}"
    name = mapping[MP4].split("/")[-1]
    assert (media_dir / name).exists(), "文件未落盘"
    assert mapping[MP4].startswith("media/"), f"相对路径错: {mapping[MP4]}"
    print(f"  ✓ mp4 落盘 {name}，映射 {mapping[MP4]}")


def test_build_view():
    print("[测试4] _build_view 解析 video_url")
    posts = [_video_post(1, {"mp4_url": MP4})]
    vmap = {MP4: "media/user_x/abc.mp4"}
    view = exporter._build_view(posts, url_map=None, video_map=vmap)
    assert view[0]["video_url"] == "media/user_x/abc.mp4", view[0]["video_url"]
    # 无 video_map → 原始远程链接
    view2 = exporter._build_view(posts)
    assert view2[0]["video_url"] == MP4
    print(f"  ✓ video_url 解析: 本地={view[0]['video_url']}")


def test_export_html(tmp):
    print("[测试5] export(html) 端到端含 <video>")
    import tempfile, os
    out_dir = tmp / "out"
    out_dir.mkdir()
    dbp = tmp / "t.db"
    conn = db.connect(str(dbp))
    user_id = "999"
    db.upsert_user(conn, {"user_id": user_id, "screen_name": "测试",
                          "description": "", "statuses_count": 1, "followers_count": 0})
    db.upsert_posts(conn, [{
        "mblog_id": "1", "user_id": user_id, "created_at": "2024-01-01 00:00:00",
        "text": "带视频", "text_raw": "带视频", "reposts_count": 0, "comments_count": 0,
        "attitudes_count": 0, "source": "", "region_name": "",
        "pic_urls": [], "raw": _video_post(1, {"mp4_url": MP4})["raw"],
    }])
    # mock 掉真实网络下载
    orig_media = exporter._download_media
    orig_video = exporter._download_videos
    exporter._download_media = lambda urls, md, user_rel="media": {}
    exporter._download_videos = lambda urls, md, user_rel="media": {MP4: f"{user_rel}/abc.mp4"}
    try:
        res = exporter.export(conn, user_id, str(out_dir), "html")
    finally:
        exporter._download_media = orig_media
        exporter._download_videos = orig_video
    html = Path(res["path"]).read_text(encoding="utf-8")
    assert "<video" in html, "HTML 未渲染 video 标签"
    assert "abc.mp4" in html, "video src 未指向本地 mp4"
    conn.close()
    print(f"  ✓ HTML 含 <video>，src=media/abc.mp4")


def main():
    from pathlib import Path as P
    tmp = P(__file__).parent / "_qa" / "test_video_tmp"
    import shutil
    if tmp.exists():
        shutil.rmtree(tmp, onerror=lambda *a, **k: None)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        test_extract()
        test_collect()
        test_download(tmp)
        test_build_view()
        test_export_html(tmp)
    finally:
        pass  # 留下 _qa/test_video_tmp 供人工检查，不自动删（避开系统保留扫描）
    print("\n" + "=" * 60)
    print("结果：视频下载功能 5/5 通过 ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()
