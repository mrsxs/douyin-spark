"""
JSON API：模板、定时、发送、立即续火花、联系人刷新。
每个请求携带 account_id，deps 自动验证归属。
"""
import asyncio
import json
import queue
import time
import traceback
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Body, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..db import get_db, SessionLocal
from ..models import DouyinAccount, Schedule, AuditLog, Announcement, JobRun, JobRunItem, Notification
from ..deps import require_user
from ..storage import AccountCtx, set_account_ctx
from .. import (trigger, jobs, contacts_service, messages_service, realtime,
                ai_reply_config, llm, voice_service, video_service)
import douyin_im as dy

router = APIRouter(prefix="/api")

# 批量拨开关的上限。比 jobs.MAX_BATCH_UIDS(200) 宽松得多 ——
# 这里只写本地 DB，不打抖音接口，没有风控代价
MAX_BULK_UIDS = 1000


def _safe_err(e: Exception, fallback: str = "操作失败，请稍后重试") -> str:
    """过滤异常信息给前端。业务异常（NotReady / AlreadyRunning / ValueError）保留原文；
    其它异常（DB / 第三方 SDK）统一替换为通用文案，避免泄露内部实现。"""
    # 这些异常的 str 本身就是给用户看的人话
    safe_types = (trigger.NotReady, trigger.AlreadyRunning,
                  ValueError, KeyError, HTTPException)
    if isinstance(e, safe_types):
        return str(e)[:200]
    # 其它（包括 sqlalchemy error、连接拒绝等）一律脱敏
    return fallback


def _iso_utc(dt) -> str | None:
    """带 Z 后缀的 ISO 时间。

    DB 里存的是 naive UTC，直接 isoformat() 给前端的话
    JS 的 new Date() 会当成本地时间解析 —— 刚同步完却显示「8 小时前」。
    """
    return dt.isoformat() + "Z" if dt else None


@router.get("/contacts/{account_id}")
def api_contacts(
    account_id: int,
    refresh: int = 0,
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    """联系人列表。

    默认读冷备（毫秒级）；refresh=1 时才走抖音 API 拉最新并回写冷备。
    页面首屏渲染冷备，加载完再用 refresh=1 覆盖，避免白屏。
    """
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id,
        DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404, "账户不存在")

    from .. import contacts_service
    if not refresh:
        rows = contacts_service.load_cached(db, acc.id)
        synced = contacts_service.last_synced_at(db, acc.id)
        return {"ok": True, "contacts": rows, "cached": True,
                "synced_at": _iso_utc(synced)}

    try:
        rows, _tpl = trigger.get_contacts(user.id, acc.id)
        # 补昵称/头像：不补的话抓不到名字的会显示成一长串 uid
        filled = contacts_service.enrich_names_and_avatars(user.id, acc.id, rows)
        # 全量刷新 → 清掉抖音那边已经没有的人，否则用户会一直看到早已消失的好友
        contacts_service.upsert_cache(db, acc.id, rows, prune=True)
        db.commit()
        fresh = contacts_service.load_cached(db, acc.id)
        return {"ok": True, "contacts": fresh, "cached": False,
                "count": len(fresh), "filled": filled,
                "synced_at": _iso_utc(datetime.utcnow())}
    except Exception as e:
        traceback.print_exc()
        # 刷新失败时退回冷备，让页面至少有数据可用
        return {"ok": False, "error": _safe_err(e),
                "contacts": contacts_service.load_cached(db, acc.id),
                "cached": True}


# 注意路由顺序：/messages/{account_id}/stream 必须注册在 /{peer_uid} 之前。
# FastAPI 按注册顺序匹配，反过来的话 "stream" 会被当成一个 peer_uid，
# SSE 请求直接掉进读会话的接口，返回一坨 JSON 而不是事件流。

