#!/bin/bash
# 服务器端续费脚本：交互输入新 License → 备份旧 .env → 替换 → 重启 → 验证
set -e

CONTAINER="${CONTAINER:-douyin-spark}"
DIR="${INSTALL_DIR:-/opt/douyin-spark}"

cd "$DIR" || { echo "❌ $DIR 不存在"; exit 1; }
[ -f .env ] || { echo "❌ .env 不存在"; exit 1; }

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  续火花 License 续费"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 显示当前 license 状态
echo "  当前 License："
docker compose logs --tail 200 2>&1 | grep -A 5 "✓ License 已验证" | tail -6 || echo "  （无法读取，可能容器未运行）"
echo

# 输入新 license
echo "请粘贴卖家新签发的 License Key（一长串，含 . 分隔签名）："
read -r NEW_LICENSE
NEW_LICENSE="$(echo "$NEW_LICENSE" | xargs)"

if [ -z "$NEW_LICENSE" ] || [[ ! "$NEW_LICENSE" == *.* ]]; then
    echo "❌ License 格式异常，已取消"
    exit 1
fi

# 备份
BACKUP=".env.bak.$(date +%Y%m%d_%H%M%S)"
cp .env "$BACKUP"
echo "✓ 旧 .env 备份到 $BACKUP"

# 替换 LICENSE_KEY 行（不存在则追加）
if grep -q '^LICENSE_KEY=' .env; then
    # 用 # 作分隔符避免 license 里的 / 干扰
    sed -i.tmp "s#^LICENSE_KEY=.*#LICENSE_KEY=$NEW_LICENSE#" .env && rm -f .env.tmp
else
    echo "LICENSE_KEY=$NEW_LICENSE" >> .env
fi
echo "✓ .env 已更新"

# 重启容器
echo
echo "重启容器..."
docker compose restart

# 等就绪
echo "等容器就绪..."
for i in $(seq 1 30); do
    if docker compose ps app 2>&1 | grep -q "Up"; then
        sleep 2
        break
    fi
    sleep 1
done

# 验证新 license
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
docker compose logs --tail 30 2>&1 | grep -A 5 "✓ License 已验证" | tail -6 || {
    echo "⚠️ 未找到续费成功标志，看完整日志："
    echo
    docker compose logs --tail 30
    echo
    echo "若验证失败可恢复旧 license："
    echo "  cp $BACKUP .env && docker compose restart"
    exit 1
}
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✓ 续费成功"
