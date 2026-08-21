"""动态响应必须带 no-store。

线上真出过事：域名挂在阿里云 ESA 后面，CDN 对没有 Cache-Control 的
200 HTML 默认缓存 30 天，Vary 里又只有 Accept-Encoding（不认 Cookie），
于是 admin 打开面板看到的是 adong 的页面，页面里写死的 account_id
不属于他，点扫码登录直接 404、短信验证码输入框也永远出不来。

这些用例锁住「除静态资源外一律 no-store，且 Vary 带 Cookie」。
"""
import pytest

from app.cache_mw import CACHEABLE_PREFIXES, _rewrite


def header(resp, name):
    return resp.headers.get(name, "")


# ── 页面 ──────────────────────────────────────────────────────────────

def test_login_page_is_no_store(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "no-store" in header(r, "cache-control")


def test_dashboard_is_no_store(client, login, make_user):
    from datetime import datetime, timedelta
    u = make_user("cacheuser", expires_at=datetime.utcnow() + timedelta(days=30))
    c = login(u)
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "no-store" in header(r, "cache-control"), \
        "已登录页面被 CDN 缓存 = 直接发给别的用户"
    assert "private" in header(r, "cache-control")


def test_dashboard_varies_on_cookie(client, login, make_user):
    """就算某层缓存忽略 no-store，Vary: Cookie 也能兜住不跨用户复用。"""
    from datetime import datetime, timedelta
    u = make_user("cacheuser", expires_at=datetime.utcnow() + timedelta(days=30))
    r = login(u).get("/dashboard")
    assert "cookie" in header(r, "vary").lower()


def test_redirect_is_no_store(client):
    """302 也要盖 —— 未登录跳 /login 的那一跳被缓存同样会串。"""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "no-store" in header(r, "cache-control")


def test_api_response_is_no_store(client):
    r = client.get("/healthz")
    assert "no-store" in header(r, "cache-control")


def test_error_page_is_no_store(client):
    r = client.get("/this-route-does-not-exist")
    assert r.status_code == 404
    assert "no-store" in header(r, "cache-control")


# ── 静态资源要放行，否则 CDN 白挂 ─────────────────────────────────────

def test_static_is_not_forced_no_store(client):
    r = client.get("/static/favicon.svg")
    if r.status_code == 404:
        pytest.skip("static/favicon.svg 不存在")
    assert "no-store" not in header(r, "cache-control")


@pytest.mark.parametrize("path", ["/static/app.css", "/static/x/y.js", "/favicon.ico"])
def test_cacheable_prefixes_cover_static(path):
    assert path.startswith(CACHEABLE_PREFIXES)


@pytest.mark.parametrize("path", ["/", "/dashboard", "/login", "/api/anything",
                                  "/accounts/1/chat", "/staticky"])
def test_dynamic_paths_not_treated_as_static(path):
    """/staticky 这种前缀相近的必须仍然按动态处理。"""
    assert not path.startswith(CACHEABLE_PREFIXES)


# ── _rewrite 的细节 ───────────────────────────────────────────────────

def test_rewrite_replaces_existing_cache_control():
    out = _rewrite([(b"cache-control", b"public, max-age=2592000")])
    vals = [v for k, v in out if k == b"cache-control"]
    assert len(vals) == 1
    assert b"no-store" in vals[0]
    assert b"2592000" not in vals[0]


def test_rewrite_appends_to_existing_vary():
    """不能覆盖 Accept-Encoding —— 覆盖了会让压缩/非压缩版本混用。"""
    out = _rewrite([(b"vary", b"Accept-Encoding")])
    vary = next(v for k, v in out if k == b"vary")
    assert b"Accept-Encoding" in vary
    assert b"Cookie" in vary


def test_rewrite_does_not_duplicate_cookie_in_vary():
    out = _rewrite([(b"vary", b"Cookie, Accept-Encoding")])
    vary = next(v for k, v in out if k == b"vary")
    assert vary.lower().count(b"cookie") == 1


def test_rewrite_keeps_other_headers():
    out = _rewrite([(b"content-type", b"text/html"),
                    (b"set-cookie", b"csrf=abc; Path=/")])
    assert (b"content-type", b"text/html") in out
    assert (b"set-cookie", b"csrf=abc; Path=/") in out


def test_rewrite_drops_stale_pragma_and_expires():
    out = _rewrite([(b"pragma", b"public"),
                    (b"expires", b"Thu, 01 Jan 2099 00:00:00 GMT")])
    assert (b"pragma", b"no-cache") in out
    assert (b"expires", b"0") in out
    assert not any(v == b"public" for k, v in out if k == b"pragma")


def test_rewrite_is_idempotent():
    once = _rewrite([])
    twice = _rewrite(once)
    assert sorted(once) == sorted(twice)


# ── 中间件真的挂上了 ──────────────────────────────────────────────────

def test_middleware_is_registered():
    import app.main as main_mod
    from app.cache_mw import NoStoreMiddleware
    assert any(m.cls is NoStoreMiddleware for m in main_mod.app.user_middleware), \
        "NoStoreMiddleware 没挂上，等于这一层防护不存在"


def test_middleware_wraps_csrf():
    """必须比 CSRF 更靠外，否则 CSRF 自己返回的 403 页面不会被盖到。

    Starlette 的 user_middleware 里，**后 add 的排在前面**（更外层）。
    """
    import app.main as main_mod
    from app.cache_mw import NoStoreMiddleware
    from app.csrf_mw import CSRFMiddleware

    names = [m.cls for m in main_mod.app.user_middleware]
    assert names.index(NoStoreMiddleware) < names.index(CSRFMiddleware)
