"""
业务入口：给一个 (user_id, account_id) 做一次"续火花"。
不依赖 HTTP context，可被 web 请求或 scheduler 调用。
"""
import contextlib as _contextlib
import json
import os
import random
import time
from datetime import datetime

import douyin_im as dy
from .storage import AccountCtx, set_account_ctx
from .db import SessionLocal
from .models import DouyinAccount, AuditLog, Schedule, JobRun, JobRunItem, Contact
from .notify import notify
# 模块级导入，不能只在函数里 import：_ensure_active 里也用到它。
# 少了这行在纯 Python 下只是被 try/except 吞掉的 NameError（头像永远补不上，
# dy_uid 也写不进去，静默失效）；而 cythonize 时是**编译错误**，
# 会让整个 .so 编译失败 —— Dockerfile 那步又吞了退出码，
# 结果就是镜像里一个 .so 都没有、全量源码明文发给客户。
from . import contacts_service


class NotReady(Exception):
    """账户尚未就绪（cookies 缺失、init_req 没抓到等）"""


class AlreadyRunning(Exception):
    """同账户已有另一个 auto_run 在执行"""


# 进程内账户级互斥：防 scheduler + /api/auto 并发触发同一个账户
import threading as _threading
_account_locks: dict[int, _threading.Lock] = {}
_account_locks_registry_lock = _threading.Lock()


def _get_account_lock(account_id: int) -> _threading.Lock:
    with _account_locks_registry_lock:
        lock = _account_locks.get(account_id)
        if lock is None:
            lock = _threading.Lock()
            _account_locks[account_id] = lock
        return lock


@_contextlib.contextmanager
def account_lock(account_id: int):
    """非阻塞地占住账户锁；抢不到就 yield False。

    给 AI 自动回复用：续火花批量任务正在跑的时候不该插进去发消息 ——
    两条链路同时打同一个抖音号，等于把发送频率翻倍，直接踩风控。
    抢不到就跳过这条回复，比排队等半分钟再发一条驴唇不对马嘴的回复要好。
    """
    lock = _get_account_lock(account_id)
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def _adaptive_interval(account_id: int, base: float = 5.0) -> tuple[float, str]:
    """基于最近 3 次 auto 任务的失败率，返回发送间隔 + 提示文字。

    - 失败率 <20%：base 秒（正常）
    - 20%-50%：3x base（有些慢）
    - >=50%：12x base（严重降速，避免持续触发风控）
    """
    with SessionLocal() as db:
        recent = (db.query(JobRun)
                    .filter(JobRun.douyin_account_id == account_id,
                            JobRun.kind.in_(("auto", "manual_batch")),
                            JobRun.status == "done")
                    .order_by(JobRun.started_at.desc())
                    .limit(3).all())
    total_sent = sum(r.sent for r in recent)
    total_failed = sum(r.failed for r in recent)
    denom = total_sent + total_failed
    if denom == 0:
        return base, "normal"
    rate = total_failed / denom
    if rate >= 0.5:
        return base * 12, f"high_risk ({int(rate*100)}% fail)"
    if rate >= 0.2:
        return base * 3, f"medium_risk ({int(rate*100)}% fail)"
    return base, "normal"


