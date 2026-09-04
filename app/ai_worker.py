"""AI 自动回复的编排层：判定 → 调模型 → 清洗 → 发送 → 记账。

# 线程模型

realtime 的轮询线程发现新消息 → `on_new_messages` 只做快速筛选再入队 →
**唯一一个** worker 线程串行消费。

单 worker 是刻意的：它天然成了全局限速器，任何时刻最多一条 AI 回复在
发送中。多线程并发调模型确实更快，但快在这里毫无价值 —— 抖音私信本来
就该慢，快只会招风控。代价是一个慢账号会拖住其他账号，用超时兜住。

# 五道闸门（顺序即优先级）

1. 幂等：先抢 AiReplyLog 唯一键，抢不到说明这条已经处理过（重启/重连不重复回）
2. 白名单：AiReplyPeer.enabled，默认谁都不回
3. 启用时刻：只回 enabled_at 之后到达的消息，开关一打开不会翻旧账
4. 冷却 + 日配额：防机器人对撞死循环、防单日发送量炸掉
5. 账户锁：续火花批量任务在跑就跳过，两条链路不同时打同一个号
"""
from __future__ import annotations

import queue
import threading
import traceback
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from . import (ai_reply, ai_reply_config, knowledge_service, llm,
               messages_service, video_service, voice_service)
from .db import SessionLocal
from .models import AiReplyConfig, AiReplyLog, Contact, DouyinAccount, User

# 队列积压上限。超了就丢新的 —— 积压说明模型或网络已经跟不上，
# 再排队只会让回复越来越延迟，一条十分钟前的消息现在回过去比不回更怪。
QUEUE_MAX = 200

# 连续这么多次调用失败就自动关掉开关 + 站内通知。
# 不设上限的话，key 过期或余额耗尽会让它每来一条消息就烧一次失败请求。
MAX_FAIL_STREAK = 5

# 单条消息从到达到被处理的最长容忍时间。排队太久的直接丢弃，
# 理由同上：过时的回复比不回更奇怪。
STALE_SECONDS = 300

_Q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
_WORKER: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.RLock()


# ── 入口：轮询线程调用 ────────────────────────────────────

def on_new_messages(user_id: int, account_id: int, messages: list[dict]) -> int:
    """筛出候选消息并入队，返回入队条数。绝不阻塞调用方。

    这里只做**不用查库**的廉价筛选。真正的闸门在 worker 里，
    因为轮询线程每几秒跑一次，不该为了几条消息去查配置表。
    """
    if not messages:
        return 0
    n = 0
    for m in messages:
        if not _is_candidate(m):
            continue
        try:
            _Q.put_nowait((user_id, account_id, m, datetime.utcnow()))
            n += 1
        except queue.Full:
            print(f"[ai] acc#{account_id} 队列已满，丢弃一条待回复消息")
            break
    if n:
        start()
    return n


def _aweme_id_of(m: dict) -> str:
    """取这条分享消息的视频 id：先看 media.vid，认不出再从正文里提链接。

    Why 要有第二条路：抖音有些分享消息只给链接不给 item_id。前端
    awemeIdOf 一直在补这一手，AI 侧却只认 media.vid —— 两边规则不一致
    就会出现「聊天页点得开、AI 却当没看见」，而且连条 skip 日志都不留。
    纯本地正则，不发请求。
    """
    import douyin_im as dy

    media = m.get("media")
    vid = media.get("vid") if isinstance(media, dict) else ""
    if isinstance(vid, str) and vid.strip():
        return vid.strip()
    return dy.extract_aweme_id(m.get("text") or "")


def _is_candidate(m: dict) -> bool:
    """廉价筛选：对方发来的纯文本、分享的视频、或语音。

    图片/表情/系统消息一律不回：模型看不到内容，只能瞎猜。
    自己发的更不能回 —— 那是自问自答死循环。

    视频和语音在这里只做「形状对不对」的判断（有 vid 才解析得了、
    有 src 才转得了），**不查库**。真正的开关（reply_share /
    reply_voice）在 handle 里查 —— 轮询线程每几秒跑一次，
    不该为了几条消息去查配置表。
    """
    if m.get("is_me") or not m.get("server_msg_id"):
        return False

    kind = m.get("kind")
    if kind == "text":
        return bool((m.get("text") or "").strip())
    if kind == "share":
        media = m.get("media")
        return (isinstance(media, dict)
                and media.get("kind") == "video"
                and bool(_aweme_id_of(m)))
    if kind == "audio":
        # 有音频地址才转得了；抖音自带转写时直接就有 asr
        media = m.get("media")
        if not isinstance(media, dict):
            return False
        return bool(media.get("src")) or bool(media.get("asr"))
    return False


