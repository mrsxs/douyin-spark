"""短信登录 API：号码校验、并发上限、状态回收。

codex 指出：登录状态永久留在内存没有过期清理，短信登录启动缺锁和
全局 Chromium 上限 —— 连续请求可能拉起大量浏览器进程把内存吃光。
"""
import time

import pytest

from app.models import DouyinAccount, User
from app.routers import login_flow as lf
from app.security import hash_password


@pytest.fixture(autouse=True)
def _clear_state():
    lf.LOGIN_STATE.clear()
    yield
    lf.LOGIN_STATE.clear()


@pytest.fixture
def acc(db):
    u = User(username="smsuser", password_hash=hash_password("x"),
             max_accounts=3)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="主号", status="pending_login")
    db.add(a); db.commit(); db.refresh(a)
    return u, a


def _post(client, url, payload):
    client.get("/login")
    tok = client.cookies.get("csrf", "")
    return client.post(url, json=payload, headers={"X-CSRF-Token": tok})


# ── 号码校验前置 ─────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", "123", "abc", "28812345678"])
def test_bad_mobile_rejected_without_launching_browser(acc, login, monkeypatch, bad):
    """号码不对就别开浏览器空跑 20 秒。"""
    launched = {"n": 0}
    monkeypatch.setattr(lf.threading, "Thread",
                        lambda *a, **k: launched.__setitem__("n", launched["n"] + 1))
    u, a = acc
    r = _post(login(u), "/api/login/sms/send-code",
              {"account_id": a.id, "mobile": bad})
    assert r.json()["ok"] is False
    assert launched["n"] == 0, "非法号码仍然启动了浏览器"


def test_valid_mobile_starts_one_browser(acc, login, monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, *a, **k): started.append(k.get("args"))
        def start(self): pass

    monkeypatch.setattr(lf.threading, "Thread", FakeThread)
    u, a = acc
    r = _post(login(u), "/api/login/sms/send-code",
              {"account_id": a.id, "mobile": "188 1234 5678"})
    assert r.json()["ok"] is True
    assert len(started) == 1
    assert started[0][2] == "18812345678", "号码没被规范化"


# ── 重复点击不重复起浏览器 ───────────────────────────────────────

def test_double_click_does_not_launch_twice(acc, login, monkeypatch):
    """核心回归：双击「发送验证码」原本会起两个 Chromium。"""
    count = {"n": 0}

    class FakeThread:
        def __init__(self, *a, **k): count["n"] += 1
        def start(self): pass

    monkeypatch.setattr(lf.threading, "Thread", FakeThread)
    u, a = acc
    c = login(u)
    _post(c, "/api/login/sms/send-code", {"account_id": a.id, "mobile": "18812345678"})
    r2 = _post(c, "/api/login/sms/send-code", {"account_id": a.id, "mobile": "18812345678"})
    assert count["n"] == 1, "重复请求又起了一个浏览器"
    assert r2.json().get("already") is True


# ── 并发上限 ─────────────────────────────────────────────────────

def test_browser_slots_capped(acc, login, monkeypatch):
    class FakeThread:
        def __init__(self, *a, **k): pass
        def start(self): pass
    monkeypatch.setattr(lf.threading, "Thread", FakeThread)

    # 占满其它用户的槽位
    for i in range(lf.MAX_CONCURRENT_BROWSERS):
        lf.LOGIN_STATE[(900 + i, 900 + i)] = {
            "status": "waiting_sms_code", "started_at": time.time()}

    u, a = acc
    r = _post(login(u), "/api/login/sms/send-code",
              {"account_id": a.id, "mobile": "18812345678"})
    body = r.json()
    assert body["ok"] is False
    assert "过多" in body["error"]


def test_stale_states_are_pruned(acc, login, monkeypatch):
    """老状态要能被回收，否则上限会被永久占死。"""
    class FakeThread:
        def __init__(self, *a, **k): pass
        def start(self): pass
    monkeypatch.setattr(lf.threading, "Thread", FakeThread)

    old = time.time() - lf.STATE_TTL_SECONDS - 60
    for i in range(lf.MAX_CONCURRENT_BROWSERS):
        lf.LOGIN_STATE[(900 + i, 900 + i)] = {
            "status": "waiting_sms_code", "started_at": old}

    u, a = acc
    r = _post(login(u), "/api/login/sms/send-code",
              {"account_id": a.id, "mobile": "18812345678"})
    assert r.json()["ok"] is True, "过期状态没被回收，新登录被误挡"
    assert (900, 900) not in lf.LOGIN_STATE


# ── 越权 ─────────────────────────────────────────────────────────

def test_cannot_start_login_for_foreign_account(acc, login, db):
    u, _ = acc
    other = User(username="stranger9", password_hash="x")
    db.add(other); db.commit(); db.refresh(other)
    foreign = DouyinAccount(user_id=other.id, label="别人的", status="pending_login")
    db.add(foreign); db.commit(); db.refresh(foreign)

    r = _post(login(u), "/api/login/sms/send-code",
              {"account_id": foreign.id, "mobile": "18812345678"})
    assert r.status_code == 404