def _ensure_active(ctx: AccountCtx) -> tuple[dict, list[dict]]:
    """加载 cookies + 拉联系人；返回 (constants, contacts)"""
    # 设置线程 ctx，这样 douyin_im 的所有路径会路由到本账户目录
    set_account_ctx(ctx)
    if not os.path.exists(str(dy.COOKIE_FILE)):
        raise NotReady("cookies.json 不存在（未登录）")
    if not os.path.exists(str(dy.INIT_REQ_BIN)):
        raise NotReady("init_req.bin 不存在（登录时未抓到会话数据）")
    session = dy._load_session()
    if not session or not dy._check_login(session):
        raise NotReady("cookies 无效")
    constants = dy.extract_constants(str(dy.INIT_REQ_BIN))
    # 给 init 请求加会话数上限：不加的话抖音只回 ~25 个会话就截断，
    # 长期没互动的老火花（906 天那种）根本进不来。见 dy.INIT_CONV_LIMIT。
    # 响应会从 ~2.5MB 涨到 ~4.7MB，多花 1 秒左右，换完整列表值得。
    resp = session.post(
        "https://imapi.douyin.com/v1/message/get_message_by_init",
        data=dy.build_init_body(open(str(dy.INIT_REQ_BIN), "rb").read()),
        headers={**dy.HEADERS, "Content-Type": "application/x-protobuf"},
        timeout=30,
    )
    if resp.status_code != 200 or len(resp.content) < 1024:
        raise NotReady(f"get_message_by_init 失败: HTTP {resp.status_code}")
    # include_all：连一条火花标记都没有的普通好友也带出来（status="none"）。
    # 用户要「不管有没有火花都能选」，那些人只能从这条路进联系人列表；
    # 是否真给他们发消息，由 Schedule.send_to_broken 决定，和这里无关。
    contacts = dy.parse_fire_streaks(resp.content, include_all=True)
    # 诊断：统计 init 响应里所有私聊 conv_id 个数，对比解析出的各状态条目数
    try:
        import re as _re
        all_convs = set(m.group() for m in _re.finditer(rb'0:[12]:\d+:\d+', resp.content))
        _n_spark = sum(1 for c in contacts if c.get("status") != "none")
        dy._log(f"[ensure_active] init resp={len(resp.content)}B, "
                f"conv_ids={len(all_convs)} 个, 解析出联系人={len(contacts)} 个"
                f"（其中有火花痕迹 {_n_spark} 个）", "INFO")
    except Exception:
        pass
    # 顺手把 self uid + avatar 写回 DouyinAccount（dashboard 显示真实头像用）
    try:
        my_uid = dy.extract_my_uid(resp.content)
        if my_uid:
            with SessionLocal() as _db:
                from .models import DouyinAccount
                _acc = _db.get(DouyinAccount, ctx.account_id)
                changed = False
                if _acc and _acc.dy_uid != my_uid:
                    _acc.dy_uid = my_uid
                    changed = True
                if _acc and not _acc.avatar:
                    prof = dy.fetch_self_profile(session, my_uid)
                    if prof.get("avatar"):
                        _acc.avatar = contacts_service.normalize_avatar_url(prof["avatar"])
                        changed = True
                        if prof.get("nickname") and not _acc.nickname:
                            _acc.nickname = prof["nickname"]
                if changed:
                    _db.commit()
    except Exception as _e:
        print(f"[ensure_active] 写 self uid/avatar 失败: {_e}")
    # 顺手解析聊天消息入库。
    # init 响应里本来就带着每个会话最近 ~21 条消息 —— 复用同一份响应，
    # 不额外请求抖音。解析失败只丢消息，不能连累拉联系人这个主流程。
    try:
        _my_uid = dy.extract_my_uid(resp.content)
        _msgs = dy.parse_messages(resp.content, my_uid=_my_uid)
        if _msgs:
            from . import messages_service
            with SessionLocal() as _db:
                _added = messages_service.sync_messages(_db, ctx.account_id, _msgs)
                _db.commit()
            dy._log(f"[ensure_active] 消息同步: 解析 {len(_msgs)} 条，新增 {_added} 条", "INFO")
    except Exception as _e:
        print(f"[ensure_active] 消息同步失败（不影响联系人）: {_e}")
    # 合并缓存昵称
    try:
        cache = json.load(open(str(dy.CONTACTS_FILE)))
    except Exception:
        cache = {}
    for c in contacts:
        info = cache.get(c["uid"], {})
        if isinstance(info, dict):
            c["nickname"] = info.get("remark") or info.get("nick") or c["uid"]
            c["avatar"] = info.get("avatar") or ""
        else:
            c["nickname"] = info or c["uid"]
            c["avatar"] = ""
    return session, constants, contacts


