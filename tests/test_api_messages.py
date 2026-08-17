"""/api/messages/* 的 HTTP 契约。

重点是越权隔离 —— 聊天记录是最私密的数据，串号等于直接泄露私聊。
"""
import pytest

from app import messages_service as ms, realtime
from app.db import SessionLocal
from app.models import DouyinAccount, User
from app.security import hash_password


@pytest.fixture(autouse=True)
def _no_real_douyin(monkeypatch):
    """不真的连抖音；轮询线程也别真起来。"""
    monkeypatch.setattr(realtime, "subscribe",
                        lambda user_id, account_id: realtime.Subscription(account_id))
    monkeypatch.setattr(realtime, "unsubscribe", lambda sub: None)


def _seed(account_id, peer="2000000002", n=3):
    with SessionLocal() as db:
        ms.sync_messages(db, account_id, [{
            "conv_id": f"0:1:1:{peer}", "peer_uid": peer,
            "server_msg_id": i, "conv_short_id": 1, "msg_type": 7,
            "kind": "text", "sender": peer, "is_me": False,
            "text": f"消息{i}", "created_at": i * 100,
        } for i in range(1, n + 1)])
        db.commit()


def _csrf(client):
    client.get("/login")
    return client.cookies.get("csrf", "")


# ── 读取 ─────────────────────────────────────────────────────────

def test_returns_conversation(active_user, login):
    user, acc = active_user
    _seed(acc.id)
    r = login(user).get(f"/api/messages/{acc.id}/2000000002")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert [m["text"] for m in body["messages"]] == ["消息1", "消息2", "消息3"]


def test_empty_conversation(active_user, login):
    user, acc = active_user
    r = login(user).get(f"/api/messages/{acc.id}/nobody")
    assert r.status_code == 200
    assert r.json()["messages"] == []


def test_limit_and_paging(active_user, login):
    user, acc = active_user
    _seed(acc.id, n=10)
    c = login(user)
    first = c.get(f"/api/messages/{acc.id}/2000000002?limit=4").json()
    assert [m["server_msg_id"] for m in first["messages"]] == [7, 8, 9, 10]
    oldest = first["messages"][0]["created_at"]
    older = c.get(
        f"/api/messages/{acc.id}/2000000002?limit=4&before={oldest}").json()
    assert [m["server_msg_id"] for m in older["messages"]] == [3, 4, 5, 6]


def test_has_more_false_at_end(active_user, login):
    user, acc = active_user
    _seed(acc.id, n=2)
    body = login(user).get(f"/api/messages/{acc.id}/2000000002?limit=50").json()
    assert body["has_more"] is False


def test_limit_is_capped(active_user, login):
    """limit 不设上限的话，一个请求能把整库消息拉出来。"""
    user, acc = active_user
    _seed(acc.id, n=5)
    r = login(user).get(f"/api/messages/{acc.id}/2000000002?limit=99999")
    assert r.status_code == 200
    assert len(r.json()["messages"]) <= 200


# ── 越权 ─────────────────────────────────────────────────────────

@pytest.fixture
def other_account(db):
    u = User(username="stranger", password_hash=hash_password("pw123456"))
    db.add(u); db.commit(); db.refresh(u)
    acc = DouyinAccount(user_id=u.id, label="别人的号", status="active")
    db.add(acc); db.commit(); db.refresh(acc)
    return acc


def test_cannot_read_other_users_messages(active_user, login, other_account):
    user, _ = active_user
    _seed(other_account.id)
    r = login(user).get(f"/api/messages/{other_account.id}/2000000002")
    assert r.status_code == 404


def test_cannot_stream_other_users_messages(active_user, login, other_account):
    user, _ = active_user
    r = login(user).get(f"/api/messages/{other_account.id}/stream")
    assert r.status_code == 404


def test_cannot_sync_other_users_account(active_user, login, other_account):
    user, _ = active_user
    c = login(user)
    r = c.post(f"/api/messages/{other_account.id}/sync",
               headers={"X-CSRF-Token": _csrf(c)})
    assert r.status_code == 404


def test_anonymous_is_rejected(client, active_user):
    _user, acc = active_user
    client.cookies.clear()
    r = client.get(f"/api/messages/{acc.id}/2000000002", follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403)
