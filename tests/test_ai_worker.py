"""AI 自动回复的闸门测试。

每一道闸门漏了都有具体后果，测试名就写后果：
- 白名单漏 → 所有好友同时收到 AI 回复，不可撤销
- 启用时刻漏 → 开关一打开把几百条历史消息全回一遍
- 幂等漏 → 重启后同一条消息回第二次
- 冷却漏 → 对面也挂机器人时两台机器互刷
- 日配额漏 → 单日发送量炸掉直接进风控

这里把 LLM 和抖音发送都打桩，只验判定逻辑。
"""
from datetime import datetime, timedelta

import pytest

from app import ai_reply_config, ai_worker, llm
from app.models import AiReplyLog, ChatMessage, Contact


@pytest.fixture
def acc(db, active_user):
    u, a = active_user
    return u, a


@pytest.fixture
def setup(db, acc, monkeypatch):
    """一个已配好、已开启、联系人在白名单里的账号 + 打桩的 LLM/发送。"""
    u, a = acc
    db.add(Contact(douyin_account_id=a.id, uid="1001", nickname="小明",
                   conv_id="conv-1", days=30))
    ai_reply_config.save(db, a.id, {
        "provider": "openai", "base_url": "https://x.test/v1",
        "model": "test-model", "api_key": "sk-test",
        "max_chars": 60, "cooldown_sec": 20, "daily_limit": 100,
        "enabled": True,
    })
    ai_reply_config.set_peer(db, a.id, "1001", {"enabled": True})
    db.commit()

    sent: list[tuple] = []

    def _fake_chat(cfg, system, user, history=None):
        return llm.LLMResult(text='{"should_reply": true, "reply": "好的呀"}',
                             tokens=42, latency_ms=100)

    def _fake_send(user_id, account_id, peer_uid, text):
        sent.append((account_id, peer_uid, text))
        return "ok"

    monkeypatch.setattr(llm, "chat", _fake_chat)
    monkeypatch.setattr(ai_worker, "_send", _fake_send)
    return u, a, sent


_EPOCH = datetime(1970, 1, 1)


def _utc_ms(dt: datetime | None = None) -> int:
    """naive UTC → epoch 毫秒。

    不能用 utcnow().timestamp()：那会把 naive 值当本地时间解释，
    在 UTC+8 下算出来比真实 epoch 少八小时，消息会被误判成「启用之前的历史消息」。
    """
    return int(((dt or datetime.utcnow()) - _EPOCH).total_seconds() * 1000)


def _msg(**kw):
    base = {
        "server_msg_id": 7001, "peer_uid": "1001", "is_me": False,
        "kind": "text", "text": "在吗", "conv_id": "conv-1",
        "created_at": _utc_ms() + 60_000,
    }
    base.update(kw)
    return base


# ── 正常路径 ─────────────────────────────────────────────

def test_白名单内的文本消息会被回复(db, setup):
    u, a, sent = setup
    assert ai_worker.handle(u.id, a.id, _msg()) == "sent"
    assert sent == [(a.id, "1001", "好的呀")]


def test_回复内容与配额都记进日志(db, setup):
    u, a, _ = setup
    ai_worker.handle(u.id, a.id, _msg())
    log = db.query(AiReplyLog).one()
    assert log.status == "sent"
    assert log.final_text == "好的呀"
    assert log.incoming == "在吗"
    assert log.tokens == 42


def test_联系人级回复格式覆盖账号级(db, setup):
    u, a, sent = setup
    ai_reply_config.set_peer(db, a.id, "1001", {"reply_format": "{{message}}~"})
    db.commit()
    ai_worker.handle(u.id, a.id, _msg())
    assert sent[0][2] == "好的呀~"


# ── 闸门 1：白名单 ────────────────────────────────────────

def test_不在白名单的联系人不回(db, setup):
    """漏了这道闸，一个手滑就让所有好友同时收到 AI 回复。"""
    u, a, sent = setup
    assert ai_worker.handle(u.id, a.id, _msg(peer_uid="9999")) == "not_whitelisted"
    assert sent == []


def test_白名单里关掉的联系人不回(db, setup):
    u, a, sent = setup
    ai_reply_config.set_peer(db, a.id, "1001", {"enabled": False})
    db.commit()
    assert ai_worker.handle(u.id, a.id, _msg()) == "not_whitelisted"
    assert sent == []


def test_总开关关掉后谁都不回(db, setup):
    u, a, sent = setup
    ai_reply_config.save(db, a.id, {"enabled": False})
    db.commit()
    assert ai_worker.handle(u.id, a.id, _msg()) == "disabled"
    assert sent == []


# ── 闸门 2：启用时刻 ──────────────────────────────────────