def poll_new_messages(user_id: int, account_id: int) -> list[dict]:
    """实时轮询专用：只打一次 init 拿新消息，返回本次新增的那些。

    Why 不复用 _ensure_active：那条路会顺带补头像、拉昵称、upsert 联系人，
    每次都跑一遍太重 —— 轮询几秒一次，只需要消息。

    调用方（realtime 轮询线程）负责限频；这里不加锁，因为这是只读请求，
    和续火花发送互不冲突，拿发送锁反而会让聊天在跑任务时卡死。
    """
    ctx = AccountCtx(user_id=user_id, account_id=account_id)
    set_account_ctx(ctx)
    if not os.path.exists(str(dy.COOKIE_FILE)) or not os.path.exists(str(dy.INIT_REQ_BIN)):
        raise NotReady("账号未登录或缺少会话数据")
    session = dy._load_session()
    if not session:
        raise NotReady("cookies 无效")
    resp = session.post(
        "https://imapi.douyin.com/v1/message/get_message_by_init",
        data=open(str(dy.INIT_REQ_BIN), "rb").read(),
        headers={**dy.HEADERS, "Content-Type": "application/x-protobuf"},
        timeout=15,
    )
    if resp.status_code != 200 or len(resp.content) < 1024:
        raise NotReady(f"get_message_by_init 失败: HTTP {resp.status_code}")

    my_uid = dy.extract_my_uid(resp.content)
    msgs = dy.parse_messages(resp.content, my_uid=my_uid)
    if not msgs:
        return []
    from . import messages_service
    with SessionLocal() as db:
        added = messages_service.sync_and_collect(db, account_id, msgs)
        db.commit()
    return added


def _read_init_req() -> bytes:
    """读当前账户的 init_req.bin。调用前 set_account_ctx 必须已经设过。"""
    path = str(dy.INIT_REQ_BIN)
    if not os.path.exists(path):
        raise NotReady("init_req.bin 不存在（登录时未抓到会话数据）")
    with open(path, "rb") as f:
        return f.read()


def _resolve_my_uid(conv_id: str, peer_uid: str, account_dy_uid: str | None) -> str:
    """判断「我」的 uid —— parse_messages 靠它区分左右两边的气泡。

    Why 不用 dy.extract_my_uid：那个是从 init **响应**里按 uid 频次反推的，
    喂 init_req.bin（请求体，几百字节、没有会话列表）永远返回空串，
    结果整段历史全被判成对方发的，聊天页里挤成一边。

    conv_id 形如 0:<type>:<uid_a>:<uid_b>，去掉对方那个剩下的就是我 ——
    纯本地推导，不依赖频次也不依赖网络。格式不对时退回账户表里的 dy_uid。
    """
    parts = (conv_id or "").split(":")
    if len(parts) == 4:
        a, b = parts[2], parts[3]
        if a == str(peer_uid) and b != str(peer_uid):
            return b
        if b == str(peer_uid) and a != str(peer_uid):
            return a
    return str(account_dy_uid or "")


def backfill_history(user_id: int, account_id: int, uid: str,
                     max_pages: int = 40) -> dict:
    """把一个会话的历史消息从抖音云端拉回来，补进冷备表。

    Why: get_message_by_init 每个会话只回最近 ~21 条，两次同步之间聊得多一点，
    中间就是永久空档。cmd=301 带 cursor 能翻完整个会话，已经丢的也追得回来。

    只读接口 + 幂等入库（按 server_msg_id 去重），重复跑不会翻倍。
    conv_short_id 缺失时直接返回 error，不去打无效请求。
    """
    from . import messages_service

    ctx = AccountCtx(user_id=user_id, account_id=account_id)
    set_account_ctx(ctx)

    with SessionLocal() as db:
        row = (db.query(Contact)
                 .filter(Contact.douyin_account_id == account_id,
                         Contact.uid == str(uid))
                 .first())
        if not row:
            return {"added": 0, "error": f"联系人 {uid} 不在冷备表里"}
        conv_id, short_id = row.conv_id or "", row.conv_short_id
        nickname = row.nickname or uid
        acc = db.get(DouyinAccount, account_id)
        my_uid = _resolve_my_uid(conv_id, str(uid), acc.dy_uid if acc else None)

    if not short_id or not conv_id:
        # short_id 是 parse_fire_streaks 顺带解析的，老数据没有 ——
        # 刷新一次联系人就会补上，让用户知道该怎么办
        return {"added": 0,
                "error": f"{nickname} 缺会话 id，请先刷新一次联系人再回填"}
    if not my_uid:
        # my_uid 是 parse_messages 分左右两边的唯一依据。空串会让整段历史
        # 都判成对方发的 —— 宁可不回填，也不能静默灌一批躺错边的数据。
        return {"added": 0,
                "error": f"认不出你自己的 uid（{nickname} 的会话 id 异常），"
                         f"请先刷新一次联系人再回填"}

    session = dy._load_session()
    if not session:
        raise NotReady("cookies 无效")
    init_bytes = _read_init_req()

    msgs = dy.fetch_history(session, init_bytes, conv_id=conv_id,
                            conv_short_id=int(short_id),
                            my_uid=my_uid, max_pages=max_pages)
    if not msgs:
        return {"added": 0, "fetched": 0, "nickname": nickname}

    with SessionLocal() as db:
        added = messages_service.sync_messages(db, account_id, msgs)
        # 云端是权威：库里躺错边的历史（早期回填 my_uid 判错留下的）在这里纠正
        fixed = messages_service.fix_is_me(db, account_id, msgs)
        db.commit()
    dy._log(f"[history] {nickname}: 拉到 {len(msgs)} 条，新增 {added} 条"
            + (f"，纠正 {fixed} 条方向" if fixed else ""), "INFO")
    return {"added": added, "fetched": len(msgs), "fixed": fixed,
            "nickname": nickname}


