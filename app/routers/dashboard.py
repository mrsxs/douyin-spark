"""
用户主面板 + 单账户页面
"""
import os
import threading
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db, SessionLocal
from ..models import DouyinAccount, Schedule, JobRun, JobRunItem
from ..deps import page_require_active, page_require_user
from ..storage import AccountCtx, set_account_ctx, delete_account_dir
from .. import templates_service, contacts_service
import douyin_im as dy

router = APIRouter()

# 后台补头像的去重：同一批账户同时只跑一个线程
_avatar_inflight: set = set()
_avatar_lock = threading.Lock()


@router.get("/", response_class=HTMLResponse)
def root(request: Request, user=Depends(page_require_user)):
    # 未激活 → /activate；已激活 → /dashboard
    now = datetime.utcnow()
    if not user.expires_at or user.expires_at < now:
        return RedirectResponse("/activate", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)


def _compute_health_map(accounts: list[DouyinAccount], db: Session,
                        now: datetime) -> dict[int, dict]:
    """一次性计算所有账户的健康状态，避免 N+1 查询。"""
    if not accounts:
        return {}

    acc_ids = [a.id for a in accounts]
    # 一条 SQL 拉最近的 auto JobRun（带排序，应用层按账户取前 5 个）
    # 依赖 idx_jobrun_acc_kind_started 复合索引
    runs = (db.query(JobRun)
              .filter(JobRun.douyin_account_id.in_(acc_ids),
                      JobRun.kind == "auto",
                      JobRun.status == "done")
              .order_by(JobRun.douyin_account_id,
                        JobRun.started_at.desc())
              .all())
    from collections import defaultdict
    runs_by_acc: dict[int, list] = defaultdict(list)
    for r in runs:
        if len(runs_by_acc[r.douyin_account_id]) < 5:
            runs_by_acc[r.douyin_account_id].append(r)

    out = {}
    for acc in accounts:
        out[acc.id] = _compute_health_one(acc, runs_by_acc.get(acc.id, []), now)
    return out


def _compute_health_one(acc: DouyinAccount, recent_runs: list, now: datetime) -> dict:
    if acc.status == "pending_login":
        return {"level": "slate", "label": "待登录", "reason": "还未完成登录"}
    if acc.status == "cookies_expired" or not acc.cookies_exist:
        return {"level": "red", "label": "需重登", "reason": "cookies 失效，请重新扫码"}
    if acc.status == "login_failed":
        return {"level": "red", "label": "登录失败", "reason": "上次登录未成功"}
    total_sent = sum(r.sent for r in recent_runs)
    total_failed = sum(r.failed for r in recent_runs)
    if total_sent + total_failed > 0:
        fail_rate = total_failed / (total_sent + total_failed)
        if fail_rate >= 0.5:
            return {"level": "amber", "label": "风控风险",
                    "reason": f"近 {len(recent_runs)} 次失败率 {int(fail_rate*100)}%"}
    if acc.last_run_at and (now - acc.last_run_at).days >= 7:
        return {"level": "amber", "label": "久未运行",
                "reason": f"{(now - acc.last_run_at).days} 天未触发"}
    return {"level": "green", "label": "正常", "reason": "运行正常"}


def _fill_missing_avatars(accounts: list[DouyinAccount], user_id: int, db: Session) -> None:
    """对 avatar 为空且能用 cookies 拉的账户，同步拉一次 self profile 写入。

    注意：这个函数会走网络，不要在请求路径里调用 —— 用 _schedule_avatar_fill。
    """
    targets = [a for a in accounts
               if not a.avatar and a.cookies_exist and a.status == "active"]
    if not targets:
        return
    changed = False
    for acc in targets:
        try:
            set_account_ctx(AccountCtx(user_id, acc.id))
            # 缺 dy_uid 先补
            if not acc.dy_uid:
                my_uid = dy.load_my_uid_from_cache()
                if my_uid:
                    acc.dy_uid = my_uid
                    changed = True
            if not acc.dy_uid:
                continue
            session = dy._load_session()
            if not session:
                continue
            prof = dy.fetch_self_profile(session, acc.dy_uid)
            if prof.get("avatar"):
                acc.avatar = contacts_service.normalize_avatar_url(prof["avatar"])
                changed = True
                if prof.get("nickname") and not acc.nickname:
                    acc.nickname = prof["nickname"]
        except Exception as e:
            print(f"[dashboard] 拉 acc#{acc.id} self avatar 失败: {e}")
    if changed:
        try:
            db.commit()
        except Exception as e:
            print(f"[dashboard] 写入 avatar 失败: {e}")
            db.rollback()


