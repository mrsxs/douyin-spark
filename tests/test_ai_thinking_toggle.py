"""思考开关：两种输出模式必须整套切换。

背景：这个网关上 thinking=disabled 和 response_format=json_object
不能并存（同时给会返回一串空白）。所以「关思考」不只是省钱，
它连带把输出格式从 JSON 换成纯文本，弃权信号也得跟着换成 [SKIP] 哨兵。

契约、示例、请求参数、Anthropic 预填 —— 这四样必须同步切换，
漏一个就是「模型照着示例吐 JSON，但那边根本没开 response_format」。
"""
import json

import pytest
import requests

from app import ai_reply, ai_reply_config, llm


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._p


def _capture(monkeypatch, payload):
    """拦下请求体，用来断言到底发了什么参数。"""
    seen = []

    def _post(url, **kw):
        seen.append(kw.get("json"))
        return _Resp(payload)
    monkeypatch.setattr(requests, "post", _post)
    return seen


_OK = {"choices": [{"message": {"content": "好的"}, "finish_reason": "stop"}],
       "usage": {"total_tokens": 10}}


# ── 请求参数：两个参数绝不能并存 ──────────────────────────

def test_开思考时强制json且不带thinking参数(monkeypatch):
    seen = _capture(monkeypatch, _OK)
    llm.chat(llm.LLMConfig(base_url="https://x.test/v1", api_key="k",
                           model="m", thinking=True), "s", "u")
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert "thinking" not in seen[0]


def test_关思考时禁用思考且不带response_format(monkeypatch):
    """同时给会返回一串空白字符 —— 实测过，不是理论问题。"""
    seen = _capture(monkeypatch, _OK)
    llm.chat(llm.LLMConfig(base_url="https://x.test/v1", api_key="k",
                           model="m", thinking=False), "s", "u")
    assert seen[0]["thinking"] == {"type": "disabled"}
    assert "response_format" not in seen[0]


def test_网关不认这些扩展参数时去掉重试(monkeypatch):
    """两个都是厂商扩展，别的网关会 400。"""
    calls = []

    def _post(url, **kw):
        calls.append(kw.get("json"))
        if len(calls) == 1:
            return _Resp({"error": "unknown field"}, status=400)
        return _Resp(_OK)
    monkeypatch.setattr(requests, "post", _post)

    llm.chat(llm.LLMConfig(base_url="https://x.test/v1", api_key="k",
                           model="m", thinking=False), "s", "u")
    assert len(calls) == 2
    assert "thinking" not in calls[1]
    assert "response_format" not in calls[1]


# ── Anthropic：预填只在 JSON 模式下做 ─────────────────────

def test_anthropic开思考时预填json(monkeypatch):
    seen = _capture(monkeypatch, {"content": [{"type": "text", "text": ' true,"reply":"好"}'}],
                                  "stop_reason": "end_turn", "usage": {}})
    out = llm.chat(llm.LLMConfig(provider="anthropic", base_url="https://a.test",
                                 api_key="k", model="c", thinking=True), "s", "u").text
    assert seen[0]["messages"][-1]["role"] == "assistant"
    assert json.loads(out)["reply"] == "好"


def test_anthropic关思考时不预填(monkeypatch):
    """预填了模型会接着写 JSON，和纯文本契约、纯文本示例全对不上。"""
    seen = _capture(monkeypatch, {"content": [{"type": "text", "text": "好的呀"}],
                                  "stop_reason": "end_turn", "usage": {}})
    out = llm.chat(llm.LLMConfig(provider="anthropic", base_url="https://a.test",
                                 api_key="k", model="c", thinking=False), "s", "u").text
    assert seen[0]["messages"][-1]["role"] == "user"
    assert out == "好的呀"


# ── 契约与示例成对切换 ────────────────────────────────────

def test_开思考用json契约():
    s = ai_reply.build_system_prompt("", "", 60, thinking=True)
    assert "只输出一个 JSON 对象" in s
    assert "[SKIP]" not in s


def test_关思考用纯文本契约与哨兵():
    s = ai_reply.build_system_prompt("", "", 60, thinking=False)
    assert "直接输出回复正文本身" in s
    assert "[SKIP]" in s
    assert "只输出一个 JSON 对象" not in s


