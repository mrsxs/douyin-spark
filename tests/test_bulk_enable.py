"""批量开关「自动续火花」：PUT /api/templates/{account_id}/bulk-enabled

前端全选之后逐个 PUT 会打出几十个请求、写几十次 templates.json 备份。
这个端点一次收下所有 uid，单次 commit + 单次备份。

关键语义：uid 没有模板 entry 时要**新建**。因为 douyin_im._pick_message
对「没有 entry」的联系人直接跳过 —— 不建 entry 的话，用户把开关拨亮了
也一条都发不出去，这正是无火花好友首次启用时的情形。
"""
import json
from datetime import datetime, timedelta

import pytest

from app.models import DouyinAccount, MessageTemplate, User
from app.security import hash_password


@pytest.fixture
def acc(db):
    u = User(username="bulkuser", password_hash=hash_password("pw123456"),
             expires_at=datetime.utcnow() + timedelta(days=30), max_accounts=3)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="主号", status="active",
                      cookies_exist=True)
    db.add(a); db.commit(); db.refresh(a)
    return u, a


@pytest.fixture
def other_acc(db):
    u = User(username="bulkother", password_hash=hash_password("pw123456"),
             expires_at=datetime.utcnow() + timedelta(days=30), max_accounts=3)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="别人的号", status="active",
                      cookies_exist=True)
    db.add(a); db.commit(); db.refresh(a)
    return u, a


def _csrf(client):
    client.get("/login")
    return client.cookies.get("csrf", "")


def _bulk(client, account_id, uids, enabled):
    return client.put(f"/api/templates/{account_id}/bulk-enabled",
                      json={"uids": uids, "enabled": enabled},
                      headers={"X-CSRF-Token": _csrf(client)})


def _rows(db, account_id):
    return {t.uid: t for t in db.query(MessageTemplate).filter(
        MessageTemplate.douyin_account_id == account_id).all()}


# ── 基本行为 ─────────────────────────────────────────────────────

def test_creates_entries_for_unknown_uids(db, acc, login):
    """核心：没有 entry 的联系人（无火花好友常态）必须被建出来。"""
    u, a = acc
    r = _bulk(login(u), a.id, ["111", "222"], True)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["updated"] == 2

    rows = _rows(db, a.id)
    assert set(rows) == {"111", "222"}
    assert all(t.enabled for t in rows.values())


def test_disable_bulk(db, acc, login):
    u, a = acc
    c = login(u)
    _bulk(c, a.id, ["111", "222"], True)
    _bulk(c, a.id, ["111", "222"], False)

    rows = _rows(db, a.id)
    assert not rows["111"].enabled
    assert not rows["222"].enabled


def test_keeps_existing_messages(db, acc, login):
    """只拨开关，绝不能碰用户写好的话术。"""
    u, a = acc
    db.add(MessageTemplate(douyin_account_id=a.id, uid="111", enabled=False,
                           name="小明",
                           messages_json=json.dumps(["早安", "在吗"],
                                                    ensure_ascii=False)))
    db.commit()

    _bulk(login(u), a.id, ["111"], True)

    row = _rows(db, a.id)["111"]
    db.refresh(row)
    assert row.enabled is True
    assert json.loads(row.messages_json) == ["早安", "在吗"]
    assert row.name == "小明"


def test_is_idempotent(db, acc, login):
    u, a = acc
    c = login(u)
    _bulk(c, a.id, ["111"], True)
    _bulk(c, a.id, ["111"], True)
    assert db.query(MessageTemplate).filter(
        MessageTemplate.douyin_account_id == a.id).count() == 1


def test_dedupes_uids(db, acc, login):
    u, a = acc
    r = _bulk(login(u), a.id, ["111", "111", "222"], True)
    assert r.json()["updated"] == 2
    assert db.query(MessageTemplate).filter(
        MessageTemplate.douyin_account_id == a.id).count() == 2


def test_does_not_touch_other_uids(db, acc, login):
    """没勾的人不能被顺带改掉。"""
    u, a = acc
    db.add(MessageTemplate(douyin_account_id=a.id, uid="999", enabled=True,
                           messages_json="[]"))
    db.commit()

    _bulk(login(u), a.id, ["111"], False)

    row = _rows(db, a.id)["999"]
    db.refresh(row)
    assert row.enabled is True


def test_default_template_cannot_be_bulk_toggled(db, acc, login):
    """"default" 是兜底模板，不是联系人 —— 混进 uids 会误关全局兜底。"""
    u, a = acc
    db.add(MessageTemplate(douyin_account_id=a.id, uid="default", enabled=True,
                           messages_json=json.dumps(["早"], ensure_ascii=False)))
    db.commit()

    _bulk(login(u), a.id, ["default", "111"], False)

    rows = _rows(db, a.id)
    db.refresh(rows["default"])
    assert rows["default"].enabled is True, "全局兜底模板被批量操作关掉了"


# ── 校验与越权 ───────────────────────────────────────────────────

def test_empty_uids_rejected(acc, login):
    u, a = acc
    r = _bulk(login(u), a.id, [], True)
    assert r.json()["ok"] is False


def test_too_many_uids_rejected(acc, login):
    u, a = acc
    r = _bulk(login(u), a.id, [str(i) for i in range(1001)], True)
    assert r.json()["ok"] is False


def test_other_users_account_is_404(db, acc, other_acc, login):
    """越权隔离：不能改别人账号的模板。"""
    u, _ = acc
    _, victim = other_acc
    r = _bulk(login(u), victim.id, ["111"], True)
    assert r.status_code == 404
    assert db.query(MessageTemplate).filter(
        MessageTemplate.douyin_account_id == victim.id).count() == 0


def test_requires_login(client, acc):
    _, a = acc
    client.cookies.clear()
    r = _bulk(client, a.id, ["111"], True)
    assert r.status_code in (401, 403, 302, 307)


def test_requires_csrf(acc, login):
    u, a = acc
    c = login(u)
    r = c.put(f"/api/templates/{a.id}/bulk-enabled",
              json={"uids": ["111"], "enabled": True})
    assert r.status_code == 403