def _schedule_avatar_fill(account_ids: list[int], user_id: int) -> None:
    """后台补账户头像。同一批只跑一个线程，跑完即退。

    Why: 每个账户一次抖音 API 调用，串行放在请求里会让 /dashboard 白屏几秒；
    头像不是关键信息，缺了有首字母 fallback，异步补齐足够。
    """
    if not account_ids:
        return
    key = (user_id, tuple(sorted(account_ids)))
    with _avatar_lock:
        if key in _avatar_inflight:
            return
        _avatar_inflight.add(key)

    def worker():
        try:
            with SessionLocal() as db:
                accs = db.query(DouyinAccount).filter(
                    DouyinAccount.id.in_(account_ids)).all()
                _fill_missing_avatars(accs, user_id, db)
        except Exception as e:
            print(f"[dashboard] 后台补头像失败: {e}")
        finally:
            with _avatar_lock:
                _avatar_inflight.discard(key)

    threading.Thread(target=worker, daemon=True,
                     name=f"avatar-fill-{user_id}").start()


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(page_require_active), db: Session = Depends(get_db)):
    accounts = (db.query(DouyinAccount)
                  .filter(DouyinAccount.user_id == user.id)
                  .order_by(DouyinAccount.created_at.desc())
                  .all())
    now = datetime.utcnow()
    remain_days = (user.expires_at - now).days if user.expires_at else 0
    # 一次聚合查询计算所有账户的健康状态（避免 N+1）
    health_map = _compute_health_map(accounts, db, now)
    # 头像补齐要走抖音 API（每个账户一次网络请求），放后台线程别阻塞首屏。
    # 拉到后写库，下次进来就有了。
    _schedule_avatar_fill([a.id for a in accounts
                           if not a.avatar and a.cookies_exist and a.status == "active"],
                          user.id)
    return request.app.state.tmpl.TemplateResponse("dashboard/index.html", {
        "request": request,
        "user": user,
        "accounts": accounts,
        "remain_days": remain_days,
        "now": now,
        "health_map": health_map,
    })


