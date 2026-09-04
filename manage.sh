#!/bin/bash
# 续火花 服务器端运维主菜单
#
# 一键启动：
#   curl -fsSL https://raw.githubusercontent.com/mrsxs/douyin-spark/main/manage.sh -o manage.sh
#   chmod +x manage.sh
#   ./manage.sh
#
# 功能：
#   [1] 安装/启动服务（首次部署 + 后续升级）
#   [2] 重置用户密码
#   [3] 查看服务状态 & 日志
#   [0] 退出

set -e

BOLD='\033[1m'; GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; CYAN='\033[36m'; NC='\033[0m'
COMPOSE_URL="https://raw.githubusercontent.com/mrsxs/douyin-spark/main/docker-compose.yml"
IMAGE="mrsxs/douyin-spark:latest"
INSTALL_DIR="${INSTALL_DIR:-/opt/douyin-spark}"
CONTAINER="douyin-spark"

if [ "$(id -u)" -ne 0 ] && [[ "$INSTALL_DIR" == /opt/* ]]; then
    INSTALL_DIR="$HOME/douyin-spark"
fi
mkdir -p "$INSTALL_DIR"

hr()    { printf "${BOLD}━%.0s${NC}" $(seq 1 60); echo; }
title() { hr; echo -e "${BOLD}  $*${NC}"; hr; }
info()  { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC}  $*"; }
err()   { echo -e "${RED}❌${NC} $*" >&2; }
die()   { err "$*"; exit 1; }

check_deps() {
    command -v docker >/dev/null || die "未安装 Docker。Ubuntu: apt install docker.io"
    docker compose version >/dev/null 2>&1 || die "未安装 Docker Compose v2。apt install docker-compose-plugin"
    command -v curl >/dev/null || die "未安装 curl"
}

container_running() {
    docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"
}

# ────────────────────────────────────────────
# [1] 安装/启动服务
# ────────────────────────────────────────────
do_install() {
    title "[1] 安装/启动服务"
    cd "$INSTALL_DIR"

    # 下载 compose 文件（如果没）
    if [ ! -f docker-compose.yml ]; then
        echo "下载 docker-compose.yml..."
        curl -fsSL -o docker-compose.yml "$COMPOSE_URL" || die "compose 下载失败"
        info "docker-compose.yml 已下载"
    else
        info "docker-compose.yml 已存在"
    fi

    # .env 检查
    if [ -s .env ]; then
        warn "已有 .env 配置"
        read -r -p "重新配置？[y/N]: " RECONFIG
        if [[ ! "$RECONFIG" =~ ^[Yy]$ ]]; then
            echo
            echo "拉取最新镜像并重启..."
            docker compose pull
            docker compose up -d
            wait_ready
            return
        fi
    fi

    # 向导：4 步
    echo
    echo -e "${CYAN}━━━ 配置向导（3 步）━━━${NC}"

    # [1/3] 数据存储路径
    echo
    echo -e "${BOLD}[1/3] 数据存储路径${NC}（cookies / DB / 日志 / 加密密钥全存这里）"
    echo "  默认: $INSTALL_DIR/data（相对当前目录）"
    echo "  也可填绝对路径如 /mnt/data/spark 或 /external/backup/spark"
    read -r -p "DATA_PATH (回车用默认): " DATA_PATH
    DATA_PATH="$(echo "$DATA_PATH" | xargs)"
    [ -z "$DATA_PATH" ] && DATA_PATH="./data"
    # 算真实路径
    if [[ "$DATA_PATH" = /* ]]; then
        REAL_DATA_PATH="$DATA_PATH"
    else
        REAL_DATA_PATH="$INSTALL_DIR/${DATA_PATH#./}"
    fi
    mkdir -p "$REAL_DATA_PATH" || die "无法创建 $REAL_DATA_PATH（权限？）"
    chmod 700 "$REAL_DATA_PATH" 2>/dev/null || true
    info "数据目录：$REAL_DATA_PATH（删除容器不丢）"

    echo
    echo -e "${BOLD}[2/3] 验证码服务${NC}（自建 ddddocr 滑块识别，可回车跳过）"
    echo "      只有「短信登录」用得上；扫码登录不需要。"
    read -r -p "DOUYIN_CAPTCHA_KEY: " CAPTCHA_KEY
    CAPTCHA_KEY="$(echo "$CAPTCHA_KEY" | xargs)"
    CAPTCHA_URL=""
    if [ -n "$CAPTCHA_KEY" ]; then
        # 光有 key 没有地址是配了个寂寞：compose 里 DOUYIN_CAPTCHA_URL 默认空，
        # 运行时滑块验证会直接失败，而用户以为自己配好了。
        read -r -p "DOUYIN_CAPTCHA_URL（服务地址，如 https://ocr.example.com）: " CAPTCHA_URL
        CAPTCHA_URL="$(echo "$CAPTCHA_URL" | xargs)"
        if [ -n "$CAPTCHA_URL" ]; then
            info "验证码服务已配置"
        else
            warn "只填了 Key 没填地址 —— 短信登录的滑块验证仍然不可用"
        fi
    else
        warn "跳过（短信登录滑块无法自动识别，扫码登录不受影响）"
    fi

    echo
    echo -e "${BOLD}[3/3] 管理员密码${NC}（自定义或回车让系统随机生成）"
    read -r -s -p "ADMIN_PASSWORD（输入隐藏）: " ADMIN_PASS
    echo
    USE_CUSTOM_PASS=0
    if [ -n "$ADMIN_PASS" ]; then
        read -r -s -p "再次确认: " ADMIN_PASS2
        echo
        [ "$ADMIN_PASS" = "$ADMIN_PASS2" ] || die "两次输入不一致"
        [ ${#ADMIN_PASS} -lt 6 ] && die "密码至少 6 位"
        USE_CUSTOM_PASS=1
        info "密码已接收（${#ADMIN_PASS} 位）"
    else
        info "跳过，将由容器自动生成强随机密码"
    fi

    # 拉镜像
    echo
    echo "拉取镜像 ${IMAGE}..."
    docker pull "$IMAGE"

    # 自定义密码 → bcrypt 哈希
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

    # 写 .env
    {
        echo "# 由 manage.sh 生成 - $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "DATA_VOLUME_PATH=$DATA_PATH"
        [ -n "$CAPTCHA_KEY" ] && echo "DOUYIN_CAPTCHA_KEY=$CAPTCHA_KEY"
        [ -n "$CAPTCHA_URL" ] && echo "DOUYIN_CAPTCHA_URL=$CAPTCHA_URL"
        echo "ADMIN_USERNAME=admin"
        # ⚠️ docker compose 把 $2b$12$xxx 当变量引用 → 必须 $ → $$ 转义
        if [ -n "$ADMIN_HASH" ]; then
            ESCAPED_HASH="$(printf '%s' "$ADMIN_HASH" | sed 's/\$/$$/g')"
            echo "ADMIN_PASSWORD_HASH=$ESCAPED_HASH"
        fi
    } > .env
    chmod 600 .env
    info ".env 已生成"

    # 启动
    docker compose up -d
    wait_ready

    echo
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo -e "  🌐 Web 地址: ${BOLD}http://${IP:-服务器IP}:8765${NC}"
    echo -e "  👤 用户名  : ${BOLD}admin${NC}"
    if [ "$USE_CUSTOM_PASS" = "1" ]; then
        echo -e "  🔐 密码    : ${BOLD}（你刚输入的）${NC}"
    else
        echo
        echo "  📋 系统自动生成的 admin 密码："
        sleep 2
        docker compose logs 2>&1 | grep -A 8 "管理员账户" | tail -10 || echo "  （首次启动还没打印，稍等再 docker compose logs 查）"
    fi
}

# ────────────────────────────────────────────
# [2] 重置用户密码
# ────────────────────────────────────────────
do_reset_password() {
    title "[2] 重置用户密码"
    cd "$INSTALL_DIR"
    container_running || die "容器 ${CONTAINER} 未运行，请先选 [1] 安装/启动"

    echo "  当前用户一览："
    docker exec "$CONTAINER" python -m app.cli list-users 2>/dev/null \
        | sed 's/^/    /' || warn "（老镜像不支持 list-users，跳过）"
    echo

    read -r -p "用户名（默认 admin）: " USERNAME
    USERNAME="${USERNAME:-admin}"

    # 旧版本在这里无条件 is_admin=True —— 给普通用户改个密码就把他变成了管理员，
    # 提示语还只说「已重置密码」。默认必须保持原权限不变，提权要显式确认。
    MAKE_ADMIN=0
    read -r -p "同时赋予管理员权限？（能看全部用户数据）[y/N]: " PROMOTE
    [[ "$PROMOTE" =~ ^[Yy]$ ]] && MAKE_ADMIN=1

    while true; do
        read -r -s -p "新密码（隐藏）: " NEW
        echo
        [ -z "$NEW" ] && { warn "不能为空"; continue; }
        [ ${#NEW} -lt 6 ] && { warn "至少 6 位"; continue; }
        read -r -s -p "再次确认: " NEW2
        echo
        [ "$NEW" = "$NEW2" ] && break
        warn "两次不一致，重新输入"
    done

    # 生成内嵌 python 脚本，cp 进容器执行（兼容老镜像，不依赖 cli）
    cat > /tmp/_reset_admin.py <<'PYEOF'
import os, bcrypt
from app.db import SessionLocal, init_db
from app.models import DEFAULT_MAX_ACCOUNTS, User
init_db()
username = os.environ.get('UNAME', 'admin')
pwd = os.environ['NEW']
make_admin = os.environ.get('MAKE_ADMIN') == '1'
h = bcrypt.hashpw(pwd.encode()[:72], bcrypt.gensalt(12)).decode()
with SessionLocal() as db:
    u = db.query(User).filter(User.username == username).first()
    if not u:
        u = User(username=username, password_hash=h,
                 is_admin=make_admin, is_active=True,
                 max_accounts=100 if make_admin else DEFAULT_MAX_ACCOUNTS)
        db.add(u)
        msg = f'✓ 已创建{"管理员" if make_admin else "普通用户"} {username}'
    else:
        u.password_hash = h
        u.is_active = True
        if make_admin and not u.is_admin:
            u.is_admin = True
            print(f'⚠️ 已把 {username} 提升为管理员')
        # 旧 cookie 立即失效，否则改了密码也踢不掉已登录的会话
        u.session_version = (u.session_version or 0) + 1
        msg = f'✓ 已重置 {username} 密码（权限：{"管理员" if u.is_admin else "普通用户"}）'
    db.commit()
    print(msg)
PYEOF
    # 关键：cp 到 /app（容器 WORKDIR）让 python 能 import app 包
    docker cp /tmp/_reset_admin.py "${CONTAINER}:/app/_reset_admin.py" >/dev/null
    docker exec -e NEW="$NEW" -e UNAME="$USERNAME" -e MAKE_ADMIN="$MAKE_ADMIN" \
        --workdir /app "$CONTAINER" python /app/_reset_admin.py
    docker exec "$CONTAINER" rm -f /app/_reset_admin.py >/dev/null
    rm -f /tmp/_reset_admin.py

    echo
    info "密码已生效，立即可登录："
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "  🌐 http://${IP:-服务器IP}:8765/login"
    echo "  👤 $USERNAME"
}

# ────────────────────────────────────────────
# [4] 状态 & 日志
# ────────────────────────────────────────────
do_status() {
    title "[4] 状态 & 日志"
    cd "$INSTALL_DIR"
    echo
    echo "── 容器状态 ──────────"
    docker compose ps 2>&1 || echo "未启动"
    echo
    echo "── 最近 30 行日志 ────"
    docker compose logs --tail 30 2>&1 || echo "无日志"
}

# ────────────────────────────────────────────
wait_ready() {
    echo "等待容器就绪..."
    for i in $(seq 1 60); do
        if curl -sf http://127.0.0.1:8765/healthz >/dev/null 2>&1; then
            info "服务已就绪"
            return 0
        fi
        sleep 1
    done
    warn "60s 内未就绪，请用 [4] 查日志"
    return 1
}

# ────────────────────────────────────────────
# 主菜单
# ────────────────────────────────────────────
check_deps

while true; do
    clear
    title "续火花 服务器运维 ($(hostname))"
    echo
    if container_running; then
        echo -e "  状态: ${GREEN}● 运行中${NC}    数据目录: $INSTALL_DIR"
    else
        echo -e "  状态: ${RED}● 未运行${NC}    数据目录: $INSTALL_DIR"
    fi
    echo
    echo "  [1] 安装 / 启动 / 升级服务"
    echo "  [2] 重置用户密码"
    echo "  [3] 查看状态 & 日志"
    echo
    echo "  [0] 退出"
    echo
    read -r -p "选择 [0-3]: " CHOICE

    case "$CHOICE" in
        1) do_install ;;
        2) do_reset_password ;;
        3) do_status ;;
        0) echo "再见 👋"; exit 0 ;;
        *) warn "无效选择" ;;
    esac

    echo
    read -r -p "按回车返回主菜单..." _
done
