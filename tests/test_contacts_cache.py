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

    注意 init 只回最近活跃的会话，所以要过了宽限期才删（见下面
    test_prune_keeps_recently_synced_contact）。
    """
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()
    assert db.query(Contact).filter(Contact.douyin_account_id == acc.id).count() == 2

    # 222 已经很久没被同步到了 —— 这次抖音也没回它，可以确认真没了
    gone = db.query(Contact).filter(Contact.uid == "222").first()
    gone.last_synced_at = datetime.utcnow() - timedelta(days=cs.PRUNE_GRACE_DAYS + 1)
    db.commit()

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


# ── 无火花好友（status="none"）────────────────────────────────────

NO_SPARK = {"uid": "333", "nickname": "普通好友", "conv_id": "c3", "days": 0,
            "avatar": "", "status": "none"}


def test_none_status_is_persisted(db, acc):
    """include_all 解析出来的无火花好友要能存进冷备。"""
    cs.upsert_cache(db, acc.id, [NO_SPARK]); db.commit()
    row = db.query(Contact).filter(Contact.uid == "333").first()
    assert row.status == "none"
    assert row.days == 0


def test_none_never_downgrades_a_burning_contact(db, acc):
    """核心保护：解析抖动把有火花的人认成 none 时，不能覆盖库里的真实火花。

    解析是拿正则打 protobuf 的，窗口一偏就会漏掉 consecutive_chat ——
    历史上「甜豆包」「Momo」就因为窗口没上界串过天数。真覆盖下去，
    用户看到的是 342 天一夜变 0 天。
    """
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()   # 111 active/30 天

    cs.upsert_cache(db, acc.id, [
        {"uid": "111", "nickname": "小明", "conv_id": "c1",
         "days": 0, "avatar": "", "status": "none"},
    ]); db.commit()

    row = db.query(Contact).filter(Contact.uid == "111").first()
    assert row.status == "active", "有火花的人被降级成了无火花"
    assert row.days == 30, "火花天数被 0 覆盖了"


def test_none_does_not_downgrade_broken_either(db, acc):
    """broken 也是「有过火花」，同样不能被 none 抹掉 —— 那是要去 App 重燃的人。"""
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()   # 222 broken/5 天
    cs.upsert_cache(db, acc.id, [
        {"uid": "222", "nickname": "小红", "conv_id": "c2",
         "days": 0, "avatar": "", "status": "none"},
    ]); db.commit()

    row = db.query(Contact).filter(Contact.uid == "222").first()
    assert row.status == "broken"
    assert row.days == 5


def test_none_to_active_upgrade_still_works(db, acc):
    """反向升级要照常：无火花的人今天续上了，得如实更新。"""
    cs.upsert_cache(db, acc.id, [NO_SPARK]); db.commit()
    cs.upsert_cache(db, acc.id, [
        {**NO_SPARK, "days": 1, "status": "active"},
    ]); db.commit()

    row = db.query(Contact).filter(Contact.uid == "333").first()
    assert row.status == "active"
    assert row.days == 1


def test_load_cached_sorts_none_last(db, acc):
    """段控顺序：续火花 → 需重燃 → 无火花。"""
    cs.upsert_cache(db, acc.id, [*SAMPLE, NO_SPARK]); db.commit()
    out = cs.load_cached(db, acc.id)
    assert [c["status"] for c in out] == ["active", "broken", "none"]


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


# ── conversation_short_id（拉历史消息要用）─────────────────────────

def test_conv_short_id_is_persisted(db, acc):
    """parse_fire_streaks 给了 conversation_short_id，得存下来 ——
    cmd=301 拉历史消息必须带它，不存就得每次重打 1.5MB 的 init 去要。"""
    cs.upsert_cache(db, acc.id, [
        {"uid": "111", "nickname": "小明", "conv_id": "0:1:9:8", "days": 3,
         "status": "active", "conversation_short_id": 7610461610200629770},
    ]); db.commit()

    row = db.query(Contact).filter(Contact.uid == "111").first()
    assert row.conv_short_id == 7610461610200629770


def test_conv_short_id_survives_refresh_without_it(db, acc):
    """某次解析没带出 short_id 时，不能把已存的抹成 0 —— 那条会话就再也拉不了历史。"""
    cs.upsert_cache(db, acc.id, [
        {"uid": "111", "conv_id": "0:1:9:8", "days": 3, "status": "active",
         "conversation_short_id": 555},
    ]); db.commit()
    cs.upsert_cache(db, acc.id, [
        {"uid": "111", "conv_id": "0:1:9:8", "days": 4, "status": "active"},
    ]); db.commit()

    row = db.query(Contact).filter(Contact.uid == "111").first()
    assert row.conv_short_id == 555


def test_load_cached_exposes_conv_short_id(db, acc):
    cs.upsert_cache(db, acc.id, [
        {"uid": "111", "conv_id": "0:1:9:8", "days": 3, "status": "active",
         "conversation_short_id": 777},
    ]); db.commit()
    out = {c["uid"]: c for c in cs.load_cached(db, acc.id)}
    assert out["111"]["conversation_short_id"] == 777


# ── prune 宽限期 ─────────────────────────────────────────────────

def test_prune_keeps_recently_synced_contact(db, acc):
    """核心回归：init 只回最近活跃的会话，一次没回不代表好友没了。

    真实事故：账号 3 的「王女士 906 天」「顺风吖 905 天」「团团 327 天」
    最近没互动，掉出 init 返回范围，prune 直接把三条最久的火花删了。
    """
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()

    # 这次抖音只回了 111 —— 222 刚同步过，不该动它
    cs.upsert_cache(db, acc.id, [SAMPLE[0]], prune=True); db.commit()

    uids = {c.uid for c in db.query(Contact).filter(
        Contact.douyin_account_id == acc.id).all()}
    assert uids == {"111", "222"}, "一次没回就被删了"


def test_prune_removes_long_missing_contact(db, acc):
    """真删好友/注销的还是要清掉 —— 超过宽限期没同步到才删。"""
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()

    stale = db.query(Contact).filter(Contact.uid == "222").first()
    stale.last_synced_at = datetime.utcnow() - timedelta(days=8)
    db.commit()

    cs.upsert_cache(db, acc.id, [SAMPLE[0]], prune=True); db.commit()

    uids = {c.uid for c in db.query(Contact).filter(
        Contact.douyin_account_id == acc.id).all()}
    assert uids == {"111"}, "超过宽限期的没被清理"


def test_prune_grace_boundary(db, acc):
    """刚好卡在宽限期内的要留着。"""
    cs.upsert_cache(db, acc.id, SAMPLE); db.commit()
    row = db.query(Contact).filter(Contact.uid == "222").first()
    row.last_synced_at = datetime.utcnow() - timedelta(days=6)
    db.commit()

    cs.upsert_cache(db, acc.id, [SAMPLE[0]], prune=True); db.commit()
    assert db.query(Contact).filter(Contact.uid == "222").first() is not None


# ── 重燃中（recovering）落库与展示 ────────────────────────────────

RECOVERING = {"uid": "444", "nickname": "慢慢", "conv_id": "c4", "days": 543,
              "avatar": "", "status": "recovering",
              "recover_days": 2, "recover_need_days": 3}


def test_recovering_progress_is_persisted(db, acc):
    """重燃进度要存下来 —— 用户得知道还差几天才能把 543 天救回来。"""
    cs.upsert_cache(db, acc.id, [RECOVERING]); db.commit()
    row = db.query(Contact).filter(Contact.uid == "444").first()
    assert row.status == "recovering"
    assert row.days == 543
    assert (row.recover_days, row.recover_need_days) == (2, 3)


def test_load_cached_exposes_recover_progress(db, acc):
    cs.upsert_cache(db, acc.id, [RECOVERING]); db.commit()
    out = {c["uid"]: c for c in cs.load_cached(db, acc.id)}
    assert out["444"]["recover_days"] == 2
    assert out["444"]["recover_need_days"] == 3


def test_recovering_sorts_between_active_and_broken(db, acc):
    """段控顺序：在烧 → 重燃中 → 已断 → 无火花。"""
    cs.upsert_cache(db, acc.id, [*SAMPLE, RECOVERING, NO_SPARK]); db.commit()
    assert [c["status"] for c in cs.load_cached(db, acc.id)] == \
        ["active", "recovering", "broken", "none"]


def test_recovering_progress_cleared_when_back_to_active(db, acc):
    """重燃成功变回 active 后，进度要清掉，别在界面上留个 2/3 的残影。"""
    cs.upsert_cache(db, acc.id, [RECOVERING]); db.commit()
    cs.upsert_cache(db, acc.id, [
        {**RECOVERING, "status": "active", "recover_days": 0,
         "recover_need_days": 0},
    ]); db.commit()

    row = db.query(Contact).filter(Contact.uid == "444").first()
    assert row.status == "active"
    assert (row.recover_days or 0) == 0
