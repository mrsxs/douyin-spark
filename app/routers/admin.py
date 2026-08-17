"""
管理员后台：用户、授权码、审计日志
"""
import json
import secrets
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Body
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import or_, func
from sqlalchemy.orm import Session
import io
import csv

from ..db import get_db
from ..models import User, LicenseCode, DouyinAccount, AuditLog, Announcement, AppSetting, JobRun
from ..deps import page_require_admin, require_admin
from ..security import hash_password
from ..notify import notify

router = APIRouter(prefix="/admin")


def _gen_code(length: int = 16) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 去掉 I/O/0/1 避免混淆
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _broadcast_announcement(db: Session, ann: Announcement) -> int:
    """把启用的公告广播到每个活跃用户的站内通知。返回推送数。
    kind="announcement" 不在 _EMAIL_KINDS 里，默认不发邮件。
    """
    rows = db.query(User.id).filter(User.is_active == True).all()
    content = (ann.content or "")[:500]
    for (uid,) in rows:
        notify(db, user_id=uid, kind="announcement",
               title=f"[公告] {ann.title}",
               content=content, url="/dashboard")
    return len(rows)


def _build_stats(db: Session) -> dict:
    now = datetime.utcnow()
    return {
        "users_total":    db.query(func.count(User.id)).scalar() or 0,
        "users_active":   db.query(func.count(User.id)).filter(User.expires_at > now).scalar() or 0,
        "codes_total":    db.query(func.count(LicenseCode.id)).scalar() or 0,
        "codes_unused":   db.query(func.count(LicenseCode.id)).filter(
            LicenseCode.used_by.is_(None), LicenseCode.revoked_at.is_(None)).scalar() or 0,
        "accounts_total": db.query(func.count(DouyinAccount.id)).scalar() or 0,
        "runs_today":     db.query(func.count(AuditLog.id)).filter(
            AuditLog.action == "auto_run",
            AuditLog.ts >= now.replace(hour=0, minute=0, second=0, microsecond=0)
        ).scalar() or 0,
    }


