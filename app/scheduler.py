"""
DB 驱动的后台定时调度：每 30s 扫一遍，到点触发续火花。
- 过期用户（expires_at < now）自动被 SQL 过滤掉
- 禁用用户（is_active=False）同上
- 每个账户一线程，不互相阻塞
- 重复/并发触发通过 JobRun 状态判断（不再依赖 last_ran_date，该字段仅用于 UI 展示）
- 错过整点那一分钟也能补跑（time_hhmm <= 当前时间且今日未 done）
"""
import time
import threading
import traceback
from datetime import datetime, date, timedelta
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import Schedule, DouyinAccount, User, JobRun
from .notify import notify
from . import trigger


# 每日失败重试上限：避免 auto_run 持续失败时死循环触发
MAX_DAILY_RETRIES = 3
# 判断"正在跑"的窗口：超过此时长的 running 视为卡死，不再阻塞新触发
RUNNING_STALE_MINUTES = 15
# 失败后退避：上次 error 在此窗口内则暂不重试（避免持续击打风控）
RETRY_BACKOFF_MINUTES = 5


_STOP = threading.Event()


def _run_one(user_id: int, account_id: int):
    try:
        trigger.auto_run(user_id, account_id)
    except trigger.NotReady as e:
        # cookies 失效 / init_req 丢失：发站内通知（+ 邮件）提醒用户重登
        traceback.print_exc()
        try:
            with SessionLocal() as db:
                acc = db.get(DouyinAccount, account_id)
                label = (acc.nickname or acc.label) if acc else f"账户#{account_id}"
                notify(db, user_id=user_id, kind="cookies_expired",
                       title=f"🔴 {label} 账号失效，需要重新登录",
                       content=f"定时任务未能触发：{e}",
                       url=f"/accounts/{account_id}/login")
                # 同时更新账户状态
                if acc and acc.status == "active":
                    acc.status = "cookies_expired"
                db.commit()
        except Exception:
            traceback.print_exc()
    except Exception:
        traceback.print_exc()


def _loop():
    from sqlalchemy import or_, and_
    while not _STOP.is_set():
        try:
            now = datetime.now()                       # 容器本地时间（TZ=Shanghai）
            hhmm = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")
            # JobRun.started_at 存 UTC，用"最近 24 小时"作为"今日"的保守窗口
            since_utc = datetime.utcnow() - timedelta(hours=24)
            stale_cutoff_utc = datetime.utcnow() - timedelta(minutes=RUNNING_STALE_MINUTES)

            with SessionLocal() as db:
                # time_hhmm <= 当前时间 → 允许错过那一分钟后补跑（直到跨日）
                rows = (db.query(Schedule, DouyinAccount, User)
                          .join(DouyinAccount, Schedule.douyin_account_id == DouyinAccount.id)
                          .join(User, DouyinAccount.user_id == User.id)
                          .filter(Schedule.enabled == True,
                                  Schedule.time_hhmm <= hhmm,
                                  User.is_active == True,
                                  User.expires_at > datetime.utcnow(),
                                  DouyinAccount.status == "active")
                          .all())
                for sch, acc, user in rows:
                    # ① 今天已经成功 done 过 → 跳过
                    done_today = (db.query(JobRun.id)
                                    .filter(JobRun.douyin_account_id == acc.id,
                                            JobRun.kind == "auto",
                                            JobRun.status == "done",
                                            JobRun.started_at >= since_utc)
                                    .first())
                    if done_today:
                        continue
                    # ② 还有活跃的 running（未超过 stale 阈值）→ 跳过，等它结束
                    active_running = (db.query(JobRun.id)
                                        .filter(JobRun.douyin_account_id == acc.id,
                                                JobRun.kind == "auto",
                                                JobRun.status == "running",
                                                JobRun.started_at >= stale_cutoff_utc)
                                        .first())
                    if active_running:
                        continue
                    # ③ 今日已失败次数超过上限 → 跳过，避免死循环
                    error_today = (db.query(JobRun.id)
                                     .filter(JobRun.douyin_account_id == acc.id,
                                             JobRun.kind == "auto",
                                             JobRun.status == "error",
                                             JobRun.started_at >= since_utc)
                                     .count())
                    if error_today >= MAX_DAILY_RETRIES:
                        continue
                    # ④ 失败后退避：最近一次 error 在 RETRY_BACKOFF_MINUTES 分钟内则暂不重试
                    if error_today > 0:
                        backoff_cutoff_utc = datetime.utcnow() - timedelta(minutes=RETRY_BACKOFF_MINUTES)
                        recent_error = (db.query(JobRun.started_at)
                                          .filter(JobRun.douyin_account_id == acc.id,
                                                  JobRun.kind == "auto",
                                                  JobRun.status == "error",
                                                  JobRun.started_at >= backoff_cutoff_utc)
                                          .first())
                        if recent_error:
                            continue
                    # ⑤ 保留 last_ran_date 更新仅作 UI 展示，不再做触发判断
                    sch.last_ran_date = today
                    db.commit()
                    retry_hint = f" retry={error_today}" if error_today else ""
                    print(f"[scheduler] trigger user={user.id} account={acc.id} "
                          f"sched_time={sch.time_hhmm} now={hhmm}{retry_hint}")
                    threading.Thread(
                        target=_run_one,
                        args=(user.id, acc.id),
                        daemon=True,
                    ).start()
        except Exception as e:
            print(f"[scheduler] loop error: {e}")
            traceback.print_exc()
        # 30s 分钟级精度；分钟级别的扫描足够
        _STOP.wait(30)


