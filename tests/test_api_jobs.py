"""/api/auto、/api/send-batch、/api/runs/{id}/progress 的 HTTP 契约。

重点是「立即返回」和「越权隔离」——这两条错了分别等于整站卡死和数据泄露。
"""
import time

import pytest

from app import jobs
from app.db import SessionLocal
from app.models import DouyinAccount, JobRun, User


@pytest.fixture(autouse=True)
def _no_real_douyin(monkeypatch):
    """所有用例都不真的连抖音。"""
    monkeypatch.setattr(jobs.trigger, "auto_run",
                        lambda *a, **k: {"sent": 0, "skipped": 0, "failed": 0})
    monkeypatch.setattr(jobs.trigger, "send_batch",
                        lambda *a, **k: {"sent": 0, "failed": 0},
                        raising=False)


def _csrf(client):
    client.get("/login")                       # 让中间件下发 csrf cookie
    return client.cookies.get("csrf", "")


def _post(client, url, payload):
    return client.post(url, json=payload,
                       headers={"X-CSRF-Token": _csrf(client)})


# ── /api/auto ────────────────────────────────────────────────────

def test_auto_returns_run_id_immediately(active_user, login):
    user, acc = active_user
    c = login(user)
    t0 = time.monotonic()
    r = _post(c, "/api/auto", {"account_id": acc.id})
    elapsed = time.monotonic() - t0

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["run_id"], int)
    assert body["started"] is True
    assert elapsed < 2.0, f"/api/auto 阻塞了 {elapsed:.1f}s"


def test_auto_twice_reuses_run(active_user, login, monkeypatch):
    """连点两次不该产生两个任务。"""
    import threading
    release = threading.Event()
    monkeypatch.setattr(jobs.trigger, "auto_run",
                        lambda *a, **k: release.wait(timeout=10))
    user, acc = active_user
    c = login(user)
    first = _post(c, "/api/auto", {"account_id": acc.id}).json()
    second = _post(c, "/api/auto", {"account_id": acc.id}).json()
    release.set()

    assert first["run_id"] == second["run_id"]
    assert first["started"] is True
    assert second["started"] is False


def test_auto_rejects_foreign_account(active_user, login, db):
    """别人的账号不能触发。"""
    user, _ = active_user
    other = User(username="stranger", password_hash="x")
    db.add(other); db.commit(); db.refresh(other)
    foreign = DouyinAccount(user_id=other.id, label="别人的号", status="active")
    db.add(foreign); db.commit(); db.refresh(foreign)

    r = _post(login(user), "/api/auto", {"account_id": foreign.id})
    assert r.status_code == 404


# ── /api/send-batch 输入校验 ─────────────────────────────────────

@pytest.mark.parametrize("payload_extra,expect", [
    ({"uids": [], "text": "hi"}, "联系人"),
    ({"uids": ["1"], "text": ""}, "不能为空"),
    ({"uids": ["1"], "text": "x" * (jobs.MAX_TEXT_LEN + 1)}, "过长"),
    ({"uids": [str(i) for i in range(jobs.MAX_BATCH_UIDS + 1)], "text": "hi"}, "最多"),
])
def test_batch_input_validation(active_user, login, payload_extra, expect):
    user, acc = active_user
    r = _post(login(user), "/api/send-batch",
              {"account_id": acc.id, **payload_extra})
    body = r.json()
    assert body["ok"] is False
    assert expect in body["error"]


def test_batch_returns_immediately(active_user, login):
    user, acc = active_user
    t0 = time.monotonic()
    r = _post(login(user), "/api/send-batch",
              {"account_id": acc.id, "uids": ["a", "b"], "text": "hi"})
    assert r.json()["ok"] is True
    assert time.monotonic() - t0 < 2.0


# ── /api/runs/{id}/progress ──────────────────────────────────────

def test_progress_returns_shape(active_user, login, db):
    user, acc = active_user
    run = JobRun(douyin_account_id=acc.id, kind="auto", triggered_by="user",
                 status="running", total=10, sent=3, failed=1)
    db.add(run); db.commit(); db.refresh(run)

    body = login(user).get(f"/api/runs/{run.id}/progress").json()
    assert body["ok"] is True
    assert body["total"] == 10
    assert body["sent"] == 3
    assert body["failed"] == 1
    assert body["finished"] is False
    assert 0 <= body["percent"] <= 100


def test_progress_hides_other_users_run(active_user, login, db):
    """核心回归：不能拿别人的 run_id 查进度。"""
    user, _ = active_user
    other = User(username="stranger2", password_hash="x")
    db.add(other); db.commit(); db.refresh(other)
    foreign_acc = DouyinAccount(user_id=other.id, label="别人的号", status="active")
    db.add(foreign_acc); db.commit(); db.refresh(foreign_acc)
    foreign_run = JobRun(douyin_account_id=foreign_acc.id, kind="auto",
                         triggered_by="user", status="running")
    db.add(foreign_run); db.commit(); db.refresh(foreign_run)

    r = login(user).get(f"/api/runs/{foreign_run.id}/progress")
    assert r.status_code == 404


def test_progress_unknown_run_404(active_user, login):
    user, _ = active_user
    assert login(user).get("/api/runs/999999/progress").status_code == 404


def test_progress_requires_login(client):
    r = client.get("/api/runs/1/progress")
    assert r.status_code == 401
    assert r.json()["ok"] is False


# ── API 错误必须是 JSON，不能重定向 ──────────────────────────────

@pytest.mark.parametrize("url", ["/api/runs/1/progress", "/api/notifications"])
def test_api_errors_are_json_not_redirect(client, url):
    """轮询进度时 session 过期，前端要能认出 401。

    原先 401 被统一重定向到 /login：fetch 跟过去拿回 HTML，
    r.json() 解析失败，用户只看到「非 JSON 响应」，
    base.html 里那段 401 处理成了永远走不到的死代码。
    """
    r = client.get(url, follow_redirects=False)
    assert r.status_code == 401, f"{url} 返回了 {r.status_code}（重定向？）"
    assert r.headers.get("content-type", "").startswith("application/json")
    assert r.json()["ok"] is False


def test_disabled_user_gets_401_json(client, db, login):
    """被停用的用户调 API 拿到 401 JSON，而不是跳转 /login 的 HTML。"""
    from app.security import hash_password
    u = User(username="banned", password_hash=hash_password("x"),
             max_accounts=1, is_active=False)
    db.add(u); db.commit(); db.refresh(u)

    r = login(u).get("/api/notifications", follow_redirects=False)
    assert r.status_code == 401
    assert r.headers.get("content-type", "").startswith("application/json")
    assert r.json()["ok"] is False


def test_html_pages_still_redirect(client):
    """网页路径保持原有重定向行为，别被 API 改动波及。"""
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_auto_then_progress_end_to_end(active_user, login):
    """点一次续火花 → 拿 run_id → 查进度，全程不阻塞。"""
    user, acc = active_user
    c = login(user)
    run_id = _post(c, "/api/auto", {"account_id": acc.id}).json()["run_id"]

    for _ in range(50):
        body = c.get(f"/api/runs/{run_id}/progress").json()
        if body["finished"]:
            break
        time.sleep(0.05)

    assert body["ok"] is True
    assert body["run_id"] == run_id
    with SessionLocal() as s:
        assert s.get(JobRun, run_id).status in ("done", "error")
