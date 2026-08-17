"""scheduler → trigger.auto_run 触发链的兼容性。

auto_run 新增了 run_id 参数（HTTP 层要先建 JobRun 才能立刻返回 id）。
scheduler 是不传该参数的老调用方，签名变更不能把定时任务打坏 ——
这条链断了等于所有客户的自动续火花全停，且不会有人立刻发现。
"""
import inspect
import threading

import pytest

from app import jobs, scheduler, trigger
from app.db import SessionLocal
from app.models import DouyinAccount, JobRun, User


@pytest.fixture
def acc(db):
    u = User(username="sched_user", password_hash="x", max_accounts=3)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="主号", status="active",
                      cookies_exist=True)
    db.add(a); db.commit(); db.refresh(a)
    return a


def test_scheduler_calls_auto_run_positionally(acc, monkeypatch):
    """scheduler._run_one 的调用方式必须仍然有效。"""
    captured = {}

    def fake(user_id, account_id, triggered_by="scheduler", run_id=None):
        captured.update(user_id=user_id, account_id=account_id,
                        triggered_by=triggered_by, run_id=run_id)
        return {"sent": 0, "skipped": 0, "failed": 0}

    monkeypatch.setattr(scheduler.trigger, "auto_run", fake)
    scheduler._run_one(acc.user_id, acc.id)

    assert captured["user_id"] == acc.user_id
    assert captured["account_id"] == acc.id
    assert captured["run_id"] is None, "scheduler 不该传 run_id，应由 trigger 自建"


def test_auto_run_signature_keeps_run_id_optional():
    """run_id 必须是可选参数，否则所有老调用方都会 TypeError。"""
    sig = inspect.signature(trigger.auto_run)
    assert sig.parameters["run_id"].default is None
    assert sig.parameters["triggered_by"].default == "scheduler"


def test_auto_run_creates_own_jobrun_when_no_run_id(acc, monkeypatch):
    """不传 run_id 时（scheduler 路径）trigger 自己建 JobRun 记账。"""
    def fake_ensure(ctx):
        raise trigger.NotReady("cookies 无效")     # 尽早返回，只验证记账
    monkeypatch.setattr(trigger, "_ensure_active", fake_ensure)

    with pytest.raises(trigger.NotReady):
        trigger.auto_run(acc.user_id, acc.id)

    with SessionLocal() as s:
        runs = s.query(JobRun).filter(JobRun.douyin_account_id == acc.id).all()
        assert len(runs) == 1, "scheduler 路径没有记账"
        assert runs[0].status == "error"
        assert runs[0].triggered_by == "scheduler"


def test_http_path_reuses_prebuilt_jobrun(acc, monkeypatch):
    """HTTP 路径传了 run_id 时不能再建一条，否则历史里出现重复任务。"""
    def fake_ensure(ctx):
        raise trigger.NotReady("cookies 无效")
    monkeypatch.setattr(trigger, "_ensure_active", fake_ensure)

    with SessionLocal() as s:
        pre = JobRun(douyin_account_id=acc.id, kind="auto",
                     triggered_by="user", status="running")
        s.add(pre); s.commit(); s.refresh(pre)
        pre_id = pre.id

    with pytest.raises(trigger.NotReady):
        trigger.auto_run(acc.user_id, acc.id, triggered_by="user", run_id=pre_id)

    with SessionLocal() as s:
        runs = s.query(JobRun).filter(JobRun.douyin_account_id == acc.id).all()
        assert len(runs) == 1, f"重复建了 JobRun: {[r.id for r in runs]}"
        assert runs[0].id == pre_id


def test_scheduler_and_http_cannot_run_together(acc, monkeypatch):
    """定时任务正在跑时，用户手动触发不能挤进去同时发。"""
    entered = threading.Event()
    release = threading.Event()

    def blocking_ensure(ctx):
        entered.set()
        release.wait(timeout=10)
        raise trigger.NotReady("done")

    monkeypatch.setattr(trigger, "_ensure_active", blocking_ensure)

    def bg():
        try:
            trigger.auto_run(acc.user_id, acc.id)
        except Exception:
            pass

    t = threading.Thread(target=bg, daemon=True)
    t.start()
    assert entered.wait(timeout=5)

    # scheduler 占着锁 → HTTP 层应识别为忙碌，而不是并发触发
    assert jobs.is_account_busy(acc.id) is True
    with pytest.raises(trigger.AlreadyRunning):
        trigger.auto_run(acc.user_id, acc.id, triggered_by="user")

    release.set()
    t.join(timeout=5)
