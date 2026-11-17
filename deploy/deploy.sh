#!/usr/bin/env bash
# 极客港 · 微博导出 —— 云服务器 ECS 一键部署脚本
# 用法：
#   chmod +x deploy.sh
#   sudo ./deploy.sh
#
# 前置条件：
#   - Ubuntu 20.04/22.04/24.04
#   - 项目已上传到 /opt/weibo-export
#   - 已放行安全组 80/443（EO 回源）
set -euo pipefail

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
info() { echo -e "${BLUE}[i]${NC} $*"; }

# =============================================================
# 配置
# =============================================================
PROJECT_DIR="/opt/weibo-export"
SERVICE_USER="www-data"
SERVICE_NAME="weibo-export"
NGINX_CONF="your-subdomain.example.com"
PYTHON_VERSION="3.10"

info "=========================================="
info " 极客港 · 微博导出 ECS 部署"
info "=========================================="

# =============================================================
# 1. 检查权限
# =============================================================
if [[ $EUID -ne 0 ]]; then
    err "必须以 root 运行（sudo ./deploy.sh）"
    exit 1
fi

if [[ ! -d "$PROJECT_DIR" ]]; then
    err "项目目录不存在：$PROJECT_DIR"
    err "请先把 weibo-export 项目上传到 /opt/weibo-export"
    exit 1
fi

info "项目目录：$PROJECT_DIR"

# =============================================================
# 2. 更新系统 + 安装基础依赖
# =============================================================
log "更新 apt 索引..."
apt-get update -qq

log "安装系统依赖（python3、nginx、certbot 等）..."
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev \
    build-essential \
    nginx \
    curl wget \
    ca-certificates \
    > /dev/null

log "系统依赖安装完成"

# =============================================================
# 3. 创建服务用户
# =============================================================
if ! id "$SERVICE_USER" &>/dev/null; then
    log "创建服务用户：$SERVICE_USER"
    useradd --system --shell /usr/sbin/nologin --home /home/$SERVICE_USER --create-home $SERVICE_USER
else
    log "服务用户已存在：$SERVICE_USER"
fi

# =============================================================
# 4. 创建 Python 虚拟环境
# =============================================================
log "创建 Python venv..."
sudo -u $SERVICE_USER python3 -m venv "$PROJECT_DIR/.venv"

log "升级 pip/setuptools..."
sudo -u $SERVICE_USER "$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip setuptools wheel

log "安装 Python 依赖（fastapi/uvicorn/requests/reportlab 等）..."
sudo -u $SERVICE_USER "$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

log "Python 依赖安装完成"

# =============================================================
# 5. 设置目录权限
# =============================================================
log "设置项目目录权限..."
chown -R $SERVICE_USER:$SERVICE_USER "$PROJECT_DIR"

# 数据目录（paths.py 会自动创建，这里预创建并赋权）
DATA_DIR="/home/$SERVICE_USER/极客港微博导出"
EXPORT_DIR="/home/$SERVICE_USER/微博导出"
mkdir -p "$DATA_DIR" "$EXPORT_DIR"
chown -R $SERVICE_USER:$SERVICE_USER "$DATA_DIR" "$EXPORT_DIR"

log "数据目录：$DATA_DIR"
log "导出目录：$EXPORT_DIR"

# =============================================================
# 6. 安装 systemd 服务
# =============================================================
log "安装 systemd 服务..."
cp "$PROJECT_DIR/deploy/weibo-export.service" "/etc/systemd/system/${SERVICE_NAME}.service"
chmod 644 "/etc/systemd/system/${SERVICE_NAME}.service"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service" > /dev/null 2>&1

log "启动服务..."
systemctl start "${SERVICE_NAME}.service"

sleep 2

if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    log "服务已启动"
else
    err "服务启动失败，查看日志："
    journalctl -u "${SERVICE_NAME}.service" -n 50 --no-pager
    exit 1
fi

# =============================================================
# 7. 配置 Nginx
# =============================================================
log "配置 Nginx..."
cp "$PROJECT_DIR/deploy/nginx-wb.conf" "/etc/nginx/sites-available/${NGINX_CONF}"

# 移除默认站点（避免冲突）
rm -f /etc/nginx/sites-enabled/default

# 启用站点
ln -sf "/etc/nginx/sites-available/${NGINX_CONF}" "/etc/nginx/sites-enabled/${NGINX_CONF}"

# 测试 Nginx 配置
if nginx -t > /dev/null 2>&1; then
    log "Nginx 配置测试通过"
    systemctl reload nginx
else
    err "Nginx 配置有误："
    nginx -t
    exit 1
fi

# =============================================================
# 8. 防火墙（ufw）
# =============================================================
if command -v ufw &>/dev/null; then
    log "配置防火墙..."
    ufw allow 22/tcp  > /dev/null 2>&1  # SSH
    ufw allow 80/tcp  > /dev/null 2>&1  # HTTP（EO 回源）
    ufw allow 443/tcp > /dev/null 2>&1  # HTTPS（可选）
    # 如需限制只允许 EO 回源 IP，取消下面注释并填 EO IP 段
    # ufw delete allow 80/tcp
    # ufw insert 1 allow from <EO_IP_1> to any port 80 proto tcp
    # ufw insert 2 allow from <EO_IP_2> to any port 80 proto tcp
    ufw --force enable > /dev/null 2>&1
    log "防火墙已配置（22/80/443）"
else
    warn "未检测到 ufw，请手动配置安全组放行 80/443"
fi

# =============================================================
# 9. 本地验证
# =============================================================
log "本地健康检查..."
sleep 2

if curl -sf http://127.0.0.1:8765/api/status > /dev/null; then
    log "FastAPI 服务响应正常（127.0.0.1:8765）"
else
    err "FastAPI 服务无响应"
    journalctl -u "${SERVICE_NAME}.service" -n 20 --no-pager
    exit 1
fi

if curl -sf http://127.0.0.1/healthz > /dev/null; then
    log "Nginx 反向代理正常（127.0.0.1/healthz）"
else
    warn "Nginx 反向代理异常，检查配置"
fi

# =============================================================
# 10. 输出部署信息
# =============================================================
SERVER_IP=$(curl -s your-ip-lookup-service || echo "未知")
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} 部署完成！${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${BLUE}本地访问：${NC}http://127.0.0.1:8765"
echo -e "${BLUE}公网访问：${NC}http://${SERVER_IP}（需配 EO CNAME 后用 your-subdomain.example.com）"
echo ""
echo -e "${BLUE}服务管理：${NC}"
echo "  sudo systemctl status  ${SERVICE_NAME}"
echo "  sudo systemctl restart ${SERVICE_NAME}"
echo "  sudo systemctl stop    ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f   # 查看实时日志"
echo ""
echo -e "${BLUE}数据位置：${NC}"
echo "  数据库：${DATA_DIR}/weibo.db"
echo "  导出：  ${EXPORT_DIR}/"
echo ""
echo -e "${YELLOW}下一步：${NC}"
echo "  1. 在腾讯云 EO 添加站点 your-subdomain.example.com"
echo "     回源 IP：${SERVER_IP}"
echo "     回源端口：80"
echo "  2. 在域名 DNS 添加 CNAME：your-subdomain.example.com → <EO分配的CNAME>"
echo "  3. 在 EO 配置缓存规则："
echo "     /api/* → 不缓存（关键！）"
echo "     /files/* → 可缓存 1 小时"
echo "     /static/* → 可缓存 1 小时"
echo "  4. 访问 https://your-subdomain.example.com 测试"
echo ""
