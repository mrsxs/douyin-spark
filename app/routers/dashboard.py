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


def _fill_missing_avatars(accounts: list[DouyinAccount], user_id: int, db: Session) -> None:
    """对 avatar 为空且能用 cookies 拉的账户，同步拉一次 self profile 写入。

    Why: 用户进 dashboard 期望看到真实头像，而不是首字母 fallback。
    How: 每个账户独立 try，失败/超时静默跳过——不阻塞 dashboard 渲染。
    若 dy_uid 也缺失，先从已缓存的 init.bin 抽出来。
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
                acc.avatar = prof["avatar"]
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


def _backfill_avatars_for_page(user_id: int, account_id: int, contacts_page: list[dict]) -> None:
    """对本页缺头像的联系人，调一次 fetch_nicknames 写回缓存 + 注入返回结构。

    Why: 旧 contacts.json 缓存里只存了 nick/remark，没存 avatar 字段。
    用户进 account 页时若发现一批可见联系人都没头像，懒加载补齐——
    只补"本页可见的 30 个"，避免一次性抓 4000+ 个慢。
    """
    missing = [c for c in contacts_page
               if not c.get("avatar") and c.get("sec_uid")]
    if not missing:
        return
    try:
        set_account_ctx(AccountCtx(user_id, account_id))
        session = dy._load_session()
        if not session:
            return
        captured = dy.fetch_nicknames(session, missing)
        if not captured:
            return
        # 合并写 contacts.json 缓存（原子写）
        cache_path = str(dy.CONTACTS_FILE)
        try:
            existing = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
        except Exception:
            existing = {}
        for uid, info in captured.items():
            prev = existing.get(uid) if isinstance(existing.get(uid), dict) else {}
            merged = dict(prev) if isinstance(prev, dict) else {}
            for k in ("nick", "remark", "avatar"):
                if info.get(k):
                    merged[k] = info[k]
            existing[uid] = merged
        # 原子写
        import tempfile
        cache_dir = os.path.dirname(cache_path) or "."
        os.makedirs(cache_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=cache_dir, prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False)
                f.flush(); os.fsync(f.fileno())
            os.replace(tmp_path, cache_path)
        except Exception:
            if os.path.exists(tmp_path): os.unlink(tmp_path)
            raise
        # 把 avatar 注入本页 contacts，让本次渲染就能看到
        for c in contacts_page:
            info = captured.get(c.get("uid"))
            if info and info.get("avatar"):
                c["avatar"] = info["avatar"]
    except Exception as e:
        print(f"[backfill_avatars] acc#{account_id} 失败: {e}")


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
    # 顺手为没有 avatar 的活跃账户拉一次 self profile（best-effort, 静默失败）
    _fill_missing_avatars(accounts, user.id, db)
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
    active_total = sum(1 for c in contacts if c.get("status", "active") == "active")
    broken_total = filtered_total - active_total
    per_page = 30
    total_pages = max(1, (filtered_total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    contacts_page = contacts[start:start + per_page]
    # 当本页联系人缺头像（旧 cache 未存 avatar）→ 同步拉一次写回（best-effort）
    _backfill_avatars_for_page(user.id, acc.id, contacts_page)

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
        "active_total": active_total, "broken_total": broken_total,
        "per_page": per_page,
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