def test_示例形态跟着契约走():
    """示例还是 JSON 的话，模型会照着示例吐 JSON —— 那边没开 response_format。"""
    on = ai_reply.build_system_prompt("", "", 60, thinking=True)
    off = ai_reply.build_system_prompt("", "", 60, thinking=False)
    assert '{"should_reply": true' in on
    assert '{"should_reply"' not in off
    assert "输出：咋啦" in off


def test_自定义示例两种模式都原样用():
    """用户自己写的就是用户负责，界面上会提示他检查形态。"""
    for mode in (True, False):
        s = ai_reply.build_system_prompt("", "", 60, fewshot="我的示例", thinking=mode)
        assert "我的示例" in s


def test_两种模式的红线都在():
    for mode in (True, False):
        s = ai_reply.build_system_prompt("", "", 60, thinking=mode)
        for redline in ("转账", "借钱", "微信", "诈骗"):
            assert redline in s


# ── 哨兵词识别 ───────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "[SKIP]", "SKIP", " [SKIP] ", "[skip]", "[SKIP]。", '"[SKIP]"', "`[SKIP]`",
])
def test_哨兵词各种写法都算弃权(raw):
    text, why = ai_reply.sanitize_reply(raw, ai_reply.ReplyPolicy())
    assert text is None
    assert why == "model_declined"


@pytest.mark.parametrize("raw", [
    "我不会skip的", "这个skip掉吧", "跳过SKIP这一关",
])
def test_正文里出现skip不算弃权(raw):
    """只有整条就是哨兵才算 —— 夹在句子里多半是模型在复述规则。"""
    text, _ = ai_reply.sanitize_reply(raw, ai_reply.ReplyPolicy())
    assert text is not None


def test_关思考模式下纯文本正常通过():
    text, why = ai_reply.sanitize_reply("在的，咋啦", ai_reply.ReplyPolicy())
    assert text == "在的，咋啦"
    assert why == "ok"


def test_关思考模式下硬拦截照样生效():
    """模型判断力降了，但链接、手机号这些是代码拦的，不受影响。"""
    for bad, reason in [("看这个 https://x.com/a", "link"),
                        ("打我 13812345678", "phone"),
                        ("加微信 abc12345", "contact")]:
        text, why = ai_reply.sanitize_reply(bad, ai_reply.ReplyPolicy())
        assert text is None and why == reason


# ── 配置层串联 ───────────────────────────────────────────

def test_开关默认是开(db, active_user):
    """升级前的行为是带思考的，默认值不能改变现有用户的表现。"""
    _, a = active_user
    ai_reply_config.save(db, a.id, {"model": "m"})
    db.commit()
    assert ai_reply_config.load(db, a.id).thinking is True
    assert ai_reply_config.to_public(ai_reply_config.load(db, a.id))["thinking"] is True


def test_开关存得进读得出(db, active_user):
    _, a = active_user
    ai_reply_config.save(db, a.id, {"thinking": False})
    db.commit()
    cfg = ai_reply_config.load(db, a.id)
    assert cfg.thinking is False
    assert ai_reply_config.resolve(cfg, None).thinking is False


def test_关掉后系统提示词换成纯文本契约(db, active_user):
    _, a = active_user
    ai_reply_config.save(db, a.id, {"thinking": False})
    db.commit()
    s = ai_reply_config.resolve(ai_reply_config.load(db, a.id), None).system_prompt("")
    assert "[SKIP]" in s
    assert "只输出一个 JSON 对象" not in s


def test_接口能读写这个开关(db, active_user, login):
    from tests.test_ai_api import _W
    u, a = active_user
    c = _W(login(u))
    c.put(f"/api/ai/{a.id}", json={"thinking": False})
    assert c.get(f"/api/ai/{a.id}").json()["config"]["thinking"] is False
    c.put(f"/api/ai/{a.id}", json={"thinking": True})
    assert c.get(f"/api/ai/{a.id}").json()["config"]["thinking"] is True


def test_预览接口跟着开关变(db, active_user, login):
    from tests.test_ai_api import _W
    u, a = active_user
    c = _W(login(u))
    c.put(f"/api/ai/{a.id}", json={"thinking": False})
    assert "[SKIP]" in c.get(f"/api/ai/{a.id}/prompt").json()["system_prompt"]