@router.get("/messages/{account_id}/stream")
async def api_messages_stream(
    account_id: int,
    request: Request,
    interval: str = "auto",
    user = Depends(require_user),
):
    """SSE 实时消息流。浏览器开着聊天页就连着，关掉就断。

    interval 是用户在聊天页选的刷新秒数（或 auto 自适应），
    非法值一律回落到自适应，并夹在 3~300 秒之间。

    刻意不用 Depends(get_db)：StreamingResponse 会把依赖一直持有到流结束，
    那样每条长连接都占住一个 DB 连接，几个人同时开聊天页就把连接池吃干净。
    归属校验用一次性短会话做完就放掉。
    """
    with SessionLocal() as db:
        acc = db.query(DouyinAccount).filter(
            DouyinAccount.id == account_id,
            DouyinAccount.user_id == user.id).first()
        if not acc:
            raise HTTPException(404, "账户不存在")
        acc_id, uid = acc.id, user.id

    poll_interval = realtime.normalize_interval(interval)

    async def event_stream():
        sub = realtime.subscribe(uid, acc_id, poll_interval)
        last_beat = time.monotonic()
        try:
            yield f": connected acc={acc_id}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    batch = sub.get_nowait()
                except queue.Empty:
                    # 队列是进程内内存，0.5s 轮一次几乎不花开销，
                    # 换来的是整条链路不用跨线程唤醒事件循环
                    await asyncio.sleep(0.5)
                    if time.monotonic() - last_beat > 20:
                        last_beat = time.monotonic()
                        yield ": ping\n\n"      # 心跳，防代理掐断闲置连接
                    continue
                last_beat = time.monotonic()
                yield f"event: messages\ndata: {json.dumps(batch, ensure_ascii=False)}\n\n"
        finally:
            realtime.unsubscribe(sub)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",     # nginx 默认会缓冲，SSE 必须关掉
        },
    )


@router.get("/messages/{account_id}/{peer_uid}")
def api_messages(
    account_id: int, peer_uid: str,
    limit: int = 50, before: int | None = None, before_id: int | None = None,
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    """读某个联系人的聊天记录（纯冷备，毫秒级）。

    往上翻历史时把上一页最早一条的 created_at + id 一起传回来 ——
    只传时间戳的话，同毫秒的消息会在页边界被跳过。
    """
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id,
        DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404, "账户不存在")

    limit = max(1, min(limit, 200))
    rows = messages_service.load_conversation(
        db, acc.id, peer_uid, limit=limit, before=before, before_id=before_id)
    return {"ok": True, "messages": rows,
            # 少于 limit 说明到头了，前端据此停止「加载更多」
            "has_more": len(rows) >= limit}


@router.post("/messages/{account_id}/sync")
def api_messages_sync(
    account_id: int,
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    """主动同步一次消息。

    走的还是拉联系人那条链路（get_contacts → _ensure_active），
    消息在里面顺带解析入库 —— 不额外请求抖音。
    """
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id,
        DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404, "账户不存在")
    try:
        trigger.get_contacts(user.id, acc.id)
        return {"ok": True, "synced_at": _iso_utc(datetime.utcnow())}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": _safe_err(e)}


def _account_session_for(user_id: int, account_id: int):
    """拿这个账号的登录态 session。抖音协议只在 douyin_im.py，这里只切上下文。"""
    set_account_ctx(AccountCtx(user_id=user_id, account_id=account_id))
    return dy._load_session()


