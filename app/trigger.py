"""
业务入口：给一个 (user_id, account_id) 做一次"续火花"。
不依赖 HTTP context，可被 web 请求或 scheduler 调用。
"""
import json
import os
import random
import time
from datetime import datetime

import douyin_im as dy
from .storage import AccountCtx, set_account_ctx
from .db import SessionLocal
from .models import DouyinAccount, AuditLog, Schedule, JobRun, JobRunItem
from .notify import notify


class NotReady(Exception):
    """账户尚未就绪（cookies 缺失、init_req 没抓到等）"""


class AlreadyRunning(Exception):
    """同账户已有另一个 auto_run 在执行"""


# 进程内账户级互斥：防 scheduler + /api/auto 并发触发同一个账户
import threading as _threading
_account_locks: dict[int, _threading.Lock] = {}
_account_locks_registry_lock = _threading.Lock()


def _get_account_lock(account_id: int) -> _threading.Lock:
    with _account_locks_registry_lock:
        lock = _account_locks.get(account_id)
        if lock is None:
            lock = _threading.Lock()
            _account_locks[account_id] = lock
        return lock


def _adaptive_interval(account_id: int, base: float = 5.0) -> tuple[float, str]:
    """基于最近 3 次 auto 任务的失败率，返回发送间隔 + 提示文字。

    - 失败率 <20%：base 秒（正常）
    - 20%-50%：3x base（有些慢）
    - >=50%：12x base（严重降速，避免持续触发风控）
    """
    with SessionLocal() as db:
        recent = (db.query(JobRun)
                    .filter(JobRun.douyin_account_id == account_id,
                            JobRun.kind.in_(("auto", "manual_batch")),
                            JobRun.status == "done")
                    .order_by(JobRun.started_at.desc())
                    .limit(3).all())
    total_sent = sum(r.sent for r in recent)
    total_failed = sum(r.failed for r in recent)
    denom = total_sent + total_failed
    if denom == 0:
        return base, "normal"
    rate = total_failed / denom
    if rate >= 0.5:
        return base * 12, f"high_risk ({int(rate*100)}% fail)"
    if rate >= 0.2:
        return base * 3, f"medium_risk ({int(rate*100)}% fail)"
    return base, "normal"


def _ensure_active(ctx: AccountCtx) -> tuple[dict, list[dict]]:
    """加载 cookies + 拉联系人；返回 (constants, contacts)"""
    # 设置线程 ctx，这样 douyin_im 的所有路径会路由到本账户目录
    set_account_ctx(ctx)
    if not os.path.exists(str(dy.COOKIE_FILE)):
        raise NotReady("cookies.json 不存在（未登录）")
    if not os.path.exists(str(dy.INIT_REQ_BIN)):
        raise NotReady("init_req.bin 不存在（登录时未抓到会话数据）")
    session = dy._load_session()
    if not session or not dy._check_login(session):
        raise NotReady("cookies 无效")
    constants = dy.extract_constants(str(dy.INIT_REQ_BIN))
    resp = session.post(
        "https://imapi.douyin.com/v1/message/get_message_by_init",
        data=open(str(dy.INIT_REQ_BIN), "rb").read(),
        headers={**dy.HEADERS, "Content-Type": "application/x-protobuf"},
        timeout=15,
    )
    if resp.status_code != 200 or len(resp.content) < 1024:
        raise NotReady(f"get_message_by_init 失败: HTTP {resp.status_code}")
    contacts = dy.parse_fire_streaks(resp.content)
    # 合并缓存昵称
    try:
        cache = json.load(open(str(dy.CONTACTS_FILE)))
    except Exception:
        cache = {}
    for c in contacts:
        info = cache.get(c["uid"], {})
        if isinstance(info, dict):
            c["nickname"] = info.get("remark") or info.get("nick") or c["uid"]
        else:
            c["nickname"] = info or c["uid"]
    return session, constants, contacts


def get_contacts(user_id: int, account_id: int) -> tuple[list[dict], dict]:
    """给 Web 页面用：返回 contacts + templates（模板从 DB 读）"""
    ctx = AccountCtx(user_id=user_id, account_id=account_id)
    _, _, contacts = _ensure_active(ctx)
    # 模板改从 DB 读；首次没数据时 fall back 到文件，之后 api 写入都进 DB
    from . import templates_service
    templates = templates_service.load_templates(account_id)
    if not templates:
        # DB 还没有迁移到 → 退回文件
        try:
            templates = json.load(open(str(dy.TEMPLATES_FILE)))
        except Exception:
            templates = {}
    # 同时 upsert 联系人缓存（跑一次就有 DB 快照，便于将来搜索/统计）
    try:
        with SessionLocal() as db:
            templates_service.upsert_contact_cache(db, account_id, contacts)
            db.commit()
    except Exception as e:
        print(f"[trigger] 联系人缓存 upsert 失败: {e}")
    return contacts, templates


def send_single(user_id: int, account_id: int, conv_id: str, contact: dict, text: str) -> bool:
    ctx = AccountCtx(user_id=user_id, account_id=account_id)
    session, constants, _ = _ensure_active(ctx)
    return dy.send_text(session, conv_id, text, contact, constants)


def auto_run(user_id: int, account_id: int, triggered_by: str = "scheduler") -> dict:
    """主流程：给所有启用的联系人按模板发一条。
    账户级互斥：同一 account_id 不允许并发执行（scheduler + /api/auto 同时触发）。
    """
    lock = _get_account_lock(account_id)
    if not lock.acquire(blocking=False):
        raise AlreadyRunning(f"账户 {account_id} 已有任务在执行，请等待上次完成")
    try:
        return _auto_run_locked(user_id, account_id, triggered_by)
    finally:
        lock.release()


