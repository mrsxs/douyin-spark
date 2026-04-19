"""
FastAPI 主入口：app factory + 路由挂载 + scheduler 启动 + admin upsert
"""
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import text

from .config import settings
from .db import engine, SessionLocal, init_db
from .models import User
from .security import hash_password
from . import scheduler
from .deps import RedirectToLogin, RedirectToActivate


def ensure_admin():
    """启动时 upsert 管理员（根据 .env 配置）"""
    if not settings.admin_username or not settings.admin_password_hash:
        print("[bootstrap] 警告：.env 未配置 ADMIN_USERNAME / ADMIN_PASSWORD_HASH")
        return
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == settings.admin_username).first()
        if user:
            user.password_hash = settings.admin_password_hash
            user.is_admin = True
            user.is_active = True
            if user.expires_at is None or user.expires_at < datetime(2099, 1, 1):
                # 管理员永久有效
                user.expires_at = datetime(2099, 12, 31)
            user.max_accounts = max(user.max_accounts or 0, 100)
        else:
            user = User(
                username=settings.admin_username,
                password_hash=settings.admin_password_hash,
                is_admin=True, is_active=True,
                expires_at=datetime(2099, 12, 31),
                max_accounts=100,
            )
            db.add(user)
        db.commit()
        print(f"[bootstrap] 管理员已就绪: {settings.admin_username}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_admin()
    scheduler.start()
    yield
    scheduler.stop()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.site_name, lifespan=lifespan)

    # CSRF 中间件（纯 ASGI，能正确透传 body；必须在路由之前 add）
    from .csrf_mw import CSRFMiddleware
    app.add_middleware(CSRFMiddleware, secure_cookie=settings.cookie_secure)

    # 模板
    tmpl = Jinja2Templates(directory="templates")
    tmpl.env.globals["site_name"] = settings.site_name
    app.state.tmpl = tmpl

    # 静态文件
    import os
    if os.path.isdir("static"):
        app.mount("/static", StaticFiles(directory="static"), name="static")

    # 路由
    from .routers import auth, dashboard, api, admin, login_flow
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(admin.router)
    app.include_router(api.router)
    app.include_router(login_flow.router)

    # 健康检查
    @app.get("/healthz")
    def healthz():
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    # SEO: robots.txt
    from fastapi.responses import PlainTextResponse, Response
    @app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
    def robots(request: Request):
        base = f"{request.url.scheme}://{request.url.hostname}"
        if request.url.port and request.url.port not in (80, 443):
            base += f":{request.url.port}"
        return (
            "User-agent: *\n"
            "Allow: /\n"
            "Allow: /login\n"
            "Allow: /register\n"
            "Disallow: /admin/\n"
            "Disallow: /api/\n"
            "Disallow: /dashboard\n"
            "Disallow: /accounts/\n"
            "Disallow: /activate\n"
            f"\nSitemap: {base}/sitemap.xml\n"
        )

    # SEO: sitemap.xml（只暴露公开页）
    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap(request: Request):
        base = f"{request.url.scheme}://{request.url.hostname}"
        if request.url.port and request.url.port not in (80, 443):
            base += f":{request.url.port}"
        pages = [
            (f"{base}/login",     "1.0", "weekly"),
            (f"{base}/register",  "0.8", "weekly"),
        ]
        urls = "\n".join(
            f"  <url><loc>{loc}</loc><priority>{pri}</priority>"
            f"<changefreq>{freq}</changefreq></url>"
            for loc, pri, freq in pages
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{urls}\n"
            "</urlset>\n"
        )
        return Response(content=xml, media_type="application/xml")

    # 异常处理：redirect 异常
    @app.exception_handler(RedirectToLogin)
    async def _rl(request: Request, exc: RedirectToLogin):
        return RedirectResponse("/login", status_code=302)

    @app.exception_handler(RedirectToActivate)
    async def _ra(request: Request, exc: RedirectToActivate):
        return RedirectResponse("/activate", status_code=302)

    # 友好 404/403/500 页面
    @app.exception_handler(StarletteHTTPException)
    async def _he(request: Request, exc: StarletteHTTPException):
        code = exc.status_code
        if code in (401, ):
            return RedirectResponse("/login", status_code=302)
        if code in (402, ):
            return RedirectResponse("/activate", status_code=302)
        if code in (403, 404):
            template = f"errors/{code}.html"
            user = None
            try:
                from .deps import SESSION_COOKIE
                from .security import read_session
                from .db import SessionLocal as SL
                uid = read_session(request.cookies.get(SESSION_COOKIE))
                if uid:
                    with SL() as db:
                        user = db.query(User).filter(User.id == uid).first()
            except Exception:
                pass
            return app.state.tmpl.TemplateResponse(
                template, {"request": request, "user": user, "detail": exc.detail},
                status_code=code,
            )
        return JSONResponse({"detail": exc.detail}, status_code=code)

    # （user 注入 + csrf 校验全部由 CSRFMiddleware 处理，避免 BaseHTTPMiddleware 消耗 body）

    # 模板 globals
    def _get_user_from_request(request: Request):
        return getattr(request.state, "user", None)
    def _get_unread_count(request: Request):
        return getattr(request.state, "unread_notifications", 0)
    def _get_csrf(request: Request):
        return getattr(request.state, "csrf_token", "")
    tmpl.env.globals["get_current_user"] = _get_user_from_request
    tmpl.env.globals["get_unread_notifications"] = _get_unread_count
    tmpl.env.globals["csrf_token"] = _get_csrf

    # 适配新版 starlette 的 TemplateResponse(request, name, context, ...) 签名
    # 让旧写法 TemplateResponse("name", {...}) 仍然工作 + 自动注入 user
    original_template_response = tmpl.TemplateResponse

    def patched_template_response(*args, **kwargs):
        # 解析参数
        if args and isinstance(args[0], Request):
            request = args[0]; name = args[1]; context = args[2] if len(args) > 2 else (kwargs.pop("context", None) or {})
        else:
            name = args[0]; context = args[1] if len(args) > 1 else (kwargs.pop("context", None) or {})
            request = context.get("request") if isinstance(context, dict) else None
        # 注入 user
        if isinstance(context, dict) and request is not None and "user" not in context:
            context["user"] = getattr(request.state, "user", None)
        # 用新签名调用
        if request is not None:
            return original_template_response(request, name, context, **kwargs)
        return original_template_response(name, context, **kwargs)

    tmpl.TemplateResponse = patched_template_response

    return app


app = create_app()