@router.post("/messages/{account_id}/transcribe")
def api_messages_transcribe(
    account_id: int,
    payload: dict = Body(default={}),
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    """把一条语音转成文字。payload: {id: int} —— **本地行 id，不是 server_msg_id**。

    Why 要有手动入口：自动转写只在 AI 准备回复时才发生，用户自己翻记录
    看到语音就只能听。转写结果写回 media.asr，点一次之后谁都受益
    （AI 后面要回也直接命中缓存，不重复计费）。

    Why 用本地 id：抖音的 server_message_id 是 19 位雪花号，全部超出 JS 的
    Number.MAX_SAFE_INTEGER，一进浏览器就被四舍五入，发回来已经不是原值。
    """
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id,
        DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404, "账户不存在")

    try:
        msg_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "缺少消息 id"}
    if msg_id <= 0:
        return {"ok": False, "error": "缺少消息 id"}

    msg = messages_service.get_message(db, acc.id, msg_id)
    if msg is None:
        return {"ok": False, "error": "消息不存在"}
    if msg.get("kind") != "audio":
        return {"ok": False, "error": "这条不是语音消息"}

    cfg = ai_reply_config.load(db, acc.id)
    asr_cfg = llm.LLMConfig(
        asr_base_url=(cfg.asr_base_url if cfg else "") or "",
        asr_model=(cfg.asr_model if cfg else "") or "",
        asr_api_key=ai_reply_config.asr_api_key(cfg),
    )
    # 先给一句人话，别让用户对着「转写失败」猜是没配还是服务挂了
    if not (asr_cfg.asr_base_url and asr_cfg.asr_model and asr_cfg.asr_api_key):
        return {"ok": False, "error": "尚未配置语音转写服务，请在 AI 面板里填好"}

    try:
        session = _account_session_for(user.id, acc.id)
        if session is None:
            return {"ok": False, "error": "账号未登录，无法下载语音"}
        text = voice_service.transcribe_message(session, asr_cfg, acc.id, msg)
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": _safe_err(e)}

    if not text:
        return {"ok": False, "error": "没转出内容（可能是纯环境音或音频已过期）"}
    return {"ok": True, "text": text}


@router.get("/videos/{account_id}/{aweme_id}/stream")
def api_video_stream(
    account_id: int,
    aweme_id: str,
    request: Request,
    user = Depends(require_user),
):
    """把分享的视频反代给聊天页的 <video>，让它在气泡里原生播放。

    Why 不把直链甩给浏览器：直链带时效签名（约两天）、CDN 认 Referer，
    而且一旦进了 HTML，用户随手一复制就把只有登录态才看得到的内容散出去了。
    走自己的端点，鉴权和归属校验就都还在。

    Range 透传是必须的：不支持 206 的话进度条拖不动，Safari 干脆不播。

    刻意不用 Depends(get_db)：StreamingResponse 会把依赖一直持有到流结束，
    那样一条视频从头看到尾就占住一个 DB 连接（和 SSE 那条一样的坑）。
    归属校验用一次性短会话做完就放掉。
    """
    with SessionLocal() as db:
        acc = db.query(DouyinAccount).filter(
            DouyinAccount.id == account_id,
            DouyinAccount.user_id == user.id).first()
        if not acc:
            raise HTTPException(404, "账户不存在")
        acc_id = acc.id

    vid = dy.extract_aweme_id(aweme_id)
    if not vid:
        raise HTTPException(404, "视频不存在")

    session = _account_session_for(user.id, acc_id)
    if session is None:
        raise HTTPException(409, "账号未登录，无法取播放地址")

    url = video_service.play_url(session, vid)
    if not url:
        raise HTTPException(404, "拿不到播放地址（视频可能已删除或仅粉丝可见）")

    upstream = dy.open_video_stream(session, url, request.headers.get("range"))
    if upstream is None:
        raise HTTPException(502, "视频源暂时不可用")

    def _pump():
        try:
            for chunk in upstream.iter_content(chunk_size=dy.VIDEO_CHUNK):
                if chunk:
                    yield chunk
        finally:
            upstream.close()          # 用户关页面/切走时也要收摊

    # 只转发播放必需的头。上游其它头（含 Set-Cookie）一律不带过来
    passthru = {}
    for k in ("Content-Length", "Content-Range"):
        v = upstream.headers.get(k)
        if v:
            passthru[k] = v
    passthru["Accept-Ranges"] = "bytes"
    # 不设 Cache-Control：NoStoreMiddleware 对非 /static/ 一律改写成 no-store
    # （线上 CDN 不按 Cookie 区分，缓存过登录页面出过事故）。写了也是死代码。

    return StreamingResponse(
        _pump(), status_code=upstream.status_code, headers=passthru,
        media_type=upstream.headers.get("Content-Type") or "video/mp4")


