"""messages_service：聊天消息落库 / 读取。

关键行为是「累积」——抖音每次只回每个会话最近 ~21 条，
所以同步必须是幂等 upsert，把新消息并进来而不是覆盖，
否则用户翻不到更早的记录。
"""
import pytest

from app import messages_service as ms
from app.db import SessionLocal
from app.models import ChatMessage, DouyinAccount, User


@pytest.fixture
def account(make_user):
    """建一个用户 + 一个抖音账号，返回 account_id。"""
    u = make_user(username="chatuser")
    with SessionLocal() as db:
        acc = DouyinAccount(user_id=u.id, label="测试号", status="active")
        db.add(acc)
        db.commit()
        return acc.id


def _m(msg_id, peer="2000000002", text="你好", is_me=False, created=1000,
       kind="text", mtype=7):
    return {
        "conv_id": f"0:1:1000000001:{peer}",
        "peer_uid": peer,
        "server_msg_id": msg_id,
        "conv_short_id": 42,
        "msg_type": mtype,
        "kind": kind,
        "sender": "1000000001" if is_me else peer,
        "is_me": is_me,
        "text": text,
        "created_at": created,
    }


# ── 写入 ──────────────────────────────────────────────────────────

def test_sync_inserts_messages(account):
    with SessionLocal() as db:
        n = ms.sync_messages(db, account, [_m(1), _m(2)])
        db.commit()
        assert n == 2
        assert db.query(ChatMessage).count() == 2


def test_sync_is_idempotent(account):
    """同一批消息同步两次不能翻倍 —— 每次拉联系人都会重跑。"""
    with SessionLocal() as db:
        ms.sync_messages(db, account, [_m(1), _m(2)])
        db.commit()
    with SessionLocal() as db:
        n = ms.sync_messages(db, account, [_m(1), _m(2)])
        db.commit()
        assert n == 0
        assert db.query(ChatMessage).count() == 2


def test_sync_accumulates_history(account):
    """核心：抖音只回最近 N 条，老消息必须留在库里。"""
    with SessionLocal() as db:
        ms.sync_messages(db, account, [_m(1, created=100), _m(2, created=200)])
        db.commit()
    # 第二次抖音只回了更新的两条，老的 1/2 不在响应里
    with SessionLocal() as db:
        ms.sync_messages(db, account, [_m(3, created=300), _m(4, created=400)])
        db.commit()
        assert db.query(ChatMessage).count() == 4


def test_sync_ignores_messages_without_id(account):
    """没有 server_msg_id 的没法去重，收进来会无限重复。"""
    with SessionLocal() as db:
        ms.sync_messages(db, account, [_m(0)])
        db.commit()
        assert db.query(ChatMessage).count() == 0


def test_sync_empty_list_is_noop(account):
    with SessionLocal() as db:
        assert ms.sync_messages(db, account, []) == 0


def test_same_msg_id_different_accounts_kept(account):
    """去重键含 account —— 两个账号各自的消息不能互相顶掉。"""
    with SessionLocal() as db:
        u = db.query(User).first()
        acc2 = DouyinAccount(user_id=u.id, label="二号", status="active")
        db.add(acc2)
        db.commit()
        acc2_id = acc2.id
    with SessionLocal() as db:
        ms.sync_messages(db, account, [_m(1)])
        ms.sync_messages(db, acc2_id, [_m(1)])
        db.commit()
        assert db.query(ChatMessage).count() == 2


def test_long_text_is_truncated_not_rejected(account):
    with SessionLocal() as db:
        ms.sync_messages(db, account, [_m(1, text="超长" * 5000)])
        db.commit()
        assert db.query(ChatMessage).count() == 1


# ── 读取 ──────────────────────────────────────────────────────────

def test_load_conversation_returns_time_ascending(account):
    with SessionLocal() as db:
        ms.sync_messages(db, account, [
            _m(3, created=300), _m(1, created=100), _m(2, created=200)])
        db.commit()
        out = ms.load_conversation(db, account, "2000000002")
        assert [m["server_msg_id"] for m in out] == [1, 2, 3]


def test_load_conversation_filters_by_peer(account):
    with SessionLocal() as db:
        ms.sync_messages(db, account, [_m(1, peer="111"), _m(2, peer="222")])
        db.commit()
        out = ms.load_conversation(db, account, "111")
        assert len(out) == 1
        assert out[0]["peer_uid"] == "111"


