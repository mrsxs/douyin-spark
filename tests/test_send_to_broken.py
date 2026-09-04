"""两个账户级发送开关：

- Schedule.send_to_broken   —— 火花已断(broken)的人。**默认开**：
  断了的火花直接发消息就能续上，跳过他们等于白白少续一批人。
- Schedule.send_to_no_spark —— 从来没有火花(none)的普通好友。**默认关**：
  那是主动去骚扰没在互动的人，风控面和用户预期都不一样，得显式选。
"""

import pytest

from app import trigger
from app.models import DouyinAccount, JobRun, Schedule, User
from app.security import hash_password


@pytest.fixture
def acc(db):
    u = User(username="brokenuser", password_hash=hash_password("pw123456"),
             max_accounts=3)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="主号", status="active",
                      cookies_exist=True)
    db.add(a); db.commit(); db.refresh(a)
    return u, a


CONTACTS = [
    {"uid": "111", "nickname": "在烧", "conv_id": "c1", "days": 30, "status": "active"},
    {"uid": "222", "nickname": "已断", "conv_id": "c2", "days": 5, "status": "broken"},
    {"uid": "333", "nickname": "没火花", "conv_id": "c3", "days": 0, "status": "none"},
]


@pytest.fixture
def fake_douyin(monkeypatch):
    """拦掉网络：_ensure_active 直接给三种状态的联系人，send_text 记账不发。"""
    sent_uids: list[str] = []

    monkeypatch.setattr(trigger, "_ensure_active",
                        lambda ctx: ({}, {}, [dict(c) for c in CONTACTS]))
    monkeypatch.setattr(trigger, "send_interval_range",
                        lambda account_id: (0.0, 0.0, "test"))
    monkeypatch.setattr(trigger.dy, "_log", lambda *a, **k: None)
    monkeypatch.setattr(trigger.dy, "get_last_send_info", lambda: {"msg": "OK"})
    monkeypatch.setattr(trigger.dy, "_pick_message",
                        lambda uid, name, tpl: "早",
                        raising=False)

    def _send(session, conv_id, text, contact, constants):
        sent_uids.append(contact["uid"])
        return True

    monkeypatch.setattr(trigger.dy, "send_text", _send)
    monkeypatch.setattr(trigger.time, "sleep", lambda *_: None)
    return sent_uids


def _sch(db, acc_id, **kw):
    sch = Schedule(douyin_account_id=acc_id, **kw)
    db.add(sch); db.commit(); db.refresh(sch)
    return sch


# ── 默认值 ───────────────────────────────────────────────────────

def test_broken_defaults_on(db, acc):
    """断了的火花直接发就能续，默认不该跳过。"""
    _, a = acc
    assert _sch(db, a.id).send_to_broken is True


def test_no_spark_defaults_off(db, acc):
    """没火花的普通好友是主动搭讪，得用户显式打开。"""
    _, a = acc
    assert _sch(db, a.id).send_to_no_spark is False


# ── 发送筛选 ─────────────────────────────────────────────────────

def test_defaults_send_active_and_broken_only(db, acc, fake_douyin):
    u, a = acc
    _sch(db, a.id)

    summary = trigger.auto_run(u.id, a.id, triggered_by="test")

    assert set(fake_douyin) == {"111", "222"}
    assert summary["sent"] == 2
    assert summary["skipped"] == 1        # 只跳过没火花的 333


def test_no_schedule_row_uses_same_defaults(db, acc, fake_douyin):
    """从没配过定时的账户也走同一套默认，不能悄悄换一套行为。"""
    u, a = acc
    trigger.auto_run(u.id, a.id, triggered_by="test")
    assert set(fake_douyin) == {"111", "222"}


def test_broken_off_skips_broken(db, acc, fake_douyin):
    u, a = acc
    _sch(db, a.id, send_to_broken=False)

    summary = trigger.auto_run(u.id, a.id, triggered_by="test")

    assert fake_douyin == ["111"]
    assert summary["skipped"] == 2


def test_no_spark_on_sends_everyone(db, acc, fake_douyin):
    u, a = acc
    _sch(db, a.id, send_to_broken=True, send_to_no_spark=True)

    summary = trigger.auto_run(u.id, a.id, triggered_by="test")

    assert set(fake_douyin) == {"111", "222", "333"}
    assert summary["skipped"] == 0


def test_two_switches_are_independent(db, acc, fake_douyin):
    """只开无火花、关掉已断 —— 两个开关不能互相绑架。"""
    u, a = acc
    _sch(db, a.id, send_to_broken=False, send_to_no_spark=True)

    trigger.auto_run(u.id, a.id, triggered_by="test")
    assert set(fake_douyin) == {"111", "333"}