def backfill_all(user_id: int, account_id: int, max_pages: int = 40) -> dict:
    """回填整个账号所有会话的历史。

    逐个会话串行跑，单个失败不影响其它 —— 回填一次要打几十上百个请求，
    中途因为一个会话抽风就整轮放弃，用户得从头再来。
    """
    with SessionLocal() as db:
        uids = [r.uid for r in db.query(Contact).filter(
            Contact.douyin_account_id == account_id,
            Contact.conv_short_id.isnot(None)).all()]

    total_added = done = failed = 0
    for uid in uids:
        try:
            r = backfill_history(user_id, account_id, uid, max_pages=max_pages)
            if r.get("error"):
                failed += 1
            else:
                total_added += r.get("added", 0)
                done += 1
        except Exception as e:
            failed += 1
            print(f"[history] 回填 uid={uid} 失败: {e}")
    dy._log(f"[history] 全量回填完成：{done}/{len(uids)} 个会话，"
            f"新增 {total_added} 条，失败 {failed}", "DONE")
    return {"contacts": done, "added": total_added, "failed": failed,
            "total": len(uids)}


def get_contacts(user_id: int, account_id: int) -> tuple[list[dict], dict]:
    """给 Web 页面用：返回 contacts + templates（模板从 DB 读）"""
    ctx = AccountCtx(user_id=user_id, account_id=account_id)
    _, _, contacts = _ensure_active(ctx)
    # 模板改从 DB 读；首次没数据时 fall back 到文件，之后 api 写入都进 DB
    from . import templates_service
    templates = templates_service.load_templates(account_id)
    if not templates:
        # DB 还没有迁移到 → 退回文件
        try:
            templates = json.load(open(str(dy.TEMPLATES_FILE)))
        except Exception:
            templates = {}
    # 同时 upsert 联系人缓存（跑一次就有 DB 快照，首屏直接读它秒开）
    try:
        from . import contacts_service
        with SessionLocal() as db:
            contacts_service.upsert_cache(db, account_id, contacts)
            db.commit()
    except Exception as e:
        print(f"[trigger] 联系人缓存 upsert 失败: {e}")
    return contacts, templates


def send_single(user_id: int, account_id: int, conv_id: str, contact: dict, text: str) -> bool:
    ctx = AccountCtx(user_id=user_id, account_id=account_id)
    session, constants, _ = _ensure_active(ctx)
    return dy.send_text(session, conv_id, text, contact, constants)


def send_to_uid(user_id: int, account_id: int, uid: str,
                text: str) -> tuple[bool, dict | None]:
    """按 uid 发一条消息，返回 (是否成功, 联系人)。

    和「get_contacts() 找人 → send_single() 发」相比只打一次 _ensure_active。
    那条路会拉两次联系人（各带一个 1.5MB 的 init 请求），手动发一条无所谓，
    但 AI 自动回复是常态化触发的，翻倍的请求量就是翻倍的风控面。
    """
    ctx = AccountCtx(user_id=user_id, account_id=account_id)
    session, constants, contacts = _ensure_active(ctx)
    contact = next((c for c in contacts if c["uid"] == str(uid)), None)
    if not contact:
        return False, None
    ok = dy.send_text(session, contact["conv_id"], text, contact, constants)
    return bool(ok), contact


