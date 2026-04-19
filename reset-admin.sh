#!/bin/bash
# 在服务器上重置 admin 密码（无需重启容器）
# 用法：
#   ./reset-admin.sh                # 交互式输入新密码
#   ./reset-admin.sh -p mypassword  # 直接传新密码（不推荐：进 history）
#   ./reset-admin.sh -u other -p X  # 重置其他管理员
set -e

USERNAME="admin"
PASSWORD=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -u|--username) USERNAME="$2"; shift 2;;
        -p|--password) PASSWORD="$2"; shift 2;;
        -h|--help)
            echo "用法: $0 [-u 用户名] [-p 新密码]"
            echo "  -u 默认 admin"
            echo "  -p 留空则交互式输入"
            exit 0;;
        *) echo "未知参数: $1"; exit 1;;
    esac
done

# 容器名（如果用了 docker compose 默认 container_name 就是 douyin-spark）
CONTAINER="${CONTAINER:-douyin-spark}"

if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "❌ 容器 ${CONTAINER} 未运行。先 docker compose up -d"
    exit 1
fi

if [ -z "$PASSWORD" ]; then
    # 交互式输入（隐藏）
    read -s -p "请输入 ${USERNAME} 的新密码: " PASSWORD
    echo
    read -s -p "再次确认: " PASSWORD2
    echo
    [ "$PASSWORD" = "$PASSWORD2" ] || { echo "两次输入不一致"; exit 1; }
fi

if [ ${#PASSWORD} -lt 6 ]; then
    echo "密码至少 6 位"
    exit 1
fi

# 通过容器里的 cli 重置（直接改 DB User 表，不用重启）
docker exec -e NEW_PASS="$PASSWORD" -e NEW_USER="$USERNAME" "$CONTAINER" \
    python -m app.cli reset-admin --username "$USERNAME" --password "$PASSWORD"

echo
echo "✓ 密码已重置，可立刻用新密码登录"
echo "  用户名: $USERNAME"
echo "  地址  : http://$(hostname -I 2>/dev/null | awk '{print $1}'):8765/login"
