# 极客港 · 微博导出 —— 阿里云 ECS 部署包

把 **wb.jikegang.com** 跑在阿里云 ECS 上，前面用 **腾讯云 EdgeOne（EO）** 接入并隐藏源站 IP。

---

## 一、这个包做什么

| 文件 | 作用 |
|---|---|
| `deploy.sh` | 一键部署脚本：装依赖、建 systemd 服务、装/配 Nginx、开放防火墙 |
| `weibo-export.service` | systemd 服务单元（开机自启 + 崩溃自动重启） |
| `nginx-wb.conf` | Nginx 反向代理配置（**专门针对 EO/CDN 动态接口做了不缓冲处理**） |
| `.env.production` | 生产环境参数（端口、导出目录、限速等） |

---

## 二、部署步骤（在阿里云 ECS 上执行）

> 假设 ECS 是 **Ubuntu 20.04/22.04/24.04**，已放行安全组 `22`(SSH)、`80`(HTTP)、`443`(HTTPS)。

### 1. 上传项目
```bash
# 在本地（你的 Windows 开发机）打包并上传
scp -r C:/Users/Admin/RaccoonWork/weibo-export root@<ECS_IP>:/opt/weibo-export
scp -r C:/Users/Admin/RaccoonWork/weibo-export/deploy root@<ECS_IP>:/opt/weibo-export/deploy

# 或者只传关键文件（跳过 .git / *.zip / build 日志）
```

### 2. SSH 登录 ECS 并执行部署
```bash
ssh root@<ECS_IP>
cd /opt/weibo-export/deploy
chmod +x deploy.sh
sudo ./deploy.sh
```

### 3. 验证
```bash
sudo systemctl status weibo-export   # 服务状态应为 active (running)
curl http://127.0.0.1:8765/api/status # 应返回 JSON
```

---

## 三、腾讯云 EdgeOne 接入要点

EO 不只是缓存，它是 **CDN + WAF + DDoS 防护 + 隐藏源站 IP** 的组合。**接入时必须区分动态接口**，否则进度轮询会卡死。在 EO 控制台配置：

### 缓存规则（关键）
| 路径 | 缓存策略 | 原因 |
|---|---|---|
| `/api/*` | **不缓存 / 遵循源站** | 抓取进度轮询是实时的，缓存会拿到旧数据 |
| `/files/*` | **不缓存** | 导出文件按用户请求动态下载 |
| `/`、`/static/*` | 缓存 1 小时 | 静态 HTML/CSS/图标 |

### 回源配置
- 回源协议：HTTP（Nginx 监听 80）
- 回源端口：`80`
- 回源 Host：`wb.jikegang.com`
- **回源 IP**：填你的 ECS 公网 IP

### 安全策略（可选但推荐）
- 开启 **WAF 基础规则**
- 开启 **Bot 管理**（拦截恶意爬虫）
- 开启 **DDoS 防护**
- 可加 **IP 白名单/黑名单**

### DNS
在域名服务商处加 CNAME 记录：
```
wb.jikegang.com  CNAME  <EO分配的CNAME>.eo.dnse2.com
```

---

## 四、数据隐私边界

**部署到 ECS 后的运行链路**：
```
用户浏览器 → EO（保护源站 IP）→ Nginx(80) → server.py(8765) → ECS 本机磁盘
```

| 数据 | 是否经过 ECS | 存储位置 | 说明 |
|---|---|---|---|
| **Cookie** | ✅ 经过 | **仅内存**，不落盘 | 任务结束/重启即消失 |
| **博文数据** | ✅ 经过 | ECS 磁盘 `~/极客港微博导出/weibo.db` | 持久化存储 |
| **导出文件** | ✅ 经过 | ECS 磁盘 `~/微博导出/` | 可定期清理 |
| **日志** | ✅ 经过 | systemd journal | 不含 Cookie 明文 |

**注意**：这是**服务端运行**模式，不是"网页版样式的桌面端"。
- ❌ 不是：把桌面端搬到云端
- ✅ 而是：`server.py` 跑在 ECS 上，用户远程访问

如果你希望"数据完全不进 ECS"，应改用 **方案 A（内网穿透）**，本次你选的是方案 B。

---

## 五、导出目录

`paths.py` 会自动适配：
- **Linux ECS**：`~/微博导出/`（无 D 盘概念）
- **Windows 本机**：`D:\微博导出\`（有 D 盘时）/ `C:\Users\<你>\Documents\微博导出\`（无 D 盘回退）

如需**固定到特定 Linux 路径**（如 `/data/exports/`），编辑 `.env.production` 的 `EXPORT_DIR` 并重载服务。

---

## 六、常见问题

### Q: Cookie 会被 EO 缓存吗？
不会。Cookie 是浏览器到 EO 的请求头，EO **不缓存请求头**，只透传给源站。

### Q: 多人同时抓会打架吗？
`server.py` 用 `_STATE["job_lock"]` 保证同一时刻只有一个抓取任务。第二个请求会返回 `409 已有抓取任务在进行中`。
**如果要多用户并发**，需改造成"每用户独立 job"，这是后续迭代。

### Q: EO 能加速抓取吗？
不能。EO 只加速"EO → 用户"这一段，**抓取是 server.py 直接请求 m.weibo.cn**，不经过 EO。

### Q: 源站 IP 会泄露吗？
只要：
1. ECS 安全组**只放行 EO 回源 IP 段**（腾讯云文档有列表），不放行 `0.0.0.0/0` 的 80/443
2. 不在其他子域名/历史 DNS 记录里暴露 ECS 真实 IP
3. ECS 不主动外发请求到被记录的第三方

就基本不会泄露。

---

## 七、下一步（部署后）

1. 访问 `https://wb.jikegang.com`，填 Cookie 抓自己的微博
2. 测试导出 HTML/MD/PDF/JSON/TXT
3. 确认数据落盘位置 `~/微博导出/`
4. 如需**公网多用户**，考虑加：
   - 简单访问鉴权（Basic Auth / 邀请码）
   - 每用户并发隔离
   - 任务队列 + 进度 WS 推送

有问题随时改 `deploy.sh` 或 `.env.production`。
