"""用户授权码（LicenseCode）兑换业务。

与 `app/license.py` 区分：那个是部署用的 License Key（RSA 签名、启动时验），
这里是用户注册后在 /activate 兑换时长与账号配额的短码。

兑换必须是原子的：按码售卖的产品，一码多用 = 直接收入漏损。
SQLite 不支持 SELECT ... FOR UPDATE（SQLAlchemy dialect 静默忽略），
所以用条件 UPDATE + rowcount 判定是否抢到，而不是「先查后写」。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from .models import AuditLog, LicenseCode, User

CODE_RE = re.compile(r"^[A-Z0-9]{8,24}$")

# 兑换结果 → 前端 ?error= 文案键
OK = "ok"
ERR_FORMAT = "code_format"
ERR_NOT_FOUND = "code_not_found"
ERR_REVOKED = "code_revoked"
ERR_USED = "code_used"
ERR_NO_USER = "user_not_found"


def _diagnose(db: Session, code: str) -> str:
    """没抢到时回查一次，给用户一个准确原因。"""
    lc = db.query(LicenseCode).filter(LicenseCode.code == code).first()
    if lc is None:
        return ERR_NOT_FOUND
    if lc.revoked_at is not None:
        return ERR_REVOKED
    return ERR_USED


def redeem_code(db: Session, user_id: int, code: str,
                ip: str | None = None) -> str:
    """兑换授权码。返回 OK 或某个 ERR_* 常量；只有 OK 时才写入用户权益。"""
    code = (code or "").strip().upper()
    if not CODE_RE.match(code):
        return ERR_FORMAT

    user = db.get(User, user_id)
    if user is None:
        return ERR_NO_USER

    now = datetime.utcnow()
    try:
        # 原子占用：并发时只有一个事务能把 used_by 从 NULL 改成 user_id，
        # 其余的 WHERE 匹配不到行 → rowcount == 0
        result = db.execute(
            update(LicenseCode)
            .where(
                LicenseCode.code == code,
                LicenseCode.used_by.is_(None),
                LicenseCode.revoked_at.is_(None),
            )
            .values(used_by=user_id, used_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            return _diagnose(db, code)

        lc = db.query(LicenseCode).filter(LicenseCode.code == code).one()
        # 未过期则从原到期日往后叠加，避免续期时吞掉剩余天数
        base = user.expires_at if (user.expires_at and user.expires_at > now) else now
        user.expires_at = base + timedelta(days=lc.duration_days)
        # 配额只升不降：兑换小额度码不该把已有配额改小
        user.max_accounts = max(user.max_accounts or 0, lc.max_accounts)

        db.add(AuditLog(
            actor_user_id=user_id, actor_kind="user", action="activate_code",
            target_type="license_code", target_id=str(lc.id),
            meta=f"+{lc.duration_days}d, max={lc.max_accounts}",
            ip=ip,
        ))
        db.commit()
        return OK
    except Exception:
        db.rollback()
        raise
