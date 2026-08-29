"""聊天消息冷备读写。

数据源：get_message_by_init 的响应 —— 拉联系人时已经打过这个接口，
消息直接从同一份响应里解析（见 douyin_im.parse_messages），
不额外请求抖音。多打一个「收消息」接口就是多一份风控风险。

抖音每次只回每个会话最近 ~21 条，所以这张表的定位是「累积」：
同步做幂等 upsert 把新消息并进来，老消息留着，用户才翻得到历史。
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from .models import ChatMessage

# 单条消息正文上限。抖音的分享类消息正文可能很长（带标题+话题），
# 聊天气泡里也显示不下，截断入库避免把 DB 撑大。
_TEXT_MAX = 2000

# 前端一屏的消息条数
DEFAULT_LIMIT = 50

# 本地占位行能被真身认领的时间窗。发出去到同步回来通常只隔几秒，
# 给到 1 小时足够覆盖同步失败重试；再宽就有几天前的残留占位
# 跑来认领今天新消息的风险。
_CLAIM_WINDOW_MS = 3600 * 1000


# 单条媒体 JSON 的长度上限：正常就是两个 URL 加个 id，
# 超出说明抖音塞了别的东西，宁可不存也别把 DB 撑大
_MEDIA_MAX = 2000


def _media_json(media) -> str | None:
    if not isinstance(media, dict) or not media:
        return None
    try:
        raw = json.dumps(media, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return raw if len(raw) <= _MEDIA_MAX else None


def _load_media(raw: str | None):
    if not raw:
        return None
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


def _row_to_dict(m: ChatMessage) -> dict:
    return {
        # 翻页游标要用 (created_ms, id) 两段，单靠时间戳会漏掉同毫秒的消息
        "id": m.id,
        "server_msg_id": m.server_msg_id,
        "peer_uid": m.peer_uid,
        "conv_id": m.conv_id,
        "is_me": bool(m.is_me),
        "kind": m.kind,
        "msg_type": m.msg_type,
        "text": m.text or "",
        "media": _load_media(m.media),
        "created_at": m.created_ms,
    }


def sync_messages(db: Session, account_id: int, messages: list[dict]) -> int:
    """把解析出的消息并入冷备表，返回新增条数。"""
    return len(sync_and_collect(db, account_id, messages))


def sync_and_collect(db: Session, account_id: int,
                     messages: list[dict]) -> list[dict]:
    """同 sync_messages，但返回「这次新增的那些消息」。

    实时推送要的就是这个差集 —— 每次轮询把整份响应喂进来，
    只有真正新增的才推给浏览器，否则前端会反复收到同样的历史消息。

    我方消息还会去认领本地占位行（append_local 写的负数 id），
    认领到的结果带 replaces 字段，前端据此把临时气泡换成真身而不是再追加。

    幂等：靠 (account_id, server_msg_id) 去重，重复同步不会翻倍。
    不 commit，由调用方决定事务边界。
    """
    if not messages:
        return []

    # 没有 server_msg_id 的消息无法去重，收进来每次同步都会重复插一条
    incoming = {m["server_msg_id"]: m for m in messages if m.get("server_msg_id")}
    if not incoming:
        return []

    existing = set(db.scalars(
        select(ChatMessage.server_msg_id).where(
            ChatMessage.douyin_account_id == account_id,
            ChatMessage.server_msg_id.in_(list(incoming)),
        )
    ).all())

    fresh = [m for msg_id, m in incoming.items() if msg_id not in existing]
    fresh.sort(key=lambda m: m.get("created_at") or 0)

    pending = _claimable_placeholders(db, account_id, fresh)

    added: list[dict] = []
    for m in fresh:
        claimed = _claim(pending, m)
        if claimed is not None:
            added.append(claimed)
            continue
        row = ChatMessage(
            douyin_account_id=account_id,
            peer_uid=str(m.get("peer_uid") or ""),
            conv_id=(m.get("conv_id") or "")[:80] or None,
            server_msg_id=m["server_msg_id"],
            sender=str(m.get("sender") or "")[:40] or None,
            is_me=bool(m.get("is_me")),
            msg_type=int(m.get("msg_type") or 0),
            kind=str(m.get("kind") or "other")[:16],
            text=(m.get("text") or "")[:_TEXT_MAX],
            media=_media_json(m.get("media")),
            created_ms=int(m.get("created_at") or 0),
        )
        db.add(row)
        added.append(_row_to_dict(row))
    added.sort(key=lambda m: m["created_at"])
    return added


def _claimable_placeholders(db: Session, account_id: int,
                            fresh: list[dict]) -> dict[tuple, list[ChatMessage]]:
    """按 (peer_uid, text) 归拢本地占位行，供我方消息认领。

    只在本次真有我方新消息时才查库 —— 绝大多数轮询是纯收消息，不该白跑一次查询。
    """
    if not any(m.get("is_me") for m in fresh):
        return {}
    rows = db.scalars(
        select(ChatMessage).where(
            ChatMessage.douyin_account_id == account_id,
            ChatMessage.server_msg_id < 0,
        ).order_by(ChatMessage.created_ms.asc())
    ).all()
    out: dict[tuple, list[ChatMessage]] = {}
    for r in rows:
        out.setdefault((r.peer_uid, r.text or ""), []).append(r)
    return out


def _claim(pending: dict[tuple, list[ChatMessage]], m: dict) -> dict | None:
    """让抖音回来的真身认领对应的本地占位行；认领不到返回 None。

    只认同一联系人 + 同一文本 + 我方发出的，且占位不能太旧 ——
    几天前的残留占位跑来认领今天的新消息，会把时间线搞乱。
    每个占位只能被认领一次，所以连发两条相同内容也能一一对应。
    """
    if not m.get("is_me"):
        return None
    key = (str(m.get("peer_uid") or ""), (m.get("text") or "")[:_TEXT_MAX])
    queue = pending.get(key)
    if not queue:
        return None

    created = int(m.get("created_at") or 0)
    row = queue.pop(0)
    if abs(created - (row.created_ms or 0)) > _CLAIM_WINDOW_MS:
        queue.insert(0, row)          # 太旧，放回去别误伤
        return None

    old_id = row.server_msg_id
    row.server_msg_id = m["server_msg_id"]
    row.created_ms = created
    row.conv_id = (m.get("conv_id") or row.conv_id or "")[:80] or None
    row.sender = str(m.get("sender") or "")[:40] or row.sender
    row.msg_type = int(m.get("msg_type") or row.msg_type)
    row.kind = str(m.get("kind") or row.kind)[:16]
    row.media = _media_json(m.get("media")) or row.media

    out = _row_to_dict(row)
    out["replaces"] = old_id          # 前端据此换掉临时气泡，而不是再追加一条
    return out


def fix_is_me(db: Session, account_id: int, messages: list[dict]) -> int:
    """按云端数据纠正已入库消息的 is_me，返回纠正条数。

    Why: sync_and_collect 按 server_msg_id 去重，已存在的行原样不动。
    早期回填传错了 my_uid（喂的是 init_req.bin，提取结果恒为空串），
    整段历史被判成对方发的，聊天页里全挤在左边 —— 光修根因救不了那批数据，
    得让重跑回填能把方向掰回来。

    只动 is_me 一个字段：文本/时间/媒体以库里的为准，避免把用户看过的
    内容改掉。不 commit，由调用方决定事务边界。
    """
    wanted = {m["server_msg_id"]: bool(m.get("is_me"))
              for m in messages if m.get("server_msg_id")}
    if not wanted:
        return 0
    rows = db.scalars(
        select(ChatMessage).where(
            ChatMessage.douyin_account_id == account_id,
            ChatMessage.server_msg_id.in_(list(wanted)),
        )
    ).all()
    fixed = 0
    for r in rows:
        want = wanted.get(r.server_msg_id)
        if want is not None and bool(r.is_me) != want:
            r.is_me = want
            fixed += 1
    return fixed


def load_conversation(db: Session, account_id: int, peer_uid: str,
                      limit: int = DEFAULT_LIMIT,
                      before: int | None = None,
                      before_id: int | None = None) -> list[dict]:
    """读一个会话的消息，按时间正序返回（聊天窗从上往下渲染）。

    取的是「最近 limit 条」，所以先按时间倒序取再翻转。
    before/before_id 是往上翻历史的游标，取排序上严格早于它的那些。

    游标必须是 (created_ms, id) 两段：抖音同一秒能推好几条，
    只用 created_ms 的话，页边界正好落在两条同毫秒消息之间时，
    `created_ms < before` 会把同毫秒的那条一起排除掉 —— 它就永远翻不出来了。
    id 是自增的，同批入库时按时间排序插入，可以当稳定的次级序。
    """
    stmt = select(ChatMessage).where(
        ChatMessage.douyin_account_id == account_id,
        ChatMessage.peer_uid == str(peer_uid),
    )
    if before is not None:
        if before_id is not None:
            stmt = stmt.where(or_(
                ChatMessage.created_ms < int(before),
                and_(ChatMessage.created_ms == int(before),
                     ChatMessage.id < int(before_id)),
            ))
        else:
            stmt = stmt.where(ChatMessage.created_ms < int(before))
    stmt = stmt.order_by(ChatMessage.created_ms.desc(),
                         ChatMessage.id.desc()).limit(max(1, int(limit)))
    rows = list(db.scalars(stmt).all())
    rows.reverse()
    return [_row_to_dict(m) for m in rows]


def last_message_map(db: Session, account_id: int) -> dict[str, dict]:
    """每个联系人最后一条消息，给会话列表做预览用。

    联系人数量是几十级别，一次全取按 peer 归并即可 ——
    SQLite 没有窗口函数友好的写法，分组子查询反而更慢更绕。
    """
    stmt = (select(ChatMessage)
            .where(ChatMessage.douyin_account_id == account_id)
            .order_by(ChatMessage.created_ms.asc(), ChatMessage.id.asc()))
    out: dict[str, dict] = {}
    for m in db.scalars(stmt):
        out[m.peer_uid] = _row_to_dict(m)      # 正序遍历，后写的覆盖前面 = 最后一条
    return out


def append_local(db: Session, account_id: int, peer_uid: str, text: str,
                 conv_id: str | None = None) -> dict:
    """把自己刚发出去的消息立刻写进库。

    抖音的 server_message_id 要等下次 init 同步才拿得到，这里先用负数占位：
    真实 id 恒为正，负数不会和它撞，下次同步补回真身时也不会被当成重复。
    """
    floor = db.scalar(
        select(ChatMessage.server_msg_id)
        .where(ChatMessage.douyin_account_id == account_id,
               ChatMessage.server_msg_id < 0)
        .order_by(ChatMessage.server_msg_id.asc()).limit(1)
    )
    placeholder_id = (floor - 1) if floor else -1

    row = ChatMessage(
        douyin_account_id=account_id,
        peer_uid=str(peer_uid),
        conv_id=(conv_id or "")[:80] or None,
        server_msg_id=placeholder_id,
        is_me=True,
        msg_type=7,
        kind="text",
        text=(text or "")[:_TEXT_MAX],
        created_ms=int(datetime.now().timestamp() * 1000),
    )
    db.add(row)
    db.flush()
    return _row_to_dict(row)
