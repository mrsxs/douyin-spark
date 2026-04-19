#!/bin/bash
# 一键部署脚本 - 服务器上跑：
#   wget -qO- https://mrsxs.github.io/douyin-spark-deploy/deploy.sh | bash
# 或直接：curl -O 本文件 + bash deploy.sh
#
# 注意：因为 GitHub repo 私有，本脚本需要你手动把 docker-compose.yml 贴过去，
# 或把这个脚本放到你的公开 gist / 对象存储。

set -e

INSTALL_DIR="${INSTALL_DIR:-/opt/douyin-spark}"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  续火花 SaaS 一键部署"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 检查 docker
command -v docker >/dev/null 2>&1 || { echo "❌ 未装 Docker。请先 apt install docker.io 或访问 docker.com"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "❌ Docker Compose v2 未装。请 apt install docker-compose-plugin"; exit 1; }

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# 写 docker-compose.yml（如果不存在）
if [ ! -f docker-compose.yml ]; then
    cat > docker-compose.yml <<'YMLEOF'
services:
  app:
    image: mrsxs/douyin-spark:latest
    container_name: douyin-spark
    restart: unless-stopped
    ports:
      - "8765:8765"
    environment:
      LICENSE_KEY: "${LICENSE_KEY:?LICENSE_KEY is required}"
      ADMIN_USERNAME: "${ADMIN_USERNAME:-admin}"
      SECRET_KEY: "${SECRET_KEY:-}"
      COOKIE_SECURE: "${COOKIE_SECURE:-false}"
      SESSION_MAX_AGE: "${SESSION_MAX_AGE:-2592000}"
      DATA_DIR: /data
      DB_URL: "${DB_URL:-}"
      SITE_NAME: "${SITE_NAME:-续火花}"
      DOUYIN_CAPTCHA_URL: "${DOUYIN_CAPTCHA_URL:-http://ocr.example.com}"
      DOUYIN_CAPTCHA_ENDPOINT: "${DOUYIN_CAPTCHA_ENDPOINT:-/slide_match}"
      DOUYIN_CAPTCHA_KEY: "${DOUYIN_CAPTCHA_KEY:-}"
    volumes:
      - spark-data:/data
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).read()"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s

volumes:
  spark-data:
    driver: local
YMLEOF
    echo "✓ docker-compose.yml 已创建"
fi

# 检查 .env
if [ ! -f .env ]; then
    echo ""
    echo "⚠️ 未发现 .env 文件，创建一个模板："
    cat > .env <<'ENVEOF'
# 必填（从卖家拿）
LICENSE_KEY=

# 可选：抖音滑块服务 key
DOUYIN_CAPTCHA_KEY=
ENVEOF
    echo "✓ .env 模板已创建，请编辑 $INSTALL_DIR/.env 填入 LICENSE_KEY 后重跑 deploy.sh"
    exit 0
fi

# 拉镜像
echo ""
echo "拉取最新镜像..."
docker compose pull

# 启动
echo ""
echo "启动容器..."
docker compose up -d

# 等启动
echo ""
echo "等容器就绪（最多 60s）..."
for i in $(seq 1 60); do
    if curl -sf http://127.0.0.1:8765/healthz >/dev/null 2>&1; then
        echo "✓ 服务已就绪"
        break
    fi
    sleep 1
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🎉 部署完成"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Web 地址: http://$(hostname -I | awk '{print $1}'):8765"
echo ""
echo "  查看首次 admin 密码："
echo "  docker compose logs | grep -A 8 '管理员账户'"
echo ""
echo "  日常操作："
echo "  docker compose logs -f   # 实时日志"
echo "  docker compose ps        # 状态"
echo "  docker compose pull && docker compose up -d   # 升级"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
