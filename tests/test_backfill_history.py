"""历史消息回填：把抖音云端的历史拉回来，补上冷备表里的空档。

Why: get_message_by_init 每会话只回 ~21 条，两次同步之间的消息永久丢失。
cmd=301 能按 cursor 翻完整个会话，已经丢的也追得回来。

回填是用户手动触发的低频操作，不进 scheduler —— 那会持续加风控面。
"""

import pytest

from app import trigger
from app.models import ChatMessage, Contact, DouyinAccount, User
from app.security import hash_password


@pytest.fixture
def acc(db):
    u = User(username="bfuser", password_hash=hash_password("pw123456"),
             max_accounts=3)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="主号", status="active",
                      cookies_exist=True)
    db.add(a); db.commit(); db.refresh(a)
    db.add(Contact(douyin_account_id=a.id, uid="111", nickname="甲",
                   conv_id="0:1:10000000001:111",
                   conv_short_id=7610461610200629770,
                   status="active", days=30))
    db.add(Contact(douyin_account_id=a.id, uid="222", nickname="乙",
                   conv_id="0:1:10000000001:222",
                   conv_short_id=None,   # 没有 short_id
                   status="active", days=10))
    db.commit()
    return u, a


def _msg(sid, ms, text, is_me=False):
    return {"server_msg_id": sid, "peer_uid": "111", "conv_id": "0:1:10000000001:111",
            "is_me": is_me, "kind": "text", "text": text,
            "created_at": ms, "msg_type": 7}


@pytest.fixture
def fake(monkeypatch):
    """拦掉网络：session 加载、init_req 读取、fetch_history 全部替身。"""
    calls = {"fetch": [], "history": [_msg(1, 1000, "老消息"),
                                      _msg(2, 2000, "更老的"),
                                      _msg(3, 3000, "最近的")]}

    monkeypatch.setattr(trigger, "set_account_ctx", lambda ctx: None)
    monkeypatch.setattr(trigger.dy, "_load_session", lambda: object())
    monkeypatch.setattr(trigger.dy, "_log", lambda *a, **k: None)
    monkeypatch.setattr(trigger, "_read_init_req", lambda: b"fake-init")
    monkeypatch.setattr(trigger.dy, "extract_my_uid", lambda b: "me")

    def _fetch(session, init_bytes, conv_id, conv_short_id, **kw):
        calls["fetch"].append({"conv_id": conv_id, "short_id": conv_short_id,
                               **kw})
        return list(calls["history"])

    monkeypatch.setattr(trigger.dy, "fetch_history", _fetch)
    return calls


def _stored(db, account_id):
    return db.query(ChatMessage).filter(
        ChatMessage.douyin_account_id == account_id).all()


# ── 单个会话回填 ─────────────────────────────────────────────────

def test_backfill_stores_history(db, acc, fake):
    u, a = acc
    out = trigger.backfill_history(u.id, a.id, "111")

    assert out["added"] == 3
    assert {m.text for m in _stored(db, a.id)} == {"老消息", "更老的", "最近的"}


def test_backfill_passes_conv_ids(db, acc, fake):
    """conv_id 和 short_id 都得从冷备表读出来传下去。"""
    u, a = acc
    trigger.backfill_history(u.id, a.id, "111")

    assert fake["fetch"][0]["conv_id"] == "0:1:10000000001:111"
    assert fake["fetch"][0]["short_id"] == 7610461610200629770


def test_backfill_is_idempotent(db, acc, fake):
    """跑两遍不能翻倍 —— sync_messages 按 server_msg_id 去重。"""
    u, a = acc
    trigger.backfill_history(u.id, a.id, "111")
    second = trigger.backfill_history(u.id, a.id, "111")

    assert second["added"] == 0
    assert len(_stored(db, a.id)) == 3


def test_backfill_merges_with_existing(db, acc, fake):
    """已有的消息留着，只补新的 —— 回填是累加不是覆盖。"""
    from app import messages_service

    u, a = acc
    messages_service.sync_messages(db, a.id, [_msg(99, 500, "本来就有的")])
    db.commit()

    trigger.backfill_history(u.id, a.id, "111")

    texts = {m.text for m in _stored(db, a.id)}
    assert "本来就有的" in texts, "回填把原有消息弄丢了"
    assert len(texts) == 4


def test_missing_short_id_is_reported_not_crashed(db, acc, fake):
    """没有 short_id 的联系人拉不了历史，要如实报告而不是抛异常。"""
    u, a = acc
    out = trigger.backfill_history(u.id, a.id, "222")

    assert out["added"] == 0
    assert out.get("error")
    assert fake["fetch"] == [], "没有 short_id 却还去打了接口"


def test_unknown_uid_is_reported(db, acc, fake):
    u, a = acc
    out = trigger.backfill_history(u.id, a.id, "not-exist")
    assert out.get("error")
    assert fake["fetch"] == []


