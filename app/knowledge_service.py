"""知识库 —— 通用 + 联系人专属，两个池子互不影响。

uid='*' 是**通用知识库**，对所有联系人生效（营业时间、常见问题…）；
uid=某联系人 uid 是**该联系人专属**（这个人的订单号、上次聊到哪…）。

检索时两个池子**分别打分、分别取 top-k**，再拼起来注入 prompt。
不放一起排序是刻意的：混在一起排，通用库条目一多就会把专属条目全挤掉，
用户会觉得"我给这个人单独加的知识没生效"。分开取，两边都保证有位置。

没有向量库（项目无 Redis / 无外部依赖），检索用中文 2-gram 相似度 +
关键词命中加权。条目量级是几十条，这个方案够用且零依赖 ——
SQLite FTS5 要额外的中文分词器，为几十条知识引这个不值。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import KnowledgeEntry

# 通用知识库的 uid 哨兵值。抖音真实 uid 是纯数字，'*' 不会撞上。
GLOBAL_UID = "*"

# 每个池子最多取几条
TOP_K = 3
# 注入 prompt 的知识总字数上限。太长会挤掉对话上下文，也烧 token
BUDGET = 1500
# 相似度低于这个分不算命中 —— 硬塞不相关的知识只会把模型带偏
MIN_SCORE = 1.0

CONTENT_MAX = 800
TITLE_MAX = 120
KEYWORDS_MAX = 255

_NOISE = re.compile(r"""[\s，。！？、；：“”‘’"'（）《》【】,.!?;:()\[\]<>~`\-_/\\|@#$%^&*+=]""")


def _norm(s: str) -> str:
    return _NOISE.sub("", (s or "").lower())


def _bigrams(s: str) -> set[str]:
    """中文没空格，2-gram 是最省事的切分。单字太噪，3-gram 太稀疏。"""
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def score(query: str, entry: KnowledgeEntry) -> float:
    """给一条知识打分。关键词命中权重最高 —— 那是用户明确指定的触发词。"""
    q = _norm(query)
    if not q:
        return 0.0

    total = 0.0
    for kw in (entry.keywords or "").replace("，", ",").split(","):
        k = _norm(kw)
        if k and k in q:
            total += 10.0

    title = _norm(entry.title)
    if title and (title in q or q in title):
        total += 5.0

    qg = _bigrams(q)
    eg = _bigrams(title + _norm(entry.content))
    if qg and eg:
        total += 6.0 * len(qg & eg) / len(qg)
    return total


def retrieve(db: Session, account_id: int, peer_uid: str, query: str,
             top_k: int = TOP_K, budget: int = BUDGET) -> str:
    """检索通用 + 专属知识，拼成可直接注入 prompt 的文本块。

    返回空串表示没有命中 —— 调用方据此让模型走"不知道就说不清楚"的分支。
    """
    shared = _pick(db, account_id, GLOBAL_UID, query, top_k)
    has_private = bool(peer_uid) and str(peer_uid) != GLOBAL_UID
    private = (_pick(db, account_id, str(peer_uid), query, top_k)
               if has_private else [])

    lines: list[str] = []
    used = 0
    # 专属排在前面：同样命中时，为这个人单独写的更贴切
    for label, rows in (("专属", private), ("通用", shared)):
        for e in rows:
            piece = f"[{label}] {e.title}：{e.content}".strip()
            if used + len(piece) > budget:
                continue
            lines.append(piece)
            used += len(piece)
    return "\n".join(lines)


def _pick(db: Session, account_id: int, uid: str, query: str,
          top_k: int) -> list[KnowledgeEntry]:
    rows = db.scalars(
        select(KnowledgeEntry).where(
            KnowledgeEntry.douyin_account_id == account_id,
            KnowledgeEntry.uid == uid,
            KnowledgeEntry.enabled.is_(True),
        )
    ).all()
    scored = [(score(query, e), e) for e in rows]
    hits = [(s, e) for s, e in scored if s >= MIN_SCORE]
    hits.sort(key=lambda t: (-t[0], t[1].id))
    return [e for _, e in hits[:top_k]]


# ── CRUD ─────────────────────────────────────────────────

def list_entries(db: Session, account_id: int, uid: str) -> list[dict]:
    rows = db.scalars(
        select(KnowledgeEntry).where(
            KnowledgeEntry.douyin_account_id == account_id,
            KnowledgeEntry.uid == str(uid),
        ).order_by(KnowledgeEntry.id.asc())
    ).all()
    return [_to_dict(e) for e in rows]


def count_entries(db: Session, account_id: int) -> dict[str, int]:
    """按 uid 统计条数，给前端在联系人旁边标「已配 N 条」用。"""
    rows = db.execute(
        select(KnowledgeEntry.uid, KnowledgeEntry.id).where(
            KnowledgeEntry.douyin_account_id == account_id)
    ).all()
    out: dict[str, int] = {}
    for uid, _ in rows:
        out[uid] = out.get(uid, 0) + 1
    return out


def upsert_entry(db: Session, account_id: int, uid: str, data: dict,
                 entry_id: int | None = None) -> KnowledgeEntry:
    """新建或更新一条知识。不 commit，由调用方决定事务边界。

    entry_id 必须同时匹配 account_id —— 否则改个 id 就能编辑别人的知识库。
    """
    row: KnowledgeEntry | None = None
    if entry_id:
        row = db.scalar(select(KnowledgeEntry).where(
            KnowledgeEntry.id == int(entry_id),
            KnowledgeEntry.douyin_account_id == account_id))
        if row is None:
            raise ValueError("知识条目不存在")
    if row is None:
        row = KnowledgeEntry(douyin_account_id=account_id, uid=str(uid))
        db.add(row)

    if "title" in data:
        row.title = (data.get("title") or "").strip()[:TITLE_MAX]
    if "content" in data:
        row.content = (data.get("content") or "").strip()[:CONTENT_MAX]
    if "keywords" in data:
        row.keywords = (data.get("keywords") or "").strip()[:KEYWORDS_MAX]
    if "enabled" in data:
        row.enabled = bool(data.get("enabled"))
    if not row.title and not row.content:
        raise ValueError("标题和内容不能都为空")
    return row


def delete_entry(db: Session, account_id: int, entry_id: int) -> bool:
    row = db.scalar(select(KnowledgeEntry).where(
        KnowledgeEntry.id == int(entry_id),
        KnowledgeEntry.douyin_account_id == account_id))
    if not row:
        return False
    db.delete(row)
    return True


def _to_dict(e: KnowledgeEntry) -> dict:
    return {
        "id": e.id, "uid": e.uid, "title": e.title, "content": e.content,
        "keywords": e.keywords, "enabled": bool(e.enabled),
    }
