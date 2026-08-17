"""后台任务：启动、进度、并发去重。

背景：/api/auto 原本同步调 trigger.auto_run()，而它对每个联系人 sleep 5s。
100 个联系人 = 500 秒的 HTTP 请求 —— 浏览器必然超时；更糟的是 FastAPI 的
同步 def 跑在 anyio threadpool（默认 40 线程），几个用户同时点就能打满线程池
把整站拖死。scheduler.py 早就用 threading.Thread 异步触发了，这里复用同一范式。
"""
import threading
import time

import pytest

from app import jobs, trigger
from app.db import SessionLocal
from app.models import DouyinAccount, JobRun, JobRunItem, User


@pytest.fixture
def acc(db):
    u = User(username="jobs_user", password_hash="x", max_accounts=5)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="主号", status="active",
                      cookies_exist=True)
    db.add(a); db.commit(); db.refresh(a)
    return a


# ── JobRun.total 迁移 ────────────────────────────────────────────

def test_jobrun_has_total_column(db, acc):
    """进度百分比需要分母。"""
    r = JobRun(douyin_account_id=acc.id, kind="auto",
               triggered_by="user", status="running", total=42)
    db.add(r); db.commit(); db.refresh(r)
    assert r.total == 42


def test_total_defaults_to_zero(db, acc):
    r = JobRun(douyin_account_id=acc.id, kind="auto",
               triggered_by="user", status="running")
    db.add(r); db.commit(); db.refresh(r)
    assert r.total == 0


# ── 启动即返回，不阻塞 ───────────────────────────────────────────

def test_start_auto_run_returns_immediately(acc, monkeypatch):
    """核心回归：即使任务要跑很久，start 也必须立刻返回。"""
    started = threading.Event()
    release = threading.Event()

    def slow_auto_run(user_id, account_id, triggered_by="scheduler", run_id=None):
        started.set()
        release.wait(timeout=10)      # 模拟长时间发送
        return {"sent": 1, "skipped": 0, "failed": 0}

    monkeypatch.setattr(jobs.trigger, "auto_run", slow_auto_run)

    t0 = time.monotonic()
    run_id, is_new = jobs.start_auto_run(acc.user_id, acc.id)
    elapsed = time.monotonic() - t0

    assert is_new is True
    assert isinstance(run_id, int)
    assert elapsed < 1.0, f"start_auto_run 阻塞了 {elapsed:.1f}s"
    assert started.wait(timeout=5), "后台线程没跑起来"
    release.set()


def test_run_row_exists_before_returning(acc, monkeypatch):
    """返回 run_id 时该行必须已落库，否则前端拿去查进度会 404。"""
    monkeypatch.setattr(jobs.trigger, "auto_run",
                        lambda *a, **k: {"sent": 0, "skipped": 0, "failed": 0})
    run_id, _ = jobs.start_auto_run(acc.user_id, acc.id)
    with SessionLocal() as s:
        assert s.get(JobRun, run_id) is not None


# ── 重复触发去重 ─────────────────────────────────────────────────

def test_second_start_returns_same_run(acc, monkeypatch):
    """用户连点两次不能产生两个任务，应接管同一个进度。"""
    release = threading.Event()

    def slow(user_id, account_id, triggered_by="scheduler", run_id=None):
        release.wait(timeout=10)
        return {"sent": 0, "skipped": 0, "failed": 0}

    monkeypatch.setattr(jobs.trigger, "auto_run", slow)

    first, new1 = jobs.start_auto_run(acc.user_id, acc.id)
    second, new2 = jobs.start_auto_run(acc.user_id, acc.id)

    assert new1 is True and new2 is False
    assert first == second
    release.set()

    with SessionLocal() as s:
        running = s.query(JobRun).filter(
            JobRun.douyin_account_id == acc.id,
            JobRun.kind == "auto").count()
        assert running == 1, "重复触发产生了多余的 JobRun"


def test_failed_thread_marks_run_error(acc, monkeypatch):
    """后台线程抛异常时不能把 JobRun 永远留在 running。"""
    def boom(user_id, account_id, triggered_by="scheduler", run_id=None):
        raise RuntimeError("cookies 失效")

    monkeypatch.setattr(jobs.trigger, "auto_run", boom)
    run_id, _ = jobs.start_auto_run(acc.user_id, acc.id)

    for _ in range(50):
        with SessionLocal() as s:
            r = s.get(JobRun, run_id)
            if r.status != "running":
                break
        time.sleep(0.05)

    with SessionLocal() as s:
        r = s.get(JobRun, run_id)
        assert r.status == "error"
        assert r.finished_at is not None
        assert "cookies" in (r.error or "")


