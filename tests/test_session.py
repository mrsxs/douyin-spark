"""Session 失效机制。

背景：session cookie 原本只装 {"uid": N}，签名有效期 30 天。
管理员重置某人密码后，那个人手里的旧 cookie 照样能用满 30 天 ——
被盗号后改密码根本踢不掉攻击者。

做法：User.session_version 参与签名内容，版本对不上即失效。
"""

import pytest

from app.deps import SESSION_COOKIE, resolve_session_user
from app.models import User
from app.security import issue_session, read_session, read_session_full


@pytest.fixture
def user(db):
    u = User(username="alice", password_hash="x",
             max_accounts=1)
    db.add(u); db.commit(); db.refresh(u)
    return u


# ── 令牌载荷 ─────────────────────────────────────────────────────

def test_session_carries_version():
    tok = issue_session(7, version=3)
    assert read_session_full(tok) == (7, 3)
    assert read_session(tok) == 7          # 旧接口仍可用


def test_legacy_cookie_without_version_reads_as_zero():
    """升级前签发的 cookie 里没有 v 字段，应视作版本 0 而不是直接失效。"""
    from app.security import _session_serializer
    legacy = _session_serializer.dumps({"uid": 42})
    assert read_session_full(legacy) == (42, 0)


def test_garbage_token_rejected():
    assert read_session_full("not-a-token") is None
    assert read_session_full("") is None
    assert read_session_full(None) is None


# ── 版本校验 ─────────────────────────────────────────────────────

def test_valid_session_resolves(db, user):
    tok = issue_session(user.id, user.session_version)
    assert resolve_session_user(db, tok).id == user.id


def test_bumping_version_invalidates_old_session(db, user):
    """核心回归：改密码后旧 cookie 立刻失效。"""
    old = issue_session(user.id, user.session_version)
    assert resolve_session_user(db, old) is not None

    user.session_version = (user.session_version or 0) + 1
    db.commit()

    assert resolve_session_user(db, old) is None, "旧 session 仍然有效"
    new = issue_session(user.id, user.session_version)
    assert resolve_session_user(db, new).id == user.id


def test_inactive_user_rejected(db, user):
    tok = issue_session(user.id, user.session_version)
    user.is_active = False
    db.commit()
    assert resolve_session_user(db, tok) is None


def test_deleted_user_rejected(db, user):
    tok = issue_session(user.id, user.session_version)
    db.delete(user); db.commit()
    assert resolve_session_user(db, tok) is None


# ── 端到端：管理员重置密码把人踢下线 ────────────────────────────

def test_admin_reset_password_kicks_user_out(client, db):
    from app.security import hash_password

    victim = User(username="victim", password_hash=hash_password("oldpw"),
                  max_accounts=1)
    admin = User(username="root", password_hash=hash_password("x"), is_admin=True,
                 max_accounts=9)
    db.add_all([victim, admin]); db.commit()
    db.refresh(victim); db.refresh(admin)

    # 受害者持有的 cookie，此刻可用
    victim_cookie = issue_session(victim.id, victim.session_version)
    client.cookies.set(SESSION_COOKIE, victim_cookie)
    assert client.get("/dashboard", follow_redirects=False).status_code == 200

    # 管理员重置其密码
    client.cookies.set(SESSION_COOKIE, issue_session(admin.id, admin.session_version))
    client.get("/login")
    tok = client.cookies.get("csrf", "")
    r = client.post(f"/admin/users/{victim.id}/reset-password",
                    data={"new_password": "brandnew123", "csrf_token": tok},
                    headers={"X-CSRF-Token": tok}, follow_redirects=False)
    assert r.status_code in (200, 302), r.text

    # 受害者的旧 cookie 必须失效
    client.cookies.set(SESSION_COOKIE, victim_cookie)
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login", \
        "重置密码后旧 session 仍然能用"


def test_other_users_sessions_unaffected(db, user):
    """踢一个人不能把所有人都踢下线。"""
    other = User(username="bob", password_hash="x")
    db.add(other); db.commit(); db.refresh(other)

    other_tok = issue_session(other.id, other.session_version)
    user.session_version += 1
    db.commit()

    assert resolve_session_user(db, other_tok).id == other.id
