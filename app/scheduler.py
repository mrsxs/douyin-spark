"""
DB 驱动的后台定时调度：每 30s 扫一遍，到点触发续火花。
- 过期用户（expires_at < now）自动被 SQL 过滤掉
- 禁用用户（is_active=False）同上
- 每个账户一线程，不互相阻塞
- 当日重复触发通过 Schedule.last_ran_date 防御
"""
import time
import threading
import traceback
from datetime import datetime, date
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import Schedule, DouyinAccount, User
from .notify import notify
from . import trigger


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
    from sqlalchemy import or_
    while not _STOP.is_set():
        try:
            now = datetime.now()
            hhmm = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")
            with SessionLocal() as db:
                rows = (db.query(Schedule, DouyinAccount, User)
                          .join(DouyinAccount, Schedule.douyin_account_id == DouyinAccount.id)
                          .join(User, DouyinAccount.user_id == User.id)
                          .filter(Schedule.enabled == True,
                                  Schedule.time_hhmm == hhmm,
                                  User.is_active == True,
                                  User.expires_at > datetime.utcnow(),
                                  DouyinAccount.status == "active")
                          .all())
                for sch, acc, user in rows:
                    if sch.last_ran_date == today:
                        continue
                    # 原子 claim：只有当数据库里 last_ran_date 仍不是 today 才更新成功
                    # 多进程/多 worker 同时扫到同一行时，只有一个 UPDATE 能 rowcount=1
                    updated = (db.query(Schedule)
                                 .filter(Schedule.id == sch.id,
                                         or_(Schedule.last_ran_date.is_(None),
                                             Schedule.last_ran_date != today))
                                 .update({"last_ran_date": today},
                                         synchronize_session=False))
                    db.commit()
                    if updated != 1:
                        # 被别的进程抢到了
                        continue
                    print(f"[scheduler] trigger user={user.id} account={acc.id} time={hhmm}")
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


def start():
    """在 FastAPI startup 时调用，拉起后台线程"""
    global _thread, _health_thread
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
