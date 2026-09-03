"""语音转文字：音频下载 + OpenAI 兼容 /audio/transcriptions。

Why 自己做 ASR：抖音的语音消息里**没有**转写。实测库里 69 条语音，
带 ai_audio_text 的 0 条 —— 那个字段在 web 端的 IM 响应里根本不出现。
拿得到的只有音频地址（.mpeg，带签名，实测无需 Referer 即可下载）。

Why 独立配置而不是复用主网关：主网关默认是 DeepSeek，它没有
/audio/transcriptions。所以 ASR 走单独的 base_url + model + key，
空着就是不启用。
"""
import pytest

import douyin_im as dy
from app import llm

AUDIO_URL = ("https://sf26-sign.douyinstatic.com/douyin-user-audio-file/"
             "abc123.mpeg?biz_tag=aweme_im&x-signature=xxx")


# ── 音频下载（协议层）────────────────────────────────────────

class _Resp:
    def __init__(self, content=b"", status=200, ctype="video/mpeg", chunks=None):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": ctype}
        self._chunks = chunks if chunks is not None else [content]

    def iter_content(self, chunk_size=8192):
        for c in self._chunks:
            if isinstance(c, Exception):
                raise c
            yield c

    def close(self):
        pass


class _Session:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_fetch_audio_returns_bytes():
    s = _Session(_Resp(b"ID3fake-audio-bytes"))
    assert dy.fetch_audio(s, AUDIO_URL) == b"ID3fake-audio-bytes"


@pytest.mark.parametrize("bad_url", [
    "", "   ", None,
    "javascript:alert(1)",
    "file:///etc/passwd",
    "https://evil.com/audio.mpeg",          # 非抖音域名
    "http://169.254.169.254/latest/meta-data/",   # 云元数据，典型 SSRF 目标
])
def test_fetch_audio_rejects_untrusted_urls(bad_url):
    """URL 来自抖音响应，但它进的是我们带登录态的 session ——
    域名白名单是这里唯一挡住 SSRF 的东西。"""
    s = _Session()
    assert dy.fetch_audio(s, bad_url) == b""
    assert s.calls == []


@pytest.mark.parametrize("host", [
    "https://sf26-sign.douyinstatic.com/x.mpeg",
    "https://sf3-sign.douyinstatic.com/x.mpeg",
    "https://v26-web.douyinvod.com/x.mpeg",
    "https://p3.douyinpic.com/x.mpeg",
])
def test_fetch_audio_allows_douyin_hosts(host):
    s = _Session(_Resp(b"ok"))
    assert dy.fetch_audio(s, host) == b"ok"


def test_fetch_audio_stops_at_size_cap():
    """语音正常几百 KB。不封顶的话，一个坏 URL 能把内存吃光。"""
    big = [b"x" * 100_000] * 50            # 5MB
    s = _Session(_Resp(chunks=big))
    out = dy.fetch_audio(s, AUDIO_URL, max_bytes=200_000)
    assert out == b"", "超限应整个放弃，而不是回一段截断的坏音频"


def test_fetch_audio_returns_empty_on_http_error():
    s = _Session(_Resp(b"", status=403))
    assert dy.fetch_audio(s, AUDIO_URL) == b""


def test_fetch_audio_swallows_network_error():
    s = _Session(RuntimeError("connection reset"))
    assert dy.fetch_audio(s, AUDIO_URL) == b""


def test_fetch_audio_swallows_midstream_error():
    s = _Session(_Resp(chunks=[b"abc", RuntimeError("reset")]))
    assert dy.fetch_audio(s, AUDIO_URL) == b""


# ── ASR 端点解析 ──────────────────────────────────────────────

