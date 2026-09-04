"""登录 / 注册的限流。

这两个端点原本毫无限流：登录可被撞库和针对单账号爆破，
注册可被脚本批量刷号。
"""
import pytest

from app import ratelimit
from app.models import User
from app.security import hash_password


@pytest.fixture(autouse=True)
def _clean_limiter():
    """用例之间清空计数，否则前一个测试打满配额会污染后面的。"""
    ratelimit.reset()
    yield
    ratelimit.reset()


def _csrf(client):
    client.get("/login")
    return client.cookies.get("csrf", "")


def _form(client, url, data):
    return client.post(url, data={**data, "csrf_token": _csrf(client)},
                       headers={"X-CSRF-Token": _csrf(client)},
                       follow_redirects=False)


# ── 登录爆破 ─────────────────────────────────────────────────────

def test_login_blocks_after_limit(client, db):
    db.add(User(username="victim", password_hash=hash_password("realpw")))
    db.commit()

    limit = int(ratelimit.LOGIN_LIMIT.split("/")[0])
    seen_block = False
    for i in range(limit + 3):
        r = _form(client, "/login", {"username": "victim", "password": f"guess{i}"})
        if "rate_limited" in (r.headers.get("location") or ""):
            seen_block = True
            break
    assert seen_block, f"连续 {limit + 3} 次撞库都没被限流"


def test_login_within_limit_still_works(client, db):
    db.add(User(username="normal", password_hash=hash_password("goodpw")))
    db.commit()
    r = _form(client, "/login", {"username": "normal", "password": "goodpw"})
    assert r.status_code == 302
    assert "rate_limited" not in (r.headers.get("location") or "")
    assert r.headers["location"] == "/"


def test_register_is_limited(client, monkeypatch):
    # 注册默认关着（见 test_register_gate.py），这里要测的是开着时的限流
    from app.config import settings
    monkeypatch.setattr(settings, "allow_register", True)

    limit = int(ratelimit.REGISTER_LIMIT.split("/")[0])
    blocked = False
    for i in range(limit + 3):
        r = _form(client, "/register", {
            "username": f"spam{i}", "password": "pw123456",
            "password2": "pw123456", "email": ""})
        if "rate_limited" in (r.headers.get("location") or ""):
            blocked = True
            break
    assert blocked, "注册端点可被脚本刷号"


# ── 限流不该误伤 ─────────────────────────────────────────────────

def test_readonly_pages_not_limited(client):
    """GET 页面不受限流影响，正常用户不会被挡。"""
    for _ in range(30):
        assert client.get("/login").status_code == 200


def test_api_gets_json_not_redirect(client, db, login, monkeypatch):
    """API 路径超限要返回 JSON 429，不能重定向。"""
    from fastapi import Request
    from app.ratelimit import rate_limit_handler
    from slowapi.errors import RateLimitExceeded
    import asyncio

    scope = {"type": "http", "path": "/api/auto", "method": "POST",
             "headers": [], "query_string": b""}
    req = Request(scope)
    resp = asyncio.run(rate_limit_handler(req, RateLimitExceeded(
        type("L", (), {"error_message": None, "limit": None})())))
    assert resp.status_code == 429
    assert "频繁" in resp.body.decode("utf-8")


def test_forwarded_for_used_as_key():
    """反代场景下按真实客户端 IP 限流，而不是把所有人算作反代那一个 IP。"""
    from fastapi import Request
    scope = {"type": "http", "path": "/login", "method": "POST",
             "headers": [(b"x-forwarded-for", b"1.2.3.4, 10.0.0.1")],
             "query_string": b"", "client": ("10.0.0.1", 1234)}
    assert ratelimit._client_key(Request(scope)) == "1.2.3.4"


# ── 限流提示要能被用户看懂 ───────────────────────────────────────

@pytest.mark.parametrize("page", ["/login", "/register"])
def test_rate_limited_message_is_shown(client, page, monkeypatch):
    """被限流时不能显示成「用户名或密码错误」—— 用户密码明明是对的。"""
    from app.config import settings
    monkeypatch.setattr(settings, "allow_register", True)

    html = client.get(f"{page}?error=rate_limited").text
    assert "频繁" in html
    assert "用户名或密码错误" not in html

