"""AI 自动回复的配置接口。

单独一个 router 而不是塞进 api.py：那边已经 590 行，
再加配置 + 知识库 + 白名单 + 试跑会彻底失控。

安全约定：
- api_key **只进不出**。GET 只回 has_key 布尔值，任何路径都不回明文。
- 所有查询都带 user_id 归属过滤 —— 换个 account_id 就能读别人的知识库
  和聊天记录，这里是唯一的闸门。
- 试跑接口要花钱调模型，挂限流。
"""
from __future__ import annotations

import traceback
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ai_reply, ai_reply_config, ai_worker, knowledge_service, llm
from ..db import get_db
from ..deps import require_active
from ..models import AiReplyLog, AuditLog, Contact, DouyinAccount
from ..ratelimit import limiter

router = APIRouter(prefix="/api/ai")

# 试跑要真调模型，是花钱的。限得比登录还紧一点 ——
# 正常调试话术一分钟点不了十次。
TEST_LIMIT = "10/minute"

LOGS_MAX = 100


def _acc(account_id: int, user, db: Session) -> DouyinAccount:
    acc = db.scalar(select(DouyinAccount).where(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id))
    if not acc:
        raise HTTPException(404, "账户不存在")
    return acc


@router.get("/{account_id}")
def get_config(account_id: int, user=Depends(require_active),
               db: Session = Depends(get_db)):
    """一次回全：账号级配置 + 每个联系人的开关 + 知识库条数 + 策略默认值。

    聊天页一进来就要这些，拆成三个请求只会让面板闪三次。
    defaults 里是代码内置的策略原文，界面拿它做占位和「恢复默认」——
    用户看到的就是模型真正收到的那段文本。
    """
    acc = _acc(account_id, user, db)
    cfg = ai_reply_config.load(db, acc.id)
    return {
        "ok": True,
        "config": ai_reply_config.to_public(cfg),
        "peers": ai_reply_config.peer_map(db, acc.id),
        "kb_counts": knowledge_service.count_entries(db, acc.id),
        "global_uid": knowledge_service.GLOBAL_UID,
        "defaults": ai_reply_config.defaults(),
    }


@router.get("/{account_id}/prompt")
def preview_prompt(account_id: int, uid: str = "",
                   user=Depends(require_active), db: Session = Depends(get_db)):
    """把此刻真正会发给模型的 system prompt 原样吐出来。

    改策略最怕的是「界面上写了 A，实际发的是 B」—— 有这个预览，
    用户能直接看到人设、弃权策略、示例拼接后的完整样子，
    包括联系人级覆盖生效没有。
    """
    acc = _acc(account_id, user, db)
    cfg = ai_reply_config.load(db, acc.id)
    if not cfg:
        cfg = ai_reply_config.get_or_create(db, acc.id)
        db.rollback()          # 只为拿默认值，不落库
    peer = ai_reply_config.get_peer(db, acc.id, uid) if uid else None
    eff = ai_reply_config.resolve(cfg, peer)
    return {
        "ok": True,
        # 知识是按对方那句话现查的，预览时给个占位说明，别让人以为知识库没生效
        "system_prompt": eff.system_prompt(""),
        "user_prompt_template": eff.prompt_template,
        "uses_custom_policy": bool(eff.decline_policy),
        "uses_custom_fewshot": bool(eff.fewshot),
    }


@router.put("/{account_id}")
def save_config(account_id: int, payload: dict = Body(...),
                user=Depends(require_active), db: Session = Depends(get_db)):
    """保存账号级配置。开关状态变化时同步常驻轮询。"""
    acc = _acc(account_id, user, db)
    try:
        cfg = ai_reply_config.save(db, acc.id, payload)
        # 开着开关却没配 key，等于每来一条消息烧一次失败请求 —— 直接拒绝
        if cfg.enabled and not (cfg.api_key_enc and cfg.model):
            db.rollback()
            return {"ok": False, "error": "启用前请先填好模型名和 API Key"}
        db.add(AuditLog(actor_user_id=user.id, actor_kind="user",
                        action="ai_config_save", target_type="account",
                        target_id=str(acc.id)))
        db.commit()
        ai_worker.sync_watch(db, user.id, acc.id)
        return {"ok": True, "config": ai_reply_config.to_public(
            ai_reply_config.load(db, acc.id))}
    except ValueError as e:
        db.rollback()
        return {"ok": False, "error": str(e)}
    except Exception as e:
        db.rollback()
        traceback.print_exc()
        return {"ok": False, "error": f"保存失败: {type(e).__name__}"}


@router.put("/{account_id}/peer/{uid}")
def save_peer(account_id: int, uid: str, payload: dict = Body(...),
              user=Depends(require_active), db: Session = Depends(get_db)):
    """联系人级开关与话术覆盖 —— 「对谁回复」就是在这里定的。"""
    acc = _acc(account_id, user, db)
    try:
        row = ai_reply_config.set_peer(db, acc.id, uid, payload)
        db.commit()
        return {"ok": True, "peer": {"enabled": bool(row.enabled),
                                     "persona": row.persona or "",
                                     "reply_format": row.reply_format or ""}}
    except Exception as e:
        db.rollback()
        traceback.print_exc()
        return {"ok": False, "error": f"保存失败: {type(e).__name__}"}


# ── 知识库 ───────────────────────────────────────────────

@router.get("/{account_id}/knowledge")
def list_knowledge(account_id: int, uid: str = knowledge_service.GLOBAL_UID,
                   user=Depends(require_active), db: Session = Depends(get_db)):
    """uid='*' 取通用库，uid=联系人 uid 取该联系人专属库。两者互不影响。"""
    acc = _acc(account_id, user, db)
    return {"ok": True, "uid": uid,
            "entries": knowledge_service.list_entries(db, acc.id, uid)}