@pytest.mark.parametrize("base,expected", [
    ("https://api.siliconflow.cn", "https://api.siliconflow.cn/v1/audio/transcriptions"),
    ("https://api.siliconflow.cn/v1", "https://api.siliconflow.cn/v1/audio/transcriptions"),
    ("https://api.siliconflow.cn/v1/", "https://api.siliconflow.cn/v1/audio/transcriptions"),
    # 已经是完整端点就别再拼
    ("https://x.com/v1/audio/transcriptions", "https://x.com/v1/audio/transcriptions"),
])
def test_asr_endpoint_tolerates_common_base_url_forms(base, expected):
    assert llm.asr_endpoint(base) == expected


# ── transcribe ────────────────────────────────────────────────

class _Cfg:
    def __init__(self, base="https://api.siliconflow.cn/v1",
                 model="FunAudioLLM/SenseVoiceSmall", key="sk-test"):
        self.asr_base_url = base
        self.asr_model = model
        self.asr_api_key = key


def _post_stub(captured, payload, status=200):
    def _post(url, **kw):
        captured["url"] = url
        captured.update(kw)
        class R:
            status_code = status
            text = payload if isinstance(payload, str) else ""
            def json(self):
                if isinstance(payload, str):
                    raise ValueError("not json")
                return payload
        return R()
    return _post


def test_transcribe_returns_text(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm.requests, "post",
                        _post_stub(cap, {"text": "  今天下午三点开会  "}))
    assert llm.transcribe(_Cfg(), b"audio-bytes") == "今天下午三点开会"


def test_transcribe_posts_multipart_with_model_and_key(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm.requests, "post", _post_stub(cap, {"text": "喂"}))
    llm.transcribe(_Cfg(), b"audio-bytes")

    assert cap["url"] == "https://api.siliconflow.cn/v1/audio/transcriptions"
    assert cap["headers"]["Authorization"] == "Bearer sk-test"
    assert cap["data"]["model"] == "FunAudioLLM/SenseVoiceSmall"
    assert "file" in cap["files"]


@pytest.mark.parametrize("cfg", [
    _Cfg(base=""),          # 没配 base_url
    _Cfg(model=""),         # 没配模型
    _Cfg(key=""),           # 没配 key
])
def test_transcribe_without_config_is_disabled(monkeypatch, cfg):
    """没配全就是没启用 —— 静默不转写，不该抛错也不该乱打请求。"""
    def _boom(*a, **k):
        raise AssertionError("不该发请求")
    monkeypatch.setattr(llm.requests, "post", _boom)
    assert llm.transcribe(cfg, b"audio") == ""


def test_transcribe_with_empty_audio_does_not_request(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("不该发请求")
    monkeypatch.setattr(llm.requests, "post", _boom)
    assert llm.transcribe(_Cfg(), b"") == ""


def test_transcribe_raises_on_http_error(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm.requests, "post",
                        _post_stub(cap, {"error": "bad key"}, status=401))
    with pytest.raises(llm.LLMError):
        llm.transcribe(_Cfg(), b"audio")


def test_transcribe_handles_non_json_response(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm.requests, "post", _post_stub(cap, "<html>502</html>"))
    with pytest.raises(llm.LLMError):
        llm.transcribe(_Cfg(), b"audio")


def test_transcribe_clamps_long_text(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm.requests, "post",
                        _post_stub(cap, {"text": "啊" * 3000}))
    out = llm.transcribe(_Cfg(), b"audio")
    assert 0 < len(out) <= llm.ASR_TEXT_MAX + 1


def test_transcribe_accepts_alternate_text_field(monkeypatch):
    """有的网关回 {"result": ...} 而不是 {"text": ...}。"""
    cap = {}
    monkeypatch.setattr(llm.requests, "post", _post_stub(cap, {"result": "你好"}))
    assert llm.transcribe(_Cfg(), b"audio") == "你好"


def test_transcribe_empty_result_is_empty(monkeypatch):
    cap = {}
    monkeypatch.setattr(llm.requests, "post", _post_stub(cap, {"text": "   "}))
    assert llm.transcribe(_Cfg(), b"audio") == ""
