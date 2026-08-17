"""后台任务编排：把耗时的续火花 / 批量发送从 HTTP 请求里挪出去。

Why: trigger.auto_run 对每个联系人 sleep 5s，100 个联系人就是 500 秒。
放在同步请求里必然浏览器超时；更要命的是 FastAPI 的同步 def 跑在 anyio
threadpool（默认 40 线程），几个用户同时点「立即续火花」就能占满线程池，
整站跟着卡死。

不引入 Celery/Redis —— scheduler.py 早就用 threading.Thread 异步触发了，
这里复用同一范式：HTTP 层建好 JobRun 立刻返回 run_id，前端拿它轮询进度。
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timedelta

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from . import trigger
from .db import SessionLocal
from .models import JobRun, JobRunItem

# 批量发送的输入上限：uids 太多会让任务跑几十分钟且更容易触发风控
MAX_BATCH_UIDS = 200
MAX_TEXT_LEN = 500
# 超过这个时长仍是 running 的任务视为卡死，允许重新触发
RUNNING_STALE_MINUTES = 15


def _find_active_run(db: Session, account_id: int,
                     kinds: tuple[str, ...]) -> JobRun | None:
    """该账户是否已有在跑的任务（用于去重，避免用户连点产生多个任务）。"""
    cutoff = datetime.utcnow() - timedelta(minutes=RUNNING_STALE_MINUTES)
    return (db.query(JobRun)
              .filter(JobRun.douyin_account_id == account_id,
                      JobRun.kind.in_(kinds),
                      JobRun.status == "running",
                      JobRun.started_at >= cutoff)
              .order_by(JobRun.started_at.desc())
              .first())


def is_account_busy(account_id: int) -> bool:
    """账户当前是否被某个任务占用（auto / batch 共用同一把账户锁）。"""
    lock = trigger._get_account_lock(account_id)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def _finish_with_error(run_id: int, exc: BaseException) -> None:
    """后台线程挂掉时收尾，避免 JobRun 永远停在 running 卡住后续触发。"""
    _close_run(run_id, "error", f"{type(exc).__name__}: {exc}"[:500])


def _ensure_finished(run_id: int) -> None:
    """任务函数正常返回却没收尾时补一刀。

    Why: JobRun 卡在 running 会被 _find_active_run 当成「还在跑」，
    从此该账户再也触发不了任何任务，直到 15 分钟 stale 窗口过去。
    """
    _close_run(run_id, "done", None)


def _close_run(run_id: int, status: str, error: str | None) -> None:
    """把仍在 running 的任务置为终态。

    用条件 UPDATE 而不是 ORM 读改写：任务跑着时用户可能删掉抖音账号，
    CASCADE 会带走 JobRun，此时 ORM 提交会抛 StaleDataError；
    条件 UPDATE 匹配不到行就是 rowcount=0，本来就该静默跳过。
    """
    values = {"status": status, "finished_at": datetime.utcnow()}
    if error is not None:
        values["error"] = error
    try:
        with SessionLocal() as db:
            db.execute(
                update(JobRun)
                .where(JobRun.id == run_id, JobRun.status == "running")
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            db.commit()
    except Exception:
        traceback.print_exc()


def _spawn(fn, run_id: int, name: str) -> None:
    def runner():
        try:
            fn()
        except Exception as e:
            traceback.print_exc()
            _finish_with_error(run_id, e)
        else:
            _ensure_finished(run_id)

    threading.Thread(target=runner, daemon=True, name=name).start()


def start_auto_run(user_id: int, account_id: int) -> tuple[int, bool]:
    """触发一次续火花。返回 (run_id, 是否新建)。

    已有在跑的任务时不新建，直接返回那个 run_id —— 前端接管同一个进度条。
    """
    with SessionLocal() as db:
        existing = _find_active_run(db, account_id, ("auto",))
        if existing:
            return existing.id, False
        run = JobRun(douyin_account_id=account_id, kind="auto",
                     triggered_by="user", status="running")
        db.add(run); db.commit(); db.refresh(run)
        run_id = run.id

    _spawn(lambda: trigger.auto_run(user_id, account_id,
                                    triggered_by="user", run_id=run_id),
           run_id, f"auto-{account_id}")
    return run_id, True


def start_batch_send(user_id: int, account_id: int, uids: list,
                     text: str) -> tuple[int, bool]:
    """批量发送。返回 (run_id, 是否新建)。输入非法时抛 ValueError。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("消息内容不能为空")
    if len(text) > MAX_TEXT_LEN:
        raise ValueError(f"消息内容过长（上限 {MAX_TEXT_LEN} 字）")

    # 去重但保持用户勾选的顺序 —— 重复 uid 会让同一个人收到多条
    seen, clean = set(), []
    for u in (uids or []):
        s = str(u).strip()
        if s and s not in seen:
            seen.add(s)
            clean.append(s)
    if not clean:
        raise ValueError("请至少选择一个联系人")
    if len(clean) > MAX_BATCH_UIDS:
        raise ValueError(f"一次最多发送 {MAX_BATCH_UIDS} 个联系人")

    with SessionLocal() as db:
        existing = _find_active_run(db, account_id, ("auto", "manual_batch"))
        if existing:
            return existing.id, False
        run = JobRun(douyin_account_id=account_id, kind="manual_batch",
                     triggered_by="user", status="running", total=len(clean))
        db.add(run); db.commit(); db.refresh(run)
        run_id = run.id

    _spawn(lambda: trigger.send_batch(user_id, account_id, clean, text,
                                      run_id=run_id),
           run_id, f"batch-{account_id}")
    return run_id, True


def get_progress(db: Session, run_id: int) -> dict | None:
    """进度快照。done 用 COUNT(JobRunItem) 实时算 —— 明细是逐条提交的。"""
    run = db.get(JobRun, run_id)
    if not run:
        return None
    done = (db.query(func.count(JobRunItem.id))
              .filter(JobRunItem.job_run_id == run_id).scalar()) or 0
    total = run.total or 0
    finished = run.status != "running"
    return {
        "run_id": run.id,
        "account_id": run.douyin_account_id,
        "kind": run.kind,
        "status": run.status,
        "finished": finished,
        "total": total,
        "done": done,
        "sent": run.sent,
        "skipped": run.skipped,
        "failed": run.failed,
        "percent": 100 if finished else (int(done * 100 / total) if total else 0),
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
