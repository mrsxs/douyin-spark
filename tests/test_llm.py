"""LLM 客户端：截断检测与协议细节。

真实事故：模型是推理型（DeepSeek-V4 有 reasoning_content），
思考过程也算在 max_tokens 里。默认 300 太小，思考烧完 300 就被砍断，
content 是空字符串 —— 界面只显示「模型没给出内容」，完全指不出原因，
而那次调用的钱一分没少付。
"""
import json

import pytest
import requests

from app import llm


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._p


def _openai_payload(content, finish="stop", reasoning=None):
    usage = {"total_tokens": 720}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": usage}


@pytest.fixture
def cfg():
    return llm.LLMConfig(base_url="https://x.test/v1", api_key="sk-t", model="m")


def _patch(monkeypatch, payload, status=200, seen=None):
    def _post(url, **kw):
        if seen is not None:
            seen.append(kw.get("json"))
        return _Resp(payload, status)
    monkeypatch.setattr(requests, "post", _post)


# ── 截断检测 ─────────────────────────────────────────────

def test_思考烧光预算时给出可照做的错误(monkeypatch, cfg):
    """这正是线上那两条「模型没给出内容」的真身。"""
    _patch(monkeypatch, _openai_payload("", finish="length", reasoning=300))
    with pytest.raises(llm.LLMError) as e:
        llm.chat(cfg, "sys", "user")
    msg = str(e.value)
    assert "max_tokens" in msg
    assert "思考" in msg
    assert "300" in msg          # 把思考占了多少直接摆出来


def test_anthropic的截断也认得(monkeypatch, cfg):
    _patch(monkeypatch, {"content": [], "stop_reason": "max_tokens", "usage": {}})
    c = llm.LLMConfig(provider="anthropic", base_url="https://a.test",
                      api_key="sk-t", model="claude")
    with pytest.raises(llm.LLMError, match="max_tokens"):
        llm.chat(c, "sys", "user")


def test_正常结束但内容为空不报截断(monkeypatch, cfg):
    """模型真的什么都没说，交给 sanitize 报 empty，别误导成 max_tokens 问题。"""
    _patch(monkeypatch, _openai_payload("", finish="stop"))
    assert llm.chat(cfg, "sys", "user").text == ""


def test_有内容时截断标记不拦截(monkeypatch, cfg):
    """正文写完了只是被截了个尾巴，能用就用 —— sanitize 本来也要截断。"""
    _patch(monkeypatch, _openai_payload('{"should_reply":true,"reply":"好的"}',
                                        finish="length"))
    assert "好的" in llm.chat(cfg, "sys", "user").text


# ── 预算默认值 ───────────────────────────────────────────

def test_默认预算够推理模型用():
    """300 会被思考吃光。回复正文由 sanitize 截到几十字，宽预算不会让回复变长。"""
    assert llm.LLMConfig().max_tokens >= 1024


def test_预算随配置传给接口(monkeypatch, cfg):
    seen = []
    _patch(monkeypatch, _openai_payload('{"reply":"x"}'), seen=seen)
    llm.chat(llm.LLMConfig(base_url="https://x.test/v1", api_key="k",
                           model="m", max_tokens=4096), "s", "u")
    assert seen[0]["max_tokens"] == 4096


# ── 端点补全 ─────────────────────────────────────────────

@pytest.mark.parametrize("base,want", [
    ("https://api.deepseek.com", "https://api.deepseek.com/v1/chat/completions"),
    ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/chat/completions"),
    ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1/chat/completions"),
    ("https://x.com/v1/chat/completions", "https://x.com/v1/chat/completions"),
    ("", "https://api.deepseek.com/v1/chat/completions"),
])
def test_openai端点容忍三种写法(base, want):
    """填错 base_url 是接入时最常见的错，与其让人对着 404 猜不如兜住。"""
    assert llm.openai_endpoint(base) == want