@router.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_page(
    request: Request,
    account_id: int,
    user=Depends(page_require_active),
    db: Session = Depends(get_db),
    page: int = 1, q: str = "",
):
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id
    ).first()
    if not acc:
        raise HTTPException(404)

    sch = db.query(Schedule).filter(Schedule.douyin_account_id == acc.id).first()
    if not sch:
        sch = Schedule(douyin_account_id=acc.id, enabled=False, time_hhmm="09:00")
        db.add(sch); db.commit(); db.refresh(sch)

    # 首屏只读 Contact 冷备表 —— 纯 DB，毫秒级返回。
    # 原本这里同步调 trigger.get_contacts()（抖音 API，15s timeout）
    # 再叠一次头像补齐请求，白屏 5~20 秒，还占着同步 threadpool。
    # 最新数据由前端加载后异步打 /api/contacts 刷新。
    from .. import contacts_service, site_settings

    contacts, load_error, finalizing = [], None, False
    synced_at = None
    if acc.status == "active" and acc.cookies_exist:
        ctx = AccountCtx(user.id, acc.id)
        set_account_ctx(ctx)
        contacts = contacts_service.load_cached(db, acc.id)
        synced_at = contacts_service.last_synced_at(db, acc.id)
        if not os.path.exists(str(dy.INIT_REQ_BIN)) and not contacts:
            finalizing = True

    # 首屏这份冷备的指纹（全量，不是分页后的）。
    # 前端异步同步回来后拿它比对，数据变了才重渲染 —— 否则用户看到的
    # 天数/头像/已消失的好友会一直停在打开页面那一刻的旧快照上。
    contacts_snapshot = {
        c["uid"]: f"{c.get('days')}|{c.get('status')}|{1 if c.get('avatar') else 0}"
        for c in contacts
    }

    templates = templates_service.load_templates(acc.id)

    # 前端过滤 + 分页
    full_total = len(contacts)
    if q:
        q_lower = q.lower()
        contacts = [c for c in contacts
                    if q_lower in (c.get("nickname") or "").lower()
                    or q_lower in (c.get("uid") or "").lower()
                    or q_lower in (c.get("remark") or "").lower()]
    filtered_total = len(contacts)
    active_total = sum(1 for c in contacts if c.get("status", "active") == "active")
    # none = 从来没有火花的普通好友（include_all 解析出来的）；
    # 和 broken 分开计数，段控要分四段，红色横幅也只该为 broken 报警
    none_total = sum(1 for c in contacts if c.get("status") == "none")
    # 重燃中：断过但还在恢复窗口内，最紧急 —— 窗口过了原来那几百天就没了
    recovering_total = sum(1 for c in contacts if c.get("status") == "recovering")
    broken_total = filtered_total - active_total - none_total - recovering_total
    per_page = 30
    total_pages = max(1, (filtered_total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    contacts_page = contacts[start:start + per_page]

    return request.app.state.tmpl.TemplateResponse("dashboard/account.html", {
        "request": request,
        "user": user, "acc": acc,
        "schedule": sch,
        "contacts": contacts_page, "templates": templates,
        "default_tpl": templates.get("default", {"enabled": True, "messages": ["早"]}),
        "load_error": load_error,
        "finalizing": finalizing,
        "synced_at": synced_at,
        # 带 Z：DB 存的是 naive UTC，不标时区 JS 会当本地时间，
        # 刚同步完却显示「8 小时前」
        "synced_at_iso": (synced_at.isoformat() + "Z") if synced_at else None,
        "contacts_snapshot": contacts_snapshot,
        # 分享卡二维码（服务端生成的 data URL，避免 html2canvas 跨域污染 canvas）
        "share_qr_data_url": site_settings.qr_data_url(
            site_settings.load(db).get("site_url", "")),
        "q": q, "page": page, "total_pages": total_pages,
        "full_total": full_total, "filtered_total": filtered_total,
        "active_total": active_total, "broken_total": broken_total,
        "none_total": none_total,
        "recovering_total": recovering_total,
        "per_page": per_page,
        "now": datetime.now(),
    })


@router.get("/accounts/{account_id}/chat", response_class=HTMLResponse)
def account_chat(
    request: Request,
    account_id: int,
    uid: str = "",
    user=Depends(page_require_active),
    db: Session = Depends(get_db),
):
    """聊天页。首屏只读冷备（毫秒级），新消息由 SSE 推。"""
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id
    ).first()
    if not acc:
        raise HTTPException(404)

    from .. import contacts_service, messages_service

    contacts = contacts_service.load_cached(db, acc.id)
    last_map = messages_service.last_message_map(db, acc.id)

    # 会话列表：有过聊天的排前面并按最后一条时间倒序，
    # 没聊过的按火花天数排 —— 否则每次进来都要翻半天找人
    for c in contacts:
        last = last_map.get(c["uid"])
        c["last_text"] = last["text"] if last else ""
        c["last_ms"] = last["created_at"] if last else 0
        c["last_is_me"] = last["is_me"] if last else False
    contacts.sort(key=lambda c: (-c["last_ms"], -(c.get("days") or 0)))

    active_uid = uid or (contacts[0]["uid"] if contacts else "")
    limit = messages_service.DEFAULT_LIMIT
    messages = (messages_service.load_conversation(db, acc.id, active_uid,
                                                   limit=limit)
                if active_uid else [])
    peer = next((c for c in contacts if c["uid"] == active_uid), None)

    return request.app.state.tmpl.TemplateResponse("dashboard/chat.html", {
        "request": request,
        "user": user, "acc": acc,
        "contacts": contacts,
        "active_uid": active_uid,
        "peer": peer,
        "messages": messages,
        # 首屏就把「还有没有更早的」算准，前端别再拿条数去猜
        "has_more": len(messages) >= limit,
        "now": datetime.now(),
    })


@router.get("/accounts/{account_id}/runs", response_class=HTMLResponse)
def account_runs(
    request: Request, account_id: int,
    user=Depends(page_require_active), db: Session = Depends(get_db),
    page: int = 1,
):
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)
    per_page = 30
    total = db.query(JobRun).filter(JobRun.douyin_account_id == acc.id).count()
    runs = (db.query(JobRun)
              .filter(JobRun.douyin_account_id == acc.id)
              .order_by(JobRun.started_at.desc())
              .offset((page - 1) * per_page).limit(per_page).all())
    return request.app.state.tmpl.TemplateResponse("dashboard/runs.html", {
        "request": request, "user": user, "acc": acc,
        "runs": runs, "page": page, "total": total, "per_page": per_page,
    })