def send_batch(user_id: int, account_id: int, uids: list[str], text: str,
               run_id: int | None = None) -> dict:
    """给一组 uid 发同一条消息。

    和 auto_run 共用账户锁 —— 两者都在打同一个抖音号，
    并发跑等于把发送频率翻倍，直接踩风控。
    """
    lock = _get_account_lock(account_id)
    if not lock.acquire(blocking=False):
        raise AlreadyRunning(f"账户 {account_id} 已有任务在执行，请等待上次完成")
    try:
        return _send_batch_locked(user_id, account_id, uids, text, run_id)
    finally:
        lock.release()


def _send_batch_locked(user_id: int, account_id: int, uids: list[str],
                       text: str, run_id: int | None) -> dict:
    if run_id is None:
        with SessionLocal() as db:
            run = JobRun(douyin_account_id=account_id, kind="manual_batch",
                         triggered_by="user", status="running", total=len(uids))
            db.add(run); db.commit(); db.refresh(run)
            run_id = run.id
    else:
        with SessionLocal() as db:
            r = db.get(JobRun, run_id)
            if r and not r.total:
                r.total = len(uids)
                db.commit()

    ctx = AccountCtx(user_id=user_id, account_id=account_id)
    session, constants, contacts = _ensure_active(ctx)
    by_uid = {c["uid"]: c for c in contacts}

    results, sent, failed = [], 0, 0
    interval, risk = _adaptive_interval(account_id, base=5.0)
    dy._log(f"[batch] send interval = {interval}s ({risk})", "RATE")

    for i, uid in enumerate(uids):
        c = by_uid.get(str(uid))
        if not c:
            failed += 1
            results.append({"uid": uid, "ok": False, "detail": "联系人不存在"})
            _write_item(run_id, str(uid), None, None, text, False, "联系人不存在")
            continue
        if i > 0:
            time.sleep(interval + random.uniform(-0.5, 0.5))
        try:
            ok = bool(dy.send_text(session, c["conv_id"], text, c, constants))
            detail = format_send_detail(dy.get_last_send_info())
        except Exception as e:
            ok, detail = False, f"exception: {e}"
        sent += 1 if ok else 0
        failed += 0 if ok else 1
        results.append({"uid": uid, "nickname": c.get("nickname"),
                        "ok": ok, "detail": detail})
        _write_item(run_id, str(uid), c.get("nickname"), c.get("conv_id"),
                    text, ok, detail)
        if ok:
            _record_sent_message(account_id, str(uid), c.get("conv_id"), text)

    with SessionLocal() as db:
        r = db.get(JobRun, run_id)
        if r:
            r.finished_at = datetime.utcnow()
            r.sent, r.failed, r.status = sent, failed, "done"
        db.add(AuditLog(actor_user_id=user_id, actor_kind="user",
                        action="send_batch", target_type="account",
                        target_id=str(account_id),
                        meta=json.dumps({"count": len(uids), "sent": sent,
                                         "failed": failed, "run_id": run_id})))
        db.commit()
    return {"sent": sent, "failed": failed, "results": results,
            "run_id": run_id}


def _write_item(run_id: int, uid: str, nickname: str | None,
                conv_id: str | None, message: str, ok: bool, detail: str) -> None:
    """每条独立提交 —— 进度查询靠 COUNT(JobRunItem) 得到实时 done 值。"""
    try:
        with SessionLocal() as db:
            db.add(JobRunItem(job_run_id=run_id, uid=uid, nickname=nickname,
                              conv_id=conv_id, message=message,
                              ok=ok, detail=(detail or "")[:1000]))
            db.commit()
    except Exception as e:
        print(f"[batch] 写 JobRunItem 失败: {e}")


def _record_sent_message(account_id: int, uid: str, conv_id: str | None,
                         text: str) -> None:
    """把刚发出去的消息写进聊天记录。

    Why: 抖音的 get_message_by_init 每个会话只回最近 ~21 条，续火花发的消息
    要等下次同步才捞得回来 —— 中间对方多聊几句就永久没了。真实数据里
    6 条成功续火花有 4 条不在聊天表。/api/send 和 AI 回复早就这么做了，
    auto_run 和 send_batch 一直漏着。

    写的是负数 server_msg_id 占位行，下次 init 同步时 messages_service._claim
    按 (peer_uid, text) 认领并换成抖音的真 id，不会留重复。

    附带动作：失败只记日志，绝不能连累发送主流程。
    """
    try:
        from . import messages_service
        with SessionLocal() as db:
            messages_service.append_local(db, account_id, uid, text, conv_id)
            db.commit()
    except Exception as e:
        print(f"[send] 写聊天记录失败 uid={uid}: {e}")


