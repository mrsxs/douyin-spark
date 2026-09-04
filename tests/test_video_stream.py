"""聊天气泡里的原生播放：直链挑档 + 反代拉流 + 端点鉴权。

Why 要反代而不是把直链甩给浏览器：
  1. 直链带时效签名（实测约两天），塞进页面等于发一张会过期的票；
  2. CDN 认 Referer，浏览器这边不好稳定伪造；
  3. 直链一旦进了 HTML，用户复制粘贴就把账号能看到的内容散出去了。
走自己的端点，鉴权和归属校验就都还在。

夹具全是合成 JSON：真实响应含真实 uid / 作品，按安全红线不进 git。
"""
import json
from datetime import datetime, timedelta

import pytest

import douyin_im as dy
from app import video_service
from app.models import DouyinAccount, User
from app.security import hash_password

AWEME_ID = "7600000000000000001"
CDN = "https://v26-web.douyinvod.com/abc/6a994d5d/video/tos/cn/x"


def _bitrate(gear, br, fmt="mp4", url=None, height=1080):
    return {"gear_name": gear, "bit_rate": br, "format": fmt,
            "play_addr": {"width": 1920, "height": height,
                          "data_size": br * 10,
                          "url_list": [url or f"{CDN}?gear={gear}"]}}


def _video(**over):
    v = {
        "play_addr": {"url_list": [f"{CDN}?src=play_addr"]},
        "play_addr_h264": {"url_list": [f"{CDN}?src=h264"]},
        "bit_rate": [
            _bitrate("normal_1080_0", 2234705, height=1080),
            _bitrate("normal_1080_dash", 2168981, fmt="dash", height=1080),
            _bitrate("normal_540_0", 1636627, height=540),
            _bitrate("540_4_1", 349994, height=540),
        ],
    }
    v.update(over)
    return v


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class _Session:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.urls: list[str] = []
        self.headers_seen: list[dict] = []

    def get(self, url, **kw):
        self.urls.append(url)
        self.headers_seen.append(kw.get("headers") or {})
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def no_sign(monkeypatch):
    def _fake(params):
        from urllib.parse import urlencode
        return urlencode(dict(params, a_bogus="FAKE"))
    monkeypatch.setattr(dy, "_sign_params", _fake)


@pytest.fixture(autouse=True)
def clear_play_cache():
    video_service._play_cache.clear()
    yield
    video_service._play_cache.clear()


# ── detail 的 403 重试 ────────────────────────────────────────
# 实测：ArgusSecurityPlugin 以「Uifid Not Found」挡下请求（403 纯文本），
# 同一个 session 连打 6 个视频，分别在第 1/2/3/4 次才过 —— 每个新建 session
# 都要靠重试「热」起来。服务端每个请求都新建 session，所以每次都撞得上；
# 重试不够就是随机失败，AI 回复分享视频会时灵时不灵。

@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """重试退避在单测里没意义，只会让整套慢下来。"""
    monkeypatch.setattr(dy.time, "sleep", lambda *_: None)


@pytest.mark.parametrize("blocked", [1, 2, 3, dy._DETAIL_MAX_TRIES - 1])
def test_detail_retries_through_403(no_sign, blocked):
    s = _Session(*([_Resp("Blocked by ArgusSecurityPlugin Uifid Not Found",
                          status=403)] * blocked),
                 _Resp({"status_code": 0, "aweme_detail": {"aweme_id": AWEME_ID,
                                                           "desc": "过了"}}))
    out = dy.fetch_aweme_detail(s, AWEME_ID)

    assert out["desc"] == "过了"
    assert len(s.urls) == blocked + 1


def test_detail_gives_up_after_repeated_403(no_sign):
    s = _Session(*([_Resp("Blocked", status=403)] * dy._DETAIL_MAX_TRIES))
    assert dy.fetch_aweme_detail(s, AWEME_ID) == {}
    assert len(s.urls) == dy._DETAIL_MAX_TRIES, "不该无限重试 —— 那是在给风控喂料"


def test_detail_does_not_retry_on_business_error(no_sign):
    """业务错误重试也是同样的结果，白送一次风控额度。"""
    s = _Session(_Resp({"status_code": 8, "aweme_detail": None}))
    assert dy.fetch_aweme_detail(s, AWEME_ID) == {}
    assert len(s.urls) == 1


