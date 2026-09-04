"""
CLI 工具：
  python -m app.cli hash-password <plaintext>
  python -m app.cli gen-secret
  python -m app.cli reset-password [--username admin] [--password XXX] [--make-admin]
  python -m app.cli set-admin --username X --on|--off
  python -m app.cli list-users
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


def cmd_reset_password(args):
    """直接修改 DB 里某个用户的密码（无需重启容器）。

    ⚠️ 这里**绝不能**顺手 `is_admin = True`。
    这个命令（以及 manage.sh / manage.ps1 的 [2]）是客户唯一的改密工具，
    拿它给普通用户重置密码是完全正常的用法。旧版本无条件提权，
    提示语还只说「已重置密码」—— 结果是普通用户登录后就是管理员，
    能看到全部租户的数据，而且没有任何痕迹。提权必须显式 --make-admin。
    """
    from .db import SessionLocal, init_db
    from .models import DEFAULT_MAX_ACCOUNTS, AuditLog, User
    from .security import hash_password
    from .config import settings
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
            # 新建时只有两种情况给管理员：显式 --make-admin，或就是 .env 配的那个管理员名
            as_admin = bool(args.make_admin) or username == settings.admin_username
            user = User(
                username=username, password_hash=h, is_active=True,
                is_admin=as_admin,
                max_accounts=100 if as_admin else DEFAULT_MAX_ACCOUNTS,
            )
            db.add(user)
            print(f"用户 {username} 不存在，已创建为"
                  f"{'管理员' if as_admin else '普通用户'}")
        else:
            user.password_hash = h
            user.is_active = True
            if args.make_admin and not user.is_admin:
                user.is_admin = True
                print(f"⚠️ 已按 --make-admin 把 {username} 提升为管理员")
            # 旧 cookie 立即失效 —— 否则改了密码也踢不掉已登录的会话
            user.session_version = (user.session_version or 0) + 1
        # 新建的用户 id 是 commit 时才分配的，不 flush 的话审计日志的
        # actor/target 全是 NULL —— 恰恰是最需要留痕的那一条
        db.flush()
        db.add(AuditLog(actor_user_id=user.id, actor_kind="system",
                        action="reset_password", target_type="user",
                        target_id=str(user.id),
                        meta="make_admin" if args.make_admin else None,
                        ts=_dt.utcnow()))
        db.commit()
        role = "管理员" if user.is_admin else "普通用户"
    print(f"✓ {username}（{role}）密码已重置成功，可立即用新密码登录")


def cmd_set_admin(args):
    """显式开/关管理员权限 —— 用来修复被误提权的账号。"""
    from .db import SessionLocal, init_db
    from .models import User, AuditLog
    from datetime import datetime as _dt

    init_db()
    turn_on = args.on
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == args.username).first()
        if not user:
            print(f"用户不存在: {args.username}")
            sys.exit(1)
        if user.is_admin == turn_on:
            print(f"{args.username} 已经是{'管理员' if turn_on else '普通用户'}，无需改动")
            return
        if not turn_on:
            others = db.query(User).filter(User.is_admin.is_(True),
                                           User.id != user.id).count()
            if others == 0:
                print("拒绝：这是最后一个管理员，撤销后没人能进后台")
                sys.exit(1)
        user.is_admin = turn_on
        # 权限变了就让旧 cookie 失效，避免降权后旧会话还带着管理员视图
        user.session_version = (user.session_version or 0) + 1
        db.add(AuditLog(actor_user_id=user.id, actor_kind="system",
                        action="set_admin", target_type="user",
                        target_id=str(user.id), meta=str(turn_on), ts=_dt.utcnow()))
        db.commit()
    print(f"✓ {args.username} 已{'提升为管理员' if turn_on else '降为普通用户'}"
          f"（该用户需重新登录）")


def cmd_list_users(args):
    """列出所有用户及其权限 —— 排查「谁是管理员」用。"""
    from .db import SessionLocal, init_db
    from .models import User

    init_db()
    with SessionLocal() as db:
        users = db.query(User).order_by(User.id).all()
        print(f"{'ID':>4}  {'用户名':<20} {'权限':<8} {'状态':<6} 账号配额")
        for u in users:
            print(f"{u.id:>4}  {u.username:<20} "
                  f"{'管理员' if u.is_admin else '普通':<8} "
                  f"{'启用' if u.is_active else '停用':<6} {u.max_accounts}")
        admins = [u.username for u in users if u.is_admin]
        print(f"\n管理员共 {len(admins)} 个: {', '.join(admins) or '（无）'}")
        if len(admins) > 1:
            print("⚠️ 管理员多于 1 个。若非本意，用 "
                  "`python -m app.cli set-admin --username X --off` 撤销。")


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

    for name, helptext in (("reset-password", "重置任意用户密码（直接改 DB）"),
                           ("reset-admin", "（旧名，等同 reset-password）")):
        r = sub.add_parser(name, help=helptext)
        r.add_argument("--username", default="admin", help="用户名（默认 admin）")
        r.add_argument("--password", default=None, help="新密码；省略则交互式输入（隐藏）")
        r.add_argument("--make-admin", action="store_true",
                       help="同时提升为管理员（不加此参数则保持原权限不变）")
        r.set_defaults(func=cmd_reset_password)

    s = sub.add_parser("set-admin", help="开/关某用户的管理员权限")
    s.add_argument("--username", required=True)
    grp = s.add_mutually_exclusive_group(required=True)
    grp.add_argument("--on", dest="on", action="store_true", help="提升为管理员")
    grp.add_argument("--off", dest="on", action="store_false", help="降为普通用户")
    s.set_defaults(func=cmd_set_admin)

    lu = sub.add_parser("list-users", help="列出所有用户及权限")
    lu.set_defaults(func=cmd_list_users)

    i = sub.add_parser("import-legacy", help="迁移旧 ~/.douyin/<name> 数据到 SaaS")
    i.add_argument("--user", required=True, help="目标用户名（必须已注册）")
    i.add_argument("--label", required=True, help="抖音账户 label")
    i.add_argument("--legacy-name", default="default", help="旧目录名")
    i.set_defaults(func=cmd_import_legacy)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
