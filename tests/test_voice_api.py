"""聊天页手动「转文字」接口。

Why 要有手动入口：自动转写只在 AI 准备回复时才发生。用户自己翻聊天记录
看到一条语音，不点一下就只能戴耳机听 —— 而转写结果本来就会写进
media.asr，点一次全局受益（AI 之后要回也直接命中缓存）。
"""
import json

import pytest

from app import crypto, voice_service
from app.models import (AiReplyConfig, ChatMessage, DouyinAccount, User)
from app.security import hash_password

AUDIO_URL = ("https://sf26-sign.douyinstatic.com/douyin-user-audio-file/"
             "abc.mpeg?x-signature=xxx")
MSG_ID = 555001          # server_msg_id，只用来建夹具


@pytest.fixture
def acc(db):
    u = User(username="voiceapi", password_hash=hash_password("pw123456"),
             max_accounts=5)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="主号", status="active", cookies_exist=True)
    db.add(a); db.commit(); db.refresh(a)
    db.add(AiReplyConfig(douyin_account_id=a.id,
                         asr_base_url="https://api.x.com/v1",
                         asr_model="whisper-1",
                         asr_key_enc=crypto.encrypt("sk-asr")))
    db.add(ChatMessage(douyin_account_id=a.id, peer_uid="123",
                       server_msg_id=MSG_ID, kind="audio", text="[语音] 11.7″",
                       created_ms=1788061500000,
                       media=json.dumps({"kind": "audio", "src": AUDIO_URL,
                                         "duration_ms": 11699},
                                        ensure_ascii=False)))
    db.commit()
    row = db.query(ChatMessage).filter_by(server_msg_id=MSG_ID).one()
    return u, a, row.id


@pytest.fixture
def csrf(client):
    client.get("/login")
    return {"X-CSRF-Token": client.cookies.get("csrf", "")}


@pytest.fixture
def stub_asr(monkeypatch):
    monkeypatch.setattr(voice_service.dy, "fetch_audio", lambda *a, **k: b"audio")
    monkeypatch.setattr(voice_service.llm, "transcribe",
                        lambda *a, **k: "今天下午三点开会")
    from app.routers import api as api_mod
    monkeypatch.setattr(api_mod, "_account_session_for", lambda *a: object())


def _url(acc_id):
    return f"/api/messages/{acc_id}/transcribe"


def test_transcribes_and_returns_text(client, login, db, acc, csrf, stub_asr):
    u, a, mid = acc
    c = login(u)
    r = c.post(_url(a.id), json={"id": mid}, headers=csrf)

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["text"] == "今天下午三点开会"


def test_result_is_persisted_for_next_time(client, login, db, acc, csrf, stub_asr):
    u, a, mid = acc
    c = login(u)
    c.post(_url(a.id), json={"id": mid}, headers=csrf)

    row = db.query(ChatMessage).filter_by(server_msg_id=MSG_ID).one()
    assert json.loads(row.media)["asr"] == "今天下午三点开会"


def test_second_call_uses_cache(client, login, db, acc, csrf, monkeypatch):
    """点两次不该转两次 —— ASR 按时长计费。"""
    calls = {"n": 0}

    def _t(*a, **k):
        calls["n"] += 1
        return "今天下午三点开会"

    monkeypatch.setattr(voice_service.dy, "fetch_audio", lambda *a, **k: b"audio")
    monkeypatch.setattr(voice_service.llm, "transcribe", _t)
    from app.routers import api as api_mod
    monkeypatch.setattr(api_mod, "_account_session_for", lambda *a: object())

    u, a, mid = acc
    c = login(u)
    c.post(_url(a.id), json={"id": mid}, headers=csrf)
    r = c.post(_url(a.id), json={"id": mid}, headers=csrf)

    assert r.json()["text"] == "今天下午三点开会"
    assert calls["n"] == 1


def test_requires_login(client, db, acc, csrf):
    _u, a, mid = acc
    r = client.post(_url(a.id), json={"id": mid}, headers=csrf)
    assert r.status_code in (401, 403, 302)


def test_cannot_transcribe_foreign_account(client, login, db, acc, csrf, stub_asr):
    """别人的账号里的语音，碰都不能碰。"""
    u, _a, mid = acc
    other = User(username="intruder", password_hash=hash_password("pw123456"),
                 max_accounts=5)
    db.add(other); db.commit(); db.refresh(other)
    foreign = DouyinAccount(user_id=other.id, label="别人的号", status="active")
    db.add(foreign); db.commit(); db.refresh(foreign)

    c = login(u)
    r = c.post(_url(foreign.id), json={"id": mid}, headers=csrf)
    assert r.status_code == 404


def test_unknown_message_is_rejected(client, login, db, acc, csrf, stub_asr):
    u, a, mid = acc
    c = login(u)
    r = c.post(_url(a.id), json={"id": 999999999}, headers=csrf)
    assert r.json()["ok"] is False


