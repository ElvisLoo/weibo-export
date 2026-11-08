# 极客港 · 微博博文导出工具

<div align="left">
  <b>品牌归属：极客港 · jikegang.com</b><br>
  英文名：<code>jikegang-weibo-export</code>
</div>

一个**新浪微博博文导出工具**的「核心引擎 + 可复用本地界面」。

- 核心抓取与导出逻辑**全部在 Python 后端**，前端通过 HTTP 调用 → 未来网页版复用同一引擎。
- 本期跑通：**填写 Cookie → 抓取自己/指定用户博文 → 导出 HTML / Markdown / PDF / JSON / TXT**。
- 桌面端用 **Electron 壳**包一层（打开本地界面）；本期以「本地 Web 界面 + 后端」为主并完成自测，
  Electron 打包脚本已就绪但不在本期强求出安装包（中国大陆网络下 electron-builder 拉二进制常超时）。

---

## 一、目录结构

```
weibo-export/
├── weibo_export/          # Python 后端引擎（可复用）
│   ├── __init__.py
│   ├── crawler.py         # 抓取：限速、断点续传、Cookie 鉴权、进度回调
│   ├── mock_crawler.py    # 模拟抓取器（自测导出链路，不触网）
│   ├── db.py              # SQLite 读写（weibo.db）
│   ├── exporter.py        # 导出 HTML / MD / PDF / JSON / TXT
│   ├── server.py          # FastAPI 路由 + serve 前端（python -m weibo_export.server）
│   └── static/
│       └── index.html     # 单文件前端界面（内联 CSS + 原生 JS）
├── electron/              # Electron 桌面壳
│   ├── main.js            # 启动后端子进程 + 打开窗口
│   ├── preload.js
│   └── package.json       # electron-builder 打包配置
├── exports/               # 导出文件输出目录
├── requirements.txt
├── run.py                 # 一键启动入口
└── README.md
```

---

## 二、安装依赖

用受管 Python（建议虚拟环境，勿污染系统环境）：

```bash
cd weibo-export
python -m pip install -r requirements.txt
```

依赖很轻：`fastapi` + `uvicorn`（服务）、`requests`（抓取）、`jinja2`（HTML）、`reportlab`（PDF）。
PDF 中文字体优先使用 Windows 系统字体 `msyh.ttc`（微软雅黑）/ `simsun.ttc`（宋体），
**找不到时会自动 fallback 到内置字体**，不会出现方块/乱码。

---

## 三、启动

两种等价方式（在项目根 `weibo-export/` 下）：

```bash
python run.py
# 或
python -m weibo_export.server
```

启动后浏览器打开 <http://127.0.0.1:8765> 即可。

可选参数：`--host`（默认 127.0.0.1，仅本机访问）、`--port`（默认 8765）、`--db`、`--export-dir`。

桌面版（Electron）：

```bash
cd electron
npm install        # 安装 electron
npm start          # 自动拉起后端 + 打开窗口
```

---

## 四、怎么填 Cookie（推荐登录态抓全量）

新浪移动端接口 `m.weibo.cn` 需要登录 Cookie 才能抓全量。本工具**不内置账号密码登录**
（更易触发验证），而是让你**从浏览器复制自己的 Cookie 粘贴进来**，更稳、不易触发验证：

1. 电脑浏览器打开 <https://m.weibo.cn> 并**登录自己的微博**。
2. 按 `F12` 打开开发者工具 → 切到 **Network（网络）** → 刷新页面。
3. 点任意一条请求 → **Headers** → 在 **Request Headers** 里找到 **`Cookie`**。
4. 整行复制，粘贴到界面「① 填写 Cookie」的输入框。

> 用户 ID 在哪里：在 m.weibo.cn 打开对方主页，地址栏 `/u/` 后面的**纯数字**即用户 ID。

**降级模式**：Cookie 留空即为「未登录公开抓取」，无需登录但只能抓公开可见的博文。

---

## 五、怎么导出各格式

1. 抓取完成后（或点「灌入模拟数据(自测)」快速造一批数据），在「③ 导出」选用户。
2. 勾选需要的格式（HTML / Markdown / PDF / JSON / TXT）。
3. 点「导出所选格式」，文件出现在 `exports/` 目录，界面上可直接点击下载。

