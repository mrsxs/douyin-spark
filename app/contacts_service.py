"""联系人冷备读写。

Why: /accounts/{id} 原本在请求里同步调 trigger.get_contacts()（走抖音 API，
15s timeout），再叠一次头像补齐的网络请求 —— 首屏白屏 5~20 秒，
期间还占着 FastAPI 的同步 threadpool。

改成先渲染这张冷备表（纯 DB，毫秒级），前端再异步拉最新数据覆盖。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

import douyin_im as dy

from .models import Contact

# 联系人展示/排序优先级：
#   还在烧 → 重燃中（有窗口期、最紧急）→ 已断 → 从来没火花
# 未知状态按 broken 处理（宁可让用户多看一眼，也别悄悄排到最后）
STATUS_RANK = {"active": 0, "recovering": 1, "broken": 2, "none": 3}

# prune 宽限期：连续这么多天没被 init 同步到，才认为好友真的没了。
# get_message_by_init 只回最近活跃的会话，不是完整好友列表 ——
# 火花越久的人越容易长期没互动，"这次没回就删"会专挑最珍贵的下手。
PRUNE_GRACE_DAYS = 7


def normalize_avatar_url(url: str | None) -> str:
    """把抖音头像 URL 规范化成长期可访问的形式。

    实现下沉到协议层（douyin_im）—— 头像和聊天里的视频封面、图片
    是同一套签名域名规则，两份拷贝迟早会走样。
    """
    return dy.normalize_media_url(url)


def upsert_cache(db: Session, account_id: int, contacts: list[dict],
                 prune: bool = False) -> int:
    """把抓到的联系人写入冷备表。返回写入条数。不 commit，由调用方决定事务边界。

    prune=True 时清理「已经很久没被同步到」的联系人。空列表一律不清理
    （接口异常返回空不该清空用户数据）。
    """
    existing = {c.uid: c for c in db.query(Contact).filter(
        Contact.douyin_account_id == account_id).all()}
    now = datetime.utcnow()
    seen = set()
    n = 0
    for c in contacts:
        uid = c.get("uid")
        if not uid:
            continue
        seen.add(uid)
        row = existing.get(uid)
        if not row:
            row = Contact(douyin_account_id=account_id, uid=uid)
            db.add(row)
        # 上游抓不到昵称时会把 nickname 回退成 uid ——
        # 那是占位值，不能拿它覆盖之前存下的真名（「27」曾被刷成一长串 uid）
        new_nick = (c.get("nickname") or "").strip()
        if new_nick and new_nick != uid:
            row.nickname = new_nick
        elif not row.nickname:
            row.nickname = new_nick or uid
        row.conv_id = c.get("conv_id") or row.conv_id
        # 某次解析没带出 short_id 时别抹掉已存的 —— 抹了这条会话就再也拉不了历史
        _short = c.get("conversation_short_id")
        if _short:
            row.conv_short_id = int(_short)
        new_status = c.get("status") or "active"
        # 「本次没解析出火花」不等于「这人真的没火花」——
        # parse_fire_streaks 是拿正则打 protobuf，窗口偏一点就漏掉 consecutive_chat。
        # 真让它覆盖下去，用户看到的是 342 天一夜变 0 天。所以 none 只能新增，
        # 不能把库里已经烧起来（或烧断过）的记录降级。
        downgrade = (new_status == "none"
                     and row.status in ("active", "broken")
                     and (row.days or 0) > 0)
        if not downgrade:
            row.days = c.get("days") if c.get("days") is not None else row.days
            row.status = new_status
            # 进度跟着状态走：重燃成功回到 active 时要清零，
            # 否则界面上留个「2/3」的残影
            row.recover_days = int(c.get("recover_days") or 0)
            row.recover_need_days = int(c.get("recover_need_days") or 0)
        # 刷新时偶尔拿不到头像，别把已有的抹掉
        if c.get("avatar"):
            row.avatar = normalize_avatar_url(c["avatar"])
        row.last_synced_at = now
        n += 1

    if prune and seen:
        # 抖音那边已经没有的（对方删好友/注销），本地也要清掉，
        # 否则用户会一直看到早已消失的人挂在列表里。
        #
        # 但不能「这次没回就删」—— get_message_by_init 只回最近活跃的会话，
        # 不是完整好友列表。真实事故：账号 3 的「王女士 906 天」「顺风吖 905 天」
        # 「团团 327 天」最近没互动、掉出 init 返回范围，一次刷新就被删光了。
        # 火花越久的人越容易长期没互动，恰恰是最不该删的那批。
        #
        # 改成宽限期：连续 PRUNE_GRACE_DAYS 天都没被同步到才算真没了。
        cutoff = now - timedelta(days=PRUNE_GRACE_DAYS)
        for uid, row in existing.items():
            if uid in seen:
                continue
            last = row.last_synced_at
            if last is None or last < cutoff:
                db.delete(row)
    return n


def load_cached(db: Session, account_id: int) -> list[dict]:
    """读冷备，返回和 trigger.get_contacts 同结构的 dict 列表。

    排序与抖音接口一致：还在燃烧的排前面，然后是需重燃的，
    最后才是从来没有火花的普通好友；同组内按天数倒序。
    """
    rows = (db.query(Contact)
              .filter(Contact.douyin_account_id == account_id)
              .all())
    out = [{
        "uid": r.uid,
        "nickname": r.remark or r.nickname or r.uid,
        "avatar": r.avatar or "",
        "conv_id": r.conv_id or "",
        "conversation_short_id": r.conv_short_id or 0,
        "days": r.days or 0,
        "status": r.status or "active",
        "recover_days": r.recover_days or 0,
        "recover_need_days": r.recover_need_days or 0,
        "remark": r.remark or "",
    } for r in rows]
    out.sort(key=lambda c: (STATUS_RANK.get(c["status"], 1), -(c["days"] or 0)))
    return out


def last_synced_at(db: Session, account_id: int) -> datetime | None:
    """冷备最后一次刷新时间；没有数据返回 None（前端据此显示骨架屏）。"""
    return (db.query(func.max(Contact.last_synced_at))
              .filter(Contact.douyin_account_id == account_id)
              .scalar())


def enrich_names_and_avatars(user_id: int, account_id: int,
                             contacts: list[dict]) -> int:
    """给缺昵称/头像的联系人补齐，并写回 contacts.json 缓存。

    Why: 上游 parse 出来的只有 uid，昵称要另外查。缺了的话 nickname
    会回退成一长串 uid，用户看到的就是「1234567890123456」而不是「27」。
    返回补齐的条数；任何失败都静默跳过（补不到不影响主流程）。
    """
    import json
    import os
    import tempfile

    from .storage import AccountCtx, set_account_ctx
    import douyin_im as dy

    missing = [c for c in contacts
               if (c.get("nickname") or "") == c.get("uid") or not c.get("avatar")]
    if not missing:
        return 0

    filled = 0
    try:
        set_account_ctx(AccountCtx(user_id, account_id))
        session = dy._load_session()
        if not session:
            return 0
        captured = dy.fetch_nicknames(session, missing)
        if not captured:
            return 0

        # 合并写 contacts.json（douyin_im 的降级路径还在读它）—— 原子写
        cache_path = str(dy.CONTACTS_FILE)
        try:
            existing = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
        except Exception:
            existing = {}
        for uid, info in captured.items():
            prev = existing.get(uid) if isinstance(existing.get(uid), dict) else {}
            merged = dict(prev)
            for k in ("nick", "remark", "avatar"):
                if info.get(k):
                    merged[k] = info[k]
            existing[uid] = merged

        cache_dir = os.path.dirname(cache_path) or "."
        os.makedirs(cache_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=cache_dir, prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, cache_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        # 回填到本次要返回/入库的列表
        for c in contacts:
            info = captured.get(c.get("uid"))
            if not info:
                continue
            nick = (info.get("remark") or info.get("nick") or "").strip()
            if nick and nick != c.get("uid"):
                c["nickname"] = nick
                filled += 1
            if info.get("avatar"):
                c["avatar"] = info["avatar"]
    except Exception as e:
        print(f"[contacts] 补昵称/头像失败: {e}")
    return filled