@router.post("/{account_id}/knowledge")
def save_knowledge(account_id: int, payload: dict = Body(...),
                   user=Depends(require_active), db: Session = Depends(get_db)):
    acc = _acc(account_id, user, db)
    uid = str(payload.get("uid") or knowledge_service.GLOBAL_UID)
    try:
        row = knowledge_service.upsert_entry(
            db, acc.id, uid, payload, entry_id=payload.get("id"))
        db.commit()
        return {"ok": True, "entry": knowledge_service._to_dict(row)}
    except ValueError as e:
        db.rollback()
        return {"ok": False, "error": str(e)}
    except Exception as e:
        db.rollback()
        traceback.print_exc()
        return {"ok": False, "error": f"保存失败: {type(e).__name__}"}


@router.delete("/{account_id}/knowledge/{entry_id}")
def delete_knowledge(account_id: int, entry_id: int,
                     user=Depends(require_active), db: Session = Depends(get_db)):
    acc = _acc(account_id, user, db)
    ok = knowledge_service.delete_entry(db, acc.id, entry_id)
    db.commit()
    return {"ok": ok, "error": None if ok else "条目不存在"}


# ── 试跑与日志 ───────────────────────────────────────────

@router.post("/{account_id}/test")
@limiter.limit(TEST_LIMIT)
def test_reply(account_id: int, request: Request, response: Response,
               payload: dict = Body(...),
               user=Depends(require_active), db: Session = Depends(get_db)):
    """拿一段假消息跑完整链路，但**绝不发送**。

    这是上线前的唯一验证手段：能看到模型原样吐了什么、清洗后变成什么、
    被哪条规则拦下。没有它，用户只能开着开关拿真实好友当小白鼠。

    `response` 形参不能删：limiter 开了 headers_enabled，要往响应里塞
    X-RateLimit-*，返回裸 dict 的端点必须显式给它一个 Response 对象，
    否则 slowapi 直接抛异常 —— 整个接口 500。
    """
    acc = _acc(account_id, user, db)
    cfg = ai_reply_config.load(db, acc.id)
    if not cfg or not cfg.model or not cfg.api_key_enc:
        return {"ok": False, "error": "请先填好模型名和 API Key"}

    incoming = (payload.get("text") or "").strip()
    if not incoming:
        return {"ok": False, "error": "请输入一条测试消息"}
    uid = str(payload.get("uid") or "")

    peer = ai_reply_config.get_peer(db, acc.id, uid) if uid else None
    eff = ai_reply_config.resolve(cfg, peer)
    knowledge = knowledge_service.retrieve(db, acc.id, uid, incoming)
    contact = db.scalar(select(Contact).where(
        Contact.douyin_account_id == acc.id, Contact.uid == uid)) if uid else None

    values = {
        "userinput": ai_reply.sanitize_user_input(incoming),
        "nickname": (contact.nickname if contact else "") or "朋友",
        "days": str(contact.days or "") if contact else "",
        "time": datetime.now().strftime("%H:%M"),
        "knowledge": knowledge,
    }
    system_prompt = eff.system_prompt(knowledge)
    user_prompt = ai_reply.build_user_prompt(eff.prompt_template, values)

    try:
        result = llm.chat(
            llm.LLMConfig(provider=cfg.provider, base_url=cfg.base_url,
                          api_key=ai_reply_config.api_key(cfg), model=cfg.model,
                          thinking=bool(cfg.thinking)),
            system_prompt, user_prompt)
    except llm.LLMError as e:
        return {"ok": False, "error": str(e)}

    text, why = ai_reply.sanitize_reply(result.text, eff.policy())
    return {
        "ok": True,
        "raw": result.text,
        "reply": text,
        "blocked": text is None,
        "reason": why,
        "reason_label": REASON_LABELS.get(why, why),
        "knowledge": knowledge,
        "tokens": result.tokens,
        "latency_ms": result.latency_ms,
    }


REASON_LABELS = {
    "ok": "通过",
    "empty": "模型没给出内容",
    "model_declined": "模型判断这条不该自动回（按契约弃权）",
    "link": "含链接 —— 抖音私信发链接极易触发风控，已拦下",
    "phone": "含手机号，已拦下",
    "contact": "含微信/QQ 等联系方式，已拦下",
    "ai_persona": "自曝 AI 身份，已拦下",
    "meta_leak": "提到了知识库/提示词等内部机制，已拦下",
    "banned_word": "命中你设置的禁词，已拦下",
    "cooldown": "冷却期内，跳过",
    "daily_limit": "已达当日上限，跳过",
    "account_busy": "续火花任务正在跑，跳过",
}


@router.get("/{account_id}/logs")
def list_logs(account_id: int, limit: int = 30,
              user=Depends(require_active), db: Session = Depends(get_db)):
    """最近的自动回复记录 —— 出问题时第一时间看这里。"""
    acc = _acc(account_id, user, db)
    limit = max(1, min(int(limit), LOGS_MAX))
    rows = db.scalars(
        select(AiReplyLog)
        .where(AiReplyLog.douyin_account_id == acc.id)
        .order_by(AiReplyLog.created_at.desc(), AiReplyLog.id.desc())
        .limit(limit)
    ).all()
    return {"ok": True, "logs": [{
        "id": r.id,
        "peer_uid": r.peer_uid,
        "status": r.status,
        "incoming": r.incoming or "",
        "final_text": r.final_text or "",
        "reason": r.reason or "",
        "reason_label": REASON_LABELS.get(r.reason or "", r.reason or ""),
        "tokens": r.tokens,
        "latency_ms": r.latency_ms,
        "created_at": r.created_at.isoformat() + "Z" if r.created_at else None,
    } for r in rows]}