# ── 挑档 ──────────────────────────────────────────────────────

def test_play_url_picks_smallest_mp4_gear(no_sign):
    """1080p 一条 60MB，聊天气泡里没人需要 —— 起播快比清晰重要。"""
    s = _Session(_Resp({"status_code": 0, "aweme_detail": {"video": _video()}}))
    assert dy.fetch_aweme_play_url(s, AWEME_ID) == f"{CDN}?gear=540_4_1"


def test_play_url_skips_dash_gears(no_sign):
    """dash 是分片清单，<video src> 直接喂会播不出来。"""
    v = _video(bit_rate=[_bitrate("dash_low", 1000, fmt="dash"),
                         _bitrate("mp4_high", 9999, fmt="mp4")])
    s = _Session(_Resp({"status_code": 0, "aweme_detail": {"video": v}}))
    assert dy.fetch_aweme_play_url(s, AWEME_ID) == f"{CDN}?gear=mp4_high"


def test_play_url_falls_back_to_play_addr(no_sign):
    """没有 bit_rate 时退回 play_addr —— 有的老作品就是不给分档。"""
    v = _video(bit_rate=[])
    s = _Session(_Resp({"status_code": 0, "aweme_detail": {"video": v}}))
    assert dy.fetch_aweme_play_url(s, AWEME_ID) == f"{CDN}?src=play_addr"


def test_play_url_rejects_foreign_host(no_sign):
    """直链进的是带登录态的 session，域名白名单是挡 SSRF 的唯一一道。"""
    v = _video(bit_rate=[_bitrate("evil", 1, url="https://evil.com/x.mp4")],
               play_addr={"url_list": ["https://evil.com/y.mp4"]},
               play_addr_h264={"url_list": []})
    s = _Session(_Resp({"status_code": 0, "aweme_detail": {"video": v}}))
    assert dy.fetch_aweme_play_url(s, AWEME_ID) == ""


@pytest.mark.parametrize("bad", ["", "abc", "1' OR 1=1", "../../etc/passwd"])
def test_play_url_rejects_bad_id_without_requesting(no_sign, bad):
    s = _Session()                      # 真发请求就 IndexError
    assert dy.fetch_aweme_play_url(s, bad) == ""
    assert s.urls == []


def test_play_url_swallows_network_error(no_sign):
    s = _Session(RuntimeError("connection reset"))
    assert dy.fetch_aweme_play_url(s, AWEME_ID) == ""


def test_play_url_survives_weird_field_types(no_sign):
    v = {"bit_rate": "oops", "play_addr": None, "play_addr_h264": {"url_list": "nope"}}
    s = _Session(_Resp({"status_code": 0, "aweme_detail": {"video": v}}))
    assert dy.fetch_aweme_play_url(s, AWEME_ID) == ""


# ── 拉流 ──────────────────────────────────────────────────────

class _StreamResp:
    def __init__(self, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size=1):
        yield b"\x00" * 8

    def close(self):
        self.closed = True


def test_stream_passes_range_and_referer_through():
    up = _StreamResp(206, {"Content-Range": "bytes 0-1023/9402784"})
    s = _Session(up)
    out = dy.open_video_stream(s, f"{CDN}?gear=540", "bytes=0-1023")

    assert out is up
    h = s.headers_seen[0]
    assert h["Range"] == "bytes=0-1023"
    assert h["Referer"].startswith("https://www.douyin.com")


def test_stream_without_range_sends_no_range_header():
    s = _Session(_StreamResp(200))
    dy.open_video_stream(s, f"{CDN}?gear=540")
    assert "Range" not in s.headers_seen[0]


@pytest.mark.parametrize("bad_range", [
    "bytes=0-1023\r\nX-Evil: 1",     # 头注入
    "bytes=abc-def",
    "items=0-10",
    "bytes=" + "9" * 60,             # 长得离谱，不转发
])
def test_stream_drops_malformed_range(bad_range):
    """客户端给的 Range 是外部输入，原样转发等于把注入面递给上游。"""
    s = _Session(_StreamResp(200))
    dy.open_video_stream(s, f"{CDN}?gear=540", bad_range)
    assert "Range" not in s.headers_seen[0]


