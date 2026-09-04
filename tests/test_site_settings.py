"""站点设置：对外域名。

需求：
  - 后台可配站点域名，只接受 http(s)
  - 分享卡二维码指向配置的域名，且在服务端生成
"""
import pytest

from app import site_settings as ss
from app.models import DouyinAccount, User
from app.security import hash_password


@pytest.fixture
def admin_user(db):
    u = User(username="root", password_hash=hash_password("x"), is_admin=True,
             max_accounts=9)
    db.add(u); db.commit(); db.refresh(u)
    return u


@pytest.fixture
def plain_user(db):
    u = User(username="buyer", password_hash=hash_password("x"),
             max_accounts=2)
    db.add(u); db.commit(); db.refresh(u)
    return u


# ── 读写 ─────────────────────────────────────────────────────────

def test_defaults_are_empty(db):
    assert ss.load(db)["site_url"] == ""


def test_save_and_load(db, admin_user):
    ss.save(db, {"site_url": "https://spark.example.com"}, admin_user.id)
    db.commit()
    assert ss.load(db)["site_url"] == "https://spark.example.com"


def test_site_url_trailing_slash_normalized(db, admin_user):
    ss.save(db, {"site_url": "https://spark.example.com/"}, admin_user.id)
    db.commit()
    assert ss.load(db)["site_url"] == "https://spark.example.com"


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://x", "not a url"])
def test_rejects_non_http_urls(db, admin_user, bad):
    """只接受 http(s)，否则 javascript: 会变成前台的 XSS 入口。"""
    with pytest.raises(ValueError):
        ss.save(db, {"site_url": bad}, admin_user.id)


def test_empty_url_allowed(db, admin_user):
    """留空是合法的 —— 表示没配对外域名。"""
    ss.save(db, {"site_url": ""}, admin_user.id)
    db.commit()
    assert ss.load(db)["site_url"] == ""


# ── 分享二维码绑定域名 ───────────────────────────────────────────

def test_share_qr_uses_configured_domain(db, plain_user, admin_user, login):
    ss.save(db, {"site_url": "https://spark.example.com"}, admin_user.id)
    acc = DouyinAccount(user_id=plain_user.id, label="a",
                        status="active", cookies_exist=True)
    db.add(acc); db.commit(); db.refresh(acc)

    html = login(plain_user).get(f"/accounts/{acc.id}").text
    assert 'SHARE_QR_URL = "data:image/svg+xml;base64,' in html


def test_share_qr_placeholder_when_no_domain(db, plain_user, login):
    """没配域名时不该渲染出一个指向空地址的二维码。"""
    acc = DouyinAccount(user_id=plain_user.id, label="a",
                        status="active", cookies_exist=True)
    db.add(acc); db.commit(); db.refresh(acc)

    html = login(plain_user).get(f"/accounts/{acc.id}").text
    assert 'SHARE_QR_URL = ""' in html
    # autoescape 若把引号转成 &#39; 就是 JS 语法错误
    assert "SHARE_QR_URL = &#39;" not in html


def test_qr_is_generated_server_side():
    """二维码在服务端生成 —— 外链图片会污染 html2canvas 的 canvas，
    导致分享卡「保存图片」直接失败。"""
    data = ss.qr_data_url("https://spark.example.com")
    assert data.startswith("data:image/svg+xml;base64,")
    assert len(data) > 200
    assert ss.qr_data_url("") == ""


# ── 后台页面 ─────────────────────────────────────────────────────

def test_admin_page_renders(admin_user, login):
    r = login(admin_user).get("/admin/site")
    assert r.status_code == 200
    assert "站点域名" in r.text


def test_admin_page_requires_admin(plain_user, login):
    assert login(plain_user).get("/admin/site").status_code == 403


def test_admin_save_roundtrip(admin_user, login, db):
    c = login(admin_user)
    c.get("/login")
    tok = c.cookies.get("csrf", "")
    r = c.post("/admin/site", data={
        "site_url": "https://spark.example.com",
        "csrf_token": tok,
    }, headers={"X-CSRF-Token": tok}, follow_redirects=False)
    assert r.status_code == 302
    assert ss.load(db)["site_url"] == "https://spark.example.com"


def test_admin_save_rejects_bad_url(admin_user, login):
    c = login(admin_user)
    c.get("/login")
    tok = c.cookies.get("csrf", "")
    r = c.post("/admin/site", data={
        "site_url": "javascript:alert(1)",
        "csrf_token": tok,
    }, headers={"X-CSRF-Token": tok}, follow_redirects=False)
    assert "error" in (r.headers.get("location") or "")
