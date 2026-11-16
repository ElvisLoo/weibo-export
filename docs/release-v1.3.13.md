# v1.3.13 · 修正视频字段优先级

## 核对接口后的修正

去核对了微博 `page_info.media_info` 的真实字段结构（参考 yt-dlp 的 weibo 提取器与多份工程化实现），发现 v1.3.12 的视频提取逻辑漏了两个真实存在的清晰度字段，这次补上：

- **补上 `mp4_1080p_url` / `mp4_720p_url`**：v1.3.12 只查了 `mp4_hd_url`/`mp4_url`，会漏掉用这两个字段返回的高清视频。现在按 `1080p > 720p > hd > 普通` 优先级取。
- **优化 playback_list 选择**：改为按 `quality`（清晰度）字段选最高的 mp4，比之前"取最后一个"更可靠；遇到 HLS 分片项会跳过。

## 字段优先级（修正后）

1. `playback_list` 里 `type=1`（mp4）中 `quality` 最高的
2. `mp4_1080p_url` → `mp4_1080p_mp4` → `mp4_720p_url` → `mp4_hd_url` → `mp4_url`
3. 只有 HLS（.m3u8）→ 跳过本地下载（当前环境无 ffmpeg），回退在线播放

## 验证

- `test_video.py` 新增 1080p/720p 字段、playback_list 清晰度选择用例，5/5 通过
- 现有测试全部通过（长文补全、重试、链接解析、空页判定），未破坏原有功能

真实联网下载仍需你下载后用带视频的账号实际导出确认（本机无 Cookie 未联网实测）。

---

**完整下载**：见 [Releases](https://github.com/ElvisLoo/weibo-export/releases) 页面