@pytest.mark.parametrize("payload", [{}, {"id": 0}, {"id": "abc"}, {"id": None}])
def test_bad_payload_is_rejected(client, login, db, acc, csrf, stub_asr, payload):
    u, a, mid = acc
    c = login(u)
    r = c.post(_url(a.id), json=payload, headers=csrf)
    assert r.json()["ok"] is False


def test_non_audio_message_is_rejected(client, login, db, acc, csrf, stub_asr):
    u, a, mid = acc
    other = ChatMessage(douyin_account_id=a.id, peer_uid="123",
                        server_msg_id=555002, kind="text", text="在吗",
                        created_ms=1788061500000)
    db.add(other); db.commit(); db.refresh(other)
    other_id = other.id
    c = login(u)
    r = c.post(_url(a.id), json={"id": other_id}, headers=csrf)
    assert r.json()["ok"] is False


def test_without_asr_config_says_so(client, login, db, acc, csrf, stub_asr):
    """没配转写服务时给一句人话，而不是静默失败。"""
    u, a, mid = acc
    cfg = db.query(AiReplyConfig).filter_by(douyin_account_id=a.id).one()
    cfg.asr_base_url = ""
    cfg.asr_model = ""
    cfg.asr_key_enc = ""
    db.commit()

    c = login(u)
    r = c.post(_url(a.id), json={"id": mid}, headers=csrf)

    body = r.json()
    assert body["ok"] is False
    assert "转写" in body["error"]


def test_asr_key_never_appears_in_response(client, login, db, acc, csrf, stub_asr):
    u, a, mid = acc
    c = login(u)
    r = c.post(_url(a.id), json={"id": mid}, headers=csrf)
    assert "sk-asr" not in r.text


def test_requires_csrf(client, login, db, acc, stub_asr):
    u, a, mid = acc
    c = login(u)
    r = c.post(_url(a.id), json={"id": mid})
    assert r.status_code == 403


# ── 为什么用本地 id 而不是 server_msg_id ──────────────────────
# 抖音的 server_message_id 是 19 位雪花号，实测真实库里 3885/3885 条
# **全部**超出 JS 的 Number.MAX_SAFE_INTEGER (9007199254740991)。
# 它一进浏览器就被 JSON.parse 四舍五入（7681202299335198257 →
# 7681202299335199000），发回来的值库里根本不存在 —— 按 server_msg_id 查的话
# 「转文字」按钮对每一条真实语音都会失败。

JS_MAX_SAFE = 9007199254740991
SNOWFLAKE = 7681202299335198257          # 取自真实库的量级


def test_works_for_a_real_sized_snowflake_id(client, login, db, acc, csrf,
                                             stub_asr):
    u, a, _mid = acc
    row = ChatMessage(douyin_account_id=a.id, peer_uid="123",
                      server_msg_id=SNOWFLAKE, kind="audio", text="[语音] 3.2″",
                      created_ms=1788061500000,
                      media=json.dumps({"kind": "audio", "src": AUDIO_URL},
                                       ensure_ascii=False))
    db.add(row); db.commit(); db.refresh(row)

    assert row.server_msg_id > JS_MAX_SAFE, "夹具没造出真实量级的 id，测了个寂寞"
    assert row.id < JS_MAX_SAFE, "本地 id 也超了？那这条路同样不安全"

    r = login(u).post(_url(a.id), json={"id": row.id}, headers=csrf)
    assert r.json()["ok"] is True


def test_new_messages_pushed_to_the_browser_carry_an_id(db, acc):
    """实时推送的新消息也得带 id —— 不然刚到的语音点「转文字」是空的。"""
    from app import messages_service as ms

    _u, a, _mid = acc
    added = ms.sync_and_collect(db, a.id, [{
        "server_msg_id": SNOWFLAKE + 1, "peer_uid": "123", "kind": "audio",
        "text": "[语音] 2.0″", "created_at": 1788061600000,
        "media": {"kind": "audio", "src": AUDIO_URL},
    }])
    db.commit()

    assert len(added) == 1
    assert added[0]["id"], "推给前端的消息没有 id"


# ── 转写 Key 要能撤销 ─────────────────────────────────────────
# 存进去的是凭证。只能加不能删的话，换服务商或者 key 泄露了都没法收拾。

def test_asr_key_can_be_cleared(client, login, db, acc, csrf):
    u, a, _mid = acc
    c = login(u)
    r = c.put(f"/api/ai/{a.id}", json={"asr_api_key": None, "reply_voice": False},
              headers=csrf)

    assert r.status_code == 200, r.text
    assert r.json()["config"]["has_asr_key"] is False
    cfg = db.query(AiReplyConfig).filter_by(douyin_account_id=a.id).one()
    db.refresh(cfg)
    assert not cfg.asr_key_enc


def test_panel_offers_a_remove_button(client, login, db, acc):
    u, a, _mid = acc
    body = login(u).get(f"/accounts/{a.id}/chat").text
    assert "removeAsrKey" in body
    assert "移除转写 Key" in body
