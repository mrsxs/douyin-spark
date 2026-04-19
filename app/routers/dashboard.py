"""
用户主面板 + 单账户页面
"""
import json
import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User, DouyinAccount, Schedule, JobRun, JobRunItem
from ..deps import page_require_active, page_require_user, get_account_owned, require_active
from ..storage import AccountCtx, set_account_ctx, delete_account_dir
from ..config import settings
import douyin_im as dy

router = APIRouter()


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

    # 拉联系人 + 模板（若有问题显示 error 状态而不是崩溃）
    contacts, templates, load_error = [], {}, None
    finalizing = False   # True = cookies 已有但 init_req 还在后台抓（扫码加速后才出现）
    if acc.status == "active" and acc.cookies_exist:
        ctx = AccountCtx(user.id, acc.id)
        set_account_ctx(ctx)
        if not os.path.exists(str(dy.INIT_REQ_BIN)):
            finalizing = True
        else:
            try:
                from .. import trigger
                contacts, templates = trigger.get_contacts(user.id, acc.id)
            except Exception as e:
                msg = str(e)
                if "init_req" in msg:
                    finalizing = True
                else:
                    load_error = msg

    # 前端过滤 + 分页
    full_total = len(contacts)
    if q:
        q_lower = q.lower()
        contacts = [c for c in contacts
                    if q_lower in (c.get("nickname") or "").lower()
                    or q_lower in (c.get("uid") or "").lower()
                    or q_lower in (c.get("remark") or "").lower()]
    filtered_total = len(contacts)
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
        "q": q, "page": page, "total_pages": total_pages,
        "full_total": full_total, "filtered_total": filtered_total,
        "per_page": per_page,
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