def _build_trend(db: Session, days: int = 7) -> list[dict]:
    """最近 N 天的每日触发次数，按本地自然日聚合。

    JobRun.started_at 存的是 naive UTC，而管理员看的是本地日期，
    所以按本地日边界换算成 UTC 区间来分桶。
    """
    WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    now_local = datetime.now()
    tz_offset = now_local - datetime.utcnow()

    out = []
    for i in range(days - 1, -1, -1):
        day_local = (now_local - timedelta(days=i)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        start_utc = day_local - tz_offset
        end_utc = start_utc + timedelta(days=1)
        count = (db.query(func.count(JobRun.id))
                   .filter(JobRun.started_at >= start_utc,
                           JobRun.started_at < end_utc)
                   .scalar()) or 0
        out.append({
            "label": WEEKDAYS[day_local.weekday()],
            "date": day_local.strftime("%Y-%m-%d"),
            "value": count,
        })
    return out


@router.get("", response_class=HTMLResponse)
def admin_home(request: Request, admin=Depends(page_require_admin), db: Session = Depends(get_db)):
    stats = _build_stats(db)
    trend = _build_trend(db, days=7)
    recent_logs = (db.query(AuditLog).order_by(AuditLog.ts.desc()).limit(20).all())
    return request.app.state.tmpl.TemplateResponse("admin/home.html", {
        "request": request, "admin": admin, "stats": stats,
        "trend": trend, "trend_max": max((p["value"] for p in trend), default=0),
        "logs": recent_logs,
    })


# ── 用户 ──────────────────────────────────────────────────────────────

@router.get("/users", response_class=HTMLResponse)
def admin_users(
    request: Request,
    admin = Depends(page_require_admin),
    db: Session = Depends(get_db),
    q: str = "", status: str = "all", page: int = 1,
):
    per_page = 50
    query = db.query(User)
    if q:
        query = query.filter(or_(User.username.contains(q), User.email.contains(q)))
    now = datetime.utcnow()
    if status == "active":
        query = query.filter(User.is_active == True, User.expires_at > now)
    elif status == "expired":
        query = query.filter(or_(User.expires_at.is_(None), User.expires_at < now))
    elif status == "inactive":
        query = query.filter(User.is_active == False)
    total = query.count()
    users = (query.order_by(User.created_at.desc())
                  .offset((page - 1) * per_page).limit(per_page).all())
    # 查每个用户的账户数
    acc_counts = dict(db.query(DouyinAccount.user_id, func.count(DouyinAccount.id))
                        .group_by(DouyinAccount.user_id).all())
    for u in users:
        u._acc_count = acc_counts.get(u.id, 0)
    return request.app.state.tmpl.TemplateResponse("admin/users.html", {
        "request": request, "admin": admin,
        "users": users, "q": q, "status": status,
        "page": page, "total": total, "per_page": per_page,
        "now": now,
    })


@router.get("/users/{user_id}", response_class=HTMLResponse)
def admin_user_detail(
    request: Request, user_id: int,
    admin = Depends(page_require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404)
    accounts = db.query(DouyinAccount).filter(DouyinAccount.user_id == user.id).all()
    codes = db.query(LicenseCode).filter(LicenseCode.used_by == user.id).all()
    logs = (db.query(AuditLog).filter(AuditLog.actor_user_id == user.id)
              .order_by(AuditLog.ts.desc()).limit(50).all())
    return request.app.state.tmpl.TemplateResponse("admin/user_detail.html", {
        "request": request, "admin": admin,
        "target_user": user, "accounts": accounts,
        "codes": codes, "logs": logs, "now": datetime.utcnow(),
    })


@router.post("/users/{user_id}/extend")
def admin_extend(
    user_id: int, days: int = Form(...),
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(404)
    now = datetime.utcnow()
    base = user.expires_at if (user.expires_at and user.expires_at > now) else now
    user.expires_at = base + timedelta(days=int(days))
    db.add(AuditLog(actor_user_id=admin.id, actor_kind="admin", action="extend_expiry",
                    target_type="user", target_id=str(user.id), meta=f"+{days}d"))
    db.commit()
    return RedirectResponse(f"/admin/users/{user_id}", status_code=302)


@router.post("/users/{user_id}/toggle-active")
def admin_toggle_active(
    user_id: int,
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(404)
    if user.is_admin:
        raise HTTPException(400, "不能禁用管理员")
    user.is_active = not user.is_active
    db.add(AuditLog(actor_user_id=admin.id, actor_kind="admin", action="toggle_active",
                    target_type="user", target_id=str(user.id),
                    meta=str(user.is_active)))
    db.commit()
    return RedirectResponse(f"/admin/users/{user_id}", status_code=302)


@router.post("/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: int, new_password: str = Form(...),
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(404)
    if len(new_password) < 6:
        raise HTTPException(400, "密码过短")
    user.password_hash = hash_password(new_password)
    # 让该用户已签发的所有 cookie 立即失效 —— 否则被盗号后改密码也踢不掉攻击者
    user.session_version = (user.session_version or 0) + 1
    db.add(AuditLog(actor_user_id=admin.id, actor_kind="admin", action="reset_password",
                    target_type="user", target_id=str(user.id)))
    db.commit()
    return RedirectResponse(f"/admin/users/{user_id}", status_code=302)


@router.post("/users/{user_id}/max-accounts")
def admin_set_max_accounts(
    user_id: int, max_accounts: int = Form(...),
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(404)
    user.max_accounts = max(0, int(max_accounts))
    db.add(AuditLog(actor_user_id=admin.id, actor_kind="admin", action="set_max_accounts",
                    target_type="user", target_id=str(user.id),
                    meta=str(user.max_accounts)))
    db.commit()
    return RedirectResponse(f"/admin/users/{user_id}", status_code=302)


# ── 授权码 ────────────────────────────────────────────────────────────

@router.get("/codes", response_class=HTMLResponse)
def admin_codes(
    request: Request,
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
    filter: str = "all", page: int = 1,
):
    per_page = 50
    query = db.query(LicenseCode)
    if filter == "unused":
        query = query.filter(LicenseCode.used_by.is_(None), LicenseCode.revoked_at.is_(None))
    elif filter == "used":
        query = query.filter(LicenseCode.used_by.isnot(None))
    elif filter == "revoked":
        query = query.filter(LicenseCode.revoked_at.isnot(None))
    total = query.count()
    codes = (query.order_by(LicenseCode.created_at.desc())
                  .offset((page - 1) * per_page).limit(per_page).all())
    # 带出 used_by 的 username
    user_map = {}
    uids = [c.used_by for c in codes if c.used_by]
    if uids:
        for u in db.query(User).filter(User.id.in_(uids)).all():
            user_map[u.id] = u.username
    return request.app.state.tmpl.TemplateResponse("admin/codes.html", {
        "request": request, "admin": admin,
        "codes": codes, "user_map": user_map,
        "filter": filter, "total": total,
        "page": page, "per_page": per_page,
    })


@router.post("/codes/new")
def admin_codes_new(
    count: int = Form(1), duration_days: int = Form(30),
    max_accounts: int = Form(1), note: str = Form(""),
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    count = max(1, min(int(count), 200))
    generated = []
    for _ in range(count):
        while True:
            code = _gen_code()
            if not db.query(LicenseCode).filter(LicenseCode.code == code).first():
                break
        lc = LicenseCode(
            code=code, duration_days=int(duration_days),
            max_accounts=int(max_accounts), note=note[:200] or None,
            created_by=admin.id,
        )
        db.add(lc); generated.append(code)
    db.add(AuditLog(actor_user_id=admin.id, actor_kind="admin", action="codes_generate",
                    meta=json.dumps({"count": count, "duration_days": duration_days,
                                     "max_accounts": max_accounts, "note": note})))
    db.commit()
    return RedirectResponse("/admin/codes", status_code=302)


@router.post("/codes/{code_id}/revoke")
def admin_code_revoke(
    code_id: int,
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    lc = db.query(LicenseCode).filter(LicenseCode.id == code_id).first()
    if not lc: raise HTTPException(404)
    lc.revoked_at = datetime.utcnow()
    db.add(AuditLog(actor_user_id=admin.id, actor_kind="admin", action="code_revoke",
                    target_type="license_code", target_id=str(lc.id)))
    db.commit()
    return RedirectResponse("/admin/codes", status_code=302)


@router.get("/codes.csv")
def admin_codes_csv(
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
    filter: str = "unused",
):
    query = db.query(LicenseCode)
    if filter == "unused":
        query = query.filter(LicenseCode.used_by.is_(None), LicenseCode.revoked_at.is_(None))
    codes = query.order_by(LicenseCode.created_at.desc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["code", "duration_days", "max_accounts", "note", "created_at", "used_by", "used_at", "revoked_at"])
    for c in codes:
        w.writerow([c.code, c.duration_days, c.max_accounts, c.note or "",
                    c.created_at.isoformat() if c.created_at else "",
                    c.used_by or "", c.used_at.isoformat() if c.used_at else "",
                    c.revoked_at.isoformat() if c.revoked_at else ""])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="license_codes_{filter}.csv"'},
    )


# ── 审计日志 ──────────────────────────────────────────────────────────

@router.get("/audit", response_class=HTMLResponse)
def admin_audit(
    request: Request,
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
    action: str = "", page: int = 1,
):
    per_page = 100
    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action == action)
    total = query.count()
    logs = (query.order_by(AuditLog.ts.desc())
                 .offset((page - 1) * per_page).limit(per_page).all())
    return request.app.state.tmpl.TemplateResponse("admin/audit.html", {
        "request": request, "admin": admin, "logs": logs,
        "action": action, "page": page, "total": total, "per_page": per_page,
    })


# ── 公告 ──────────────────────────────────────────────────────────────

@router.get("/announcements", response_class=HTMLResponse)
def admin_announcements(
    request: Request,
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    items = db.query(Announcement).order_by(Announcement.updated_at.desc()).all()
    return request.app.state.tmpl.TemplateResponse("admin/announcements.html", {
        "request": request, "admin": admin, "items": items,
    })


@router.post("/announcements/new")
def admin_announcement_new(
    request: Request,
    title: str = Form(...), content: str = Form(...),
    enabled: str = Form(""),
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    ann = Announcement(
        title=title.strip()[:120] or "公告",
        content=content.strip(),
        enabled=(enabled == "on"),
        updated_by=admin.id,
    )
    db.add(ann)
    db.flush()   # 生成 ann.id 供 audit 使用，否则 target_id 会是字符串 "None"
    pushed = _broadcast_announcement(db, ann) if ann.enabled else 0
    db.add(AuditLog(actor_user_id=admin.id, actor_kind="admin",
                    action="announcement_new", target_type="announcement",
                    target_id=str(ann.id),
                    meta=json.dumps({"title": ann.title, "pushed": pushed})))
    db.commit()
    return RedirectResponse("/admin/announcements", status_code=302)


@router.post("/announcements/{ann_id}/edit")
def admin_announcement_edit(
    request: Request, ann_id: int,
    title: str = Form(...), content: str = Form(...),
    enabled: str = Form(""),
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    ann = db.get(Announcement, ann_id)
    if not ann:
        raise HTTPException(404)
    was_enabled = ann.enabled
    ann.title   = title.strip()[:120] or ann.title
    ann.content = content.strip()
    ann.enabled = (enabled == "on")
    ann.updated_by = admin.id
    # 禁用 → 启用 时再广播一次；其他编辑不重复推送，避免微调骚扰
    pushed = _broadcast_announcement(db, ann) if (not was_enabled and ann.enabled) else 0
    db.add(AuditLog(actor_user_id=admin.id, actor_kind="admin",
                    action="announcement_edit", target_type="announcement",
                    target_id=str(ann.id),
                    meta=json.dumps({"pushed": pushed})))
    db.commit()
    return RedirectResponse("/admin/announcements", status_code=302)


@router.post("/announcements/{ann_id}/delete")
def admin_announcement_delete(
    request: Request, ann_id: int,
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    ann = db.get(Announcement, ann_id)
    if not ann:
        raise HTTPException(404)
    db.delete(ann)
    db.add(AuditLog(actor_user_id=admin.id, actor_kind="admin",
                    action="announcement_delete", target_type="announcement",
                    target_id=str(ann_id)))
    db.commit()
    return RedirectResponse("/admin/announcements", status_code=302)


@router.post("/announcements/{ann_id}/broadcast")
def admin_announcement_broadcast(
    request: Request, ann_id: int,
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    """把这条公告再推一次给所有活跃用户的站内通知（不发邮件）。
    用于历史公告补推、或想再提醒一次时。
    """
    ann = db.get(Announcement, ann_id)
    if not ann:
        raise HTTPException(404)
    if not ann.enabled:
        raise HTTPException(400, "公告未启用，无法广播")
    pushed = _broadcast_announcement(db, ann)
    db.add(AuditLog(actor_user_id=admin.id, actor_kind="admin",
                    action="announcement_broadcast", target_type="announcement",
                    target_id=str(ann.id),
                    meta=json.dumps({"pushed": pushed})))
    db.commit()
    return RedirectResponse("/admin/announcements", status_code=302)


# ── SMTP 设置 ────────────────────────────────────────────────────────

def _load_smtp_cfg(db: Session) -> dict:
    row = db.get(AppSetting, "smtp")
    if row and row.value:
        try:
            return json.loads(row.value)
        except Exception:
            pass
    return {}


def _save_smtp_cfg(db: Session, cfg: dict, admin_id: int) -> None:
    row = db.get(AppSetting, "smtp")
    if not row:
        row = AppSetting(key="smtp", value=json.dumps(cfg), updated_by=admin_id)
        db.add(row)
    else:
        row.value = json.dumps(cfg)
        row.updated_by = admin_id


@router.get("/site", response_class=HTMLResponse)
def admin_site(
    request: Request,
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    from .. import site_settings
    return request.app.state.tmpl.TemplateResponse("admin/site.html", {
        "request": request, "admin": admin,
        "cfg": site_settings.load(db),
        "ok": request.query_params.get("ok"),
        "error": request.query_params.get("error"),
    })


@router.post("/site")
def admin_site_save(
    request: Request,
    site_url: str = Form(""),
    xianyu_url: str = Form(""),
    xianyu_note: str = Form(""),
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    from .. import site_settings
    try:
        cfg = site_settings.save(db, {
            "site_url": site_url,
            "xianyu_url": xianyu_url,
            "xianyu_note": xianyu_note,
        }, admin.id)
    except ValueError as e:
        return RedirectResponse(f"/admin/site?error={e}", status_code=302)

    db.add(AuditLog(actor_user_id=admin.id, actor_kind="admin",
                    action="site_update", target_type="app_setting",
                    target_id="site",
                    meta=json.dumps({"site_url": cfg["site_url"],
                                     "has_xianyu": bool(cfg["xianyu_url"])})))
    db.commit()
    # 模板里的 site_cfg() 有 30s 缓存，保存后立刻失效，别让管理员以为没生效
    invalidate = getattr(request.app.state, "invalidate_site_cfg", None)
    if invalidate:
        invalidate()
    return RedirectResponse("/admin/site?ok=1", status_code=302)


@router.get("/smtp", response_class=HTMLResponse)
def admin_smtp(
    request: Request,
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    cfg = _load_smtp_cfg(db)
    return request.app.state.tmpl.TemplateResponse("admin/smtp.html", {
        "request": request, "admin": admin, "cfg": cfg,
        "test_to": admin.email or "",
    })


@router.post("/smtp")
def admin_smtp_save(
    request: Request,
    host: str = Form(""),
    port: int = Form(587),
    user: str = Form(""),
    password: str = Form(""),
    from_addr: str = Form(""),
    tls: str = Form(""),
    enabled: str = Form(""),
    admin = Depends(page_require_admin), db: Session = Depends(get_db),
):
    from ..crypto import encrypt
    old = _load_smtp_cfg(db)
    # 密码：空 → 保留旧加密值；非空 → 新密码加密存
    if password:
        encrypted_pwd = encrypt(password)
    else:
        encrypted_pwd = old.get("password", "")
    cfg = {
        "enabled": (enabled == "on"),
        "host": host.strip(),
        "port": int(port or 587),
        "user": user.strip(),
        "password": encrypted_pwd,     # DB 里永远是 ENC1:xxx 格式
        "from": from_addr.strip(),
        "tls": (tls == "on"),
    }
    _save_smtp_cfg(db, cfg, admin.id)
    db.add(AuditLog(actor_user_id=admin.id, actor_kind="admin",
                    action="smtp_update", target_type="app_setting",
                    target_id="smtp",
                    meta=json.dumps({"enabled": cfg["enabled"],
                                     "host": cfg["host"], "user": cfg["user"]})))
    db.commit()
    return RedirectResponse("/admin/smtp?ok=1", status_code=302)


@router.post("/smtp/test")
def admin_smtp_test(
    payload: dict = Body(...),
    admin = Depends(require_admin), db: Session = Depends(get_db),
):
    from ..notify import send_test_email
    to = (payload.get("to") or "").strip()
    if not to:
        return {"ok": False, "error": "收件人不能为空"}
    # 用表单上未保存的临时配置测试？简单起见：先保存再测试。这里用已保存的 cfg。
    cfg = _load_smtp_cfg(db)
    if not cfg.get("enabled"):
        return {"ok": False, "error": "SMTP 未启用，先勾选启用并保存"}
    ok, msg = send_test_email(to, cfg)
    return {"ok": ok, "error": msg if not ok else "", "message": "已发送，请查收" if ok else ""}