def test_启用之前的历史消息不回(db, setup):
    """漏了这道闸，开关一打开，库里攒的几百条老消息会被一次性全回一遍。"""
    u, a, sent = setup
    old = _utc_ms(datetime.utcnow() - timedelta(days=3))
    assert ai_worker.handle(u.id, a.id, _msg(created_at=old)) == "before_enabled"
    assert sent == []


def test_关掉再打开会刷新启用时刻(db, setup):
    u, a, _ = setup
    before = ai_reply_config.load(db, a.id).enabled_at
    ai_reply_config.save(db, a.id, {"enabled": False})
    db.commit()
    ai_reply_config.save(db, a.id, {"enabled": True})
    db.commit()
    assert ai_reply_config.load(db, a.id).enabled_at >= before


# ── 闸门 3：幂等 ─────────────────────────────────────────

def test_同一条消息只回一次(db, setup):
    """漏了这道闸，重启或 SSE 重连后同一条消息会被回第二次。"""
    u, a, sent = setup
    assert ai_worker.handle(u.id, a.id, _msg()) == "sent"
    assert ai_worker.handle(u.id, a.id, _msg()) == "duplicate"
    assert len(sent) == 1


# ── 闸门 4：冷却与配额 ────────────────────────────────────

def test_冷却期内不重复回同一个人(db, setup):
    """对面要是也挂着机器人，没有这道闸就是两台机器互相刷屏。"""
    u, a, sent = setup
    ai_worker.handle(u.id, a.id, _msg(server_msg_id=1))
    assert ai_worker.handle(u.id, a.id, _msg(server_msg_id=2)) == "skipped"
    assert len(sent) == 1
    assert db.query(AiReplyLog).filter(AiReplyLog.server_msg_id == 2).one().reason == "cooldown"


def test_冷却过后可以再回(db, setup):
    u, a, sent = setup
    ai_worker.handle(u.id, a.id, _msg(server_msg_id=1))
    # 把上一条的时间推到冷却窗口之外
    log = db.query(AiReplyLog).filter(AiReplyLog.server_msg_id == 1).one()
    log.created_at = datetime.utcnow() - timedelta(seconds=120)
    db.commit()
    assert ai_worker.handle(u.id, a.id, _msg(server_msg_id=2)) == "sent"
    assert len(sent) == 2


def test_冷却只对同一个人生效(db, setup):
    u, a, sent = setup
    db.add(Contact(douyin_account_id=a.id, uid="1002", nickname="小红",
                   conv_id="conv-2"))
    ai_reply_config.set_peer(db, a.id, "1002", {"enabled": True})
    db.commit()
    ai_worker.handle(u.id, a.id, _msg(server_msg_id=1, peer_uid="1001"))
    assert ai_worker.handle(u.id, a.id,
                            _msg(server_msg_id=2, peer_uid="1002")) == "sent"
    assert len(sent) == 2


def test_超过日配额停止回复(db, setup):
    u, a, sent = setup
    ai_reply_config.save(db, a.id, {"daily_limit": 1, "cooldown_sec": 5})
    db.commit()
    ai_worker.handle(u.id, a.id, _msg(server_msg_id=1))
    log = db.query(AiReplyLog).filter(AiReplyLog.server_msg_id == 1).one()
    log.created_at = datetime.utcnow() - timedelta(seconds=60)
    db.commit()
    assert ai_worker.handle(u.id, a.id, _msg(server_msg_id=2)) == "skipped"
    assert db.query(AiReplyLog).filter(
        AiReplyLog.server_msg_id == 2).one().reason == "daily_limit"


# ── 闸门 5：账户忙 ────────────────────────────────────────

def test_续火花任务在跑时跳过(db, acc, setup, monkeypatch):
    """两条链路同时打同一个抖音号 = 发送频率翻倍，直接踩风控。"""
    u, a, _ = setup
    monkeypatch.setattr(ai_worker, "_send", lambda *args: "locked")
    assert ai_worker.handle(u.id, a.id, _msg()) == "skipped"
    assert db.query(AiReplyLog).one().reason == "account_busy"


# ── 候选筛选 ─────────────────────────────────────────────

def test_自己发的消息不入队():
    """漏了这条就是自问自答死循环。"""
    assert ai_worker.on_new_messages(1, 1, [_msg(is_me=True)]) == 0


def test_非文本消息不入队():
    for kind in ("image", "audio", "share", "system", "emoji"):
        assert ai_worker.on_new_messages(1, 1, [_msg(kind=kind)]) == 0


def test_空文本不入队():
    assert ai_worker.on_new_messages(1, 1, [_msg(text="   ")]) == 0