# ── 整账号回填 ───────────────────────────────────────────────────

def test_backfill_all_covers_every_contact_with_short_id(db, acc, fake):
    u, a = acc
    out = trigger.backfill_all(u.id, a.id)

    assert len(fake["fetch"]) == 1, "只有 111 有 short_id，不该打第二次"
    assert out["contacts"] == 1
    assert out["added"] == 3


def test_backfill_all_keeps_going_after_one_fails(db, acc, fake, monkeypatch):
    """一个会话炸了不能让整轮回填停下。"""
    db.add(Contact(douyin_account_id=acc[1].id, uid="333", nickname="丙",
                   conv_id="0:1:10000000001:333", conv_short_id=999,
                   status="active", days=1))
    db.commit()

    seen = []

    def _boom(session, init_bytes, conv_id, conv_short_id, **kw):
        seen.append(conv_short_id)
        if conv_short_id == 7610461610200629770:
            raise RuntimeError("抖音抽风")
        return [_msg(7, 7000, "丙的消息")]

    monkeypatch.setattr(trigger.dy, "fetch_history", _boom)

    u, a = acc
    out = trigger.backfill_all(u.id, a.id)

    assert len(seen) == 2, "第一个失败后就不往下走了"
    assert out["failed"] == 1
    assert out["added"] == 1


# ── HTTP 契约 ────────────────────────────────────────────────────

def _csrf(client):
    client.get("/login")
    return client.cookies.get("csrf", "")


def _post(client, url, payload=None):
    return client.post(url, json=payload or {},
                       headers={"X-CSRF-Token": _csrf(client)})


