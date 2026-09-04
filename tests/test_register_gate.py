"""注册开关 ALLOW_REGISTER。

背景：拆掉授权码前，`/register` 是安全的 —— 注册出来的账号 `expires_at=None`、
`max_accounts=0`，不兑换授权码就是个死账号。拆掉之后注册即拿到
`DEFAULT_MAX_ACCOUNTS` 个抖音号槽位和完整 API 权限。

面板一旦暴露到公网，陌生人注册完就能用部署者的服务器和 IP 去打抖音接口，
风控算在部署者头上 —— 正是 README 里反复警告的那个风险。
所以默认关闭，要开得显式打开。
"""
import pytest

from app.config import settings


@pytest.fixture
def reg_on(monkeypatch):
    monkeypatch.setattr(settings, "allow_register", True)


@pytest.fixture
def reg_off(monkeypatch):
    monkeypatch.setattr(settings, "allow_register", False)


def _post_register(client, username="whoever"):
    client.get("/login")
    tok = client.cookies.get("csrf", "")
    return client.post("/register", data={
        "username": username, "password": "pw123456",
        "password2": "pw123456", "email": "", "csrf_token": tok,
    }, headers={"X-CSRF-Token": tok}, follow_redirects=False)


# ── 默认关闭 ─────────────────────────────────────────────────────

def test_default_is_closed():
    """核心：默认必须是关的。改这个默认值等于给所有自部署开后门。"""
    from app.config import Settings
    assert Settings().allow_register is False


def test_get_is_404_when_closed(client, reg_off):
    assert client.get("/register", follow_redirects=False).status_code == 404


def test_post_is_404_when_closed(client, reg_off, db):
    """GET 挡住不够 —— 直接 POST 也必须挡，否则表单可以被绕过。"""
    from app.models import User

    r = _post_register(client, "sneaky")
    assert r.status_code == 404
    assert db.query(User).filter(User.username == "sneaky").first() is None, \
        "注册关着却建出了用户"


def test_uses_404_not_403(client, reg_off):
    """用 404 而不是 403：403 等于告诉扫描器这里有个关着的注册端点。"""
    assert client.get("/register", follow_redirects=False).status_code != 403


# ── 打开后正常工作 ───────────────────────────────────────────────

def test_register_works_when_open(client, reg_on, db):
    from app.models import DEFAULT_MAX_ACCOUNTS, User

    r = _post_register(client, "invited")
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"

    u = db.query(User).filter(User.username == "invited").one()
    assert u.max_accounts == DEFAULT_MAX_ACCOUNTS


def test_page_renders_when_open(client, reg_on):
    assert client.get("/register").status_code == 200


# ── 前台不能宣传关掉的入口 ───────────────────────────────────────

def test_login_page_hides_link_when_closed(client, reg_off):
    """关着还挂个注册链接，用户点进去撞 404 —— 比没有链接更糟。"""
    html = client.get("/login").text
    assert 'href="/register"' not in html
    assert "未开放注册" in html


def test_login_page_shows_link_when_open(client, reg_on):
    assert 'href="/register"' in client.get("/login").text


def test_robots_disallows_register_when_closed(client, reg_off):
    body = client.get("/robots.txt").text
    assert "Disallow: /register" in body
    assert "Allow: /register" not in body


def test_sitemap_omits_register_when_closed(client, reg_off):
    assert "/register" not in client.get("/sitemap.xml").text


def test_sitemap_lists_register_when_open(client, reg_on):
    assert "/register" in client.get("/sitemap.xml").text


# ── 关掉注册不影响已有用户 ───────────────────────────────────────

def test_existing_users_can_still_log_in(client, reg_off, db):
    """关的是注册，不是登录。"""
    from app.models import User
    from app.security import hash_password

    db.add(User(username="oldtimer", password_hash=hash_password("pw123456")))
    db.commit()

    client.get("/login")
    tok = client.cookies.get("csrf", "")
    r = client.post("/login", data={"username": "oldtimer",
                                    "password": "pw123456", "csrf_token": tok},
                    headers={"X-CSRF-Token": tok}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"
