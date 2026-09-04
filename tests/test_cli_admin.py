"""改密工具不得顺手提权。

背景：线上出现「别的用户登录也是管理员」。排查后确认 Web 的
/register、/login 完全不碰 is_admin，唯一能提权的是三个运维入口：
manage.sh / manage.ps1 的 [2]、app.cli 的 reset-admin、以及启动时的
ensure_admin()。前两个旧版本无条件 `is_admin = True`，而它们是客户
唯一的改密工具 —— 给普通用户改个密码就把他变成了能看全部租户数据的
管理员，提示语还只说「已重置密码」。

这些用例锁死「默认不提权」，包括 shell / PowerShell 里那段内嵌 python
（它不走 app.cli，改了这边忘了那边一样会漏）。
"""
from argparse import Namespace
from pathlib import Path

import pytest

from app import cli
from app.models import DEFAULT_MAX_ACCOUNTS, AuditLog, User
from app.security import verify_password

ROOT = Path(__file__).resolve().parent.parent


def reset_args(username, password="newpw123456", make_admin=False):
    return Namespace(username=username, password=password, make_admin=make_admin)


# ── reset-password：默认保持原权限 ────────────────────────────────────

def test_reset_password_keeps_normal_user_normal(make_user, db):
    u = make_user("normalguy")
    assert u.is_admin is False

    cli.cmd_reset_password(reset_args("normalguy"))

    db.expire_all()
    got = db.query(User).filter(User.username == "normalguy").one()
    assert got.is_admin is False, "改密码不能把普通用户变成管理员"


def test_reset_password_actually_changes_password(make_user, db):
    make_user("normalguy")
    cli.cmd_reset_password(reset_args("normalguy", password="brandnew99"))

    db.expire_all()
    got = db.query(User).filter(User.username == "normalguy").one()
    assert verify_password("brandnew99", got.password_hash)
    assert not verify_password("pw123456", got.password_hash)


def test_reset_password_reactivates_disabled_user(make_user, db):
    make_user("frozen", is_active=False)
    cli.cmd_reset_password(reset_args("frozen"))

    db.expire_all()
    assert db.query(User).filter(User.username == "frozen").one().is_active is True


def test_reset_password_bumps_session_version(make_user, db):
    u = make_user("normalguy")
    old_ver = u.session_version or 0

    cli.cmd_reset_password(reset_args("normalguy"))

    db.expire_all()
    got = db.query(User).filter(User.username == "normalguy").one()
    assert got.session_version == old_ver + 1, "改密后旧 cookie 必须失效"


def test_old_cookie_dies_after_reset(make_user, db):
    """session_version 递增要真的能踢掉已签发的 cookie。"""
    from app.deps import resolve_session_user
    from app.security import issue_session

    u = make_user("normalguy")
    cookie = issue_session(u.id, u.session_version or 0)
    assert resolve_session_user(db, cookie) is not None

    cli.cmd_reset_password(reset_args("normalguy"))

    db.expire_all()
    assert resolve_session_user(db, cookie) is None


def test_reset_password_make_admin_promotes(make_user, db):
    make_user("normalguy")
    cli.cmd_reset_password(reset_args("normalguy", make_admin=True))

    db.expire_all()
    assert db.query(User).filter(User.username == "normalguy").one().is_admin is True


def test_reset_password_does_not_demote_existing_admin(make_user, db):
    make_user("theboss", is_admin=True)
    cli.cmd_reset_password(reset_args("theboss"))

    db.expire_all()
    assert db.query(User).filter(User.username == "theboss").one().is_admin is True


# ── reset-password：新建用户 ──────────────────────────────────────────

def test_creates_missing_user_as_normal(db, monkeypatch):
    monkeypatch.setattr(cli_settings(), "admin_username", "admin", raising=False)
    cli.cmd_reset_password(reset_args("brandnewname"))

    got = db.query(User).filter(User.username == "brandnewname").one()
    assert got.is_admin is False
    assert got.max_accounts == DEFAULT_MAX_ACCOUNTS, \
        "普通用户新建后应拿到默认账号配额"