def test_api_backfill_single_conversation(db, acc, login, fake):
    u, a = acc
    r = _post(login(u), f"/api/messages/{a.id}/backfill", {"uid": "111"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["added"] == 3


def test_api_backfill_whole_account(db, acc, login, fake):
    u, a = acc
    body = _post(login(u), f"/api/messages/{a.id}/backfill").json()
    assert body["ok"] is True
    assert body["contacts"] == 1


def test_api_reports_missing_short_id(db, acc, login, fake):
    u, a = acc
    body = _post(login(u), f"/api/messages/{a.id}/backfill", {"uid": "222"}).json()
    assert body["ok"] is False
    assert "刷新" in body["error"]


def test_api_other_users_account_is_404(db, acc, login, fake):
    """越权隔离：不能回填别人账号的消息。"""
    other_u = User(username="bfother", password_hash=hash_password("pw123456"),
                   max_accounts=3)
    db.add(other_u); db.commit(); db.refresh(other_u)
    victim = DouyinAccount(user_id=other_u.id, label="别人的", status="active")
    db.add(victim); db.commit(); db.refresh(victim)

    u, _ = acc
    r = _post(login(u), f"/api/messages/{victim.id}/backfill", {"uid": "111"})
    assert r.status_code == 404


def test_api_requires_csrf(db, acc, login, fake):
    u, a = acc
    r = login(u).post(f"/api/messages/{a.id}/backfill", json={"uid": "111"})
    assert r.status_code == 403


# ── my_uid 判定：回填的消息不能全算成对方发的 ──────────────────────

def test_refuses_when_my_uid_unknown(db, acc, monkeypatch):
    """两条路都拿不到 my_uid 时必须拒绝回填。

    这正是原 bug 的形态：my_uid 是空串 → parse_messages 把整段历史
    都判成对方发的 → 聊天页里全挤在左边。宁可不回填，也不能静默灌错数据。
    """
    called = []

    monkeypatch.setattr(trigger, "set_account_ctx", lambda ctx: None)
    monkeypatch.setattr(trigger.dy, "_load_session", lambda: object())
    monkeypatch.setattr(trigger.dy, "_log", lambda *a, **k: None)
    monkeypatch.setattr(trigger, "_read_init_req", lambda: b"fake-init")
    monkeypatch.setattr(trigger.dy, "fetch_history",
                        lambda *a_, **kw: called.append(1) or [])

    # 把 conv_id 弄成不含 peer uid 的异常形态，账户也没有 dy_uid
    row = db.query(Contact).filter(Contact.uid == "111").first()
    row.conv_id = "0:1:9:8"
    db.commit()

    u, a = acc
    out = trigger.backfill_history(u.id, a.id, "111")

    assert out.get("error"), "拿不到 my_uid 却照样回填了"
    assert called == [], "my_uid 未知却还是去打了接口"


def test_my_uid_derived_from_conv_id(db, acc, monkeypatch):
    """conv_id = 0:1:<我>:<对方>，去掉对方剩下的就是我 —— 本地推导，最可靠。"""
    seen = {}
    row = db.query(Contact).filter(Contact.uid == "111").first()
    row.conv_id = "0:1:10000000001:111"      # 对方就是 111
    db.commit()

    monkeypatch.setattr(trigger, "set_account_ctx", lambda ctx: None)
    monkeypatch.setattr(trigger.dy, "_load_session", lambda: object())
    monkeypatch.setattr(trigger.dy, "_log", lambda *a, **k: None)
    monkeypatch.setattr(trigger, "_read_init_req", lambda: b"fake-init")
    monkeypatch.setattr(trigger.dy, "fetch_history",
                        lambda s, i, conv_id, conv_short_id, **kw:
                        (seen.update(my_uid=kw.get("my_uid")), [])[1])

    u, a = acc
    trigger.backfill_history(u.id, a.id, "111")
    assert seen["my_uid"] == "10000000001"


def test_my_uid_falls_back_to_account_dy_uid(db, acc, monkeypatch):
    """conv_id 不是标准四段格式时，退回账户表里记的 dy_uid。"""
    seen = {}
    row = db.query(Contact).filter(Contact.uid == "111").first()
    row.conv_id = "weird-format"
    db.commit()
    a_row = db.get(DouyinAccount, acc[1].id)
    a_row.dy_uid = "88888888"
    db.commit()

    monkeypatch.setattr(trigger, "set_account_ctx", lambda ctx: None)
    monkeypatch.setattr(trigger.dy, "_load_session", lambda: object())
    monkeypatch.setattr(trigger.dy, "_log", lambda *a, **k: None)
    monkeypatch.setattr(trigger, "_read_init_req", lambda: b"fake-init")
    monkeypatch.setattr(trigger.dy, "fetch_history",
                        lambda s, i, conv_id, conv_short_id, **kw:
                        (seen.update(my_uid=kw.get("my_uid")), [])[1])

    u, a = acc
    trigger.backfill_history(u.id, a.id, "111")
    assert seen["my_uid"] == "88888888"


def test_backfill_fixes_wrong_is_me_on_existing_rows(db, acc, monkeypatch):
    """已经用错误 my_uid 回填过的记录，重跑回填要能纠正 is_me。

    sync_messages 按 server_msg_id 去重，已存在的不会更新 —— 光修根因
    救不了库里那批已经躺错边的历史。
    """
    from app import messages_service

    u, a = acc
    # 先灌一条「被判错」的：本来是我发的，却记成了对方
    messages_service.sync_messages(db, a.id, [_msg(1, 1000, "我说的", is_me=False)])
    db.commit()
    assert db.query(ChatMessage).filter(ChatMessage.server_msg_id == 1).first().is_me is False

    monkeypatch.setattr(trigger, "set_account_ctx", lambda ctx: None)
    monkeypatch.setattr(trigger.dy, "_load_session", lambda: object())
    monkeypatch.setattr(trigger.dy, "_log", lambda *a, **k: None)
    monkeypatch.setattr(trigger, "_read_init_req", lambda: b"fake-init")
    monkeypatch.setattr(trigger.dy, "fetch_history",
                        lambda *a_, **kw: [_msg(1, 1000, "我说的", is_me=True)])

    trigger.backfill_history(u.id, a.id, "111")

    row = db.query(ChatMessage).filter(ChatMessage.server_msg_id == 1).first()
    db.refresh(row)
    assert row.is_me is True, "重跑回填没能纠正躺错边的历史"


# ── 翻页深度 ─────────────────────────────────────────────────────
# 一页 50 条。默认 40 页只补得回两个来月，热聊会话想翻到更早得能加大；
# 但每页之间还要 sleep，页数越多请求越多，所以要有硬上限。

def test_default_page_depth_is_passed_down(db, acc, login, fake):
    u, a = acc
    _post(login(u), f"/api/messages/{a.id}/backfill", {"uid": "111"})
    assert fake["fetch"][0]["max_pages"] == trigger.BACKFILL_PAGES


def test_caller_can_ask_for_more_pages(db, acc, login, fake):
    u, a = acc
    _post(login(u), f"/api/messages/{a.id}/backfill",
          {"uid": "111", "pages": 120})
    assert fake["fetch"][0]["max_pages"] == 120


@pytest.mark.parametrize("pages,expected", [
    (99999, trigger.BACKFILL_PAGES_MAX),      # 封顶
    (0, trigger.BACKFILL_PAGES),              # 翻 1 页等于白跑，当没填
    (-5, trigger.BACKFILL_PAGES),
    ("abc", trigger.BACKFILL_PAGES),          # 坏值退回默认，不报错
    (None, trigger.BACKFILL_PAGES),
])
def test_page_depth_is_clamped(db, acc, login, fake, pages, expected):
    u, a = acc
    _post(login(u), f"/api/messages/{a.id}/backfill",
          {"uid": "111", "pages": pages})
    assert fake["fetch"][0]["max_pages"] == expected


def test_whole_account_backfill_also_honors_pages(db, acc, login, fake):
    u, a = acc
    _post(login(u), f"/api/messages/{a.id}/backfill", {"pages": 80})
    assert fake["fetch"][0]["max_pages"] == 80
