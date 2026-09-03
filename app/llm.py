"""大模型 HTTP 客户端 —— OpenAI 兼容 + Anthropic 双协议。

两套的原因：国内私信场景性价比最高的是 OpenAI 兼容那一档
（DeepSeek / 通义 / Kimi / 智谱 / 自建 vLLM 都是同一个 /chat/completions），
但也有人手里就是 Claude 的 key。抽一层 Protocol，换厂不改业务代码。

只用 requests（已在依赖里）—— 为了一个 HTTP POST 引 openai/anthropic SDK
会把生产镜像撑大一圈，还得跟着它们的大版本升级走。

安全：api_key 只出现在请求头里。任何异常信息都不带 key，
错误里的响应体也截断 —— 有些网关会把请求头回显在错误 JSON 里。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import requests

# 响应体在异常里的截断长度：够定位问题，又不至于把整页 HTML 或回显的凭证灌进日志
_ERR_BODY_MAX = 200

PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"

# 让模型输出 JSON 的两种手段：
# OpenAI 兼容走 response_format，Anthropic 没这个参数，改用 assistant 预填 ——
# 预填一段开头，模型只能顺着往下写，等于强制它进入 JSON 分支。
_ANTHROPIC_PREFILL = '{"should_reply":'


class LLMError(Exception):
    """调用失败。message 保证不含 api_key。"""


# 输出 token 预算。给得这么宽是因为**推理模型的思考过程算在 max_tokens 里**：
# DeepSeek-V4 回一句「咋啦」，实测思考在 250~2000+ token 之间浮动，
# 同一句话不同时候能差十倍。预算给小了，思考没结束就被砍断，
# content 直接是空字符串 —— 界面上显示「模型没给出内容」，无从排查。
#
# 更关键的是**截断那次的钱照付**：思考 token 已经产生并计费了，
# 砍掉只是让你付了钱拿不到答案。所以宁可给宽 ——
# 回复正文由 sanitize_reply 截到几十个字，宽预算既不会让回复变长，
# 也不会多花钱（按实际生成量计费，不是按上限）。
DEFAULT_MAX_TOKENS = 4096


@dataclass(frozen=True)
class LLMConfig:
    provider: str = PROVIDER_OPENAI
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: int = 20
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = 0.7
    # 见 _call_openai：同时决定要不要强制 JSON 输出
    thinking: bool = True

    # 语音转写。独立于上面三项 —— 主网关常是 DeepSeek，它没有
    # /audio/transcriptions。三项缺一即视为未启用（见 transcribe）。
    asr_base_url: str = ""
    asr_model: str = ""
    asr_api_key: str = ""


@dataclass(frozen=True)
class LLMResult:
    text: str
    tokens: int = 0
    latency_ms: int = 0


def chat(cfg: LLMConfig, system: str, user: str,
         history: list[dict] | None = None) -> LLMResult:
    """发一轮对话，返回模型的原始文本（不做清洗 —— 那是 ai_reply 的活）。

    history 是 [{"role": "user"|"assistant", "content": str}]，按时间正序。
    """
    if not cfg.model:
        raise LLMError("未配置模型名")
    if not cfg.api_key:
        raise LLMError("未配置 API Key")

    started = time.monotonic()
    if cfg.provider == PROVIDER_ANTHROPIC:
        text, tokens = _call_anthropic(cfg, system, user, history or [])
    else:
        text, tokens = _call_openai(cfg, system, user, history or [])
    return LLMResult(text=text, tokens=tokens,
                     latency_ms=int((time.monotonic() - started) * 1000))


# ── OpenAI 兼容 ───────────────────────────────────────────

DEFAULT_OPENAI_BASE = "https://api.deepseek.com/v1"


def openai_endpoint(base_url: str) -> str:
    """容忍用户把 base_url 填成三种样子：根域名 / 带 /v1 / 完整端点。

    填错 base_url 是接入时最常见的错，与其让他对着 404 猜，不如这里兜住。
    """
    b = (base_url or DEFAULT_OPENAI_BASE).strip().rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    if not b.endswith("/v1") and "/v1/" not in b:
        b += "/v1"
    return b + "/chat/completions"


def _call_openai(cfg: LLMConfig, system: str, user: str,
                 history: list[dict]) -> tuple[str, int]:
    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": user})

    body = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "stream": False,
    }
    # 这两个参数**不能并存** —— 实测同时给会返回一串空白字符。
    # 开思考：强制 JSON，模型能用 should_reply 表达弃权，贵而稳。
    # 关思考：收纯文本，弃权靠 [SKIP] 哨兵词，token 省一半以上、快数倍。
    if cfg.thinking:
        body["response_format"] = {"type": "json_object"}
    else:
        body["thinking"] = {"type": "disabled"}

    try:
        data = _post(openai_endpoint(cfg.base_url), body,
                     {"Authorization": f"Bearer {cfg.api_key}"}, cfg.timeout)
    except LLMError as e:
        # 这两个都是厂商扩展，不认识的网关会 400。去掉重试一次 ——
        # 输出照样能用，sanitize 的纯文本兜底会接住。
        if "400" not in str(e):
            raise
        # 复制一份再改：body 已经发出去过，就地改写会让日志/重试里的
        # 请求体和真正发出的对不上，排查时极易被误导。
        body = {k: v for k, v in body.items()
                if k not in ("response_format", "thinking")}
        data = _post(openai_endpoint(cfg.base_url), body,
                     {"Authorization": f"Bearer {cfg.api_key}"}, cfg.timeout)

    text, tokens = _content_of(data)

    # DeepSeek 官方承认的 JSON Output 缺陷：会随机返回一串空白字符。
    # 实测同一句话第一次好、第二次就是 '   '，命中率能到一半。
    # 去掉 response_format 重试一次 —— 模型照样按提示词吐 JSON，
    # 就算退化成纯文本，_extract_reply 的兜底也接得住。
    if not text.strip() and "response_format" in body:
        retry_body = {k: v for k, v in body.items() if k != "response_format"}
        retry, retry_tokens = _content_of(_post(
            openai_endpoint(cfg.base_url), retry_body,
            {"Authorization": f"Bearer {cfg.api_key}"}, cfg.timeout))
        # 两次都要计费，加起来才是这条回复的真实花销
        tokens += retry_tokens
        if retry.strip():
            return retry, tokens
        text = retry

    if not text.strip():
        _raise_if_truncated(_finish_of(data), cfg.max_tokens,
                            (data.get("usage") or {})
                            .get("completion_tokens_details", {})
                            .get("reasoning_tokens"))
    return text, tokens


def _content_of(data: dict) -> tuple[str, int]:
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"响应结构异常: {_clip(json.dumps(data, ensure_ascii=False))}")
    return text, int((data.get("usage") or {}).get("total_tokens") or 0)


def _finish_of(data: dict) -> str | None:
    try:
        return data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return None


# ── Anthropic ────────────────────────────────────────────

DEFAULT_ANTHROPIC_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


def anthropic_endpoint(base_url: str) -> str:
    b = (base_url or DEFAULT_ANTHROPIC_BASE).strip().rstrip("/")
    if b.endswith("/messages"):
        return b
    if not b.endswith("/v1"):
        b += "/v1"
    return b + "/messages"


def _call_anthropic(cfg: LLMConfig, system: str, user: str,
                    history: list[dict]) -> tuple[str, int]:
    messages = list(history)
    messages.append({"role": "user", "content": user})
    # 预填最后一个 assistant turn，把模型摁进 JSON 分支。
    # 关思考时契约要的是纯文本，这时不能预填 —— 预填了模型会接着写 JSON，
    # 和契约、和示例全都对不上。
    if cfg.thinking:
        messages.append({"role": "assistant", "content": _ANTHROPIC_PREFILL})

    body = {
        "model": cfg.model,
        "system": system,
        "messages": messages,
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature,
    }
    data = _post(anthropic_endpoint(cfg.base_url), body, {
        "x-api-key": cfg.api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }, cfg.timeout)

    try:
        text = "".join(blk.get("text") or "" for blk in (data.get("content") or [])
                       if blk.get("type") == "text")
    except (AttributeError, TypeError):
        raise LLMError(f"响应结构异常: {_clip(json.dumps(data, ensure_ascii=False))}")
    if not text.strip():
        # extended thinking 同样吃 max_tokens，截断表现和 OpenAI 那边一样
        _raise_if_truncated(data.get("stop_reason"), cfg.max_tokens, None)
    usage = data.get("usage") or {}
    tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    # 预填的那截不在响应里，补回去才是完整 JSON。没预填就原样返回。
    return (_ANTHROPIC_PREFILL + text if cfg.thinking else text), tokens


# 两家表示「被 max_tokens 砍断」的字段名不同，但语义一样
_TRUNCATED = ("length", "max_tokens")


def _raise_if_truncated(finish_reason: str | None, max_tokens: int,
                        reasoning_tokens: int | None) -> None:
    """输出为空且是被截断的，给一条能直接照做的错误。

    不这么做的话，界面上只会显示「模型没给出内容」——
    推理模型把 max_tokens 全烧在思考上是最常见的接入故障，
    而这个症状完全指不出原因，用户只能一遍遍重试、一遍遍付费。
    """
    if (finish_reason or "").lower() not in _TRUNCATED:
        return
    extra = f"（其中思考占 {reasoning_tokens}）" if reasoning_tokens else ""
    raise LLMError(
        f"输出被 max_tokens={max_tokens} 截断{extra}，没留下正文。"
        f"这个模型会思考，思考过程也算在 max_tokens 里，请调大它。")


# ── 语音转文字（OpenAI 兼容 /audio/transcriptions）─────────
#
# 抖音的 IM 语音不带转写，只能自己接。走 OpenAI 的
# /audio/transcriptions 形状 —— 硅基流动、Groq、OpenAI、本地
# whisper.cpp server 都认这个，用户换供应商不用改代码。
#
# 独立于主网关配置：主网关默认是 DeepSeek，它没有这个接口。
# 三项（base_url / model / key）缺一就是没启用，静默不转写。

# 转写文本会写回 ChatMessage.media.asr，而那个字段整体有
# 2000 字节上限（见 messages_service._MEDIA_MAX）。中文一字 3 字节，
# 加上 src / wave 已占的部分，300 字是安全线。
ASR_TEXT_MAX = 300
_ASR_TIMEOUT = 60


def asr_endpoint(base_url: str) -> str:
    """和 openai_endpoint 一样容忍 base_url 的三种写法。"""
    b = (base_url or "").strip().rstrip("/")
    if not b:
        return ""
    if b.endswith("/audio/transcriptions"):
        return b
    if not b.endswith("/v1") and "/v1/" not in b:
        b += "/v1"
    return b + "/audio/transcriptions"


def transcribe(cfg, audio: bytes, filename: str = "voice.mp3") -> str:
    """把一段语音转成文字。没配全 ASR 或没音频时返回 ""。

    出错抛 LLMError（和 chat 一致），由调用方决定是重试还是放弃。
    """
    url = asr_endpoint(getattr(cfg, "asr_base_url", ""))
    model = (getattr(cfg, "asr_model", "") or "").strip()
    key = (getattr(cfg, "asr_api_key", "") or "").strip()
    if not (url and model and key and audio):
        return ""

    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (filename, audio, "audio/mpeg")},
            data={"model": model},
            timeout=_ASR_TIMEOUT,
        )
    except requests.Timeout:
        raise LLMError(f"语音转写超时（{_ASR_TIMEOUT}s）")
    except requests.RequestException as e:
        raise LLMError(f"语音转写网络错误: {type(e).__name__}")

    if resp.status_code != 200:
        raise LLMError(f"语音转写 HTTP {resp.status_code}: {_clip(resp.text)}")
    try:
        data = resp.json()
    except ValueError:
        raise LLMError(f"语音转写响应不是 JSON: {_clip(resp.text)}")

    # 多数网关回 {"text": ...}，少数回 {"result": ...}
    raw = data.get("text") if isinstance(data, dict) else ""
    if not isinstance(raw, str) or not raw.strip():
        raw = data.get("result") if isinstance(data, dict) else ""
    text = raw.strip() if isinstance(raw, str) else ""
    if len(text) > ASR_TEXT_MAX:
        text = text[:ASR_TEXT_MAX] + "…"
    return text


# ── 共用 ─────────────────────────────────────────────────

def _post(url: str, body: dict, headers: dict, timeout: int) -> dict:
    try:
        resp = requests.post(
            url, json=body, timeout=timeout,
            headers={"Content-Type": "application/json", **headers})
    except requests.Timeout:
        raise LLMError(f"请求超时（{timeout}s）")
    except requests.RequestException as e:
        raise LLMError(f"网络错误: {type(e).__name__}")

    if resp.status_code != 200:
        raise LLMError(f"HTTP {resp.status_code}: {_clip(resp.text)}")
    try:
        return resp.json()
    except ValueError:
        raise LLMError(f"响应不是 JSON: {_clip(resp.text)}")


def _clip(s: str) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s[:_ERR_BODY_MAX] + ("…" if len(s) > _ERR_BODY_MAX else "")
