"""联系人冷备与首屏秒开。

背景：/accounts/{id} 原本在请求里同步调 trigger.get_contacts()（走抖音 API，
15s timeout），再叠一次 _backfill_avatars_for_page 的网络请求 —— 首屏白屏 5~20 秒，
期间还占着 FastAPI 的同步 threadpool。

改成：先渲染 Contact 冷备表（纯 DB，毫秒级），前端再异步拉最新数据覆盖。
前提是冷备表得存得下渲染所需的全部字段（原来缺 avatar 和 status）。
"""
from datetime import datetime, timedelta

import pytest

from app import contacts_service as cs
from app.models import Contact, DouyinAccount, User


@pytest.fixture
def acc(db):
    u = User(username="cuser", password_hash="x", max_accounts=3)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="主号", status="active",
                      cookies_exist=True)
    db.add(a); db.commit(); db.refresh(a)
    return a


SAMPLE = [
    {"uid": "111", "nickname": "小明", "conv_id": "c1", "days": 30,
     "avatar": "https://x/1.jpg", "status": "active"},
    {"uid": "222", "nickname": "小红", "conv_id": "c2", "days": 5,
     "avatar": "", "status": "broken"},
]


# ── 冷备表要能存下渲染所需的全部字段 ─────────────────────────────

def test_contact_persists_avatar_and_status(db, acc):
    """原来 Contact 没有 avatar/status，导致冷备渲染不出头像和「需重燃」。"""
    cs.upsert_cache(db, acc.id, SAMPLE)
    db.commit()

    rows = {c.uid: c for c in db.query(Contact).filter(
        Contact.douyin_account_id == acc.id).all()}
    assert rows["111"].avatar == "https://x/1.jpg"
    assert rows["111"].status == "active"
    assert rows["222"].status == "broken"
    assert rows["111"].days == 30


def test_upsert_is_idempotent(db, acc):
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()
    assert db.query(Contact).filter(
        Contact.douyin_account_id == acc.id).count() == 2


def test_upsert_updates_changed_fields(db, acc):
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()
    updated = [{**SAMPLE[0], "days": 31, "nickname": "小明改名了"}]
    cs.upsert_cache(db, acc.id, updated); db.commit()

    row = db.query(Contact).filter(Contact.uid == "111").first()
    assert row.days == 31
    assert row.nickname == "小明改名了"


def test_upsert_keeps_existing_avatar_when_new_is_blank(db, acc):
    """刷新时偶尔拿不到头像，不能把已有的抹掉。"""
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()
    cs.upsert_cache(db, acc.id, [{**SAMPLE[0], "avatar": ""}]); db.commit()
    row = db.query(Contact).filter(Contact.uid == "111").first()
    assert row.avatar == "https://x/1.jpg"


def test_uid_fallback_nickname_does_not_overwrite_real_name(db, acc):
    """抓不到昵称时上游会回退成 uid，不能拿它覆盖已存的真名。

    真实踩到的：联系人「27」刷新后变成了 1234567890123456。
    """
    cs.upsert_cache(db, acc.id, [
        {"uid": "1234567890123456", "nickname": "27", "days": 342,
         "status": "active", "conv_id": "c"},
    ]); db.commit()

    # 这次没抓到昵称 → 上游把 nickname 填成了 uid
    cs.upsert_cache(db, acc.id, [
        {"uid": "1234567890123456", "nickname": "1234567890123456",
         "days": 343, "status": "active", "conv_id": "c"},
    ]); db.commit()

    row = db.query(Contact).filter(Contact.uid == "1234567890123456").first()
    assert row.nickname == "27", "真名被 uid 覆盖了"
    assert row.days == 343, "天数应该照常更新"


def test_uid_used_when_no_name_ever_known(db, acc):
    """从来没拿到过昵称时，用 uid 兜底总比空白强。"""
    cs.upsert_cache(db, acc.id, [
        {"uid": "999", "nickname": "999", "days": 1,
         "status": "active", "conv_id": "c"},
    ]); db.commit()
    out = {c["uid"]: c for c in cs.load_cached(db, acc.id)}
    assert out["999"]["nickname"] == "999"