def test_creates_configured_admin_name_as_admin(db, monkeypatch):
    """.env 里配的那个管理员名不存在时，仍然要能一键建出来。"""
    monkeypatch.setattr(cli_settings(), "admin_username", "owner", raising=False)
    cli.cmd_reset_password(reset_args("owner"))

    got = db.query(User).filter(User.username == "owner").one()
    assert got.is_admin is True
    assert got.max_accounts == 100


def test_creates_missing_user_as_admin_when_asked(db, monkeypatch):
    monkeypatch.setattr(cli_settings(), "admin_username", "admin", raising=False)
    cli.cmd_reset_password(reset_args("helper", make_admin=True))

    assert db.query(User).filter(User.username == "helper").one().is_admin is True


def test_audit_log_records_real_user_id(db, monkeypatch):
    """新建分支不 flush 的话，审计日志的 actor/target 全是 NULL。"""
    monkeypatch.setattr(cli_settings(), "admin_username", "admin", raising=False)
    cli.cmd_reset_password(reset_args("brandnewname"))

    u = db.query(User).filter(User.username == "brandnewname").one()
    log = db.query(AuditLog).filter(AuditLog.action == "reset_password").one()
    assert log.actor_user_id == u.id
    assert log.target_id == str(u.id)


def test_short_password_refused(make_user, db):
    make_user("normalguy")
    with pytest.raises(SystemExit):
        cli.cmd_reset_password(reset_args("normalguy", password="12345"))


def cli_settings():
    from app.config import settings
    return settings


# ── set-admin：显式开关 ───────────────────────────────────────────────

def test_set_admin_off_demotes(make_user, db):
    make_user("theboss", is_admin=True)
    victim = make_user("wrongly_promoted", is_admin=True)
    old_ver = victim.session_version or 0

    cli.cmd_set_admin(Namespace(username="wrongly_promoted", on=False))

    db.expire_all()
    got = db.query(User).filter(User.username == "wrongly_promoted").one()
    assert got.is_admin is False
    assert got.session_version == old_ver + 1, "降权后旧会话必须失效"


def test_set_admin_off_refuses_last_admin(make_user, db):
    make_user("onlyadmin", is_admin=True)
    make_user("normalguy")

    with pytest.raises(SystemExit):
        cli.cmd_set_admin(Namespace(username="onlyadmin", on=False))

    db.expire_all()
    assert db.query(User).filter(User.username == "onlyadmin").one().is_admin is True


def test_set_admin_on_promotes(make_user, db):
    make_user("normalguy")
    cli.cmd_set_admin(Namespace(username="normalguy", on=True))

    db.expire_all()
    assert db.query(User).filter(User.username == "normalguy").one().is_admin is True


def test_set_admin_noop_when_already_in_state(make_user, db):
    u = make_user("normalguy")
    old_ver = u.session_version or 0
    cli.cmd_set_admin(Namespace(username="normalguy", on=False))

    db.expire_all()
    got = db.query(User).filter(User.username == "normalguy").one()
    assert got.session_version == old_ver, "无变化时不该白白踢掉会话"


def test_set_admin_missing_user_exits(db):
    with pytest.raises(SystemExit):
        cli.cmd_set_admin(Namespace(username="ghost", on=True))


# ── list-users ────────────────────────────────────────────────────────

def test_list_users_marks_admins(make_user, capsys):
    make_user("theboss", is_admin=True)
    make_user("normalguy")

    cli.cmd_list_users(Namespace())
    out = capsys.readouterr().out
    assert "theboss" in out and "normalguy" in out
    assert "管理员共 1 个" in out


def test_list_users_warns_on_multiple_admins(make_user, capsys):
    make_user("theboss", is_admin=True)
    make_user("alsoadmin", is_admin=True)

    cli.cmd_list_users(Namespace())
    out = capsys.readouterr().out
    assert "管理员共 2 个" in out
    assert "set-admin" in out, "多管理员时要给出撤销命令"