def _auto_run_locked(user_id: int, account_id: int, triggered_by: str) -> dict:
    # 先建 JobRun，确保任何异常都能被记账（便于 scheduler 判断"今日失败次数"）
    with SessionLocal() as db:
        run = JobRun(douyin_account_id=account_id, kind="auto",
                     triggered_by=triggered_by, status="running")
        db.add(run); db.commit(); db.refresh(run)
        run_id = run.id

    try:
        ctx = AccountCtx(user_id=user_id, account_id=account_id)
        session, constants, contacts = _ensure_active(ctx)
    except Exception as e:
        # cookies 失效 / init_req 缺失等前置错误：立即把 JobRun 标 error
        try:
            with SessionLocal() as db:
                r = db.get(JobRun, run_id)
                if r:
                    r.status = "error"
                    r.finished_at = datetime.utcnow()
                    r.error = f"setup: {type(e).__name__}: {e}"[:500]
                    db.commit()
        except Exception:
            pass
        raise

    from . import templates_service
    templates = templates_service.load_templates(account_id)
    if not templates:
        try:
            templates = json.load(open(str(dy.TEMPLATES_FILE)))
        except Exception:
            templates = {}

    sent = skipped = failed = 0
    # 基于近期失败率自适应限速；随机 ±0.5s jitter 更像人工操作
    SEND_INTERVAL, _risk_level = _adaptive_interval(account_id, base=5.0)
    dy._log(f"[auto] send interval = {SEND_INTERVAL}s ({_risk_level})", "RATE")
    # 连续失败熔断：连续 5 次失败则停当前任务
    consecutive_fail = 0
    for i, c in enumerate(contacts):
        msg = dy._pick_message(c["uid"], c["nickname"], templates)
        if not msg:
            skipped += 1
            # skipped 联系人不入明细（量太大会影响 DB 体积）
            continue
        if i > 0:
            time.sleep(SEND_INTERVAL + random.uniform(-0.5, 0.5))
        item_ok = False
        item_detail = ""
        try:
            item_ok = dy.send_text(session, c["conv_id"], msg, c, constants)
            # 拿诊断详情（线程局部）
            info = dy.get_last_send_info()
            if item_ok:
                sent += 1
                consecutive_fail = 0
                item_detail = f"OK (msg={info.get('msg','')})"
                dy._log(f"[auto] ✓ {c['nickname']}: {msg!r}", "SEND")
            else:
                failed += 1
                consecutive_fail += 1
                item_detail = (f"http={info.get('http')} msg={info.get('msg')} "
                               f"err={info.get('error_desc')} stage={info.get('stage')}")
                dy._log(f"[auto] ✗ {c['nickname']}: {item_detail}", "FAIL")
        except Exception as e:
            failed += 1
            consecutive_fail += 1
            item_detail = f"exception: {e}"
            dy._log(f"[auto] ✗ {c['nickname']}: {e}", "ERR")
        # 写明细
        try:
            with SessionLocal() as db:
                db.add(JobRunItem(
                    job_run_id=run_id, uid=c["uid"],
                    nickname=c.get("nickname"), conv_id=c.get("conv_id"),
                    message=msg, ok=item_ok, detail=item_detail[:1000],
                ))
                db.commit()
        except Exception as _e:
            print(f"[auto] 写 JobRunItem 失败: {_e}")

        # 连续失败熔断：避免持续击打风控
        if consecutive_fail >= 5:
            dy._log(f"[auto] 连续 {consecutive_fail} 次失败，熔断当前任务", "CIRCUIT")
            try:
                with SessionLocal() as db:
                    run = db.get(JobRun, run_id)
                    if run:
                        run.error = f"circuit_breaker: {consecutive_fail} consecutive failures"
                        db.commit()
            except Exception:
                pass
            break

    summary = {"sent": sent, "skipped": skipped, "failed": failed,
               "risk": _risk_level, "interval": SEND_INTERVAL}
    dy._log(f"[auto] DONE {summary}", "DONE")

    # 更新 JobRun + 原有 DB 记录 + 失败时发站内通知
    with SessionLocal() as db:
        run = db.get(JobRun, run_id)
        if run:
            run.finished_at = datetime.utcnow()
            run.sent = sent; run.skipped = skipped; run.failed = failed
            run.status = "done"
        acc = db.get(DouyinAccount, account_id)
        if acc:
            acc.last_run_at = datetime.utcnow()
        sch = db.query(Schedule).filter(Schedule.douyin_account_id == account_id).first()
        if sch:
            sch.last_result = json.dumps(summary)
        db.add(AuditLog(actor_user_id=user_id, actor_kind="system",
                        action="auto_run", target_type="account",
                        target_id=str(account_id),
                        meta=json.dumps({**summary, "run_id": run_id})))

        # 通知：全失败 / 部分失败 / 全成功
        acc_label = (acc.nickname or acc.label) if acc else f"账户#{account_id}"
        if failed > 0 and sent == 0 and (sent + failed) > 0:
            notify(db, user_id=user_id, kind="send_failed",
                   title=f"⚠️ {acc_label} 自动发送全部失败",
                   content=f"本次任务 {failed} 条全部失败。可能是 cookies 失效或被风控。",
                   url=f"/accounts/{account_id}/runs/{run_id}")
        elif failed > 0:
            notify(db, user_id=user_id, kind="send_failed",
                   title=f"⚠️ {acc_label} 部分发送失败",
                   content=f"送达 {sent}，失败 {failed}。点击查看详情。",
                   url=f"/accounts/{account_id}/runs/{run_id}")

        db.commit()
    return summary
