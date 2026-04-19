"""
FastAPI deps: current_user / require_active / require_admin / csrf 校验
"""
from datetime import datetime
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from .db import get_db
from .models import User, DouyinAccount
from .security import read_session


SESSION_COOKIE = "dy_session"


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """读取 session cookie → 返回 User（未登录则 None）"""
    uid = read_session(request.cookies.get(SESSION_COOKIE))
    if not uid:
        return None
    return db.query(User).filter(User.id == uid, User.is_active == True).first()


def require_user(user: User | None = Depends(current_user)) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def require_active(user: User = Depends(require_user)) -> User:
    """要求用户已激活（expires_at 在未来）"""
    if not user.expires_at or user.expires_at < datetime.utcnow():
        raise HTTPException(status_code=402, detail="账户未激活或已过期")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def get_account_owned(
    account_id: int,
    user: User = Depends(require_active),
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
class RedirectToActivate(Exception): pass


def page_current_user(request: Request, db: Session = Depends(get_db)):
    uid = read_session(request.cookies.get(SESSION_COOKIE))
    if not uid:
        return None
    return db.query(User).filter(User.id == uid, User.is_active == True).first()


def page_require_user(user = Depends(page_current_user)):
    if not user:
        raise RedirectToLogin()
    return user


def page_require_active(user = Depends(page_require_user)):
    if not user.expires_at or user.expires_at < datetime.utcnow():
        raise RedirectToActivate()
    return user


def page_require_admin(user = Depends(page_require_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