@router.get("/accounts/{account_id}/runs/{run_id}", response_class=HTMLResponse)
def account_run_detail(
    request: Request, account_id: int, run_id: int,
    user=Depends(page_require_active), db: Session = Depends(get_db),
):
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)
    run = db.get(JobRun, run_id)
    if not run or run.douyin_account_id != acc.id:
        raise HTTPException(404)
    items = (db.query(JobRunItem)
               .filter(JobRunItem.job_run_id == run_id)
               .order_by(JobRunItem.sent_at.asc()).all())
    return request.app.state.tmpl.TemplateResponse("dashboard/run_detail.html", {
        "request": request, "user": user, "acc": acc,
        "run": run, "items": items,
    })


@router.get("/accounts/{account_id}/logs", response_class=HTMLResponse)
def account_logs(
    request: Request, account_id: int,
    user=Depends(page_require_active), db: Session = Depends(get_db),
):
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id
    ).first()
    if not acc:
        raise HTTPException(404)
    log_dir = os.path.join(AccountCtx(user.id, acc.id).dir(), "logs")
    day = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(log_dir, f"{day}.log")
    lines = []
    if os.path.exists(log_file):
        with open(log_file) as f:
            lines = f.readlines()[-500:]
    return request.app.state.tmpl.TemplateResponse("dashboard/logs.html", {
        "request": request, "user": user, "acc": acc,
        "lines": lines, "day": day,
    })


# ── 账户增删 ──────────────────────────────────────────────────────────

@router.post("/accounts/new")
def create_account(
    request: Request,
    label: str = Form(...),
    user=Depends(page_require_active),
    db: Session = Depends(get_db),
):
    label = label.strip()[:40] or "未命名"
    existing = db.query(DouyinAccount).filter(DouyinAccount.user_id == user.id).count()
    if existing >= user.max_accounts:
        raise HTTPException(status_code=403, detail=f"已达到账户数上限 ({user.max_accounts})")
    if db.query(DouyinAccount).filter(
        DouyinAccount.user_id == user.id, DouyinAccount.label == label).first():
        return RedirectResponse("/dashboard?error=label_exists", status_code=302)
    acc = DouyinAccount(user_id=user.id, label=label, status="pending_login")
    db.add(acc); db.commit(); db.refresh(acc)
    return RedirectResponse(f"/accounts/{acc.id}/login", status_code=302)


@router.post("/accounts/{account_id}/delete")
def delete_account(
    request: Request, account_id: int,
    user=Depends(page_require_active), db: Session = Depends(get_db),
):
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id
    ).first()
    if not acc:
        raise HTTPException(404)
    delete_account_dir(user.id, acc.id)
    db.delete(acc); db.commit()
    return RedirectResponse("/dashboard", status_code=302)


@router.get("/accounts/{account_id}/login", response_class=HTMLResponse)
def account_login_page(
    request: Request, account_id: int,
    user=Depends(page_require_active), db: Session = Depends(get_db),
):
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id
    ).first()
    if not acc:
        raise HTTPException(404)
    return request.app.state.tmpl.TemplateResponse("dashboard/account_login.html", {
        "request": request, "user": user, "acc": acc,
    })