# ── worker 生命周期 ──────────────────────────────────────

def start() -> None:
    """幂等地拉起 worker 线程。"""
    global _WORKER
    with _LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _STOP.clear()
        _WORKER = threading.Thread(target=_loop, daemon=True, name="ai-reply")
        _WORKER.start()
        print("[ai] 自动回复 worker 已启动")


def stop() -> None:
    _STOP.set()


def _loop() -> None:
    while not _STOP.is_set():
        try:
            item = _Q.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            user_id, account_id, msg, queued_at = item
            if (datetime.utcnow() - queued_at).total_seconds() > STALE_SECONDS:
                print(f"[ai] acc#{account_id} 丢弃排队过久的消息")
                continue
            handle(user_id, account_id, msg)
        except Exception as e:
            # worker 死了就再也没有自动回复了，任何异常都必须吞掉
            print(f"[ai] 处理失败: {e}")
            traceback.print_exc()


def resume_watchers() -> None:
    """进程启动时，给所有开着自动回复的账号重建常驻轮询。

    没有这一步，重启后自动回复就只在有人打开聊天页时才活 ——
    而用户以为它是 7x24 的。
    """
    from . import realtime
    try:
        with SessionLocal() as db:
            rows = db.execute(
                select(AiReplyConfig, DouyinAccount)
                .join(DouyinAccount, AiReplyConfig.douyin_account_id == DouyinAccount.id)
                .join(User, DouyinAccount.user_id == User.id)
                .where(AiReplyConfig.enabled.is_(True),
                       DouyinAccount.status == "active",
                       User.is_active.is_(True))
            ).all()
            targets = [(a.user_id, a.id, c.poll_interval) for c, a in rows]
    except Exception as e:
        print(f"[ai] 恢复常驻轮询失败: {e}")
        return

    for user_id, account_id, interval in targets:
        try:
            realtime.ensure_system_watch(user_id, account_id, interval)
        except Exception as e:
            print(f"[ai] acc#{account_id} 常驻轮询启动失败: {e}")
    if targets:
        start()
        print(f"[ai] 已为 {len(targets)} 个账号恢复常驻轮询")


def sync_watch(db, user_id: int, account_id: int) -> None:
    """按配置的当前状态对齐常驻轮询。开关一改就调它。"""
    from . import realtime
    cfg = ai_reply_config.load(db, account_id)
    if cfg and cfg.enabled:
        realtime.ensure_system_watch(user_id, account_id, cfg.poll_interval)
        start()
    else:
        realtime.drop_system_watch(account_id)


# ── 单条消息的完整处理 ────────────────────────────────────