@pytest.mark.parametrize("url", ["https://evil.com/x.mp4", "", None,
                                 "http://v26-web.douyinvod.com/x.mp4",   # 明文 http
                                 "https://douyinvod.com.evil.com/x.mp4"])
def test_stream_rejects_non_douyin_url(url):
    s = _Session()                      # 真发请求就 IndexError
    assert dy.open_video_stream(s, url) is None
    assert s.urls == []


def test_stream_returns_none_on_upstream_error():
    up = _StreamResp(403)
    s = _Session(up)
    assert dy.open_video_stream(s, f"{CDN}?gear=540") is None
    assert up.closed, "失败也要把连接关掉，否则连接池会漏"


# ── play_url 的短 TTL 缓存 ────────────────────────────────────

def test_play_url_is_cached(monkeypatch):
    """同一条视频反复点播（拖进度条会重开好几次流），不该每次都问抖音一遍。"""
    calls = {"n": 0}

    def _fetch(_s, vid):
        calls["n"] += 1
        return f"{CDN}?gear=540"

    monkeypatch.setattr(video_service.dy, "fetch_aweme_play_url", _fetch)
    s = object()
    assert video_service.play_url(s, AWEME_ID) == f"{CDN}?gear=540"
    assert video_service.play_url(s, AWEME_ID) == f"{CDN}?gear=540"
    assert calls["n"] == 1


def test_play_url_refetches_after_ttl(monkeypatch):
    """直链带时效签名，缓存过久会在用户点播的瞬间正好 403。"""
    calls = {"n": 0}

    def _fetch(_s, vid):
        calls["n"] += 1
        return f"{CDN}?n={calls['n']}"

    monkeypatch.setattr(video_service.dy, "fetch_aweme_play_url", _fetch)
    s = object()
    video_service.play_url(s, AWEME_ID)
    stale = datetime.utcnow() - video_service.PLAY_URL_TTL - timedelta(seconds=1)
    video_service._play_cache[AWEME_ID] = (f"{CDN}?n=1", stale)

    assert video_service.play_url(s, AWEME_ID) == f"{CDN}?n=2"
    assert calls["n"] == 2


def test_play_url_does_not_cache_failure(monkeypatch):
    calls = {"n": 0}

    def _fetch(_s, vid):
        calls["n"] += 1
        return ""

    monkeypatch.setattr(video_service.dy, "fetch_aweme_play_url", _fetch)
    s = object()
    video_service.play_url(s, AWEME_ID)
    video_service.play_url(s, AWEME_ID)
    assert calls["n"] == 2


def test_play_cache_does_not_grow_without_bound(monkeypatch):
    monkeypatch.setattr(video_service.dy, "fetch_aweme_play_url",
                        lambda _s, vid: f"{CDN}?v={vid}")
    for i in range(video_service.PLAY_CACHE_MAX + 50):
        video_service.play_url(object(), str(7600000000000000000 + i))
    assert len(video_service._play_cache) <= video_service.PLAY_CACHE_MAX


# ── 端点 ──────────────────────────────────────────────────────

@pytest.fixture
def acc(db):
    u = User(username="vidstream", password_hash=hash_password("pw123456"),
             max_accounts=5)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="主号", status="active", cookies_exist=True)
    db.add(a); db.commit(); db.refresh(a)
    return u, a


@pytest.fixture
def stub_stream(monkeypatch):
    from app.routers import api as api_mod
    monkeypatch.setattr(api_mod, "_account_session_for", lambda *a: object())
    monkeypatch.setattr(api_mod.video_service, "play_url",
                        lambda *a: f"{CDN}?gear=540")
    up = _StreamResp(206, {"Content-Range": "bytes 0-7/9402784",
                           "Content-Length": "8",
                           "Content-Type": "video/mp4"})
    monkeypatch.setattr(api_mod.dy, "open_video_stream", lambda *a, **k: up)
    return up


def _url(acc_id, vid=AWEME_ID):
    return f"/api/videos/{acc_id}/{vid}/stream"


def test_streams_video_bytes(client, login, db, acc, stub_stream):
    u, a = acc
    r = login(u).get(_url(a.id))

    assert r.status_code == 206
    assert r.content == b"\x00" * 8
    assert r.headers["content-type"].startswith("video/mp4")
    assert r.headers["content-range"] == "bytes 0-7/9402784"
    assert r.headers["accept-ranges"] == "bytes"