def format_send_detail(info: dict) -> str:
    """把 dy.get_last_send_info() 拼成人话（原在 routers/api.py）。"""
    if not info:
        return "无响应信息"
    if info.get("ok"):
        extras = info.get("extras") or {}
        if extras:
            return f"服务端返回 OK（extras={extras}）"
        return "服务端返回 OK"
    stage = info.get("stage")
    if stage == "no_private_key":
        return info.get("error") or "缺少私钥，需要重新扫码登录"
    if stage == "no_ticket":
        return info.get("error") or "联系人 ticket 缺失（init_req 漏抓），需重新登录"
    if stage == "exception":
        return f"请求异常: {info.get('error')}"
    parts = []
    http = info.get("http")
    if http is not None and http != 200:
        parts.append(f"HTTP {http}")
    msg = info.get("msg")
    if msg and msg != "OK":
        parts.append(f"message={msg}")
    edesc = info.get("error_desc")
    if edesc and edesc not in ("", "0"):
        parts.append(f"error_desc={edesc}")
    if info.get("extras"):
        parts.append(f"extras={info['extras']}")
    if info.get("parse_error"):
        parts.append(f"解析失败: {info['parse_error']}")
    return " | ".join(parts) or "未知失败（见后端日志）"


def auto_run(user_id: int, account_id: int, triggered_by: str = "scheduler",
             run_id: int | None = None) -> dict:
    """主流程：给所有启用的联系人按模板发一条。
    账户级互斥：同一 account_id 不允许并发执行（scheduler + /api/auto 同时触发）。

    run_id 已存在时复用该行（HTTP 层为了立刻把 id 返回给前端会先建好），
    否则自己创建 —— scheduler 走的就是后一条路径。
    """
    lock = _get_account_lock(account_id)
    if not lock.acquire(blocking=False):
        raise AlreadyRunning(f"账户 {account_id} 已有任务在执行，请等待上次完成")
    try:
        return _auto_run_locked(user_id, account_id, triggered_by, run_id)
    finally:
        lock.release()