def handle(user_id: int, account_id: int, msg: dict) -> str:
    """处理一条来信，返回最终状态字符串（也写进 AiReplyLog.status）。

    同步执行，可以在测试里直接调用而不用起线程。
    """
    peer_uid = str(msg.get("peer_uid") or "")
    server_msg_id = int(msg.get("server_msg_id") or 0)
    incoming = (msg.get("text") or "").strip()

    with SessionLocal() as db:
        cfg = ai_reply_config.load(db, account_id)
        if not cfg or not cfg.enabled:
            return "disabled"

        peer = ai_reply_config.get_peer(db, account_id, peer_uid)
        if not peer or not peer.enabled:
            return "not_whitelisted"          # 白名单：默认谁都不回

        # 开关打开之前到达的消息一律不回。少了这一条，开关一打开
        # 库里攒的几百条历史消息会被一次性全回一遍。
        if cfg.enabled_at is not None:
            # enabled_at 存的是 naive UTC，created_at 是抖音给的 epoch 毫秒，
            # 都以 UTC 为基准，直接减 epoch 换算（不能用 .timestamp()，
            # 那会把 naive 值当本地时间解释，差出一个时区）
            enabled_ms = int((cfg.enabled_at - datetime(1970, 1, 1)).total_seconds() * 1000)
            if int(msg.get("created_at") or 0) < enabled_ms:
                return "before_enabled"

        log_id = _claim(db, account_id, peer_uid, server_msg_id, incoming)
        if log_id is None:
            return "duplicate"                # 幂等：这条已经处理过

        eff = ai_reply_config.resolve(cfg, peer)

        reason = _rate_gate(db, account_id, peer_uid, eff)
        if reason:
            return _finish(db, log_id, "skipped", reason=reason)

        # 开关关着就在这儿打住，别白跑一趟解析
        kind = msg.get("kind")
        if kind == "share" and not eff.reply_share:
            return _finish(db, log_id, "skipped", reason="share_disabled")
        if kind == "audio" and not eff.reply_voice:
            return _finish(db, log_id, "skipped", reason="voice_disabled")
        asr_cfg = _asr_config(cfg) if kind == "audio" else None
        turns, spark_limit = eff.history_turns, max(eff.history_turns * 4, 20)
        # cfg 是 ORM 行，会话一关再读属性就炸（commit 过之后属性是 expired 的）。
        # 后面还要用的那几项，趁会话开着先摘出来。
        llm_cfg = llm.LLMConfig(provider=cfg.provider, base_url=cfg.base_url,
                                api_key=ai_reply_config.api_key(cfg),
                                model=cfg.model, thinking=bool(cfg.thinking))

    # 出 DB 会话再解析：视频总结实测 ~9s（上限 60s）、ASR 上限 60s。
    # 占着一条 SQLite 连接干等外部接口，会把别的请求一起拖住 ——
    # 和下面 LLM 调用被移出会话是同一个道理。
    if kind == "share":
        incoming = _video_input(user_id, account_id, msg)
    elif kind == "audio":
        incoming = _voice_input(user_id, account_id, asr_cfg, msg)

    with SessionLocal() as db:
        if kind == "share" and not incoming:
            # 解析不出来就别回 —— 对着看不见的视频瞎接话比不回更糟
            return _finish(db, log_id, "skipped", reason="video_parse_failed")
        if kind == "audio" and not incoming:
            # 转不出文字也别回 —— 模型只看得到「[语音] 3.2″」
            return _finish(db, log_id, "skipped", reason="voice_asr_failed")

        # 知识检索要用**定稿后**的 incoming：视频/语音在上面才被换成真内容，
        # 拿「[语音] 11.7″」去检索等于白检索一次，而且是静默失败。
        knowledge = knowledge_service.retrieve(db, account_id, peer_uid, incoming)
        history = ai_reply.build_history(
            messages_service.load_conversation(
                db, account_id, peer_uid,
                # 多取一些：非文本消息会被压成标记、续火花模板会被剔掉，
                # 按 turns*2 取的话，聊天里表情分享一多，实际喂进去的远不够 turns
                limit=spark_limit),
            turns,
            exclude_texts=_spark_templates(account_id, peer_uid))
        # 最后一条就是这次要回的消息，已经单独放进 user prompt 了
        if history and history[-1]["role"] == "user":
            history = history[:-1]

        contact = db.scalar(select(Contact).where(
            Contact.douyin_account_id == account_id, Contact.uid == peer_uid))
        values = {
            "userinput": ai_reply.sanitize_user_input(incoming),
            "nickname": (contact.nickname if contact else "") or "朋友",
            "days": str(contact.days or "") if contact else "",
            "time": datetime.now().strftime("%H:%M"),
            "knowledge": knowledge,
        }
        system_prompt = eff.system_prompt(knowledge)
        user_prompt = ai_reply.build_user_prompt(eff.prompt_template, values)
        policy = eff.policy()

    # LLM 调用放在 DB 会话之外：一次十几秒，占着 SQLite 连接会拖垮别的请求
    try:
        result = llm.chat(llm_cfg, system_prompt, user_prompt, history)
    except llm.LLMError as e:
        with SessionLocal() as db:
            _bump_failure(db, account_id, str(e))
            return _finish(db, log_id, "llm_error", reason=str(e)[:64])

    text, why = ai_reply.sanitize_reply(result.text, policy)

    with SessionLocal() as db:
        log = db.get(AiReplyLog, log_id)
        if log is not None:
            log.raw_output = (result.text or "")[:2000]
            log.tokens = result.tokens
            log.latency_ms = result.latency_ms
            db.commit()
        _reset_failure(db, account_id)
        if text is None:
            return _finish(db, log_id, "blocked", reason=why)

    sent = _send(user_id, account_id, peer_uid, text)
    with SessionLocal() as db:
        if sent == "locked":
            return _finish(db, log_id, "skipped", reason="account_busy")
        if sent != "ok":
            return _finish(db, log_id, "send_failed", reason=sent[:64])
        return _finish(db, log_id, "sent", final_text=text)