def test_没有服务端id的消息不入队():
    """没 id 就没法做幂等，会被反复回。"""
    assert ai_worker.on_new_messages(1, 1, [_msg(server_msg_id=None)]) == 0


# ── 模型输出被拦时不发 ────────────────────────────────────

def test_模型输出带链接时拦下不发(db, setup, monkeypatch):
    u, a, sent = setup
    monkeypatch.setattr(llm, "chat", lambda *a_, **k: llm.LLMResult(
        text='{"should_reply": true, "reply": "看这个 https://x.com/a"}'))
    assert ai_worker.handle(u.id, a.id, _msg()) == "blocked"
    assert sent == []
    assert db.query(AiReplyLog).one().reason == "link"


def test_模型弃权时不发(db, setup, monkeypatch):
    u, a, sent = setup
    monkeypatch.setattr(llm, "chat", lambda *a_, **k: llm.LLMResult(
        text='{"should_reply": false, "reply": ""}'))
    assert ai_worker.handle(u.id, a.id, _msg()) == "blocked"
    assert sent == []
    assert db.query(AiReplyLog).one().reason == "model_declined"


def test_原始输出记进日志便于排查(db, setup, monkeypatch):
    u, a, _ = setup
    monkeypatch.setattr(llm, "chat", lambda *a_, **k: llm.LLMResult(
        text='{"should_reply": true, "reply": "加我微信 abc12345"}'))
    ai_worker.handle(u.id, a.id, _msg())
    assert "abc12345" in db.query(AiReplyLog).one().raw_output


# ── 失败熔断 ─────────────────────────────────────────────

def test_调用失败记为llm_error(db, setup, monkeypatch):
    u, a, sent = setup

    def _boom(*a_, **k):
        raise llm.LLMError("HTTP 401: unauthorized")

    monkeypatch.setattr(llm, "chat", _boom)
    assert ai_worker.handle(u.id, a.id, _msg()) == "llm_error"
    assert sent == []


def test_连续失败到阈值自动关闭开关(db, setup, monkeypatch):
    """key 过期或余额耗尽时，不熔断就会每来一条消息烧一次失败请求。"""
    u, a, _ = setup

    def _boom(*a_, **k):
        raise llm.LLMError("HTTP 401")

    monkeypatch.setattr(llm, "chat", _boom)
    for i in range(ai_worker.MAX_FAIL_STREAK):
        ai_worker.handle(u.id, a.id, _msg(server_msg_id=100 + i))
        # 每轮都要绕过冷却，否则会先被冷却闸拦掉
        db.query(AiReplyLog).filter(AiReplyLog.server_msg_id == 100 + i).one()
    db.expire_all()
    assert ai_reply_config.load(db, a.id).enabled is False


def test_成功一次就清零失败计数(db, setup):
    u, a, _ = setup
    cfg = ai_reply_config.load(db, a.id)
    cfg.fail_streak = 3
    db.commit()
    ai_worker.handle(u.id, a.id, _msg())
    db.expire_all()
    assert ai_reply_config.load(db, a.id).fail_streak == 0


# ── 上下文与知识库注入 ────────────────────────────────────

def test_历史消息进上下文但不含本条(db, setup, monkeypatch):
    u, a, _ = setup
    now = _utc_ms()
    for i, (text, is_me) in enumerate([("你好", False), ("嗨", True)]):
        db.add(ChatMessage(douyin_account_id=a.id, peer_uid="1001",
                           server_msg_id=500 + i, is_me=is_me, kind="text",
                           text=text, created_ms=now - 10_000 + i))
    db.commit()

    seen = {}

    def _capture(cfg, system, user, history=None):
        seen["history"] = history
        seen["user"] = user
        return llm.LLMResult(text='{"should_reply":true,"reply":"嗯"}')

    monkeypatch.setattr(llm, "chat", _capture)
    ai_worker.handle(u.id, a.id, _msg())

    assert [h["content"] for h in seen["history"]] == ["你好", "嗨"]
    assert "在吗" in seen["user"]


def test_知识库内容注入系统提示(db, setup, monkeypatch):
    u, a, _ = setup
    from app.models import KnowledgeEntry
    db.add(KnowledgeEntry(douyin_account_id=a.id, uid="*", title="营业时间",
                          content="早九晚六", keywords="营业,几点"))
    db.commit()

    seen = {}

    def _capture(cfg, system, user, history=None):
        seen["system"] = system
        return llm.LLMResult(text='{"should_reply":true,"reply":"早九晚六"}')

    monkeypatch.setattr(llm, "chat", _capture)
    ai_worker.handle(u.id, a.id, _msg(text="你们几点营业"))
    assert "早九晚六" in seen["system"]
