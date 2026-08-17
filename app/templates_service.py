"""
消息模板 DB 访问层：取代原本的 templates.json 文件读写。
- DB 是单一真相源（single source of truth）
- templates.json 作为降级备份同步写出（以防 DB 挂了 douyin_im 仍能读）
"""
from __future__ import annotations

import json
import os
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import MessageTemplate, DouyinAccount
from .storage import AccountCtx


def load_templates(account_id: int) -> dict:
    """返回与旧 templates.json 同结构的 dict：
    {uid: {"name": ..., "enabled": bool, "messages": [...]}, "default": {...}}
    """
    with SessionLocal() as db:
        rows = db.query(MessageTemplate).filter(
            MessageTemplate.douyin_account_id == account_id).all()
        out = {}
        for r in rows:
            try:
                msgs = json.loads(r.messages_json or "[]")
            except Exception:
                msgs = []
            out[r.uid] = {
                "name": r.name or r.uid,
                "enabled": bool(r.enabled),
                "messages": msgs,
            }
        return out


def upsert_template(db: Session, account_id: int, uid: str,
                    name: str | None = None,
                    enabled: bool | None = None,
                    messages: list[str] | None = None) -> MessageTemplate:
    row = (db.query(MessageTemplate)
             .filter(MessageTemplate.douyin_account_id == account_id,
                     MessageTemplate.uid == uid)
             .first())
    if not row:
        row = MessageTemplate(douyin_account_id=account_id, uid=uid)
        db.add(row)
    if name is not None:
        row.name = name
    if enabled is not None:
        row.enabled = bool(enabled)
    if messages is not None:
        row.messages_json = json.dumps(messages, ensure_ascii=False)
    return row


def sync_json_backup(account_id: int) -> None:
    """把 DB 里该账户的模板原子写到 templates.json（douyin_im 降级路径）"""
    try:
        with SessionLocal() as db:
            acc = db.get(DouyinAccount, account_id)
            if not acc:
                return
            ctx = AccountCtx(acc.user_id, acc.id)
        data = load_templates(account_id)
        tpl_path = os.path.join(ctx.dir(), "templates.json")
        os.makedirs(ctx.dir(), exist_ok=True)
        # 原子写：tmp + fsync + replace（避免并发写/崩溃破坏文件）
        import tempfile
        fd, tmp_path = tempfile.mkstemp(dir=ctx.dir(), prefix=".tmp_", suffix=".swap")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, tpl_path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        print(f"[tpl] 同步 JSON 备份失败: {e}")


def upsert_contact_cache(db: Session, account_id: int, contacts: list[dict]) -> None:
    """已迁移到 app/contacts_service.upsert_cache。

    保留此别名仅为兼容可能的外部调用；新代码请直接用 contacts_service，
    它会一并写入 avatar / status（首屏冷备渲染需要这两个字段）。
    """
    from . import contacts_service
    contacts_service.upsert_cache(db, account_id, contacts)
