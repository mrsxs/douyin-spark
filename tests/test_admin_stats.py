"""管理后台统计与图表。

背景：admin/home.html 的「用户增长趋势」原本是硬编码的
[("周一",46),("周二",62),...]，注释写着「前端用简化的虚拟数据」。
卖给客户的产品里，管理员看到的是编出来的数字 —— 比没有图表更糟。
"""
from datetime import datetime, timedelta

import pytest

from app.routers import admin as admin_mod
from app.models import DouyinAccount, JobRun, LicenseCode, User
from app.security import hash_password


@pytest.fixture
def admin_user(db):
    u = User(username="boss", password_hash=hash_password("x"),
             is_admin=True, expires_at=datetime.utcnow() + timedelta(days=999),
             max_accounts=100)
    db.add(u); db.commit(); db.refresh(u)
    return u


@pytest.fixture
def as_admin(admin_user, login):
    return login(admin_user)


# ── 趋势数据来自真实 SQL ─────────────────────────────────────────

def test_trend_reflects_real_runs(db, admin_user):
    """7 天趋势必须由 JobRun 聚合得到。"""
    acc = DouyinAccount(user_id=admin_user.id, label="a", status="active")
    db.add(acc); db.commit(); db.refresh(acc)

    today = datetime.utcnow()
    for i in range(3):
        db.add(JobRun(douyin_account_id=acc.id, kind="auto",
                      triggered_by="scheduler", status="done",
                      started_at=today, sent=1))
    db.add(JobRun(douyin_account_id=acc.id, kind="auto",
                  triggered_by="scheduler", status="done",
                  started_at=today - timedelta(days=2), sent=1))
    db.commit()

    trend = admin_mod._build_trend(db, days=7)
    assert len(trend) == 7
    assert all({"label", "value", "date"} <= set(p) for p in trend)
    assert trend[-1]["value"] == 3, "今天应统计到 3 次"
    assert trend[-3]["value"] == 1, "两天前应统计到 1 次"
    assert trend[-2]["value"] == 0


def test_trend_empty_db_is_all_zero(db):
    trend = admin_mod._build_trend(db, days=7)
    assert len(trend) == 7
    assert all(p["value"] == 0 for p in trend)


def test_trend_days_are_consecutive_and_end_today(db):
    trend = admin_mod._build_trend(db, days=7)
    dates = [p["date"] for p in trend]
    assert dates == sorted(dates)
    assert dates[-1] == datetime.now().strftime("%Y-%m-%d")
    assert len(set(dates)) == 7


# ── 页面不再包含编造的数据 ───────────────────────────────────────

def test_home_page_has_no_hardcoded_weekday_data(as_admin):
    """核心回归：模板里不能再出现写死的假数据。"""
    html = as_admin.get("/admin").text
    # 原来那组硬编码值
    for fake in ("周一", "周二", "周三", "周四", "周五", "周六", "周日"):
        assert f'"{fake}", 4' not in html
    assert "虚拟数据" not in html


def test_home_page_renders_with_real_data(db, admin_user, as_admin):
    acc = DouyinAccount(user_id=admin_user.id, label="a", status="active")
    db.add(acc); db.commit(); db.refresh(acc)
    db.add(JobRun(douyin_account_id=acc.id, kind="auto", triggered_by="scheduler",
                  status="done", started_at=datetime.utcnow(), sent=5))
    db.add(LicenseCode(code="ABCDEFGH12345678", duration_days=30, max_accounts=2))
    db.commit()

    r = as_admin.get("/admin")
    assert r.status_code == 200
    assert "最近 7 天" in r.text


def test_home_requires_admin(db, login):
    """普通用户进不了后台。"""
    u = User(username="plain", password_hash=hash_password("x"),
             expires_at=datetime.utcnow() + timedelta(days=30))
    db.add(u); db.commit(); db.refresh(u)
    assert login(u).get("/admin").status_code == 403


# ── stats 数字本身 ───────────────────────────────────────────────

def test_stats_counts_are_real(db, admin_user, as_admin):
    db.add(User(username="u2", password_hash="x",
                expires_at=datetime.utcnow() + timedelta(days=10)))
    db.add(User(username="u3", password_hash="x",
                expires_at=datetime.utcnow() - timedelta(days=1)))   # 已过期
    db.add(LicenseCode(code="UNUSEDCODE123456", duration_days=30, max_accounts=1))
    db.add(LicenseCode(code="USEDCODE12345678", duration_days=30, max_accounts=1,
                       used_by=admin_user.id, used_at=datetime.utcnow()))
    db.commit()

    stats = admin_mod._build_stats(db)
    assert stats["users_total"] == 3          # boss + u2 + u3
    assert stats["users_active"] == 2         # boss + u2
    assert stats["codes_total"] == 2
    assert stats["codes_unused"] == 1
