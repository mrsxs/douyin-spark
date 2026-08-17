"""抖音头像 URL 规范化。

真实踩到的：账号头像一直显示成首字母 fallback。
DB 里明明存了 URL，但浏览器加载 403 ——
fetch_self_profile 返回的是 `p3-pc-sign.douyinpic.com` 的**签名 URL**，
带时效，存下来过一阵就失效；而 fetch_nicknames 拿到的
`p3-pc.douyinpic.com` 是长期有效的。
"""
import pytest

from app.contacts_service import normalize_avatar_url as norm


# ── 签名域名要换掉 ───────────────────────────────────────────────

def test_sign_domain_is_rewritten():
    """核心回归：带 -sign 的签名域名会过期，必须换成常规域名。"""
    src = ("https://p3-pc-sign.douyinpic.com/aweme/100x100/aweme-avatar/"
           "tos-cn-avt-0015_abc.jpeg?from=xyz&x-expires=123&x-signature=q")
    out = norm(src)
    assert "-sign" not in out
    assert out.startswith("https://p3-pc.douyinpic.com/")


@pytest.mark.parametrize("host,expect", [
    ("p3-pc-sign.douyinpic.com", "p3-pc.douyinpic.com"),
    ("p6-pc-sign.douyinpic.com", "p6-pc.douyinpic.com"),
    ("p9-sign.douyinpic.com", "p9.douyinpic.com"),
])
def test_various_sign_hosts(host, expect):
    out = norm(f"https://{host}/aweme/100x100/avatar.jpeg")
    assert out.startswith(f"https://{expect}/")


def test_expiring_query_params_dropped():
    """签名参数留着没用，还会让 URL 更快失效。"""
    out = norm("https://p3-pc-sign.douyinpic.com/a.jpeg"
               "?from=327834062&x-expires=1&x-signature=2")
    assert "x-expires" not in out
    assert "x-signature" not in out


# ── 正常 URL 不动 ────────────────────────────────────────────────

def test_normal_url_untouched():
    src = "https://p3-pc.douyinpic.com/aweme/100x100/aweme-avatar/tos-cn-i-0813_x.jpeg"
    assert norm(src) == src


def test_non_douyin_url_untouched():
    """别把用户自定义的外链改坏。"""
    src = "https://example.com/avatar.png?v=2"
    assert norm(src) == src


@pytest.mark.parametrize("empty", ["", None])
def test_empty_is_safe(empty):
    assert norm(empty) == ""


def test_garbage_is_dropped():
    """不是 http(s) 的一律丢掉。

    这些值直接进 <img src>，原样放行等于给 javascript: / data: 开后门。
    """
    assert norm("not a url") == ""


@pytest.mark.parametrize("hostile", [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "//evil.com/x.png",
    "ftp://evil/x.png",
    " javascript:alert(1)",
])
def test_hostile_scheme_is_dropped(hostile):
    assert norm(hostile) == "", f"危险 URL 被放行: {hostile}"


# ── 入库时自动规范化 ─────────────────────────────────────────────

def test_upsert_normalizes(db):
    from app import contacts_service as cs
    from app.models import Contact, DouyinAccount, User

    u = User(username="normuser", password_hash="x")
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="a", status="active")
    db.add(a); db.commit(); db.refresh(a)

    cs.upsert_cache(db, a.id, [{
        "uid": "1", "nickname": "n", "days": 1, "status": "active",
        "avatar": "https://p3-pc-sign.douyinpic.com/x.jpeg?x-expires=9",
    }])
    db.commit()
    row = db.query(Contact).filter(Contact.uid == "1").first()
    assert "-sign" not in row.avatar
    assert "x-expires" not in row.avatar