# ── cookies 健康检查：每天 03:00 跑一次，真正 ping 抖音验证 sessionid ──

_HEALTH_STOP = threading.Event()


def _cookies_health_check(account_id: int, max_retries: int = 3) -> str:
    """对单个账户发一个轻量请求验证 cookies 是否还有效。
    带指数退避重试（2s → 4s → 8s），避免网络抖动误判为 cookies 失效。
    返回状态字符串。
    """
    from .storage import AccountCtx, set_account_ctx
    with SessionLocal() as db:
        acc = db.get(DouyinAccount, account_id)
        if not acc or acc.status != "active":
            return "skip"
        user_id = acc.user_id

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            set_account_ctx(AccountCtx(user_id, account_id))
            trigger._ensure_active(AccountCtx(user_id, account_id))
            return "ok"
        except trigger.NotReady as e:
            last_exc = e
            if attempt < max_retries - 1:
                _HEALTH_STOP.wait(2 ** (attempt + 1))  # 2s → 4s → 8s
                if _HEALTH_STOP.is_set():
                    return "cancelled"
                continue
            break
        except Exception as e:
            # 网络类异常（连不上抖音）也重试
            last_exc = e
            traceback.print_exc()
            if attempt < max_retries - 1:
                _HEALTH_STOP.wait(2 ** (attempt + 1))
                if _HEALTH_STOP.is_set():
                    return "cancelled"
                continue
            return "error"

    # 连续 max_retries 次 NotReady → 真失效，标记 + 通知
    try:
        with SessionLocal() as db:
            acc = db.get(DouyinAccount, account_id)
            if acc:
                acc.status = "cookies_expired"
                notify(db, user_id=acc.user_id, kind="cookies_expired",
                       title=f"🔴 {acc.nickname or acc.label} 登录态已失效",
                       content=f"每日体检发现 cookies 失效（连续 {max_retries} 次：{last_exc}），请尽快重新扫码登录。",
                       url=f"/accounts/{account_id}/login")
                db.commit()
    except Exception:
        traceback.print_exc()
    return "expired"


def _health_loop():
    """每天 03:00 跑一次 cookies 体检；进程内用日期字符串防重复"""
    last_ran = ""
    while not _HEALTH_STOP.is_set():
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if now.hour == 3 and last_ran != today:
                last_ran = today
                print(f"[health] cookies 体检开始 {today}")
                with SessionLocal() as db:
                    active_ids = [a.id for a in db.query(DouyinAccount).filter(
                        DouyinAccount.status == "active",
                        DouyinAccount.cookies_exist == True,
                    ).all()]
                for aid in active_ids:
                    _cookies_health_check(aid)
                    _HEALTH_STOP.wait(3)   # 账户间隔 3s 避免瞬间并发
                print(f"[health] 体检完成，{len(active_ids)} 个账户")
        except Exception as e:
            print(f"[health] loop error: {e}")
            traceback.print_exc()
        _HEALTH_STOP.wait(60)   # 每分钟检查一次是否到点


_thread: threading.Thread | None = None
_health_thread: threading.Thread | None = None


def _cleanup_stale_runs():
    """启动时回收上次进程崩溃遗留的 running JobRun，避免永久阻塞触发"""
    try:
        with SessionLocal() as db:
            n = (db.query(JobRun)
                   .filter(JobRun.status == "running")
                   .update({"status": "error",
                            "finished_at": datetime.utcnow(),
                            "error": "process-restart-cleanup"},
                           synchronize_session=False))
            db.commit()
            if n:
                print(f"[scheduler] 清理遗留 running 任务 {n} 条")
    except Exception as e:
        print(f"[scheduler] 启动清理失败: {e}")


def start():
    """在 FastAPI startup 时调用，拉起后台线程"""
    global _thread, _health_thread
    _cleanup_stale_runs()
    if not (_thread and _thread.is_alive()):
        _thread = threading.Thread(target=_loop, daemon=True, name="scheduler")
        _thread.start()
        print("[scheduler] 启动（每 30s 扫 schedules 表）")
    if not (_health_thread and _health_thread.is_alive()):
        _health_thread = threading.Thread(target=_health_loop, daemon=True, name="health")
        _health_thread.start()
        print("[scheduler] cookies 健康检查已启动（每日 03:00）")


def stop():
    _STOP.set()
    _HEALTH_STOP.set()