def test_load_conversation_limit_keeps_newest(account):
    """限流取最近 N 条，但返回顺序仍是正序（聊天窗从上到下）。"""
    with SessionLocal() as db:
        ms.sync_messages(db, account, [_m(i, created=i * 10) for i in range(1, 21)])
        db.commit()
        out = ms.load_conversation(db, account, "2000000002", limit=5)
        assert [m["server_msg_id"] for m in out] == [16, 17, 18, 19, 20]


def test_load_conversation_before_pages_backwards(account):
    """往上翻历史：取 created_ms 小于游标的那批。"""
    with SessionLocal() as db:
        ms.sync_messages(db, account, [_m(i, created=i * 10) for i in range(1, 21)])
        db.commit()
        out = ms.load_conversation(db, account, "2000000002", limit=5, before=160)
        assert [m["server_msg_id"] for m in out] == [11, 12, 13, 14, 15]


def test_load_conversation_empty(account):
    with SessionLocal() as db:
        assert ms.load_conversation(db, account, "nobody") == []


def test_last_message_map(account):
    """会话列表要显示每个联系人的最后一条消息。"""
    with SessionLocal() as db:
        ms.sync_messages(db, account, [
            _m(1, peer="111", text="早", created=100),
            _m(2, peer="111", text="晚", created=200),
            _m(3, peer="222", text="嗨", created=150)])
        db.commit()
        out = ms.last_message_map(db, account)
        assert out["111"]["text"] == "晚"
        assert out["222"]["text"] == "嗨"


def test_last_message_map_empty(account):
    with SessionLocal() as db:
        assert ms.last_message_map(db, account) == {}


def test_append_local_message(account):
    """自己刚发出去的消息要立刻进库，不用等下次同步。"""
    with SessionLocal() as db:
        ms.append_local(db, account, "2000000002", "我发的")
        db.commit()
        out = ms.load_conversation(db, account, "2000000002")
        assert len(out) == 1
        assert out[0]["is_me"] is True
        assert out[0]["text"] == "我发的"


def test_append_local_does_not_collide_with_synced(account):
    """本地插入用负数 id 占位，不能和抖音真实 id 撞。"""
    with SessionLocal() as db:
        ms.append_local(db, account, "2000000002", "第一条")
        ms.append_local(db, account, "2000000002", "第二条")
        db.commit()
        assert db.query(ChatMessage).count() == 2


# ── 本地占位与真身的认领 ──────────────────────────────────────────
# 用户在网页发一条「1」，会先写个负数 id 的占位行让他立刻看到；
# 几秒后同步把抖音那条真身拉回来。不认领的话就是两条一模一样的「1」。

def test_local_placeholder_is_claimed_by_real_message(account):
    with SessionLocal() as db:
        ms.append_local(db, account, "2000000002", "1")
        db.commit()

    real = _m(999, text="1", is_me=True, created=int(
        __import__("datetime").datetime.now().timestamp() * 1000))
    with SessionLocal() as db:
        ms.sync_and_collect(db, account, [real])
        db.commit()
        rows = db.query(ChatMessage).all()
        assert len(rows) == 1, f"占位行没被认领，出现重复: {[r.text for r in rows]}"
        assert rows[0].server_msg_id == 999, "认领后应该换成真实 id"


def test_claim_reports_which_placeholder_it_replaced(account):
    """前端要靠这个把临时气泡换成真身，而不是再追加一条。"""
    with SessionLocal() as db:
        local = ms.append_local(db, account, "2000000002", "1")
        db.commit()
        temp_id = local["server_msg_id"]

    with SessionLocal() as db:
        added = ms.sync_and_collect(db, account, [
            _m(999, text="1", is_me=True, created=_now_ms())])
        db.commit()
        assert len(added) == 1
        assert added[0]["replaces"] == temp_id


def test_two_identical_sends_claim_one_each(account):
    """连发两条一样的「1」，两个占位各认领一条，不能一个吃掉两条。"""
    with SessionLocal() as db:
        ms.append_local(db, account, "2000000002", "1")
        ms.append_local(db, account, "2000000002", "1")
        db.commit()

    with SessionLocal() as db:
        ms.sync_and_collect(db, account, [
            _m(901, text="1", is_me=True, created=_now_ms()),
            _m(902, text="1", is_me=True, created=_now_ms() + 1)])
        db.commit()
        rows = db.query(ChatMessage).all()
        assert len(rows) == 2
        assert {r.server_msg_id for r in rows} == {901, 902}