# ── 进度查询 ─────────────────────────────────────────────────────

def test_progress_counts_items(db, acc):
    r = JobRun(douyin_account_id=acc.id, kind="auto", triggered_by="user",
               status="running", total=5, sent=2, failed=1)
    db.add(r); db.commit(); db.refresh(r)
    for i in range(3):
        db.add(JobRunItem(job_run_id=r.id, uid=f"u{i}", ok=(i < 2)))
    db.commit()

    p = jobs.get_progress(db, r.id)
    assert p["total"] == 5
    assert p["done"] == 3
    assert p["sent"] == 2
    assert p["failed"] == 1
    assert p["status"] == "running"
    assert p["finished"] is False


def test_progress_marks_finished(db, acc):
    r = JobRun(douyin_account_id=acc.id, kind="auto", triggered_by="user",
               status="done", total=2, sent=2)
    db.add(r); db.commit(); db.refresh(r)
    p = jobs.get_progress(db, r.id)
    assert p["finished"] is True


def test_progress_missing_run_returns_none(db):
    assert jobs.get_progress(db, 999999) is None


def test_closing_deleted_run_is_silent(acc, capsys):
    """任务跑着时用户删掉账号（CASCADE 带走 JobRun），收尾不该炸。"""
    with SessionLocal() as s:
        r = JobRun(douyin_account_id=acc.id, kind="auto",
                   triggered_by="user", status="running")
        s.add(r); s.commit(); s.refresh(r)
        run_id = r.id
        s.delete(r); s.commit()

    jobs._ensure_finished(run_id)          # 不抛
    jobs._finish_with_error(run_id, RuntimeError("x"))
    assert "StaleDataError" not in capsys.readouterr().err


def test_close_run_only_touches_running(acc):
    """已经是终态的任务不能被兜底逻辑覆盖掉结果。"""
    with SessionLocal() as s:
        r = JobRun(douyin_account_id=acc.id, kind="auto", triggered_by="user",
                   status="error", error="真实失败原因", sent=3)
        s.add(r); s.commit(); s.refresh(r)
        run_id = r.id

    jobs._ensure_finished(run_id)
    with SessionLocal() as s:
        r = s.get(JobRun, run_id)
        assert r.status == "error"
        assert r.error == "真实失败原因"


# ── 批量发送的输入校验 ───────────────────────────────────────────

def test_batch_rejects_empty_and_oversized(acc):
    with pytest.raises(ValueError):
        jobs.start_batch_send(acc.user_id, acc.id, [], "hi")
    with pytest.raises(ValueError):
        jobs.start_batch_send(acc.user_id, acc.id, ["1"], "")
    with pytest.raises(ValueError):
        jobs.start_batch_send(acc.user_id, acc.id,
                              [str(i) for i in range(jobs.MAX_BATCH_UIDS + 1)], "hi")
    with pytest.raises(ValueError):
        jobs.start_batch_send(acc.user_id, acc.id, ["1"],
                              "x" * (jobs.MAX_TEXT_LEN + 1))


def test_batch_dedupes_uids(acc, monkeypatch):
    seen = {}

    def fake(user_id, account_id, uids, text, run_id=None):
        seen["uids"] = uids
        return {"sent": 0, "failed": 0}

    monkeypatch.setattr(jobs.trigger, "send_batch", fake, raising=False)
    run_id, _ = jobs.start_batch_send(acc.user_id, acc.id,
                                      ["a", "b", "a", "b", "c"], "hi")
    for _ in range(50):
        if "uids" in seen:
            break
        time.sleep(0.05)
    assert seen["uids"] == ["a", "b", "c"], "重复 uid 会对同一个人多发一次"


def test_batch_and_auto_share_account_lock(acc):
    """批量发送和定时任务打同一个抖音号，必须互斥，否则发送频率翻倍踩风控。

    直接验证契约（占住账户锁后两个入口都得拒绝），不 mock ——
    mock 掉 auto_run 就等于把加锁逻辑一起 mock 没了，测了个寂寞。
    """
    lock = trigger._get_account_lock(acc.id)
    assert lock.acquire(blocking=False)
    try:
        assert jobs.is_account_busy(acc.id) is True
        with pytest.raises(trigger.AlreadyRunning):
            trigger.auto_run(acc.user_id, acc.id)
        with pytest.raises(trigger.AlreadyRunning):
            trigger.send_batch(acc.user_id, acc.id, ["a"], "hi")
    finally:
        lock.release()
    assert jobs.is_account_busy(acc.id) is False