def _auto_run_locked(user_id: int, account_id: int, triggered_by: str,
                     run_id: int | None = None) -> dict:
    # 先建 JobRun，确保任何异常都能被记账（便于 scheduler 判断"今日失败次数"）
    if run_id is None:
        with SessionLocal() as db:
            run = JobRun(douyin_account_id=account_id, kind="auto",
                         triggered_by=triggered_by, status="running")
            db.add(run); db.commit(); db.refresh(run)
            run_id = run.id

    try:
        ctx = AccountCtx(user_id=user_id, account_id=account_id)
        session, constants, contacts = _ensure_active(ctx)
    except Exception as e:
        # cookies 失效 / init_req 缺失等前置错误：立即把 JobRun 标 error
        try:
            with SessionLocal() as db:
                r = db.get(JobRun, run_id)
                if r:
                    r.status = "error"
                    r.finished_at = datetime.utcnow()
                    r.error = f"setup: {type(e).__name__}: {e}"[:500]
                    db.commit()
        except Exception:
            pass
        raise

    from . import templates_service
    templates = templates_service.load_templates(account_id)
    if not templates:
        try:
            templates = json.load(open(str(dy.TEMPLATES_FILE)))
        except Exception:
            templates = {}

    sent = skipped = failed = 0
    # 谁进发送集合：
    #   recovering —— 断过、正在重燃窗口期内。**永远发，不受开关管**：
    #                 窗口内没连够天数，原来那几百天就真没了，这是最急的一类
    #   active     —— 火花在烧，永远发
    #   broken     —— 火花断了，直接发消息就能续上 → send_to_broken，默认开
    #   none       —— 从来没火花的普通好友，属于主动搭讪 → send_to_no_spark，默认关
    with SessionLocal() as db:
        sch = db.query(Schedule).filter(
            Schedule.douyin_account_id == account_id).first()
        to_broken = bool(sch.send_to_broken) if sch else True
        to_no_spark = bool(sch.send_to_no_spark) if sch else False

    allowed = {"active", "recovering"}
    if to_broken:
        allowed.add("broken")
    if to_no_spark:
        allowed.add("none")

    excluded = [c for c in contacts if c.get("status", "active") not in allowed]
    if excluded:
        skipped += len(excluded)
        dy._log(f"[auto] 跳过 {len(excluded)} 个联系人"
                f"（send_to_broken={to_broken}, send_to_no_spark={to_no_spark}）", "INFO")
    contacts = [c for c in contacts if c.get("status", "active") in allowed]
    # 重燃中的排最前面发：任务被熔断/限速中断时，先保住最急的那批
    contacts.sort(key=lambda c: 0 if c.get("status") == "recovering" else 1)
    _n_rec = sum(1 for c in contacts if c.get("status") == "recovering")
    if _n_rec:
        dy._log(f"[auto] {_n_rec} 个重燃中的联系人排在最前发送", "INFO")
    # 回填进度分母：启动时还不知道要发几个（得先调抖音 API），
    # 前端在 total==0 期间显示「准备中」，拿到这里的值后才有百分比。
    try:
        with SessionLocal() as db:
            r = db.get(JobRun, run_id)
            if r:
                r.total = len(contacts)
                r.skipped = skipped
                db.commit()
    except Exception as _e:
        print(f"[auto] 回填 total 失败: {_e}")
    # 基于近期失败率自适应限速；随机 ±0.5s jitter 更像人工操作
    SEND_INTERVAL, _risk_level = _adaptive_interval(account_id, base=5.0)
    dy._log(f"[auto] send interval = {SEND_INTERVAL}s ({_risk_level})", "RATE")
    # 连续失败熔断：连续 5 次失败则停当前任务
    consecutive_fail = 0
    for i, c in enumerate(contacts):
        msg = dy._pick_message(c["uid"], c["nickname"], templates)
        if not msg:
            skipped += 1
            # skipped 联系人不入明细（量太大会影响 DB 体积）
            continue
        if i > 0:
            time.sleep(SEND_INTERVAL + random.uniform(-0.5, 0.5))
        item_ok = False
        item_detail = ""
        try:
            item_ok = dy.send_text(session, c["conv_id"], msg, c, constants)
            # 拿诊断详情（线程局部）
            info = dy.get_last_send_info()
            if item_ok:
                sent += 1
                consecutive_fail = 0
                # extras 里往往藏风控/限流码（OK 但被静默丢弃时的信号）
                _extras = info.get('extras') or {}
                item_detail = f"OK (msg={info.get('msg','')}" + (f" extras={_extras}" if _extras else "") + ")"
                dy._log(f"[auto] ✓ {c['nickname']}: {msg!r}", "SEND")
            else:
                failed += 1
                consecutive_fail += 1
                item_detail = (f"http={info.get('http')} msg={info.get('msg')} "
                               f"err={info.get('error_desc')} stage={info.get('stage')}")
                dy._log(f"[auto] ✗ {c['nickname']}: {item_detail}", "FAIL")
        except Exception as e:
            failed += 1
            consecutive_fail += 1
            item_detail = f"exception: {e}"
            dy._log(f"[auto] ✗ {c['nickname']}: {e}", "ERR")
        # 写明细
        try:
            with SessionLocal() as db:
                db.add(JobRunItem(
                    job_run_id=run_id, uid=c["uid"],
                    nickname=c.get("nickname"), conv_id=c.get("conv_id"),
                    message=msg, ok=item_ok, detail=item_detail[:1000],
                ))
                db.commit()
        except Exception as _e:
            print(f"[auto] 写 JobRunItem 失败: {_e}")
        # 发出去的也要进聊天记录
        if item_ok:
            _record_sent_message(account_id, c["uid"], c.get("conv_id"), msg)

        # 连续失败熔断：避免持续击打风控
        if consecutive_fail >= 5:
            dy._log(f"[auto] 连续 {consecutive_fail} 次失败，熔断当前任务", "CIRCUIT")
            try:
                with SessionLocal() as db:
                    run = db.get(JobRun, run_id)
                    if run:
                        run.error = f"circuit_breaker: {consecutive_fail} consecutive failures"
                        db.commit()
            except Exception:
                pass
            break

    summary = {"sent": sent, "skipped": skipped, "failed": failed,
               "risk": _risk_level, "interval": SEND_INTERVAL}
    dy._log(f"[auto] DONE {summary}", "DONE")

    # 更新 JobRun + 原有 DB 记录 + 失败时发站内通知
    # status：整次失败（0 条发出 + 有失败）→ error，否则 done
    final_status = "error" if (sent == 0 and failed > 0) else "done"
    with SessionLocal() as db:
        run = db.get(JobRun, run_id)
        if run:
            run.finished_at = datetime.utcnow()
            run.sent = sent; run.skipped = skipped; run.failed = failed
            run.status = final_status
            if final_status == "error" and not run.error:
                run.error = f"all_failed: sent=0 failed={failed}"
        acc = db.get(DouyinAccount, account_id)
        if acc:
            acc.last_run_at = datetime.utcnow()
        sch = db.query(Schedule).filter(Schedule.douyin_account_id == account_id).first()
        if sch:
            sch.last_result = json.dumps(summary)
            # 只在本次 auto_run 成功时标记"今日已跑"，避免失败后次日不重试
            if final_status == "done":
                sch.last_ran_date = datetime.now().strftime("%Y-%m-%d")
        db.add(AuditLog(actor_user_id=user_id, actor_kind="system",
                        action="auto_run", target_type="account",
                        target_id=str(account_id),
                        meta=json.dumps({**summary, "run_id": run_id})))

        # 通知：全失败 / 部分失败（仅站内通知，不每次都发邮件；
        # 达到当日重试上限时由 scheduler 统一发一封邮件提醒）
        acc_label = (acc.nickname or acc.label) if acc else f"账户#{account_id}"
        if failed > 0 and sent == 0 and (sent + failed) > 0:
            notify(db, user_id=user_id, kind="send_failed",
                   title=f"⚠️ {acc_label} 自动发送全部失败",
                   content=f"本次任务 {failed} 条全部失败。可能是 cookies 失效或被风控。",
                   url=f"/accounts/{account_id}/runs/{run_id}",
                   email=False)
        elif failed > 0:
            notify(db, user_id=user_id, kind="send_failed",
                   title=f"⚠️ {acc_label} 部分发送失败",
                   content=f"送达 {sent}，失败 {failed}。点击查看详情。",
                   url=f"/accounts/{account_id}/runs/{run_id}",
                   email=False)

        # 今日累计失败达到上限 → 发一次邮件通知（每账户每日最多一封）
        if final_status == "error":
            from .models import Notification
            # "今日"按本地自然日计算（与 scheduler._loop 保持一致）
            _now_local = datetime.now()
            _tz_offset = _now_local - datetime.utcnow()
            today_start_utc = _now_local.replace(hour=0, minute=0, second=0, microsecond=0) - _tz_offset
            error_today = (db.query(JobRun)
                             .filter(JobRun.douyin_account_id == account_id,
                                     JobRun.kind == "auto",
                                     JobRun.status == "error",
                                     JobRun.started_at >= today_start_utc)
                             .count())
            MAX_DAILY_RETRIES = 3
            if error_today >= MAX_DAILY_RETRIES:
                # 今日是否已发过 retry_exhausted 通知（同时也是邮件）
                already_sent = (db.query(Notification.id)
                                  .filter(Notification.user_id == user_id,
                                          Notification.kind == "retry_exhausted",
                                          Notification.created_at >= today_start_utc)
                                  .first())
                if not already_sent:
                    notify(db, user_id=user_id, kind="retry_exhausted",
                           title=f"🔴 {acc_label} 定时发送今日已失败 {error_today} 次，已停止重试",
                           content=(f"账户 {acc_label} 的定时任务今日累计失败 {error_today} 次，"
                                    f"已达到每日重试上限 {MAX_DAILY_RETRIES}，剩余时间不再自动重试。\n"
                                    "常见原因：cookies 失效 / 被风控 / 模板为空。\n"
                                    "请登录后台查看最近几次 JobRun 详情并排查。"),
                           url=f"/accounts/{account_id}/runs/{run_id}",
                           email=True)

        db.commit()
    return summary
