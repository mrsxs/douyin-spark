#!/bin/sh
set -e

# ── 确保数据目录存在 + 权限正确 ──
mkdir -p /data
chmod 700 /data

# ── 首次启动：没配 ADMIN_PASSWORD_HASH 就随机生成一个 admin 密码 ──
if [ -z "$ADMIN_PASSWORD_HASH" ]; then
  FIRST_BOOT_FILE="/data/.admin_initialized"
  if [ ! -f "$FIRST_BOOT_FILE" ]; then
    # 生成 20 字符强随机密码
    ADMIN_PLAIN="$(python3 -c 'import secrets, string; a=string.ascii_letters+string.digits; print("".join(secrets.choice(a) for _ in range(20)))')"
    ADMIN_PASSWORD_HASH="$(python3 -c "import bcrypt; print(bcrypt.hashpw('$ADMIN_PLAIN'.encode(), bcrypt.gensalt(12)).decode())")"
    export ADMIN_PASSWORD_HASH

    echo "============================================================"
    echo "  🔐 首次启动 - 管理员账户已自动创建"
    echo "============================================================"
    echo "  用户名: ${ADMIN_USERNAME:-admin}"
    echo "  密码  : $ADMIN_PLAIN"
    echo "============================================================"
    echo "  ⚠️  请立即保存此密码！此消息不会再出现。"
    echo "  若需重置密码：删除 /data/.admin_initialized 后重启，或用"
    echo "  docker exec <容器> python -m app.cli hash-password <新密码>"
    echo "============================================================"
    touch "$FIRST_BOOT_FILE"
  else
    echo "⚠️  检测到 ADMIN_PASSWORD_HASH 未设置但已初始化过；容器将按未配置管理员启动。"
    echo "   如需重置：docker exec <容器> rm /data/.admin_initialized 后重启。"
  fi
fi

# ── SECRET_KEY：未配时自动持久化到 /data/.secret_key（app/config.py 已有该逻辑）──
# 这里不额外处理，直接让 config.py 去做

# ── 交给主进程 ──
exec "$@"