# ── argparse 接线 ─────────────────────────────────────────────────────

def test_parser_defaults_to_no_promotion(monkeypatch):
    """不加 --make-admin 时 make_admin 必须是 False（默认值写错就全线失守）。"""
    import sys as _sys
    captured = {}
    monkeypatch.setattr(cli, "cmd_reset_password", lambda a: captured.update(vars(a)))
    monkeypatch.setattr(_sys, "argv",
                        ["app.cli", "reset-password", "--username", "x",
                         "--password", "pw123456"])
    cli.main()
    assert captured["make_admin"] is False


def test_reset_admin_alias_still_works(monkeypatch):
    """老文档/老脚本里写的是 reset-admin，不能直接删掉。"""
    import sys as _sys
    captured = {}
    monkeypatch.setattr(cli, "cmd_reset_password", lambda a: captured.update(vars(a)))
    monkeypatch.setattr(_sys, "argv",
                        ["app.cli", "reset-admin", "--password", "pw123456"])
    cli.main()
    assert captured["username"] == "admin"
    assert captured["make_admin"] is False


# ── 运维脚本里的内嵌 python（不走 app.cli，得单独盯）───────────────────

@pytest.mark.parametrize("script", ["manage.sh", "manage.ps1"])
def test_manage_script_does_not_force_promote(script):
    """每一处把 is_admin 置真的地方，都必须在 make_admin 的判断里。

    不能简单断言「不含 is_admin = True」—— 显式提权分支本来就要有这行。
    所以逐行看：出现赋值时，往上两行内必须能看到 make_admin。
    """
    text = (ROOT / script).read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines()
             if not ln.lstrip().startswith(("#", "//"))]
    hits = 0
    for i, ln in enumerate(lines):
        if "is_admin = True" in ln or "is_admin=True" in ln:
            hits += 1
            window = "\n".join(lines[max(0, i - 2):i + 1])
            assert "make_admin" in window, (
                f"{script} 第 {i + 1} 行无条件提权了 —— 改密码不能顺手给管理员：\n{window}")
    assert "is_admin=make_admin" in text, (
        f"{script} 新建用户时应由 make_admin 决定权限，而不是写死")
    assert hits <= 1, f"{script} 有 {hits} 处提权赋值，超出预期"


@pytest.mark.parametrize("script", ["manage.sh", "manage.ps1"])
def test_manage_script_passes_make_admin_env(script):
    text = (ROOT / script).read_text(encoding="utf-8")
    assert "MAKE_ADMIN" in text, f"{script} 没把 MAKE_ADMIN 传进容器"


# ── ensure_admin：撞名提权要喊出来 ────────────────────────────────────

def test_ensure_admin_warns_when_promoting_existing_user(make_user, db, capsys,
                                                         monkeypatch):
    """.env 的 ADMIN_USERNAME 撞上已注册普通用户 → 静默提权 + 覆盖密码。
    至少要在启动日志里喊一声，否则永远查不出来。"""
    import app.main as main_mod

    make_user("victim")
    monkeypatch.setattr(main_mod.settings, "admin_username", "victim")
    monkeypatch.setattr(main_mod.settings, "admin_password_hash", "$2b$12$fakehash")

    main_mod.ensure_admin()

    out = capsys.readouterr().out
    assert "警告" in out and "victim" in out
    assert "set-admin" in out, "要告诉运维怎么撤销"


def test_ensure_admin_lists_unexpected_extra_admins(make_user, db, capsys,
                                                    monkeypatch):
    import app.main as main_mod

    make_user("owner", is_admin=True)
    make_user("sneaky", is_admin=True)
    monkeypatch.setattr(main_mod.settings, "admin_username", "owner")
    monkeypatch.setattr(main_mod.settings, "admin_password_hash", "$2b$12$fakehash")

    main_mod.ensure_admin()

    out = capsys.readouterr().out
    assert "sneaky" in out, "多出来的管理员必须在启动日志里点名"