def test_claim_only_matches_same_text(account):
    with SessionLocal() as db:
        ms.append_local(db, account, "2000000002", "你好")
        db.commit()
    with SessionLocal() as db:
        ms.sync_and_collect(db, account, [
            _m(999, text="别的内容", is_me=True, created=_now_ms())])
        db.commit()
        assert db.query(ChatMessage).count() == 2, "不同内容不该被认领"


def test_claim_only_matches_same_peer(account):
    with SessionLocal() as db:
        ms.append_local(db, account, "111", "1")
        db.commit()
    with SessionLocal() as db:
        ms.sync_and_collect(db, account, [
            _m(999, peer="222", text="1", is_me=True, created=_now_ms())])
        db.commit()
        assert db.query(ChatMessage).count() == 2, "不同联系人不该被认领"


def test_incoming_message_never_claims_placeholder(account):
    """对方发来的消息绝不能认领我的占位 —— 那会把我发的吞掉。"""
    with SessionLocal() as db:
        ms.append_local(db, account, "2000000002", "1")
        db.commit()
    with SessionLocal() as db:
        ms.sync_and_collect(db, account, [
            _m(999, text="1", is_me=False, created=_now_ms())])
        db.commit()
        assert db.query(ChatMessage).count() == 2


def test_stale_placeholder_is_not_claimed(account):
    """几天前的残留占位不能来认领今天的新消息。"""
    with SessionLocal() as db:
        row = ms.append_local(db, account, "2000000002", "1")
        db.commit()
        obj = db.query(ChatMessage).filter(
            ChatMessage.server_msg_id == row["server_msg_id"]).first()
        obj.created_ms = _now_ms() - 5 * 24 * 3600 * 1000
        db.commit()

    with SessionLocal() as db:
        ms.sync_and_collect(db, account, [
            _m(999, text="1", is_me=True, created=_now_ms())])
        db.commit()
        assert db.query(ChatMessage).count() == 2


def test_claim_is_idempotent(account):
    """认领完再同步一次，不能又冒出一条。"""
    with SessionLocal() as db:
        ms.append_local(db, account, "2000000002", "1")
        db.commit()
    real = [_m(999, text="1", is_me=True, created=_now_ms())]
    for _ in range(2):
        with SessionLocal() as db:
            ms.sync_and_collect(db, account, real)
            db.commit()
    with SessionLocal() as db:
        assert db.query(ChatMessage).count() == 1


def _now_ms() -> int:
    from datetime import datetime
    return int(datetime.now().timestamp() * 1000)



# ── 内嵌媒体的存取 ────────────────────────────────────────────────

_MEDIA = {"kind": "video", "cover": "https://p26.douyinpic.com/a.webp",
          "vid": "7668476126852136795"}


def test_media_round_trips(account):
    with SessionLocal() as db:
        m = _m(1)
        m["media"] = _MEDIA
        ms.sync_messages(db, account, [m])
        db.commit()
        out = ms.load_conversation(db, account, "2000000002")
        assert out[0]["media"] == _MEDIA


def test_message_without_media(account):
    with SessionLocal() as db:
        ms.sync_messages(db, account, [_m(1)])
        db.commit()
        assert ms.load_conversation(db, account, "2000000002")[0]["media"] is None


@pytest.mark.parametrize("junk", ["", "not json", "[1,2]", "null", '"str"'])
def test_corrupt_media_column_does_not_break_reading(account, junk):
    """DB 里的 media 是历史遗留脏数据时，聊天记录也得读得出来。"""
    with SessionLocal() as db:
        ms.sync_messages(db, account, [_m(1)])
        db.commit()
        db.query(ChatMessage).update({ChatMessage.media: junk})
        db.commit()
        out = ms.load_conversation(db, account, "2000000002")
        assert len(out) == 1
        assert out[0]["media"] is None


def test_oversized_media_is_dropped_not_stored(account):
    with SessionLocal() as db:
        m = _m(1)
        m["media"] = {"kind": "video", "cover": "https://x/" + "a" * 5000, "vid": "1"}
        ms.sync_messages(db, account, [m])
        db.commit()
        assert ms.load_conversation(db, account, "2000000002")[0]["media"] is None
