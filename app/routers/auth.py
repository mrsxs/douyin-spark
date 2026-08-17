"""
注册 / 登录 / 登出 / 授权码兑换
"""
import re
from datetime import datetime
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User, AuditLog
from ..security import hash_password, verify_password, issue_session
from ..deps import SESSION_COOKIE, page_current_user, page_require_user
from ..config import settings
from ..codes_service import redeem_code
from ..ratelimit import limiter, LOGIN_LIMIT, REGISTER_LIMIT, ACTIVATE_LIMIT

router = APIRouter()


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


# ── 页面 ──────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user=Depends(page_current_user)):
    if user:
        return RedirectResponse("/", status_code=302)
    return request.app.state.tmpl.TemplateResponse(
        "auth/login.html", {"request": request, "error": request.query_params.get("error")})


@router.post("/login")
@limiter.limit(LOGIN_LIMIT)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash) or not user.is_active:
        return RedirectResponse("/login?error=1", status_code=302)
    user.last_login_at = datetime.utcnow()
    db.add(AuditLog(actor_user_id=user.id, actor_kind="user", action="login",
                    ip=request.client.host if request.client else None))
    db.commit()
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE, issue_session(user.id, user.session_version or 0),
        max_age=settings.session_max_age,
        httponly=True, samesite="strict",
        secure=settings.cookie_secure,
        path="/",
    )
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, user=Depends(page_current_user)):
    if user:
        return RedirectResponse("/", status_code=302)
    return request.app.state.tmpl.TemplateResponse(
        "auth/register.html", {"request": request, "error": request.query_params.get("error")})


@router.post("/register")
@limiter.limit(REGISTER_LIMIT)
def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    email: str = Form(""),
    db: Session = Depends(get_db),
):
    # 输入校验
    if not USERNAME_RE.match(username):
        return RedirectResponse("/register?error=username_format", status_code=302)
    if len(password) < 6:
        return RedirectResponse("/register?error=password_too_short", status_code=302)
    if password != password2:
        return RedirectResponse("/register?error=password_mismatch", status_code=302)
    if db.query(User).filter(User.username == username).first():
        return RedirectResponse("/register?error=username_taken", status_code=302)
    if email and db.query(User).filter(User.email == email).first():
        return RedirectResponse("/register?error=email_taken", status_code=302)

    user = User(
        username=username, email=email or None,
        password_hash=hash_password(password),
        expires_at=None, max_accounts=0,
    )
    db.add(user); db.commit(); db.refresh(user)
    db.add(AuditLog(actor_user_id=user.id, actor_kind="user", action="register",
                    ip=request.client.host if request.client else None))
    db.commit()

    resp = RedirectResponse("/activate", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE, issue_session(user.id, user.session_version or 0),
        max_age=settings.session_max_age,
        httponly=True, samesite="strict",
        secure=settings.cookie_secure, path="/",
    )
    return resp


# ── 授权码兑换 ────────────────────────────────────────────────────────

@router.get("/activate", response_class=HTMLResponse)
def activate_page(request: Request, user=Depends(page_require_user)):
    now = datetime.utcnow()
    return request.app.state.tmpl.TemplateResponse("auth/activate.html", {
        "request": request,
        "user": user,
        "now": now,
        "is_active": bool(user.expires_at and user.expires_at > now),
        "error": request.query_params.get("error"),
        "ok": request.query_params.get("ok"),
    })


@router.post("/activate")
@limiter.limit(ACTIVATE_LIMIT)
def activate_submit(
    request: Request,
    code: str = Form(...),
    user = Depends(page_require_user),
    db: Session = Depends(get_db),
):
    result = redeem_code(
        db, user.id, code,
        ip=request.client.host if request.client else None,
    )
    if result != "ok":
        return RedirectResponse(f"/activate?error={result}", status_code=302)
    return RedirectResponse("/activate?ok=1", status_code=302)
