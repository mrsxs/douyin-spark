"""
FastAPI deps: current_user / require_user / require_admin / csrf 校验
"""
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from .db import get_db
from .models import User, DouyinAccount
from .security import read_session_full


SESSION_COOKIE = "dy_session"


def resolve_session_user(db: Session, cookie_val: str | None) -> User | None:
    """session cookie → User 的唯一入口。

    统一在这里做三件事，避免各调用点漏掉其中某一项：
      1. 验签 + 过期
      2. 用户存在且未被停用
      3. session_version 匹配（改密码/重置后旧 cookie 立即失效）
    """
    parsed = read_session_full(cookie_val)
    if not parsed:
        return None
    uid, ver = parsed
    user = db.query(User).filter(User.id == uid, User.is_active == True).first()
    if not user:
        return None
    if (user.session_version or 0) != ver:
        return None
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """读取 session cookie → 返回 User（未登录则 None）"""
    return resolve_session_user(db, request.cookies.get(SESSION_COOKIE))


def require_user(user: User | None = Depends(current_user)) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def get_account_owned(
    account_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> DouyinAccount:
    """获取属于当前用户的抖音账户（否则 404）"""
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id,
        DouyinAccount.user_id == user.id,
    ).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账户不存在或无权访问")
    return acc


# ── Page-level redirect deps（HTML 路由用，未登录重定向）──

class RedirectToLogin(Exception): pass


def page_current_user(request: Request, db: Session = Depends(get_db)):
    return resolve_session_user(db, request.cookies.get(SESSION_COOKIE))


def page_require_user(user = Depends(page_current_user)):
    if not user:
        raise RedirectToLogin()
    return user


def page_require_admin(user = Depends(page_require_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
