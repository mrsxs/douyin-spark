"""翻页游标的回归测试。

背景：用户报「聊天记录只到 7月31日」。查下来那不是 bug —— 抖音的
get_message_by_init 每个会话只回最近 ~21 条，首次同步之前的历史
它根本不给，本地是从那时起才开始累积的。

但排查时发现了一个真的会丢消息的问题：游标只用 created_ms 的话，
页边界正好落在两条同毫秒消息之间时，`created_ms < before` 会把
同毫秒的那条一起排除 —— 它就永远翻不出来。用户的库里已经有这种数据。
"""
from app import messages_service as ms
from app.models import ChatMessage


def _add(db, account_id, peer_uid, msg_id, ms_ts, text):
    row = ChatMessage(douyin_account_id=account_id, peer_uid=peer_uid,
                      server_msg_id=msg_id, is_me=False, kind="text",
                      text=text, created_ms=ms_ts)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _page_all(db, acc_id, peer, limit):
    """按前端的翻页方式一页页往上翻，返回翻到的全部文本。"""
    page = ms.load_conversation(db, acc_id, peer, limit=limit)
    seen = list(page)
    while len(page) >= limit:
        first = page[0]
        page = ms.load_conversation(db, acc_id, peer, limit=limit,
                                    before=first["created_at"],
                                    before_id=first["id"])
        seen = page + seen
    return [m["text"] for m in seen]


def test_同毫秒的消息不会在页边界被跳过(db, active_user):
    """两条消息同一毫秒，且页边界正好落在它们之间。

    只用时间戳做游标的话，靠前的那条会永远翻不出来。
    """
    _, a = active_user
    _add(db, a.id, "1001", 1, 1000, "最早")
    _add(db, a.id, "1001", 2, 2000, "同毫秒-A")
    _add(db, a.id, "1001", 3, 2000, "同毫秒-B")
    _add(db, a.id, "1001", 4, 3000, "最新")

    # limit=2 让第一页是 [同毫秒-B, 最新]，游标落在两条同毫秒消息中间
    assert _page_all(db, a.id, "1001", 2) == ["最早", "同毫秒-A", "同毫秒-B", "最新"]


def test_一整页都是同毫秒也能翻完(db, active_user):
    """极端情况：抖音一次推来的整批消息时间戳全一样。"""
    _, a = active_user
    for i in range(6):
        _add(db, a.id, "1001", 100 + i, 5000, f"第{i}条")
    _add(db, a.id, "1001", 200, 1000, "更早的")

    got = _page_all(db, a.id, "1001", 2)
    assert got == ["更早的"] + [f"第{i}条" for i in range(6)]


def test_不传游标id时退回旧行为(db, active_user):
    """老前端（不带 before_id）仍要能正常翻页，只是同毫秒那条会漏。"""
    _, a = active_user
    _add(db, a.id, "1001", 1, 1000, "早")
    _add(db, a.id, "1001", 2, 3000, "晚")
    rows = ms.load_conversation(db, a.id, "1001", before=3000)
    assert [m["text"] for m in rows] == ["早"]


def test_翻页不会重复同一条(db, active_user):
    _, a = active_user
    for i in range(10):
        _add(db, a.id, "1001", i, 1000 + i, f"m{i}")
    got = _page_all(db, a.id, "1001", 3)
    assert got == [f"m{i}" for i in range(10)]
    assert len(got) == len(set(got))


def test_消息带上id供前端做游标(db, active_user):
    """前端翻页要用它，缺了就退化成只有时间戳的旧行为。"""
    _, a = active_user
    row = _add(db, a.id, "1001", 1, 1000, "在吗")
    got = ms.load_conversation(db, a.id, "1001")[0]
    assert got["id"] == row.id


def test_接口接受before_id参数(db, active_user, login):
    _, a = active_user
    _add(db, a.id, "1001", 1, 2000, "同毫秒-A")
    _add(db, a.id, "1001", 2, 2000, "同毫秒-B")

    c = login(a and _u(db, a))
    r = c.get(f"/api/messages/{a.id}/1001?limit=1")
    assert r.status_code == 200
    first = r.json()["messages"][0]
    assert first["text"] == "同毫秒-B"

    r2 = c.get(f"/api/messages/{a.id}/1001"
               f"?limit=1&before={first['created_at']}&before_id={first['id']}")
    assert [m["text"] for m in r2.json()["messages"]] == ["同毫秒-A"]


def _u(db, acc):
    from app.models import User
    return db.get(User, acc.user_id)