导出文件名形如：`weibo_<昵称>_<用户ID>.<格式>`。所有格式内容一致，用途不同：

| 格式 | 用途 |
|------|------|
| **HTML** | 保留排版、可直接浏览器查看，适合归档阅读 |
| **Markdown** | 纯文本拼接，适合二次编辑 / 放进笔记软件 |
| **PDF** | 适合打印、长期存档（中文字体已注册） |
| **JSON** | 结构化完整数据，适合程序处理 / 迁移 |
| **TXT** | 最简纯文本，任何设备可读 |

---

## 六、限速与断点续传说明

- **限速**：默认每条请求间隔 **20 秒**（`interval` 可在界面调整）。新浪对爬虫限流严格，
  20 秒是稳定部落实测的安全值。被限流（HTTP 418）时会报错提示，此时请增大间隔或稍后重试。
- **断点续传**：每个抓取任务写入 SQLite（`tasks` 表记录已抓到的最大页码 `max_page`）。
  中断后**再次抓取同一用户**会自动从上次页码的下一页继续；博文以 `mblog_id` 为主键去重，
  不会重复入库。导出始终从数据库读取，因此**随时可导出已抓到的部分**。
- **实时进度**：抓取在后台线程执行，前端每 ~1.2 秒轮询 `/api/job`，实时显示当前页、已抓页数、库内条数。
- **停止**：点「停止」会设置停止标志，**当前页完成后安全退出**（尊重限速），断点已保存。

---

## 七、自测（已通过）

后端不依赖真实 Cookie 即可端到端验证导出链路：

- **模拟抓取器** `MockCrawler`（界面选「模拟抓取」或点「灌入模拟数据」）：行为与真实抓取器完全一致
  （同样经过限速、入库、断点续传主循环），只是返回本地确定性假数据、不触网。
- 已验证：5 种格式全部生成，HTML/PDF 中文显示正常、无方块/乱码。

命令行快速自测导出：

```bash
cd weibo-export
python -c "from weibo_export import db,exporter,mock_crawler; \
mock_crawler.seed_mock_db('weibo.db'); \
print(exporter.export_all(db.connect('weibo.db'),'1892838392','exports',['html','md','pdf','json','txt']))"
```

---

## 八、版权与合规提示（重要）

- ⚠️ **仅限个人备份**：本工具仅用于备份**你自己有权访问的**微博内容。
- ❌ **不得商用**，不得用于批量采集他人数据、不得用于任何违反微博用户协议的用途。
- 请遵守微博用户协议与 robots 约定，**控制抓取频率**（务必保留限速）。
- 🔒 **Cookie 属敏感信息**（等同账号凭证）：仅保存在本机内存用于本次抓取，**不上传任何服务器**，
  请勿泄露给他人。所有数据仅存本机 `weibo.db`，导出文件存于本机导出目录。
- **导出目录**：`当前用户/文档/微博导出`（图片下载在同目录 `media/` 子文件夹，**两者需放一起**才能离线看图）。
- **应用图标说明**：图标中的新浪微博 logo 版权归**新浪公司**所有，本工具作为「微博备份」用途的标识使用，
  开源时请在 README 保留本声明；logo 不得用于暗示官方出品或任何商业用途。
- **第三方依赖许可**：本项目使用的开源库及其许可见下方「六、开源许可与第三方组件」。

---

## 六、开源许可与第三方组件

本项目代码以 **MIT License** 发布（见 `LICENSE`）。项目**自行实现**，未复制、未搬运任何第三方抓取项目的源码。

### 使用的开源库（通过 pip 安装，非源码拷贝）