@pytest.mark.parametrize("base,want", [
    ("https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
    ("https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
    ("", "https://api.anthropic.com/v1/messages"),
])
def test_anthropic端点容忍多种写法(base, want):
    assert llm.anthropic_endpoint(base) == want


# ── 安全：错误信息不能带出 key ────────────────────────────

def test_错误信息不含api_key(monkeypatch):
    _patch(monkeypatch, {"error": "bad"}, status=401)
    c = llm.LLMConfig(base_url="https://x.test/v1", api_key="sk-super-secret", model="m")
    with pytest.raises(llm.LLMError) as e:
        llm.chat(c, "s", "u")
    assert "sk-super-secret" not in str(e.value)


def test_响应体在错误里被截断(monkeypatch, cfg):
    """有些网关会把请求头回显在错误 JSON 里，整段灌进日志就等于泄凭证。"""
    _patch(monkeypatch, {"error": "x" * 5000}, status=500)
    with pytest.raises(llm.LLMError) as e:
        llm.chat(cfg, "s", "u")
    assert len(str(e.value)) < 400


# ── 缺配置时不发请求 ─────────────────────────────────────

def test_没模型名直接报错(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("不该发请求")
    monkeypatch.setattr(requests, "post", _boom)
    with pytest.raises(llm.LLMError, match="模型名"):
        llm.chat(llm.LLMConfig(api_key="k"), "s", "u")


def test_没key直接报错(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("不该发请求")
    monkeypatch.setattr(requests, "post", _boom)
    with pytest.raises(llm.LLMError, match="API Key"):
        llm.chat(llm.LLMConfig(model="m"), "s", "u")


# ── Anthropic 预填 ───────────────────────────────────────

def test_anthropic预填被补回响应(monkeypatch):
    """预填的那截不在响应里，不补回去就拼不成合法 JSON。"""
    _patch(monkeypatch, {"content": [{"type": "text", "text": ' true, "reply": "好"}'}],
                         "stop_reason": "end_turn", "usage": {}})
    c = llm.LLMConfig(provider="anthropic", base_url="https://a.test",
                      api_key="k", model="claude")
    out = llm.chat(c, "s", "u").text
    assert out.startswith('{"should_reply":')
    assert json.loads(out)["reply"] == "好"


# ── DeepSeek JSON Output 的空白字符缺陷 ───────────────────

def test_json模式返回纯空白时去掉它重试(monkeypatch, cfg):
    """DeepSeek 官方承认的缺陷：JSON Output 会随机返回一串空白字符。

    实测同一句话第一次好、第二次就是 '   '，命中率能到一半。
    不重试的话，用户看到的是「模型没给出内容」，而且一半的消息回不出去。
    """
    calls = []

    def _post(url, **kw):
        calls.append(kw.get("json"))
        if len(calls) == 1:
            return _Resp(_openai_payload("     "))          # 纯空白
        return _Resp(_openai_payload('{"should_reply":true,"reply":"好的"}'))
    monkeypatch.setattr(requests, "post", _post)

    out = llm.chat(cfg, "s", "u")
    assert "好的" in out.text
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]     # 重试时必须去掉，否则照样空白


def test_重试的token也计入花销(monkeypatch, cfg):
    """两次都要计费，只记一次会让用量统计偏低。"""
    calls = []

    def _post(url, **kw):
        calls.append(1)
        return _Resp(_openai_payload("   " if len(calls) == 1 else "好的"))
    monkeypatch.setattr(requests, "post", _post)
    assert llm.chat(cfg, "s", "u").tokens == 1440     # 720 × 2


def test_重试仍然空白就不再重试(monkeypatch, cfg):
    """避免在真的没内容时无限重试烧钱。"""
    calls = []

    def _post(url, **kw):
        calls.append(1)
        return _Resp(_openai_payload("   "))
    monkeypatch.setattr(requests, "post", _post)
    assert llm.chat(cfg, "s", "u").text.strip() == ""
    assert len(calls) == 2


def test_关思考模式不触发这个重试(monkeypatch, cfg):
    """那边压根没发 response_format，空白是别的原因，重试也没用。"""
    calls = []

    def _post(url, **kw):
        calls.append(1)
        return _Resp(_openai_payload("   "))
    monkeypatch.setattr(requests, "post", _post)
    llm.chat(llm.LLMConfig(base_url="https://x.test/v1", api_key="k",
                           model="m", thinking=False), "s", "u")
    assert len(calls) == 1


def test_有内容时不会白重试一次(monkeypatch, cfg):
    calls = []

    def _post(url, **kw):
        calls.append(1)
        return _Resp(_openai_payload('{"reply":"好的"}'))
    monkeypatch.setattr(requests, "post", _post)
    llm.chat(cfg, "s", "u")
    assert len(calls) == 1
