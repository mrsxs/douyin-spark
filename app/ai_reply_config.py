"""AI 自动回复的配置层：账号级配置 + 联系人级开关/覆盖。

两级设计的原因：
- 账号级放「怎么接大模型」（provider/key/model）和默认话术；
- 联系人级放「对谁回、对他怎么回」。

**白名单语义**：只有 AiReplyPeer.enabled=True 的联系人才会被自动回复。
默认谁都不回 —— 一个手滑让所有好友同时收到 AI 回复是不可撤销的。

api_key 走 app.crypto 加密存，任何接口都只回 has_key 布尔值，不回明文。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ai_reply, crypto
from .models import AiReplyConfig, AiReplyPeer

PROVIDERS = ("openai", "anthropic")

MAX_CHARS_RANGE = (10, 200)
COOLDOWN_RANGE = (5, 3600)
DAILY_LIMIT_RANGE = (1, 2000)
HISTORY_RANGE = (0, 20)
# 常驻轮询的下限比聊天页(3s)高得多：没人看的时候还几秒一次打抖音，
# 纯粹是在给风控送素材。
POLL_RANGE = (15, 600)


def _clamp(value, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def get_or_create(db: Session, account_id: int) -> AiReplyConfig:
    """取配置行，没有就建一个（默认关闭）。不 commit。"""
    row = db.scalar(select(AiReplyConfig).where(
        AiReplyConfig.douyin_account_id == account_id))
    if row is None:
        row = AiReplyConfig(
            douyin_account_id=account_id,
            persona=ai_reply.DEFAULT_PERSONA,
            prompt_template=ai_reply.DEFAULT_PROMPT_TEMPLATE,
            reply_format=ai_reply.DEFAULT_REPLY_FORMAT,
        )
        db.add(row)
        db.flush()
    return row


def load(db: Session, account_id: int) -> AiReplyConfig | None:
    """只读取，不创建 —— 轮询热路径上不该每次都写库。"""
    return db.scalar(select(AiReplyConfig).where(
        AiReplyConfig.douyin_account_id == account_id))


def save(db: Session, account_id: int, data: dict) -> AiReplyConfig:
    """写配置。只认白名单字段，数值一律夹逼到合法区间。不 commit。"""
    row = get_or_create(db, account_id)

    if "provider" in data:
        p = str(data.get("provider") or "openai").strip()
        row.provider = p if p in PROVIDERS else "openai"
    if "base_url" in data:
        row.base_url = (data.get("base_url") or "").strip()[:255]
    if "model" in data:
        row.model = (data.get("model") or "").strip()[:80]
    # 空串 = 不改（前端回显不到明文，提交空值只意味着"没动它"）；
    # 显式传 null 才是清空。
    if "api_key" in data:
        key = data.get("api_key")
        if key is None:
            row.api_key_enc = ""
        elif str(key).strip():
            row.api_key_enc = crypto.encrypt(str(key).strip())

    if "persona" in data:
        row.persona = (data.get("persona") or "").strip()[:2000]
    if "prompt_template" in data:
        row.prompt_template = (data.get("prompt_template") or "").strip()[:2000]
    if "reply_format" in data:
        row.reply_format = (data.get("reply_format") or "").strip()[:200]
    # 策略和示例给得宽：它们是整段提示词，不是一句话
    if "decline_policy" in data:
        row.decline_policy = (data.get("decline_policy") or "").strip()[:4000]
    if "fewshot" in data:
        row.fewshot = (data.get("fewshot") or "").strip()[:4000]
    if "banned_words" in data:
        row.banned_words = (data.get("banned_words") or "").strip()[:1000]

    if "max_chars" in data:
        row.max_chars = _clamp(data["max_chars"], *MAX_CHARS_RANGE, 60)
    if "cooldown_sec" in data:
        row.cooldown_sec = _clamp(data["cooldown_sec"], *COOLDOWN_RANGE, 20)
    if "daily_limit" in data:
        row.daily_limit = _clamp(data["daily_limit"], *DAILY_LIMIT_RANGE, 100)
    if "history_turns" in data:
        row.history_turns = _clamp(data["history_turns"], *HISTORY_RANGE, 6)
    if "poll_interval" in data:
        row.poll_interval = _clamp(data["poll_interval"], *POLL_RANGE, 30)

    if "thinking" in data:
        row.thinking = bool(data.get("thinking"))
    if "reply_share" in data:
        row.reply_share = bool(data.get("reply_share"))
    if "reply_voice" in data:
        row.reply_voice = bool(data.get("reply_voice"))

    if "asr_base_url" in data:
        row.asr_base_url = (data.get("asr_base_url") or "").strip()[:255]
    if "asr_model" in data:
        row.asr_model = (data.get("asr_model") or "").strip()[:80]
    # 和 api_key 同一套语义：空串 = 没动它，显式 null 才是清空
    if "asr_api_key" in data:
        key = data.get("asr_api_key")
        if key is None:
            row.asr_key_enc = ""
        elif str(key).strip():
            row.asr_key_enc = crypto.encrypt(str(key).strip())

    if "enabled" in data:
        want = bool(data.get("enabled"))
        # 关→开时刷新 enabled_at：早于这一刻的历史消息一律不回。
        # 不刷新的话，关了一周再打开，这一周攒的消息会被一次性全回一遍。
        if want and not row.enabled:
            row.enabled_at = datetime.utcnow()
            row.fail_streak = 0
        row.enabled = want
    return row


def to_public(row: AiReplyConfig | None) -> dict:
    """给前端的形状。**永远不含 api_key 明文** —— 只说有没有配。

    decline_policy / fewshot 回空串表示"没自定义，正在用默认值"。
    前端据此把输入框留空、显示默认内容做占位 —— 用户一眼能看出
    自己有没有改过，也知道清空就是恢复默认。
    """
    if row is None:
        return {
            "enabled": False, "provider": "openai", "base_url": "", "model": "",
            "has_key": False, "persona": ai_reply.DEFAULT_PERSONA,
            "prompt_template": ai_reply.DEFAULT_PROMPT_TEMPLATE,
            "reply_format": ai_reply.DEFAULT_REPLY_FORMAT, "banned_words": "",
            "decline_policy": "", "fewshot": "",
            "thinking": True, "reply_share": False, "reply_voice": False,
            "asr_base_url": "", "asr_model": "", "has_asr_key": False,
            "max_chars": 60, "cooldown_sec": 20, "daily_limit": 100,
            "history_turns": 6, "poll_interval": 30,
        }
    return {
        "enabled": bool(row.enabled),
        "provider": row.provider,
        "base_url": row.base_url or "",
        "model": row.model or "",
        "has_key": bool(row.api_key_enc),
        "persona": row.persona or "",
        "prompt_template": row.prompt_template or "",
        "reply_format": row.reply_format or "",
        "banned_words": row.banned_words or "",
        "decline_policy": row.decline_policy or "",
        "fewshot": row.fewshot or "",
        "thinking": bool(row.thinking),
        "reply_share": bool(row.reply_share),
        "reply_voice": bool(row.reply_voice),
        "asr_base_url": row.asr_base_url or "",
        "asr_model": row.asr_model or "",
        # 和 has_key 一样：只说配没配，永不回明文
        "has_asr_key": bool(row.asr_key_enc),
        "max_chars": row.max_chars,
        "cooldown_sec": row.cooldown_sec,
        "daily_limit": row.daily_limit,
        "history_turns": row.history_turns,
        "poll_interval": row.poll_interval,
    }


def defaults() -> dict:
    """内置的策略原文。前端要拿它做占位符和「恢复默认」。

    直接把代码里的常量吐给界面 —— 这样用户看到的和模型收到的
    是同一份文本，不会出现"界面上写着 A、实际发的是 B"。
    """
    return {
        "output_contract": ai_reply.OUTPUT_CONTRACT,
        "decline_policy": ai_reply.DECLINE_POLICY,
        "fewshot": ai_reply.FEWSHOT,
        "persona": ai_reply.DEFAULT_PERSONA,
        "prompt_template": ai_reply.DEFAULT_PROMPT_TEMPLATE,
        "reply_format": ai_reply.DEFAULT_REPLY_FORMAT,
    }


def api_key(row: AiReplyConfig | None) -> str:
    return crypto.decrypt(row.api_key_enc) if row and row.api_key_enc else ""


def asr_api_key(row: AiReplyConfig | None) -> str:
    return crypto.decrypt(row.asr_key_enc) if row and row.asr_key_enc else ""


# ── 联系人级 ──────────────────────────────────────────────

def get_peer(db: Session, account_id: int, uid: str) -> AiReplyPeer | None:
    return db.scalar(select(AiReplyPeer).where(
        AiReplyPeer.douyin_account_id == account_id,
        AiReplyPeer.uid == str(uid)))


def set_peer(db: Session, account_id: int, uid: str, data: dict) -> AiReplyPeer:
    """建/改联系人级设置。不 commit。"""
    row = get_peer(db, account_id, uid)
    if row is None:
        row = AiReplyPeer(douyin_account_id=account_id, uid=str(uid))
        db.add(row)
    if "enabled" in data:
        row.enabled = bool(data.get("enabled"))
    # 空串 → NULL：语义是"清空覆盖，回到继承账号级"
    if "persona" in data:
        v = (data.get("persona") or "").strip()
        row.persona = v[:2000] or None
    if "reply_format" in data:
        v = (data.get("reply_format") or "").strip()
        row.reply_format = v[:200] or None
    # 三态：None = 继承账号级，True/False = 只对这个人生效。
    # 前端传 null 表示"清空覆盖"
    if "reply_share" in data:
        v = data.get("reply_share")
        row.reply_share = None if v is None else bool(v)
    if "reply_voice" in data:
        v = data.get("reply_voice")
        row.reply_voice = None if v is None else bool(v)
    return row


def enabled_uids(db: Session, account_id: int) -> set[str]:
    return set(db.scalars(select(AiReplyPeer.uid).where(
        AiReplyPeer.douyin_account_id == account_id,
        AiReplyPeer.enabled.is_(True),
    )).all())


def peer_map(db: Session, account_id: int) -> dict[str, dict]:
    """给前端一次性回全部联系人级设置，省得每个联系人一个请求。"""
    rows = db.scalars(select(AiReplyPeer).where(
        AiReplyPeer.douyin_account_id == account_id)).all()
    return {r.uid: {"enabled": bool(r.enabled),
                    "persona": r.persona or "",
                    "reply_format": r.reply_format or "",
                    # None 要原样透出：前端要区分"继承"和"显式关掉"
                    "reply_share": r.reply_share,
                    "reply_voice": r.reply_voice} for r in rows}


# ── 合并后的生效配置 ──────────────────────────────────────

@dataclass(frozen=True)
class Effective:
    """某个联系人此刻真正生效的配置（账号级 + 联系人级覆盖）。"""
    persona: str
    prompt_template: str
    reply_format: str
    max_chars: int
    cooldown_sec: int
    daily_limit: int
    history_turns: int
    banned_words: tuple[str, ...]
    # 空串 = 用 ai_reply 里的内置默认（build_system_prompt 负责兜底）
    decline_policy: str = ""
    fewshot: str = ""
    thinking: bool = True
    # 是否回复对方分享的视频（要多打一次抖音解析接口，默认关）
    reply_share: bool = False
    # 是否回复对方发的语音（要下载音频 + 调 ASR，默认关）
    reply_voice: bool = False

    def policy(self) -> ai_reply.ReplyPolicy:
        return ai_reply.ReplyPolicy(
            max_chars=self.max_chars,
            reply_format=self.reply_format,
            banned_words=self.banned_words,
        )

    def system_prompt(self, knowledge: str = "") -> str:
        """拼出实际发给模型的 system prompt。

        编排层、试跑、界面预览都走这一个入口 ——
        三处各拼一遍的话，界面上显示的和真正发出去的迟早对不上。
        """
        return ai_reply.build_system_prompt(
            self.persona, knowledge, self.max_chars,
            decline_policy=self.decline_policy, fewshot=self.fewshot,
            thinking=self.thinking)


def resolve(cfg: AiReplyConfig, peer: AiReplyPeer | None) -> Effective:
    """联系人级填了就用它的，没填继承账号级 —— 「对客户正式、对朋友随意」。"""
    words = tuple(w.strip() for w in (cfg.banned_words or "")
                  .replace("，", ",").split(",") if w.strip())
    return Effective(
        persona=(peer.persona if peer and peer.persona else cfg.persona) or ai_reply.DEFAULT_PERSONA,
        prompt_template=cfg.prompt_template or ai_reply.DEFAULT_PROMPT_TEMPLATE,
        reply_format=(peer.reply_format if peer and peer.reply_format
                      else cfg.reply_format) or ai_reply.DEFAULT_REPLY_FORMAT,
        max_chars=cfg.max_chars,
        cooldown_sec=cfg.cooldown_sec,
        daily_limit=cfg.daily_limit,
        history_turns=cfg.history_turns,
        banned_words=words,
        decline_policy=cfg.decline_policy or "",
        fewshot=cfg.fewshot or "",
        thinking=bool(cfg.thinking),
        # 三态：联系人级 None 表示继承账号级，不能用 `or` 折叠 ——
        # 那样联系人显式设的 False 会被账号级的 True 顶掉
        reply_share=bool(cfg.reply_share) if (peer is None or peer.reply_share is None)
                    else bool(peer.reply_share),
        reply_voice=bool(cfg.reply_voice) if (peer is None or peer.reply_voice is None)
                    else bool(peer.reply_voice),
    )
