"""
CLI 工具：
  python -m app.cli hash-password <plaintext>
  python -m app.cli gen-secret
  python -m app.cli reset-admin [--username admin] [--password XXX]   # 留空则提示输入
  python -m app.cli import-legacy --user <username> --label <name>
"""
import getpass
import argparse
import secrets
import shutil
import sys
import os
from datetime import datetime


def cmd_hash_password(args):
    from .security import hash_password
    h = hash_password(args.password)
    print(h)


def cmd_gen_secret(args):
    print(secrets.token_hex(32))


def cmd_reset_admin(args):
    """直接修改 DB 里管理员密码（无需重启容器）"""
    from .db import SessionLocal, init_db
    from .models import User, AuditLog
    from .security import hash_password
    from datetime import datetime as _dt

    init_db()
    username = args.username or "admin"
    pwd = args.password
    if not pwd:
        try:
            pwd = getpass.getpass(f"请输入 {username} 的新密码（不回显）: ")
            pwd2 = getpass.getpass("再次确认: ")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            sys.exit(1)
        if pwd != pwd2:
            print("两次输入不一致")
            sys.exit(1)
    if len(pwd) < 6:
        print("密码至少 6 位")
        sys.exit(1)

    h = hash_password(pwd)
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            print(f"用户 {username} 不存在，将创建为管理员")
            user = User(username=username, password_hash=h,
                        is_admin=True, is_active=True,
                        expires_at=datetime(2099, 12, 31), max_accounts=100)
            db.add(user)
        else:
            user.password_hash = h
            user.is_active = True
            if not user.is_admin:
                print(f"⚠️ 用户 {username} 不是管理员，自动提升为管理员")
                user.is_admin = True
        db.add(AuditLog(actor_user_id=user.id, actor_kind="system",
                        action="reset_admin_password", target_type="user",
                        target_id=str(user.id) if user.id else None,
                        ts=_dt.utcnow()))
        db.commit()
    print(f"✓ {username} 密码已重置成功，可立即用新密码登录")


def cmd_import_legacy(args):
    """把 ~/.douyin/default/ 导入到 SaaS 结构"""
    from .db import SessionLocal, init_db
    from .models import User, DouyinAccount, Schedule, AuditLog
    from .config import settings

    init_db()
    src_dir = os.path.expanduser(f"~/.douyin/{args.legacy_name}")
    if not os.path.isdir(src_dir):
        print(f"未找到旧目录: {src_dir}")
        sys.exit(1)

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == args.user).first()
        if not user:
            print(f"用户不存在: {args.user} —— 请先在 /register 注册并激活")
            sys.exit(1)
        acc = db.query(DouyinAccount).filter(
            DouyinAccount.user_id == user.id,
            DouyinAccount.label == args.label,
        ).first()
        if not acc:
            acc = DouyinAccount(user_id=user.id, label=args.label,
                                status="active",
                                cookies_exist=os.path.exists(os.path.join(src_dir, "cookies.json")))
            db.add(acc); db.commit(); db.refresh(acc)
        dst_dir = os.path.join(settings.data_dir, "users", str(user.id), "accounts", str(acc.id))
        os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
        if os.path.isdir(dst_dir):
            shutil.rmtree(dst_dir)
        shutil.copytree(src_dir, dst_dir)
        print(f"✓ 已复制 {src_dir} → {dst_dir}")

        # 尝试把 ~/.douyin_schedules.json 中对应条目迁过来
        sch_file = os.path.expanduser("~/.douyin_schedules.json")
        if os.path.exists(sch_file):
            import json
            try:
                scs = json.load(open(sch_file))
                entry = scs.get(args.legacy_name)
                if entry:
                    sch = db.query(Schedule).filter(Schedule.douyin_account_id == acc.id).first()
                    if not sch:
                        sch = Schedule(douyin_account_id=acc.id)
                        db.add(sch)
                    sch.enabled = bool(entry.get("enabled", False))
                    sch.time_hhmm = entry.get("time", "09:00")
                    db.commit()
                    print(f"✓ 定时配置已导入: enabled={sch.enabled} time={sch.time_hhmm}")
            except Exception as e:
                print(f"定时配置导入失败: {e}")

        db.add(AuditLog(actor_user_id=user.id, actor_kind="system",
                        action="legacy_import", target_type="account",
                        target_id=str(acc.id), ts=datetime.utcnow()))
        db.commit()
    finally:
        db.close()


def main():
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hash-password", help="生成 bcrypt 哈希")
    h.add_argument("password")
    h.set_defaults(func=cmd_hash_password)

    g = sub.add_parser("gen-secret", help="生成 64 字符 SECRET_KEY")
    g.set_defaults(func=cmd_gen_secret)

    r = sub.add_parser("reset-admin", help="重置管理员密码（直接改 DB）")
    r.add_argument("--username", default="admin", help="管理员用户名（默认 admin）")
    r.add_argument("--password", default=None, help="新密码；省略则交互式输入（隐藏）")
    r.set_defaults(func=cmd_reset_admin)

    i = sub.add_parser("import-legacy", help="迁移旧 ~/.douyin/<name> 数据到 SaaS")
    i.add_argument("--user", required=True, help="目标用户名（必须已注册）")
    i.add_argument("--label", required=True, help="抖音账户 label")
    i.add_argument("--legacy-name", default="default", help="旧目录名")
    i.set_defaults(func=cmd_import_legacy)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