@router.post("/messages/{account_id}/backfill")
def api_messages_backfill(
    account_id: int,
    payload: dict = Body(default={}),
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    """从抖音云端把历史消息拉回来，补上冷备表的空档。

    payload: {uid?: str} —— 给了 uid 只回填那一个会话，不给就整账号。

    Why 不放进 scheduler：整账号回填要打几十上百个请求，做成定时任务
    等于常态化扩大风控面。这是用户按需触发的一次性补救。
    """
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id,
        DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404, "账户不存在")

    uid = str((payload or {}).get("uid") or "").strip()
    # pages: 一页 50 条，默认 40 页 ≈ 2000 条。想翻得更早就加大 ——
    # 每页之间还要 sleep，翻页越多打的请求越多，所以给个硬上限。
    try:
        pages = int((payload or {}).get("pages") or trigger.BACKFILL_PAGES)
    except (TypeError, ValueError):
        pages = trigger.BACKFILL_PAGES
    if pages <= 0:                     # 0/负数按「没填」处理：翻 1 页等于白跑一趟
        pages = trigger.BACKFILL_PAGES
    pages = min(pages, trigger.BACKFILL_PAGES_MAX)

    try:
        if uid:
            r = trigger.backfill_history(user.id, acc.id, uid, max_pages=pages)
            if r.get("error"):
                return {"ok": False, "error": r["error"]}
            return {"ok": True, **r}
        return {"ok": True, **trigger.backfill_all(user.id, acc.id,
                                                   max_pages=pages)}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": _safe_err(e)}





