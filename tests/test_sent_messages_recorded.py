"""自己发出去的消息必须立刻进 chat_messages。

Bug：append_local 只在 /api/send（手动单发）和 AI 回复里调，
trigger.auto_run / send_batch 发完只写 JobRunItem，不碰聊天表。
那些消息只能等下次 init 同步从抖音捞回来 —— 而抖音每个会话只回最近 ~21 条，
捞不回来就永久没了。真实数据里 6 条成功续火花有 4 条不在聊天表。
"""
from datetime import datetime, timedelta

import pytest

from app import trigger
from app.models import ChatMessage, DouyinAccount, Schedule, User
from app.security import hash_password


@pytest.fixture
def acc(db):
    u = User(username="chatuser", password_hash=hash_password("pw123456"),
             expires_at=datetime.utcnow() + timedelta(days=30), max_accounts=3)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="主号", status="active",
                      cookies_exist=True)
    db.add(a); db.commit(); db.refresh(a)
    db.add(Schedule(douyin_account_id=a.id)); db.commit()
    return u, a


CONTACTS = [
    {"uid": "111", "nickname": "甲", "conv_id": "conv-1", "days": 30, "status": "active"},
    {"uid": "222", "nickname": "乙", "conv_id": "conv-2", "days": 20, "status": "active"},
]


@pytest.fixture
def fake_douyin(monkeypatch):
    """拦网络。send_ok 控制发送成败，方便测「失败的不该入库」。"""
    state = {"send_ok": True}

    monkeypatch.setattr(trigger, "_ensure_active",
                        lambda ctx: ({}, {}, [dict(c) for c in CONTACTS]))
    monkeypatch.setattr(trigger, "_adaptive_interval",
                        lambda account_id, base=5.0: (0.0, "test"))
    monkeypatch.setattr(trigger.dy, "_log", lambda *a, **k: None)
    monkeypatch.setattr(trigger.dy, "get_last_send_info", lambda: {"msg": "OK"})
    monkeypatch.setattr(trigger.dy, "_pick_message",
                        lambda uid, name, tpl: "今天也要开心呀", raising=False)
    monkeypatch.setattr(trigger.dy, "send_text",
                        lambda *a, **k: state["send_ok"])
    monkeypatch.setattr(trigger.time, "sleep", lambda *_: None)
    return state


def _mine(db, account_id, uid=None):
    q = db.query(ChatMessage).filter(
        ChatMessage.douyin_account_id == account_id,
        ChatMessage.is_me == True)                      # noqa: E712
    if uid:
        q = q.filter(ChatMessage.peer_uid == uid)
    return q.all()


# ── 自动续火花 ───────────────────────────────────────────────────

def test_auto_run_records_sent_messages(db, acc, fake_douyin):
    """核心回归：续火花发出去的每条都要进聊天记录。"""
    u, a = acc
    trigger.auto_run(u.id, a.id, triggered_by="test")

    rows = _mine(db, a.id)
    assert {r.peer_uid for r in rows} == {"111", "222"}
    assert all(r.text == "今天也要开心呀" for r in rows)


def test_recorded_message_carries_conv_id(db, acc, fake_douyin):
    """conv_id 得带上，否则这条消息挂不到会话上。"""
    u, a = acc
    trigger.auto_run(u.id, a.id, triggered_by="test")

    row = _mine(db, a.id, "111")[0]
    assert row.conv_id == "conv-1"
    assert row.kind == "text"


def test_recorded_as_placeholder_for_later_claim(db, acc, fake_douyin):
    """用负数 server_msg_id 占位，等下次 init 同步补回真身。

    正数是抖音的真 id；占位用负数，同步回来时 _claim 按 (peer_uid, text)
    认领并换成真 id —— 不占位的话同一条会重复插一份。
    """
    u, a = acc
    trigger.auto_run(u.id, a.id, triggered_by="test")

    rows = _mine(db, a.id)
    assert all(r.server_msg_id < 0 for r in rows)
    assert len({r.server_msg_id for r in rows}) == len(rows), "占位 id 撞车了"


def test_failed_sends_are_not_recorded(db, acc, fake_douyin):
    """没发出去的不能记 —— 那是骗自己。"""
    u, a = acc
    fake_douyin["send_ok"] = False
    trigger.auto_run(u.id, a.id, triggered_by="test")

    assert _mine(db, a.id) == []


def test_placeholder_is_claimed_by_real_sync(db, acc, fake_douyin):
    """下次 init 同步把真身补回来时，认领占位而不是插重复的第二条。"""
    from app import messages_service

    u, a = acc
    trigger.auto_run(u.id, a.id, triggered_by="test")
    before = len(_mine(db, a.id))

    row = _mine(db, a.id, "111")[0]
    messages_service.sync_messages(db, a.id, [{
        "server_msg_id": 9001, "peer_uid": "111", "conv_id": "conv-1",
        "is_me": True, "kind": "text", "text": "今天也要开心呀",
        "created_at": row.created_ms,
    }])
    db.commit()

    after = _mine(db, a.id)
    assert len(after) == before, "同一条消息被插了两份"
    assert 9001 in {r.server_msg_id for r in after}, "占位没被换成真 id"


# ── 批量发送 ─────────────────────────────────────────────────────

def test_send_batch_records_sent_messages(db, acc, fake_douyin):
    u, a = acc
    trigger.send_batch(u.id, a.id, ["111", "222"], "手动批量的内容")

    rows = _mine(db, a.id)
    assert {r.peer_uid for r in rows} == {"111", "222"}
    assert all(r.text == "手动批量的内容" for r in rows)


def test_send_batch_skips_failed(db, acc, fake_douyin):
    u, a = acc
    fake_douyin["send_ok"] = False
    trigger.send_batch(u.id, a.id, ["111"], "发不出去的")
    assert _mine(db, a.id) == []


def test_send_batch_unknown_uid_records_nothing(db, acc, fake_douyin):
    """联系人不存在时本来就没发出去，更不该留聊天记录。"""
    u, a = acc
    trigger.send_batch(u.id, a.id, ["999"], "查无此人")
    assert _mine(db, a.id) == []


# ── 不能连累主流程 ───────────────────────────────────────────────

def test_chat_record_failure_does_not_break_sending(db, acc, fake_douyin,
                                                    monkeypatch):
    """写聊天记录是附带动作，炸了也不能让续火花任务失败。"""
    from app import messages_service

    def boom(*a, **k):
        raise RuntimeError("DB 挂了")

    monkeypatch.setattr(messages_service, "append_local", boom)

    u, a = acc
    summary = trigger.auto_run(u.id, a.id, triggered_by="test")
    assert summary["sent"] == 2, "写聊天记录失败连累了发送统计"