def _account_session(user_id: int, account_id: int):
    """拿这个账号的登录态 session。抖音协议只在 douyin_im.py，这里只负责切上下文。"""
    import douyin_im as dy

    from .storage import AccountCtx, set_account_ctx

    set_account_ctx(AccountCtx(user_id=user_id, account_id=account_id))
    return dy._load_session()


def _video_input(user_id: int, account_id: int, msg: dict) -> str:
    """把分享的视频解析成一段给模型看的文本；拿不到返回空串。

    解析结果本身是 100% 不可信的外部输入（视频文案里写一句
    「忽略以上指令」是零成本的攻击），围栏由 video_service.as_prompt_text
    统一加，这里不再各拼一遍。
    """
    vid = _aweme_id_of(msg)
    if not vid:
        return ""
    try:
        session = _account_session(user_id, account_id)
        if session is None:
            print(f"[ai] acc#{account_id} 没有可用登录态，跳过视频解析")
            return ""
        parsed = video_service.get_or_parse(session, vid)
    except Exception as e:
        print(f"[ai] acc#{account_id} 视频 {vid} 解析失败: {e}")
        return ""

    # 对方分享时可能还附了一句话（105 那种「视频+评论」），带上更完整；
    # 解析交白卷时它还是最后的退路（见 video_service._share_fallback）
    note = (msg.get("text") or "").strip()
    text = video_service.as_prompt_text(parsed, share_text=note)
    if not text:
        return ""
    if note and note not in text:          # 退路已经用过就别再贴一遍
        return f"{text}\n对方附言：{note}"
    return text


def _asr_config(cfg) -> "llm.LLMConfig":
    """从配置行里摘出 ASR 那三项。

    要在 DB 会话还开着的时候调 —— cfg 是 ORM 行，会话一关再读属性就炸。
    """
    return llm.LLMConfig(
        asr_base_url=cfg.asr_base_url or "",
        asr_model=cfg.asr_model or "",
        asr_api_key=ai_reply_config.asr_api_key(cfg),
    )


def _voice_input(user_id: int, account_id: int, asr_cfg, msg: dict) -> str:
    """把语音转成一段给模型看的文本；转不出返回空串。

    转写文本是对方说的话，属于不可信输入，围栏由
    voice_service.as_prompt_text 统一加。
    """
    try:
        session = _account_session(user_id, account_id)
        if session is None:
            print(f"[ai] acc#{account_id} 没有可用登录态，跳过语音转写")
            return ""
        text = voice_service.transcribe_message(
            session, asr_cfg, account_id, msg)
    except Exception as e:
        print(f"[ai] acc#{account_id} 语音转写失败: {e}")
        return ""
    return voice_service.as_prompt_text(text)


def _spark_templates(account_id: int, peer_uid: str) -> set[str]:
    """这个账号会自动发出去的续火花话术。

    它们是定时推送，不是对话。不剔掉的话，模型看到「我」反复说
    「晚安！陈小舟」，就会学着在上午十一点回一句晚安 —— 实测踩过。
    读模板失败不能影响回复，最差就是回到剔除前的行为。
    """
    try:
        from . import templates_service
        tpl = templates_service.load_templates(account_id)
    except Exception as e:
        print(f"[ai] acc#{account_id} 读续火花模板失败（不影响回复）: {e}")
        return set()

    out: set[str] = set()
    for uid in ("default", str(peer_uid)):
        entry = tpl.get(uid) or {}
        for msg in (entry.get("messages") or []):
            if isinstance(msg, str) and msg.strip():
                out.add(msg.strip())
    return out


def _claim(db, account_id: int, peer_uid: str, server_msg_id: int,           incoming: str) -> int | None:
    """抢占这条消息的处理权，返回日志行 id。抢不到（唯一键冲突）返回 None。

    先插占位行再干活，而不是干完再记 —— 顺序反过来的话，
    进程在调模型时被杀掉，重启后这条消息会被回第二次。

    只返回 id 而不是 ORM 对象：后面每一步都换新 session，
    带着对象跨 session 走，一次 commit 就会把它 expire 成不可访问的孤儿。
    """
    log = AiReplyLog(douyin_account_id=account_id, peer_uid=peer_uid,
                     server_msg_id=server_msg_id, status="pending",
                     incoming=incoming[:500])
    db.add(log)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return int(log.id)