def test_stream_requires_login(client, db, acc, stub_stream):
    _u, a = acc
    r = client.get(_url(a.id))
    assert r.status_code in (401, 403, 302)


def test_cannot_stream_from_foreign_account(client, login, db, acc, stub_stream):
    u, _a = acc
    other = User(username="vidintruder", password_hash=hash_password("pw123456"),
                 max_accounts=5)
    db.add(other); db.commit(); db.refresh(other)
    foreign = DouyinAccount(user_id=other.id, label="别人的号", status="active")
    db.add(foreign); db.commit(); db.refresh(foreign)

    r = login(u).get(_url(foreign.id))
    assert r.status_code == 404


@pytest.mark.parametrize("bad", ["abc", "123", "1'%20OR%201=1"])
def test_bad_aweme_id_is_rejected(client, login, db, acc, stub_stream, bad):
    u, a = acc
    r = login(u).get(_url(a.id, bad))
    assert r.status_code == 404


def test_missing_play_url_is_404(client, login, db, acc, monkeypatch, stub_stream):
    from app.routers import api as api_mod
    monkeypatch.setattr(api_mod.video_service, "play_url", lambda *a: "")
    u, a = acc
    r = login(u).get(_url(a.id))
    assert r.status_code == 404


def test_logged_out_account_is_reported(client, login, db, acc, monkeypatch,
                                        stub_stream):
    from app.routers import api as api_mod
    monkeypatch.setattr(api_mod, "_account_session_for", lambda *a: None)
    u, a = acc
    r = login(u).get(_url(a.id))
    assert r.status_code in (409, 404, 502)


def test_signed_url_never_reaches_the_client(client, login, db, acc, stub_stream):
    """直链不进响应 —— 它是一张能被随手转走的票。"""
    u, a = acc
    r = login(u).get(_url(a.id))
    assert "douyinvod.com" not in r.text
    assert "douyinvod.com" not in json.dumps(dict(r.headers))


# ── 多段 Range ────────────────────────────────────────────────
# RFC 7233 允许一次要多段（`bytes=0-99,200-299`），上游回 206 +
# multipart/byteranges。原来一律丢掉整个 Range 头退回 200 全量，
# 对要多段的客户端来说等于「你要 200 字节，我给你 9MB」。

@pytest.mark.parametrize("rng", ["bytes=0-99,200-299",
                                 "bytes=0-99, 200-299",
                                 "bytes=0-99,200-299,400-499"])
def test_stream_passes_multi_range_through(rng):
    s = _Session(_StreamResp(206, {"Content-Type": "multipart/byteranges"}))
    dy.open_video_stream(s, f"{CDN}?gear=540", rng)
    assert s.headers_seen[0]["Range"] == rng


def test_stream_caps_multi_range_count():
    """段数没有上限的话，一个请求就能让上游拼出个超大响应。"""
    s = _Session(_StreamResp(200))
    dy.open_video_stream(s, f"{CDN}?gear=540", "bytes=" + ",".join(
        f"{i}-{i + 1}" for i in range(50)))
    assert "Range" not in s.headers_seen[0]


def test_multi_range_still_rejects_injection():
    s = _Session(_StreamResp(200))
    dy.open_video_stream(s, f"{CDN}?gear=540", "bytes=0-99,200-299\r\nX-Evil: 1")
    assert "Range" not in s.headers_seen[0]


def test_upstream_content_type_is_forwarded(client, login, db, acc, monkeypatch):
    """多段响应是 multipart/byteranges，写死 video/mp4 会让浏览器解不开。"""
    from app.routers import api as api_mod
    up = _StreamResp(206, {"Content-Type": "multipart/byteranges; boundary=x"})
    monkeypatch.setattr(api_mod, "_account_session_for", lambda *a: object())
    monkeypatch.setattr(api_mod.video_service, "play_url", lambda *a: f"{CDN}?g=1")
    monkeypatch.setattr(api_mod.dy, "open_video_stream", lambda *a, **k: up)

    u, a = acc
    r = login(u).get(_url(a.id))
    assert r.headers["content-type"].startswith("multipart/byteranges")
