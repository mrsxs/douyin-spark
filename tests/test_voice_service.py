"""语音消息转写的编排：下载 → ASR → 写回 media.asr。

Why 写回 media.asr 而不是新建一张表：前端早就会渲染 `m.media.asr`
（chat.html），写回去聊天页立刻能看到转写文字，不用碰前端；
而且语音是一条消息一份，天然按 server_msg_id 去重，不像视频会被反复分享。
"""
import json

import pytest

from app import voice_service
from app.models import ChatMessage, DouyinAccount, User
from app.security import hash_password

AUDIO_URL = ("https://sf26-sign.douyinstatic.com/douyin-user-audio-file/"
             "abc.mpeg?x-signature=xxx")


@pytest.fixture
def acc(db):
    u = User(username="voiceuser", password_hash=hash_password("pw123456"),
             max_accounts=5)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="主号", status="active", cookies_exist=True)
    db.add(a); db.commit(); db.refresh(a)
    return u, a


def _msg(**over):
    m = {
        "peer_uid": "123",
        "server_msg_id": 777,
        "is_me": False,
        "kind": "audio",
        "text": "[语音] 11.7″",
        "media": {"kind": "audio", "src": AUDIO_URL, "duration_ms": 11699,
                  "wave": [0.1, 0.5], "cover": "", "vid": ""},
        "created_at": 1788061500000,
    }
    m.update(over)
    return m


class _Cfg:
    asr_base_url = "https://api.siliconflow.cn/v1"
    asr_model = "FunAudioLLM/SenseVoiceSmall"
    asr_api_key = "sk-test"


@pytest.fixture
def stubs(monkeypatch):
    calls = {"downloads": [], "transcribes": 0}

    def _fetch(session, url, **kw):
        calls["downloads"].append(url)
        return b"fake-audio"

    def _transcribe(cfg, audio, filename="voice.mp3"):
        calls["transcribes"] += 1
        return "今天下午三点开会"

    monkeypatch.setattr(voice_service.dy, "fetch_audio", _fetch)
    monkeypatch.setattr(voice_service.llm, "transcribe", _transcribe)
    return calls


def test_transcribes_and_returns_text(db, acc, stubs):
    _u, a = acc
    assert voice_service.transcribe_message(object(), _Cfg(), a.id, _msg()) == "今天下午三点开会"
    assert stubs["downloads"] == [AUDIO_URL]


def test_result_is_written_back_to_media(db, acc, stubs):
    """写回 media.asr，聊天页就能直接看到转写文字。"""
    _u, a = acc
    db.add(ChatMessage(douyin_account_id=a.id, peer_uid="123", server_msg_id=777,
                       kind="audio", text="[语音] 11.7″", created_ms=1788061500000,
                       media=json.dumps({"kind": "audio", "src": AUDIO_URL,
                                         "duration_ms": 11699},
                                        ensure_ascii=False)))
    db.commit()

    voice_service.transcribe_message(object(), _Cfg(), a.id, _msg())

    row = db.query(ChatMessage).filter_by(server_msg_id=777).one()
    assert json.loads(row.media)["asr"] == "今天下午三点开会"


def test_cached_transcript_skips_asr(db, acc, stubs):
    """转过的不再转 —— ASR 是按秒计费的，重复转白烧钱。"""
    _u, a = acc
    db.add(ChatMessage(douyin_account_id=a.id, peer_uid="123", server_msg_id=777,
                       kind="audio", created_ms=1788061500000,
                       media=json.dumps({"kind": "audio", "src": AUDIO_URL,
                                         "asr": "已经转过了"}, ensure_ascii=False)))
    db.commit()

    out = voice_service.transcribe_message(object(), _Cfg(), a.id, _msg())

    assert out == "已经转过了"
    assert stubs["transcribes"] == 0


def test_message_carrying_asr_needs_no_work(db, acc, stubs):
    """抖音偶尔自带转写（实测 0%，但字段存在），有就直接用。"""
    _u, a = acc
    m = _msg()
    m["media"]["asr"] = "抖音自带的转写"

    assert voice_service.transcribe_message(object(), _Cfg(), a.id, m) == "抖音自带的转写"
    assert stubs["downloads"] == []


@pytest.mark.parametrize("over", [
    {"media": None},
    {"media": {"kind": "audio"}},                 # 没有 src
    {"media": {"kind": "audio", "src": ""}},
    {"media": "not-a-dict"},
])
def test_without_audio_url_returns_empty(db, acc, stubs, over):
    _u, a = acc
    assert voice_service.transcribe_message(object(), _Cfg(), a.id, _msg(**over)) == ""
    assert stubs["transcribes"] == 0