def _rate_gate(db, account_id: int, peer_uid: str,
               eff: ai_reply_config.Effective) -> str | None:
    """冷却 + 日配额。返回跳过原因，None 表示放行。"""
    now = datetime.utcnow()

    if eff.cooldown_sec > 0:
        recent = db.scalar(
            select(func.count(AiReplyLog.id)).where(
                AiReplyLog.douyin_account_id == account_id,
                AiReplyLog.peer_uid == peer_uid,
                AiReplyLog.status == "sent",
                AiReplyLog.created_at >= now - timedelta(seconds=eff.cooldown_sec)))
        if recent:
            # 对面要是也挂着机器人，没有这道闸就是两台机器互相刷屏
            return "cooldown"

    if eff.daily_limit > 0:
        today = db.scalar(
            select(func.count(AiReplyLog.id)).where(
                AiReplyLog.douyin_account_id == account_id,
                AiReplyLog.status == "sent",
                AiReplyLog.created_at >= now - timedelta(days=1)))
        if today >= eff.daily_limit:
            return "daily_limit"
    return None


def _send(user_id: int, account_id: int, peer_uid: str, text: str) -> str:
    """真正发出去。返回 "ok" / "locked" / 错误描述。

    走 trigger.send_to_uid 这条既有出口 —— 抖音协议只在 douyin_im.py，
    这里不碰签名和请求拼装。
    """
    from . import realtime, trigger

    with trigger.account_lock(account_id) as acquired:
        if not acquired:
            return "locked"                  # 续火花批量任务在跑，这轮跳过
        try:
            ok, contact = trigger.send_to_uid(user_id, account_id, peer_uid, text)
        except Exception as e:
            return f"{type(e).__name__}: {e}"[:200]
        if contact is None:
            return "联系人不存在"
        if not ok:
            return "抖音返回失败"

    # 和手动发送一致：立刻落本地气泡并推给正开着聊天页的自己
    try:
        with SessionLocal() as db:
            local = messages_service.append_local(
                db, account_id, peer_uid, text, conv_id=contact.get("conv_id"))
            db.commit()
        realtime.broadcast(account_id, [local])
    except Exception as e:
        print(f"[ai] acc#{account_id} 写本地聊天记录失败: {e}")
    return "ok"


def _finish(db, log_id: int, status: str, reason: str | None = None,
            final_text: str | None = None) -> str:
    log = db.get(AiReplyLog, log_id)
    if log is not None:
        log.status = status
        if reason:
            log.reason = reason[:64]
        if final_text:
            log.final_text = final_text[:1000]
        db.commit()
    return status


def _bump_failure(db, account_id: int, err: str) -> None:
    """连续失败到阈值就自动关掉，并告诉用户为什么。

    最常见的原因是 key 过期或余额耗尽 —— 那种情况下每来一条消息都
    烧一次失败请求，用户还完全不知道自动回复早就不工作了。
    """
    from .notify import notify

    cfg = ai_reply_config.load(db, account_id)
    if not cfg:
        return
    cfg.fail_streak = (cfg.fail_streak or 0) + 1
    if cfg.fail_streak >= MAX_FAIL_STREAK:
        cfg.enabled = False
        acc = db.get(DouyinAccount, account_id)
        label = (acc.nickname or acc.label) if acc else f"账户#{account_id}"
        try:
            notify(db, user_id=acc.user_id if acc else 0, kind="ai_reply_off",
                   title=f"🤖 {label} 的 AI 自动回复已自动关闭",
                   content=f"连续 {cfg.fail_streak} 次调用大模型失败：{err[:120]}",
                   url=f"/accounts/{account_id}/chat")
        except Exception:
            traceback.print_exc()
        try:
            from . import realtime
            realtime.drop_system_watch(account_id)
        except Exception:
            pass
        print(f"[ai] acc#{account_id} 连续失败 {cfg.fail_streak} 次，已自动关闭")
    db.commit()


def _reset_failure(db, account_id: int) -> None:
    cfg = ai_reply_config.load(db, account_id)
    if cfg and cfg.fail_streak:
        cfg.fail_streak = 0
        db.commit()