@router.post("/refresh")
def api_refresh(
    payload: dict = Body(...),
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    acc = _get_acc(payload, user, db)
    try:
        contacts, templates = trigger.get_contacts(user.id, acc.id)
        # 补昵称/头像：缺昵称（nickname==uid）或缺头像的联系人，主动调 fetch_nicknames 兜底
        missing = [c for c in contacts
                   if (c.get("nickname") or "") == c.get("uid") or not c.get("avatar")]
        filled = 0
        # 同时：自己的账户没头像 → 拉 self profile 写入 DouyinAccount.avatar
        need_self_avatar = acc.dy_uid and not acc.avatar
        if missing or need_self_avatar:
            try:
                set_account_ctx(AccountCtx(user.id, acc.id))
                session = dy._load_session()
                if session and need_self_avatar:
                    self_prof = dy.fetch_self_profile(session, acc.dy_uid)
                    if self_prof.get("avatar"):
                        acc.avatar = contacts_service.normalize_avatar_url(self_prof["avatar"])
                        if self_prof.get("nickname") and not acc.nickname:
                            acc.nickname = self_prof["nickname"]
                        db.commit()
                if session:
                    captured = dy.fetch_nicknames(session, missing)
                    if captured:
                        # 合并写 contacts.json 缓存（原子写）
                        import os as _os
                        import tempfile as _tf
                        cache_path = str(dy.CONTACTS_FILE)
                        try:
                            existing = json.load(open(cache_path)) if _os.path.exists(cache_path) else {}
                        except Exception:
                            existing = {}
                        # 逐字段合并，避免本轮没拿到的字段（如某次只回头像没回昵称）
                        # 把已有的昵称/备注清掉
                        for uid, info in captured.items():
                            prev = existing.get(uid) if isinstance(existing.get(uid), dict) else {}
                            merged = dict(prev)
                            for k in ("nick", "remark", "avatar"):
                                if info.get(k):
                                    merged[k] = info[k]
                            existing[uid] = merged
                        cache_dir = _os.path.dirname(cache_path) or "."
                        _os.makedirs(cache_dir, exist_ok=True)
                        fd, tmp_path = _tf.mkstemp(dir=cache_dir, prefix=".tmp_", suffix=".json")
                        try:
                            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                                json.dump(existing, f, ensure_ascii=False)
                                f.flush()
                                _os.fsync(f.fileno())
                            _os.replace(tmp_path, cache_path)
                        except Exception:
                            if _os.path.exists(tmp_path):
                                _os.unlink(tmp_path)
                            raise
                        # 更新 DB Contact.nickname
                        from ..models import Contact
                        for uid, info in captured.items():
                            nick = (info.get("remark") or info.get("nick") or "").strip()
                            avatar = (info.get("avatar") or "").strip()
                            if nick and nick != uid:
                                row = db.query(Contact).filter(
                                    Contact.douyin_account_id == acc.id,
                                    Contact.uid == uid).first()
                                if row:
                                    row.nickname = nick
                            # 同步更新返回给前端的 contacts 列表（避免 reload 后才看到）
                            for c in contacts:
                                if c.get("uid") == uid:
                                    if nick and nick != uid:
                                        c["nickname"] = nick
                                        filled += 1
                                    if avatar:
                                        c["avatar"] = avatar
                                    break
                        db.commit()
            except Exception as e:
                traceback.print_exc()
                print(f"[refresh] fetch_nicknames 失败: {e}")
        return {"ok": True, "count": len(contacts), "filled_nicknames": filled}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": _safe_err(e)}


@router.post("/auto")
def api_auto(
    payload: dict = Body(...),
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    """触发续火花。立即返回 run_id，真正的发送在后台线程里跑。

    Why 不同步执行：每个联系人间隔 5s，100 个联系人就是 500 秒，
    同步请求必然超时，还会占满 FastAPI 的同步 threadpool 拖垮整站。
    """
    acc = _get_acc(payload, user, db)
    try:
        run_id, is_new = jobs.start_auto_run(user.id, acc.id)
        return {"ok": True, "run_id": run_id, "started": is_new,
                "message": "已开始续火花" if is_new else "该账号已有任务在执行"}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": _safe_err(e)}


@router.get("/runs/{run_id}/progress")
def api_run_progress(
    run_id: int,
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    """任务进度轮询端点。"""
    p = jobs.get_progress(db, run_id)
    if not p:
        raise HTTPException(404, "任务不存在")
    # 越权保护：只能看自己账号下的任务
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == p["account_id"],
        DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404, "任务不存在")
    return {"ok": True, **p}


def _format_send_detail(info: dict) -> str:
    """兼容别名 —— 实现已下沉到 trigger.format_send_detail。"""
    return trigger.format_send_detail(info)


@router.post("/send")
def api_send(
    payload: dict = Body(...),
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    acc = _get_acc(payload, user, db)
    uid = payload.get("uid")
    text = (payload.get("text") or "").strip()
    if not uid or not text:
        return {"ok": False, "error": "缺少 uid/text"}
    try:
        contacts, _ = trigger.get_contacts(user.id, acc.id)
        contact = next((c for c in contacts if c["uid"] == uid), None)
        if not contact:
            return {"ok": False, "error": "联系人不存在"}
        # 记 JobRun（单发也记录，便于用户端看运行历史）
        run = JobRun(douyin_account_id=acc.id, kind="manual_single",
                     triggered_by="user", status="running")
        db.add(run); db.commit(); db.refresh(run)
        ok = trigger.send_single(user.id, acc.id, contact["conv_id"], contact, text)
        detail = _format_send_detail(dy.get_last_send_info())
        db.add(JobRunItem(
            job_run_id=run.id, uid=uid, nickname=contact.get("nickname"),
            conv_id=contact.get("conv_id"), message=text,
            ok=bool(ok), detail=detail[:1000],
        ))
        run.finished_at = datetime.utcnow()
        run.sent = 1 if ok else 0
        run.failed = 0 if ok else 1
        run.status = "done"
        db.add(AuditLog(actor_user_id=user.id, actor_kind="user", action="send_manual",
                        target_type="account", target_id=str(acc.id),
                        meta=json.dumps({"uid": uid, "ok": ok, "run_id": run.id})))
        # 发成功就立刻写进聊天记录并推给自己的其它页面 ——
        # 抖音的 server_message_id 要等下轮同步才有，不能让用户干等着
        if ok:
            try:
                local = messages_service.append_local(
                    db, acc.id, uid, text, conv_id=contact.get("conv_id"))
                realtime.broadcast(acc.id, [local])
                # 刚发完消息多半马上有回复，把轮询从退避里叫醒
                realtime.wake(acc.id)
            except Exception as _e:
                print(f"[api_send] 写本地聊天记录失败: {_e}")
        db.commit()
        return {"ok": bool(ok), "detail": detail}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": _safe_err(e)}


@router.post("/send-batch")
def api_send_batch(
    payload: dict = Body(...),
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    """批量给一组 uid 发同一条消息。payload: {account_id, uids: [...], text}

    同 /auto：立即返回 run_id，发送在后台跑（每条间隔 5s，同步会超时）。
    """
    acc = _get_acc(payload, user, db)
    try:
        run_id, is_new = jobs.start_batch_send(
            user.id, acc.id, payload.get("uids") or [], payload.get("text") or "")
        return {"ok": True, "run_id": run_id, "started": is_new,
                "message": "已开始发送" if is_new else "该账号已有任务在执行"}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": _safe_err(e)}


@router.put("/template/{account_id}/{uid}")
def api_update_template(
    account_id: int, uid: str,
    payload: dict = Body(...),
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)
    set_account_ctx(AccountCtx(user.id, acc.id))
    try:
        from .. import templates_service
        enabled = bool(payload.get("enabled", True))
        messages = payload.get("messages") or []
        # 补联系人昵称到 name 字段，仅非 default
        name = None
        if uid != "default":
            try:
                from ..models import Contact
                c = db.query(Contact).filter(
                    Contact.douyin_account_id == account_id,
                    Contact.uid == uid).first()
                if c and c.nickname:
                    name = c.nickname
            except Exception:
                pass
        templates_service.upsert_template(
            db, account_id, uid,
            name=name, enabled=enabled, messages=messages,
        )
        db.commit()
        # 同步一份 JSON 备份（douyin_im 降级路径）
        templates_service.sync_json_backup(account_id)
        return {"ok": True}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": _safe_err(e)}


@router.put("/templates/{account_id}/bulk-enabled")
def api_bulk_template_enabled(
    account_id: int,
    payload: dict = Body(...),
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    """批量拨「自动续火花」开关。payload: {uids: [...], enabled: bool}

    Why 不让前端逐个打 /api/template/{id}/{uid}：全选几十上百人时那是
    几十上百个请求 + 同样次数的 templates.json 原子写。这里一次 commit、
    一次备份。

    只动 enabled，不碰 messages —— 用户写好的话术不能被一次误点抹掉。
    uid 没有 entry 时**新建**：douyin_im._pick_message 对没有 entry 的
    联系人直接跳过，不建的话开关拨亮了也一条发不出去（无火花好友首次
    启用就是这个情形）。
    """
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)

    enabled = bool(payload.get("enabled", True))
    seen, uids = set(), []
    for raw in (payload.get("uids") or []):
        s = str(raw).strip()
        # "default" 是全局兜底模板不是联系人，混进来会把兜底一起关掉
        if not s or s == "default" or s in seen:
            continue
        seen.add(s)
        uids.append(s)
    if not uids:
        return {"ok": False, "error": "请至少选择一个联系人"}
    if len(uids) > MAX_BULK_UIDS:
        return {"ok": False, "error": f"一次最多操作 {MAX_BULK_UIDS} 个联系人"}

    try:
        from ..models import Contact
        from .. import templates_service
        # 一次查完昵称，别在循环里逐个打 DB
        names = {c.uid: c.nickname for c in db.query(Contact).filter(
            Contact.douyin_account_id == account_id,
            Contact.uid.in_(uids)).all() if c.nickname}
        for uid in uids:
            templates_service.upsert_template(
                db, account_id, uid,
                name=names.get(uid), enabled=enabled, messages=None,
            )
        db.add(AuditLog(actor_user_id=user.id, actor_kind="user",
                        action="template_bulk_enabled",
                        target_type="account", target_id=str(acc.id),
                        meta=json.dumps({"enabled": enabled, "count": len(uids)})))
        db.commit()
        templates_service.sync_json_backup(account_id)
        return {"ok": True, "updated": len(uids), "enabled": enabled}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": _safe_err(e)}


def _parse_send_range(payload: dict, cur_lo: float, cur_hi: float):
    """校验发送间隔区间。合法返回 (min, max)，不合法返回给用户看的原因字符串。

    Why 要说人话：这个值直接决定发消息的节奏，填错了要么被风控、
    要么跑到天亮。只回一句「参数错误」，用户根本不知道该改哪个。

    Why 缺的那一边取库里现有值而不是模块默认：只传了 send_max_sec 的客户端，
    另一边会被悄悄改回 4.5 —— 用户没动过的设置不该因为别人少传一个字段就变了。
    """
    try:
        lo = float(payload.get("send_min_sec", cur_lo))
        hi = float(payload.get("send_max_sec", cur_hi))
    except (TypeError, ValueError):
        return "间隔要填数字（秒）"
    if lo < trigger.SEND_MIN_FLOOR:
        return f"最小间隔太快了，不能少于 {trigger.SEND_MIN_FLOOR:g} 秒"
    if hi > trigger.SEND_MAX_CEIL:
        return f"最大间隔太慢了，不能超过 {trigger.SEND_MAX_CEIL:g} 秒"
    if lo > hi:
        return "最小间隔不能大于最大间隔"
    return lo, hi


@router.get("/schedule/{account_id}")
def api_get_schedule(account_id: int, user=Depends(require_user), db: Session = Depends(get_db)):
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)
    sch = db.query(Schedule).filter(Schedule.douyin_account_id == acc.id).first()
    if not sch:
        # 和 Schedule 的列默认值保持一致，别让「还没建行」看起来像另一套行为
        return {"ok": True, "enabled": False, "time": "09:00",
                "send_to_broken": True, "send_to_no_spark": False,
                "send_min_sec": trigger.SEND_MIN_DEFAULT,
                "send_max_sec": trigger.SEND_MAX_DEFAULT}
    lo, hi = trigger._configured_range(acc.id)
    return {"ok": True, "enabled": sch.enabled, "time": sch.time_hhmm,
            "send_to_broken": bool(sch.send_to_broken),
            "send_to_no_spark": bool(sch.send_to_no_spark),
            "send_min_sec": lo, "send_max_sec": hi}


@router.put("/schedule/{account_id}")
def api_set_schedule(
    account_id: int,
    payload: dict = Body(...),
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)
    enabled = bool(payload.get("enabled", False))
    t = str(payload.get("time", "09:00"))
    try:
        hh, mm = t.split(":")
        assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
    except Exception:
        return {"ok": False, "error": "时间格式 HH:MM"}
    # 间隔区间：两个都给了才动它。缺字段时保持原值 ——
    # 老前端不带这俩，不能把用户设过的节奏悄悄改回默认
    sch = db.query(Schedule).filter(Schedule.douyin_account_id == acc.id).first()
    if not sch:
        sch = Schedule(douyin_account_id=acc.id)
        db.add(sch)

    if "send_min_sec" in payload or "send_max_sec" in payload:
        cur_lo, cur_hi = trigger._configured_range(acc.id)
        span = _parse_send_range(payload, cur_lo, cur_hi)
        if isinstance(span, str):
            return {"ok": False, "error": span}
        sch.send_min_sec, sch.send_max_sec = span
    # 改了发送时间 → 清空"今日已跑"标记，让新时间点还能触发
    if sch.time_hhmm != t:
        sch.last_ran_date = None
    sch.enabled = enabled
    sch.time_hhmm = t
    # 缺字段时保持原值：老前端/其它调用方不带它，不能把用户的选择悄悄改掉
    if "send_to_broken" in payload:
        sch.send_to_broken = bool(payload["send_to_broken"])
    if "send_to_no_spark" in payload:
        sch.send_to_no_spark = bool(payload["send_to_no_spark"])
    db.add(AuditLog(actor_user_id=user.id, actor_kind="user", action="schedule_update",
                    target_type="account", target_id=str(acc.id),
                    meta=json.dumps({"enabled": enabled, "time": t,
                                     "send_to_broken": bool(sch.send_to_broken),
                                     "send_to_no_spark": bool(sch.send_to_no_spark)})))
    db.commit()
    return {"ok": True}


@router.get("/announcement")
def api_announcement(
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    """返回当前启用的最新公告（按 updated_at 倒序取第一条）"""
    ann = (db.query(Announcement)
             .filter(Announcement.enabled == True)
             .order_by(Announcement.updated_at.desc())
             .first())
    if not ann:
        return {"ok": True, "announcement": None}
    return {"ok": True, "announcement": {
        "id": ann.id, "title": ann.title, "content": ann.content,
        "updated_at": ann.updated_at.isoformat(),
    }}


@router.post("/cookies-check")
def api_cookies_check(
    payload: dict = Body(...),
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    """手动触发一次 cookies 体检，实时返回 ok/expired/error"""
    acc = _get_acc(payload, user, db)
    from ..scheduler import _cookies_health_check
    status = _cookies_health_check(acc.id)
    # 重新读取最新 acc 状态
    db.refresh(acc)
    return {"ok": status == "ok", "status": status,
            "account_status": acc.status}


@router.get("/notifications")
def api_notifications(
    user = Depends(require_user),
    db: Session = Depends(get_db),
    limit: int = 30,
):
    rows = (db.query(Notification)
              .filter(Notification.user_id == user.id)
              .order_by(Notification.created_at.desc())
              .limit(max(1, min(limit, 100))).all())
    unread = (db.query(Notification)
                .filter(Notification.user_id == user.id,
                        Notification.read_at.is_(None)).count())
    return {"ok": True, "unread": unread, "items": [{
        "id": n.id, "kind": n.kind, "title": n.title, "content": n.content,
        "url": n.url, "created_at": n.created_at.isoformat(),
        "read": n.read_at is not None,
    } for n in rows]}


@router.post("/notifications/read")
def api_notifications_read(
    payload: dict = Body(default={}),
    user = Depends(require_user),
    db: Session = Depends(get_db),
):
    """标记已读。payload 可带 {id: X} 标单条；否则全部标已读"""
    from datetime import datetime as _dt
    from ..csrf_mw import invalidate_unread_cache
    nid = payload.get("id") if isinstance(payload, dict) else None
    q = db.query(Notification).filter(
        Notification.user_id == user.id,
        Notification.read_at.is_(None))
    if nid:
        q = q.filter(Notification.id == int(nid))
    now = _dt.utcnow()
    updated = 0
    for n in q.all():
        n.read_at = now
        updated += 1
    db.commit()
    invalidate_unread_cache(user.id)
    return {"ok": True, "updated": updated}


# ── 辅助 ──

def _get_acc(payload: dict, user, db) -> DouyinAccount:
    aid = payload.get("account_id")
    if not aid:
        raise HTTPException(400, "缺少 account_id")
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == int(aid), DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404, "账户不存在")
    return acc