def test_total_counts_only_what_gets_sent(db, acc, fake_douyin):
    """进度条分母要跟实际发送集合一致，否则百分比永远到不了 100%。"""
    u, a = acc
    _sch(db, a.id)
    trigger.auto_run(u.id, a.id, triggered_by="test")

    run = db.query(JobRun).filter(JobRun.douyin_account_id == a.id).first()
    db.refresh(run)
    assert run.total == 2
    assert run.skipped == 1


# ── HTTP 契约 ────────────────────────────────────────────────────

def _csrf(client):
    client.get("/login")
    return client.cookies.get("csrf", "")


def test_get_schedule_exposes_both_flags(db, acc, login):
    u, a = acc
    _sch(db, a.id, send_to_broken=False, send_to_no_spark=True)

    body = login(u).get(f"/api/schedule/{a.id}").json()
    assert body["send_to_broken"] is False
    assert body["send_to_no_spark"] is True


def test_get_schedule_without_row_reports_defaults(db, acc, login):
    u, a = acc
    body = login(u).get(f"/api/schedule/{a.id}").json()
    assert body["send_to_broken"] is True
    assert body["send_to_no_spark"] is False


def test_put_schedule_persists_both_flags(db, acc, login):
    u, a = acc
    c = login(u)
    r = c.put(f"/api/schedule/{a.id}",
              json={"enabled": True, "time": "09:00",
                    "send_to_broken": False, "send_to_no_spark": True},
              headers={"X-CSRF-Token": _csrf(c)})
    assert r.status_code == 200, r.text

    sch = db.query(Schedule).filter(Schedule.douyin_account_id == a.id).first()
    db.refresh(sch)
    assert sch.send_to_broken is False
    assert sch.send_to_no_spark is True


def test_put_schedule_without_flags_keeps_current(db, acc, login):
    """老前端不带这两个字段时不能把用户的选择悄悄改掉。"""
    u, a = acc
    _sch(db, a.id, send_to_broken=False, send_to_no_spark=True)

    c = login(u)
    c.put(f"/api/schedule/{a.id}", json={"enabled": True, "time": "10:00"},
          headers={"X-CSRF-Token": _csrf(c)})

    sch = db.query(Schedule).filter(Schedule.douyin_account_id == a.id).first()
    db.refresh(sch)
    assert sch.send_to_broken is False
    assert sch.send_to_no_spark is True


# ── 重燃中（recovering）─────────────────────────────────────────────

RECOVER = {"uid": "444", "nickname": "重燃中", "conv_id": "c4", "days": 543,
           "status": "recovering", "recover_days": 2, "recover_need_days": 3}


@pytest.fixture
def fake_with_recovering(monkeypatch):
    sent = []
    monkeypatch.setattr(trigger, "_ensure_active",
                        lambda ctx: ({}, {}, [dict(c) for c in CONTACTS] + [dict(RECOVER)]))
    monkeypatch.setattr(trigger, "send_interval_range",
                        lambda account_id: (0.0, 0.0, "test"))
    monkeypatch.setattr(trigger.dy, "_log", lambda *a, **k: None)
    monkeypatch.setattr(trigger.dy, "get_last_send_info", lambda: {"msg": "OK"})
    monkeypatch.setattr(trigger.dy, "_pick_message",
                        lambda uid, name, tpl: "早", raising=False)
    monkeypatch.setattr(trigger.dy, "send_text",
                        lambda s, cid, t, c, k: sent.append(c["uid"]) or True)
    monkeypatch.setattr(trigger.time, "sleep", lambda *_: None)
    return sent


def test_recovering_is_always_sent(db, acc, fake_with_recovering):
    """重燃中的人任何开关组合下都要发 —— 窗口过了 543 天就真没了，
    这是所有联系人里最紧急的一类，不该被任何开关挡住。"""
    u, a = acc
    _sch(db, a.id, send_to_broken=False, send_to_no_spark=False)

    trigger.auto_run(u.id, a.id, triggered_by="test")

    assert "444" in fake_with_recovering, "重燃中的人被开关挡掉了"


def test_recovering_goes_first(db, acc, fake_with_recovering):
    """重燃中排在最前面发：任务被熔断/中断时，先保住最急的那批。"""
    u, a = acc
    _sch(db, a.id)
    trigger.auto_run(u.id, a.id, triggered_by="test")
    assert fake_with_recovering[0] == "444", \
        f"发送顺序 {fake_with_recovering}，重燃中的没排在最前"
