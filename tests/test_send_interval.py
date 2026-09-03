"""发消息间隔：每个账号自己设最小/最大。

Why 要可调：3 秒一条对老号可能过快，20 秒一条对小号又太磨叽 —— 合适的节奏
只有用号的人知道。但**自适应降速必须保留**：连续失败说明已经在被风控盯着，
这时候还按用户设的快节奏发，是把号往里送。所以用户设的是**基准区间**，
风控倍数在它之上叠加。

默认值 4.5~5.5 秒是刻意的：改这个功能之前的行为就是 `5.0 ± random(-0.5, 0.5)`，
默认值原样复刻它 —— 升级不该悄悄改变谁的发送节奏。
"""
from datetime import datetime, timedelta

import pytest

from app import trigger
from app.models import DouyinAccount, JobRun, Schedule, User
from app.security import hash_password


@pytest.fixture
def acc(db):
    u = User(username="intervaluser", password_hash=hash_password("pw123456"),
             expires_at=datetime.utcnow() + timedelta(days=30), max_accounts=5)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="主号", status="active", cookies_exist=True)
    db.add(a); db.commit(); db.refresh(a)
    return u, a


def _runs(db, account_id, sent, failed, n=1):
    for _ in range(n):
        db.add(JobRun(douyin_account_id=account_id, kind="auto",
                      triggered_by="scheduler", status="done",
                      started_at=datetime.utcnow(), sent=sent, failed=failed))
    db.commit()


# ── 默认值 ────────────────────────────────────────────────────

def test_default_range_reproduces_old_behaviour(db, acc):
    """老行为是 5.0 ± 0.5，默认值必须原样复刻，别悄悄改快或改慢。"""
    _u, a = acc
    lo, hi, risk = trigger.send_interval_range(a.id)

    assert (lo, hi) == (trigger.SEND_MIN_DEFAULT, trigger.SEND_MAX_DEFAULT)
    assert (lo, hi) == (4.5, 5.5)
    assert risk == "normal"


def test_account_without_schedule_row_still_works(db, acc):
    """还没建过定时的账号也要能发 —— 不能因为查不到配置就崩。"""
    _u, a = acc
    assert trigger.send_interval_range(a.id)[:2] == (4.5, 5.5)


# ── 用户设的值 ────────────────────────────────────────────────

def test_uses_configured_range(db, acc):
    _u, a = acc
    db.add(Schedule(douyin_account_id=a.id, send_min_sec=8, send_max_sec=20))
    db.commit()

    lo, hi, _risk = trigger.send_interval_range(a.id)
    assert (lo, hi) == (8, 20)


def test_risk_multiplier_stacks_on_top_of_user_range(db, acc):
    """连续失败 = 已经被盯上了，这时候还按用户设的快节奏发是送人头。"""
    _u, a = acc
    db.add(Schedule(douyin_account_id=a.id, send_min_sec=3, send_max_sec=6))
    db.commit()
    _runs(db, a.id, sent=1, failed=9)        # 90% 失败

    lo, hi, risk = trigger.send_interval_range(a.id)
    assert (lo, hi) == (36, 72), "高风险时没有按 12 倍降速"
    assert "high_risk" in risk


def test_medium_risk_triples_the_range(db, acc):
    _u, a = acc
    db.add(Schedule(douyin_account_id=a.id, send_min_sec=4, send_max_sec=10))
    db.commit()
    _runs(db, a.id, sent=7, failed=3)        # 30% 失败

    lo, hi, risk = trigger.send_interval_range(a.id)
    assert (lo, hi) == (12, 30)
    assert "medium_risk" in risk


# ── 坏数据兜底 ────────────────────────────────────────────────

@pytest.mark.parametrize("lo,hi", [
    (0, 5),          # 0 秒 = 连发，风控直接找上门
    (-3, 5),
    (20, 5),         # min > max
    (None, None),
])
def test_broken_stored_values_fall_back_to_defaults(db, acc, lo, hi):
    """库里的值可能是老数据/手改过的。宁可退回默认，也不能按 0 秒连发。"""
    _u, a = acc
    db.add(Schedule(douyin_account_id=a.id, send_min_sec=lo, send_max_sec=hi))
    db.commit()

    got = trigger.send_interval_range(a.id)[:2]
    assert got == (trigger.SEND_MIN_DEFAULT, trigger.SEND_MAX_DEFAULT)


