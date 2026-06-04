"""
JSON API：模板、定时、发送、立即续火花、联系人刷新。
每个请求携带 account_id，deps 自动验证归属。
"""
import json
import os
import traceback
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import DouyinAccount, Schedule, AuditLog, Announcement, JobRun, JobRunItem, Notification
from ..deps import require_active, get_account_owned
from ..storage import AccountCtx, set_account_ctx
from .. import trigger
import douyin_im as dy

router = APIRouter(prefix="/api")


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


@router.post("/refresh")
def api_refresh(
    payload: dict = Body(...),
    user = Depends(require_active),
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
                        acc.avatar = self_prof["avatar"]
                        if self_prof.get("nickname") and not acc.nickname:
                            acc.nickname = self_prof["nickname"]
                        db.commit()
                if session:
                    captured = dy.fetch_nicknames(session, missing)
                    if captured:
                        # 合并写 contacts.json 缓存（原子写）
                        import os as _os, tempfile as _tf
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
    user = Depends(require_active),
    db: Session = Depends(get_db),
):
    acc = _get_acc(payload, user, db)
    try:
        r = trigger.auto_run(user.id, acc.id, triggered_by="user")
        return {"ok": True, **r}
    except trigger.AlreadyRunning as e:
        return {"ok": False, "error": _safe_err(e)}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": _safe_err(e)}


def _format_send_detail(info: dict) -> str:
    """把 dy.get_last_send_info() 拼成人话"""
    if not info:
        return "无响应信息"
    if info.get("ok"):
        # extras 非空说明抖音在 OK 响应里夹带了风控/限流码，是 shadowban 的强信号
        extras = info.get("extras") or {}
        if extras:
            return f"服务端返回 OK（extras={extras}）"
        return "服务端返回 OK"
    stage = info.get("stage")
    if stage == "no_private_key":
        return info.get("error") or "缺少私钥，需要重新扫码登录"
    if stage == "no_ticket":
        return info.get("error") or "联系人 ticket 缺失（init_req 漏抓），需重新登录"
    if stage == "exception":
        return f"请求异常: {info.get('error')}"
    msg = info.get("msg")
    edesc = info.get("error_desc")
    http = info.get("http")
    parts = []
    if http is not None and http != 200:
        parts.append(f"HTTP {http}")
    if msg and msg != "OK":
        parts.append(f"message={msg}")
    if edesc and edesc not in ("", "0"):
        parts.append(f"error_desc={edesc}")
    if info.get("extras"):
        parts.append(f"extras={info['extras']}")
    if info.get("parse_error"):
        parts.append(f"解析失败: {info['parse_error']}")
    return " | ".join(parts) or "未知失败（见后端日志）"


@router.post("/send")
def api_send(
    payload: dict = Body(...),
    user = Depends(require_active),
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
        db.commit()
        return {"ok": bool(ok), "detail": detail}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": _safe_err(e)}


@router.post("/send-batch")
def api_send_batch(
    payload: dict = Body(...),
    user = Depends(require_active),
    db: Session = Depends(get_db),
):
    """批量给一组 uid 发同一条消息。payload: {account_id, uids: [...], text}"""
    import random
    import time as _time
    acc = _get_acc(payload, user, db)
    uids = payload.get("uids") or []
    text = (payload.get("text") or "").strip()
    if not uids or not text:
        return {"ok": False, "error": "缺少 uids/text"}
    try:
        contacts, _ = trigger.get_contacts(user.id, acc.id)
        by_uid = {c["uid"]: c for c in contacts}
        # 创建 JobRun
        run = JobRun(douyin_account_id=acc.id, kind="manual_batch",
                     triggered_by="user", status="running")
        db.add(run); db.commit(); db.refresh(run)
        run_id = run.id

        results = []
        sent = failed = 0
        # 基于该账户近期失败率自适应限速
        SEND_INTERVAL, _risk_level = trigger._adaptive_interval(acc.id, base=5.0)
        for i, uid in enumerate(uids):
            c = by_uid.get(str(uid))
            if not c:
                failed += 1
                results.append({"uid": uid, "ok": False, "detail": "联系人不存在"})
                db.add(JobRunItem(job_run_id=run_id, uid=str(uid),
                                  message=text, ok=False, detail="联系人不存在"))
                db.commit()
                continue
            if i > 0:
                _time.sleep(SEND_INTERVAL + random.uniform(-0.5, 0.5))
            try:
                ok = trigger.send_single(user.id, acc.id, c["conv_id"], c, text)
                detail = _format_send_detail(dy.get_last_send_info())
            except Exception as e:
                traceback.print_exc()
                ok, detail = False, str(e)
            results.append({"uid": uid, "nickname": c.get("nickname"),
                            "ok": bool(ok), "detail": detail})
            sent += 1 if ok else 0
            failed += 0 if ok else 1
            db.add(JobRunItem(
                job_run_id=run_id, uid=str(uid), nickname=c.get("nickname"),
                conv_id=c.get("conv_id"), message=text,
                ok=bool(ok), detail=(detail or "")[:1000],
            ))
            db.commit()
        # 收尾
        run = db.get(JobRun, run_id)
        run.finished_at = datetime.utcnow()
        run.sent = sent; run.failed = failed
        run.status = "done"
        db.add(AuditLog(actor_user_id=user.id, actor_kind="user", action="send_batch",
                        target_type="account", target_id=str(acc.id),
                        meta=json.dumps({"count": len(uids),
                                         "sent": sent, "failed": failed,
                                         "run_id": run_id})))
        db.commit()
        return {"ok": True, "sent": sent, "failed": failed, "results": results,
                "run_id": run_id}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": _safe_err(e)}


@router.put("/template/{account_id}/{uid}")
def api_update_template(
    account_id: int, uid: str,
    payload: dict = Body(...),
    user = Depends(require_active),
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


@router.get("/schedule/{account_id}")
def api_get_schedule(account_id: int, user=Depends(require_active), db: Session = Depends(get_db)):
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)
    sch = db.query(Schedule).filter(Schedule.douyin_account_id == acc.id).first()
    if not sch:
        return {"ok": True, "enabled": False, "time": "09:00"}
    return {"ok": True, "enabled": sch.enabled, "time": sch.time_hhmm}


@router.put("/schedule/{account_id}")
def api_set_schedule(
    account_id: int,
    payload: dict = Body(...),
    user = Depends(require_active),
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
    sch = db.query(Schedule).filter(Schedule.douyin_account_id == acc.id).first()
    if not sch:
        sch = Schedule(douyin_account_id=acc.id)
        db.add(sch)
    # 改了发送时间 → 清空"今日已跑"标记，让新时间点还能触发
    if sch.time_hhmm != t:
        sch.last_ran_date = None
    sch.enabled = enabled
    sch.time_hhmm = t
    db.add(AuditLog(actor_user_id=user.id, actor_kind="user", action="schedule_update",
                    target_type="account", target_id=str(acc.id),
                    meta=json.dumps({"enabled": enabled, "time": t})))
    db.commit()
    return {"ok": True}


@router.get("/announcement")
def api_announcement(
    user = Depends(require_active),
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
    user = Depends(require_active),
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
    user = Depends(require_active),
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
    user = Depends(require_active),
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