| 依赖 | 用途 | 许可 |
|---|---|---|
| [FastAPI](https://github.com/tiangolo/fastapi) | 后端 API 框架（网页版/服务模式） | MIT |
| [Uvicorn](https://github.com/encode/uvicorn) | ASGI 服务器 | BSD-3-Clause |
| [Jinja2](https://github.com/pallets/jinja) | HTML 模板渲染 | BSD-3-Clause |
| [Requests](https://github.com/psf/requests) | HTTP 抓取 | Apache-2.0 |
| [ReportLab](https://www.reportlab.com/opensource/) | PDF 生成 | BSD-2-Clause |
| [Tkinter](https://docs.python.org/3/library/tkinter.html) | 桌面版 GUI（Python 标准库） | Python Software Foundation License |

> 上述库的许可均允许商用与再分发；完整许可文本随各库发行包一同分发。

### 参考项目（仅学习设计思路，未复制代码）

本项目在**功能设计**上参考了以下开源项目的思路，但**全部代码为独立实现**，未复制、未修改、未引入其源码或二进制文件：

| 项目 | 许可 | 参考内容 |
|---|---|---|
| [YaoZeyuan/stablog](https://github.com/YaoZeyuan/stablog) | MIT | 微博备份工具的功能形态、HTML/PDF 双格式导出理念 |
| [kyuumm/WeiboArchive](https://github.com/kyuumm/WeiboArchive) | MIT | 微博归档的数据组织思路 |
| [addingtonbonnie2-web/weibo-archive-reader](https://github.com/addingtonbonnie2-web/weibo-archive-reader) | 其他（NOASSERTION） | 本地化归档浏览、隐私优先的设计理念 |

> 说明：本项目与其中的抓取类项目无代码继承关系。部分同类抓取项目（如 `dataabc/weibo-crawler`）**无明确开源许可**，
> 为规避版权风险，本项目**刻意不参考、不复制**其任何源码，抓取逻辑完全自行实现。

### 致谢

- 感谢 [YaoZeyuan](https://github.com/YaoZeyuan) 的 **stablog** 在微博备份领域做出的探索，其公开的文档与设计对本项目的功能规划有启发。
- 感谢以上所有开源库作者的无私贡献。

---

## 七、常见问题

<details>
<summary><b>1. 为什么需要登录？</b></summary>

只有登录后，才能获取账号下的全部微博内容（包括仅自己可见的部分）。从 GitHub Releases 下载的软件包可保证安全，源码完全开放，可自行编译验证。
</details>

<details>
<summary><b>2. 为什么每条请求间隔 20 秒？</b></summary>

新浪对爬虫限制严格，20s 是多次试验后验证的安全最短间隔。强行加速会导致账号被限制。
</details>

<details>
<summary><b>3. 大批量微博怎么办？</b></summary>

如账号有 10 万条微博（约需 180 小时），可参考高级功能分批导入，或多机分片备份后合并。
</details>

<details>
<summary><b>4. 非 Win10 64 位系统支持吗？</b></summary>

目前在 Win10 64 位、Win11 64 位系统测试通过。其他系统可提交 Issue 反馈。
</details>

---

## 八、免责声明

- ⚠️ **仅限个人备份**：本工具仅用于备份**你自己有权访问的**微博内容。
- ❌ **不得商用**，不得用于批量采集他人数据、不得用于任何违反微博用户协议的用途。
- 请遵守微博用户协议与 robots 约定，**控制抓取频率**（务必保留限速）。
- 🔒 **Cookie 属敏感信息**（等同账号凭证）：仅保存在本机内存用于本次抓取，**不上传任何服务器**，
  请勿泄露给他人。所有数据仅存本机 `weibo.db`，导出文件存于本机导出目录。
- **应用图标说明**：图标中的新浪微博 logo 版权归**新浪公司**所有，本工具作为「微博备份」用途的标识使用，
  开源时请在 README 保留本声明；logo 不得用于暗示官方出品或任何商业用途。
- **第三方组件**：本项目使用的开源库许可清单见「六、开源许可与第三方组件」，均在对应许可允许范围内使用。

---

## 九、技术栈

- 后端引擎：**Python**，`requests` 抓取，`SQLite`(weibo.db) 存储，
  `Jinja2` 生成 HTML，Markdown 纯文本拼接，PDF 用 `reportlab`（注册 Windows 中文字体）。
- 桌面版：**Tkinter 原生 GUI**（Python 标准库，无 WebView2/浏览器依赖，Win10/11 开箱即用）。
- 网页版（规划中）：复用同一套后端引擎 + FastAPI 本地服务 + 单文件 HTML 前端，未来部署到 `jikegang.com`。

---

<div align="center">
  <sub>极客港 jikegang.com · jikegang-weibo-export · 仅供个人备份，不得商用</sub>
</div>