def test_stored_value_above_ceiling_is_clamped(db, acc):
    _u, a = acc
    db.add(Schedule(douyin_account_id=a.id, send_min_sec=5, send_max_sec=99999))
    db.commit()
    assert trigger.send_interval_range(a.id)[1] == trigger.SEND_MAX_CEIL


# ── 接口 ──────────────────────────────────────────────────────

def _csrf(client):
    client.get("/login")
    return {"X-CSRF-Token": client.cookies.get("csrf", "")}


def test_get_returns_the_range(db, acc, client, login):
    u, a = acc
    db.add(Schedule(douyin_account_id=a.id, send_min_sec=6, send_max_sec=12))
    db.commit()

    body = login(u).get(f"/api/schedule/{a.id}").json()
    assert body["send_min_sec"] == 6
    assert body["send_max_sec"] == 12


def test_get_without_schedule_row_returns_defaults(db, acc, client, login):
    u, a = acc
    body = login(u).get(f"/api/schedule/{a.id}").json()
    assert body["send_min_sec"] == trigger.SEND_MIN_DEFAULT
    assert body["send_max_sec"] == trigger.SEND_MAX_DEFAULT


def test_put_saves_the_range(db, acc, client, login):
    u, a = acc
    c = login(u)
    r = c.put(f"/api/schedule/{a.id}",
              json={"enabled": False, "time": "09:00",
                    "send_min_sec": 7, "send_max_sec": 15},
              headers=_csrf(c))

    assert r.json()["ok"] is True
    sch = db.query(Schedule).filter_by(douyin_account_id=a.id).one()
    assert (sch.send_min_sec, sch.send_max_sec) == (7, 15)


@pytest.mark.parametrize("payload,hint", [
    ({"send_min_sec": 0.2, "send_max_sec": 5}, "太快"),
    ({"send_min_sec": 20, "send_max_sec": 5}, "不能大于"),
    ({"send_min_sec": 5, "send_max_sec": 99999}, "太慢"),
    ({"send_min_sec": "abc", "send_max_sec": 5}, "数字"),
])
def test_put_rejects_bad_ranges_with_a_reason(db, acc, client, login,
                                              payload, hint):
    u, a = acc
    c = login(u)
    r = c.put(f"/api/schedule/{a.id}",
              json={"enabled": False, "time": "09:00", **payload},
              headers=_csrf(c))

    body = r.json()
    assert body["ok"] is False
    assert hint in body["error"], body["error"]


def test_put_without_the_fields_keeps_the_stored_range(db, acc, client, login):
    """老前端不带这两个字段，不能把用户设过的值悄悄改回默认。"""
    u, a = acc
    db.add(Schedule(douyin_account_id=a.id, send_min_sec=9, send_max_sec=13))
    db.commit()

    c = login(u)
    c.put(f"/api/schedule/{a.id}", json={"enabled": True, "time": "10:00"},
          headers=_csrf(c))

    sch = db.query(Schedule).filter_by(douyin_account_id=a.id).one()
    assert (sch.send_min_sec, sch.send_max_sec) == (9, 13)


# ── 账号页 ────────────────────────────────────────────────────

def test_account_page_shows_the_inputs(db, acc, client, login):
    u, a = acc
    body = login(u).get(f"/accounts/{a.id}").text

    assert 'id="sch-min"' in body
    assert 'id="sch-max"' in body
    assert "每条消息间隔" in body


def test_account_page_prefills_saved_values(db, acc, client, login):
    u, a = acc
    login(u).get(f"/accounts/{a.id}")          # 建出 Schedule 行
    sch = db.query(Schedule).filter_by(douyin_account_id=a.id).one()
    sch.send_min_sec, sch.send_max_sec = 7.5, 18
    db.commit()

    body = login(u).get(f"/accounts/{a.id}").text
    assert 'value="7.5"' in body
    assert 'value="18' in body


def test_partial_payload_keeps_the_other_bound(db, acc, client, login):
    """只传一边时，另一边要保持库里的值 —— 不能被悄悄改回默认。"""
    u, a = acc
    db.add(Schedule(douyin_account_id=a.id, send_min_sec=9, send_max_sec=13))
    db.commit()

    c = login(u)
    r = c.put(f"/api/schedule/{a.id}",
              json={"enabled": False, "time": "09:00", "send_max_sec": 30},
              headers=_csrf(c))

    assert r.json()["ok"] is True
    db.expire_all()
    sch = db.query(Schedule).filter_by(douyin_account_id=a.id).one()
    assert (sch.send_min_sec, sch.send_max_sec) == (9, 30)