def test_vanished_contacts_are_removed(db, acc):
    """核心回归：抖音那边已经没有的联系人不能继续留在列表里。

    真实踩到的：用户看到早已消失的好友还挂在页面上 ——
    upsert 只增不删，旧行一直留着。
    """
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()
    assert db.query(Contact).filter(Contact.douyin_account_id == acc.id).count() == 2

    # 这次抖音只回了 111，222 已经不在了
    cs.upsert_cache(db, acc.id, [SAMPLE[0]], prune=True); db.commit()

    uids = {c.uid for c in db.query(Contact).filter(
        Contact.douyin_account_id == acc.id).all()}
    assert uids == {"111"}, f"消失的联系人没被清理: {uids}"


def test_prune_off_by_default(db, acc):
    """默认不删 —— 部分刷新（只拿到一页）时不能误删其它人。"""
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()
    cs.upsert_cache(db, acc.id, [SAMPLE[0]]); db.commit()
    assert db.query(Contact).filter(
        Contact.douyin_account_id == acc.id).count() == 2


def test_prune_never_touches_other_accounts(db, acc, make_user):
    """清理只能影响本账号。"""
    other = DouyinAccount(user_id=acc.user_id, label="小号", status="active")
    db.add(other); db.commit(); db.refresh(other)
    cs.upsert_cache(db, other.id, SAMPLE); db.commit()

    cs.upsert_cache(db, acc.id, [SAMPLE[0]], prune=True); db.commit()
    assert db.query(Contact).filter(
        Contact.douyin_account_id == other.id).count() == 2


def test_prune_with_empty_list_is_noop(db, acc):
    """抖音接口异常返回空列表时，不能把用户的联系人全删了。"""
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()
    cs.upsert_cache(db, acc.id, [], prune=True); db.commit()
    assert db.query(Contact).filter(
        Contact.douyin_account_id == acc.id).count() == 2


# ── 读缓存 ───────────────────────────────────────────────────────

def test_load_cached_returns_render_ready_dicts(db, acc):
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()
    out = cs.load_cached(db, acc.id)
    assert len(out) == 2
    first = out[0]
    for key in ("uid", "nickname", "days", "avatar", "status", "conv_id"):
        assert key in first, f"缺字段 {key}，模板会渲染不出来"


def test_load_cached_sorts_active_first(db, acc):
    """和抖音接口一致：还在燃烧的排前面。"""
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()
    out = cs.load_cached(db, acc.id)
    assert out[0]["status"] == "active"
    assert out[-1]["status"] == "broken"


def test_load_cached_empty_account(db, acc):
    assert cs.load_cached(db, acc.id) == []


# ── 新鲜度 ───────────────────────────────────────────────────────

def test_last_synced_reports_none_when_empty(db, acc):
    assert cs.last_synced_at(db, acc.id) is None


def test_last_synced_reports_time(db, acc):
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()
    ts = cs.last_synced_at(db, acc.id)
    assert ts is not None
    assert (datetime.utcnow() - ts) < timedelta(seconds=10)


# ── 页面不再阻塞 ─────────────────────────────────────────────────

def test_account_page_does_not_call_douyin(db, acc, login, monkeypatch):
    """核心回归：首屏渲染不能碰网络。"""
    from datetime import timedelta as td
    from app import trigger

    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("首屏不该调用抖音 API")

    monkeypatch.setattr(trigger, "get_contacts", boom)
    monkeypatch.setattr(trigger, "_ensure_active", boom)

    u = db.get(User, acc.user_id)
    u.expires_at = datetime.utcnow() + td(days=30)
    db.commit()

    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()

    r = login(u).get(f"/accounts/{acc.id}")
    assert r.status_code == 200
    assert called["n"] == 0
    assert "小明" in r.text, "冷备数据没被渲染出来"
