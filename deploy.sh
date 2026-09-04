#!/bin/bash
# 续火花 一键部署（交互式）
#
# 使用：
#   curl -fsSL https://raw.githubusercontent.com/mrsxs/douyin-spark/main/deploy.sh -o deploy.sh
#   chmod +x deploy.sh && ./deploy.sh
#
# 向导两步：
#   1) 验证码 Key（DDDocr 滑块服务，可跳过）
#   2) 管理员密码（可跳过，容器首启自动生成强随机密码）
# 然后自动拉镜像、写 .env、启动容器。
#
# 重复执行 = 升级（检测到已有 .env 时直接 pull + up -d）。

set -e

BOLD='\033[1m'; GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; NC='\033[0m'
REPO_RAW="https://raw.githubusercontent.com/mrsxs/douyin-spark/main"
COMPOSE_URL="$REPO_RAW/docker-compose.yml"
IMAGE="mrsxs/douyin-spark:latest"
INSTALL_DIR="${INSTALL_DIR:-/opt/douyin-spark}"

hr() { printf "${BOLD}━%.0s${NC}" $(seq 1 60); echo; }
info() { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
die()  { echo -e "${RED}❌${NC} $*" >&2; exit 1; }

hr
echo -e "${BOLD}  续火花 一键部署${NC}"
hr

# ─── 1. 前置检查 ─────────────────────────────────
command -v docker >/dev/null || die "未安装 Docker。Ubuntu: apt install docker.io"
docker compose version >/dev/null 2>&1 || die "未安装 Docker Compose v2。apt install docker-compose-plugin"
command -v curl >/dev/null || die "未安装 curl"

info "Docker 就绪"

# ─── 2. 安装目录 ─────────────────────────────────
if [ "$(id -u)" -ne 0 ] && [[ "$INSTALL_DIR" == /opt/* ]]; then
    warn "非 root 用户，切换到 ~/douyin-spark"
    INSTALL_DIR="$HOME/douyin-spark"
fi
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
info "安装目录：$INSTALL_DIR"

# ─── 3. 下载 compose 配置 ────────────────────────
if [ ! -f docker-compose.yml ]; then
    echo
    echo "下载 docker-compose.yml..."
    curl -fsSL -o docker-compose.yml "$COMPOSE_URL" || die "compose 下载失败"
    info "docker-compose.yml 已下载"
else
    info "docker-compose.yml 已存在（跳过下载）"
fi

# ─── 4. 已有部署 → 直接升级 ──────────────────────
# 判据用「.env 存在且非空」而不是某个具体变量：去掉授权码后
# .env 里已经没有任何一个必填项了，盯着单个 key 会误判成全新安装、
# 把用户已经配好的管理员密码覆盖掉。
if [ -s .env ]; then
    echo
    warn "检测到已有 .env 配置"
    read -r -p "重新配置？[y/N]: " RECONFIG
    if [[ ! "$RECONFIG" =~ ^[Yy]$ ]]; then
        echo
        info "跳过配置，直接拉最新镜像..."
        docker compose pull
        docker compose up -d
        echo
        hr
        info "已更新到最新版本"
        hr
        exit 0
    fi
fi

# ─── 5. 交互式收集配置 ───────────────────────────
echo
hr
echo -e "${BOLD}  配置向导（2 步，都可跳过）${NC}"
hr

# 5.1 验证码 Key
echo
echo -e "${BOLD}[1/2] 验证码 Key${NC}（自建 ddddocr 滑块识别服务的 API Key，直接回车跳过）"
echo "      只有「短信登录」用得上；用扫码登录可以不填。"
read -r -p "DOUYIN_CAPTCHA_KEY: " CAPTCHA_KEY
CAPTCHA_KEY="$(echo "$CAPTCHA_KEY" | xargs)"
if [ -n "$CAPTCHA_KEY" ]; then
    read -r -p "DOUYIN_CAPTCHA_URL（服务地址，如 https://ocr.example.com）: " CAPTCHA_URL
    CAPTCHA_URL="$(echo "$CAPTCHA_URL" | xargs)"
    info "验证码服务已配置"
else
    warn "跳过（短信登录的滑块验证不可用，扫码登录不受影响）"
fi

# 5.2 管理员密码
echo
echo -e "${BOLD}[2/2] 管理员密码${NC}（自定义，或直接回车由容器随机生成）"
read -r -s -p "ADMIN_PASSWORD（输入隐藏）: " ADMIN_PASS
echo
if [ -n "$ADMIN_PASS" ]; then
    read -r -s -p "再次确认密码: " ADMIN_PASS2
    echo
    [ "$ADMIN_PASS" = "$ADMIN_PASS2" ] || die "两次输入不一致"
    if [ ${#ADMIN_PASS} -lt 8 ]; then
        warn "密码少于 8 位，建议更强"
        read -r -p "继续使用此密码？[y/N]: " OK
        [[ "$OK" =~ ^[Yy]$ ]] || die "已取消"
    fi
    info "密码已接收（${#ADMIN_PASS} 位）"
    USE_CUSTOM_PASS=1
else
    info "跳过，将由容器首次启动自动生成强随机密码"
    USE_CUSTOM_PASS=0
fi

# ─── 6. 拉镜像 ───────────────────────────────────
echo
hr
echo -e "${BOLD}  拉取镜像 ${IMAGE}${NC}"
hr
docker pull "$IMAGE"

# ─── 7. 自定义密码 → 用镜像里的 bcrypt 算哈希 ────
ADMIN_HASH=""
if [ "$USE_CUSTOM_PASS" = "1" ]; then
    echo
    echo "生成 bcrypt 哈希..."
    ADMIN_HASH="$(
        ADMIN_PASS="$ADMIN_PASS" docker run --rm -e ADMIN_PASS "$IMAGE" \
            python -c "import os,bcrypt; print(bcrypt.hashpw(os.environ['ADMIN_PASS'].encode()[:72], bcrypt.gensalt(12)).decode())"
    )"
    [ -z "$ADMIN_HASH" ] && die "哈希生成失败"
    info "哈希已生成"
fi

# ─── 8. 写 .env ──────────────────────────────────
echo
echo "写入 .env..."
{
    echo "# 由 deploy.sh 自动生成 - $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "ADMIN_USERNAME=admin"
    if [ -n "$ADMIN_HASH" ]; then
        # compose 会把 .env 里的 $ 当变量插值，bcrypt 哈希里全是 $ ——
        # 不转义成 $$ 的话管理员密码直接对不上（踩过）。
        echo "ADMIN_PASSWORD_HASH=$(printf '%s' "$ADMIN_HASH" | sed 's/\$/$$/g')"
    fi
    [ -n "$CAPTCHA_KEY" ] && echo "DOUYIN_CAPTCHA_KEY=$CAPTCHA_KEY"
    [ -n "$CAPTCHA_URL" ] && echo "DOUYIN_CAPTCHA_URL=$CAPTCHA_URL"
} > .env
chmod 600 .env
info ".env 已生成（600 权限）"

# ─── 9. 启动容器 ─────────────────────────────────
echo
hr
echo -e "${BOLD}  启动容器${NC}"
hr
docker compose up -d

# ─── 10. 等待就绪 + 输出结果 ─────────────────────
echo
echo "等容器就绪（最多 60s）..."
READY=0
for i in $(seq 1 60); do
    if curl -sf http://127.0.0.1:8765/healthz >/dev/null 2>&1; then
        READY=1; break
    fi
    sleep 1
done

echo
hr
if [ "$READY" = "1" ]; then
    echo -e "${GREEN}${BOLD}  🎉 部署完成${NC}"
else
    echo -e "${YELLOW}${BOLD}  ⚠️  容器已启动但健康检查未通过，请查日志${NC}"
fi
hr

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$IP" ] && IP="服务器IP"
echo
echo -e "  🌐 Web 地址: ${BOLD}http://${IP}:8765${NC}"
echo -e "  👤 用户名  : ${BOLD}admin${NC}"

if [ "$USE_CUSTOM_PASS" = "1" ]; then
    echo -e "  🔐 密码    : ${BOLD}（你刚输入的）${NC}"
else
    echo
    echo "  📋 查看系统自动生成的 admin 密码（只显示一次，请立刻保存）："
    echo "     docker compose logs | grep -A 8 '管理员账户'"
fi

echo
echo "  📌 日常命令："
echo "     docker compose logs -f         # 实时日志"
echo "     docker compose ps              # 状态"
echo "     docker compose pull && docker compose up -d   # 升级"
echo "     ./deploy.sh                    # 重新配置 / 升级"
hr

if [ "$USE_CUSTOM_PASS" = "0" ]; then
    echo
    echo "以下是容器启动日志中的管理员密码信息（若看不到请稍后再用 docker compose logs 查）："
    hr
    sleep 2
    docker compose logs 2>&1 | grep -A 8 "管理员账户" || echo "（未找到，容器可能还在启动中）"
    hr
fi