def test_download_failure_returns_empty(db, acc, monkeypatch):
    monkeypatch.setattr(voice_service.dy, "fetch_audio", lambda *a, **k: b"")
    monkeypatch.setattr(voice_service.llm, "transcribe",
                        lambda *a, **k: pytest.fail("没音频不该调 ASR"))
    _u, a = acc
    assert voice_service.transcribe_message(object(), _Cfg(), a.id, _msg()) == ""


def test_asr_error_does_not_escape(db, acc, monkeypatch):
    """ASR 挂了不能把 AI 回复链路带崩。"""
    from app import llm
    monkeypatch.setattr(voice_service.dy, "fetch_audio", lambda *a, **k: b"x")
    monkeypatch.setattr(voice_service.llm, "transcribe",
                        lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("401")))
    _u, a = acc
    assert voice_service.transcribe_message(object(), _Cfg(), a.id, _msg()) == ""


def test_empty_transcript_returns_empty(db, acc, monkeypatch):
    """纯环境音的语音会转出空字符串，不该当成有内容。"""
    monkeypatch.setattr(voice_service.dy, "fetch_audio", lambda *a, **k: b"x")
    monkeypatch.setattr(voice_service.llm, "transcribe", lambda *a, **k: "   ")
    _u, a = acc
    assert voice_service.transcribe_message(object(), _Cfg(), a.id, _msg()) == ""


def test_writeback_survives_oversized_media(db, acc, monkeypatch):
    """media 有 2000 字节硬上限，写不下时保住原 media，别把整条清空。"""
    monkeypatch.setattr(voice_service.dy, "fetch_audio", lambda *a, **k: b"x")
    monkeypatch.setattr(voice_service.llm, "transcribe", lambda *a, **k: "啊" * 900)
    _u, a = acc
    original = json.dumps({"kind": "audio", "src": AUDIO_URL}, ensure_ascii=False)
    db.add(ChatMessage(douyin_account_id=a.id, peer_uid="123", server_msg_id=777,
                       kind="audio", created_ms=1788061500000, media=original))
    db.commit()

    voice_service.transcribe_message(object(), _Cfg(), a.id, _msg())

    row = db.query(ChatMessage).filter_by(server_msg_id=777).one()
    assert json.loads(row.media)["src"] == AUDIO_URL, "原 media 不能丢"


# ── 喂给 AI 的文本 ────────────────────────────────────────────

def test_as_prompt_text_wraps_transcript():
    text = voice_service.as_prompt_text("今天下午三点开会")
    assert "今天下午三点开会" in text
    assert "语音" in text


def test_as_prompt_text_of_empty_is_empty():
    assert voice_service.as_prompt_text("") == ""
    assert voice_service.as_prompt_text("   ") == ""


# ── 转写要进 AI 的历史上下文 ───────────────────────────────

def _hist(**over):
    m = {"kind": "audio", "text": "[语音] 11.7″", "is_me": False,
         "media": {"kind": "audio", "src": AUDIO_URL, "duration_ms": 11699}}
    m.update(over)
    return m


def test_history_uses_transcript_when_available():
    """转写已经存在 media.asr 里，历史上下文必须用它。

    不然模型翻上文看到的全是「[语音] 11.7″」—— 白存了。
    """
    from app import ai_reply
    m = _hist()
    m["media"]["asr"] = "今天下午三点开会"

    assert ai_reply._history_text(m) == "[语音] 今天下午三点开会"


def test_history_falls_back_to_marker_without_transcript():
    from app import ai_reply
    assert ai_reply._history_text(_hist()) == "[语音] 11.7″"


def test_history_ignores_junk_asr():
    from app import ai_reply
    for junk in (None, 123, {"a": 1}, "   "):
        m = _hist()
        m["media"]["asr"] = junk
        assert ai_reply._history_text(m) == "[语音] 11.7″"


def test_history_survives_media_not_a_dict():
    from app import ai_reply
    assert ai_reply._history_text(_hist(media="oops")) == "[语音] 11.7″"


def test_transcribed_audio_enters_history_as_content():
    """整条链路：转写过的语音在 build_history 里应带着内容出现。"""
    from app import ai_reply
    rows = [
        {"kind": "text", "text": "在吗", "is_me": False},
        {"kind": "audio", "text": "[语音] 3.2″", "is_me": False,
         "media": {"kind": "audio", "asr": "明天上午有空吗"}},
    ]
    hist = ai_reply.build_history(rows, turns=5)

    assert any("明天上午有空吗" in h["content"] for h in hist)
