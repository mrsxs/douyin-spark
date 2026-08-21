"""
FastAPI 主入口：app factory + 路由挂载 + scheduler 启动 + admin upsert
"""
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import text

from .config import settings
from .db import SessionLocal, init_db
from .models import User
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
            # 把 .env 里的名字改成某个已注册普通用户的名字，会在这里静默把他提权、
            # 并且每次重启都覆盖他的密码。这是「别的用户登录也是管理员」最隐蔽的一条路，
            # 所以必须在日志里喊出来，不能只是 commit 完事。
            if not user.is_admin:
                print(f"[bootstrap] ⚠️ 警告：ADMIN_USERNAME={settings.admin_username} "
                      f"命中了已存在的普通用户（id={user.id}），正把他提升为管理员并覆盖密码。"
                      f"如果这不是你要的，改 .env 里的 ADMIN_USERNAME 后重启，"
                      f"再执行 `python -m app.cli set-admin --username "
                      f"{settings.admin_username} --off` 撤销。")
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
        extra = db.query(User).filter(User.is_admin.is_(True),
                                      User.username != settings.admin_username).all()
        print(f"[bootstrap] 管理员已就绪: {settings.admin_username}")
        if extra:
            print(f"[bootstrap] ⚠️ 另有 {len(extra)} 个管理员账号: "
                  f"{', '.join(u.username for u in extra)} —— 若非本意，用 "
                  f"`python -m app.cli set-admin --username X --off` 撤销")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 放在最前：直接 `uvicorn app.main:app` 绕开 run.py 时也必须过闸门
    from .license import license_gate
    license_gate()
    init_db()
    ensure_admin()
    scheduler.start()
    yield
    scheduler.stop()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.site_name, lifespan=lifespan)

    # 限流（登录/注册/授权码兑换）——必须在挂路由前装好 state + handler
    from slowapi.errors import RateLimitExceeded
    from .ratelimit import limiter, rate_limit_handler
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

    # CSRF 中间件（纯 ASGI，能正确透传 body；必须在路由之前 add）
    from .csrf_mw import CSRFMiddleware
    app.add_middleware(CSRFMiddleware, secure_cookie=settings.cookie_secure)

    # 模板
    tmpl = Jinja2Templates(directory="templates")
    tmpl.env.globals["site_name"] = settings.site_name
    # tojson 默认 ensure_ascii=True，中文全变成 \uXXXX ——
    # 聊天记录整页都是中文，转义后体积翻好几倍，调试时也没法看
    tmpl.env.policies["json.dumps_kwargs"] = {"ensure_ascii": False,
                                              "sort_keys": False}
    app.state.tmpl = tmpl

    # 静态文件
    import os
    if os.path.isdir("static"):
        app.mount("/static", StaticFiles(directory="static"), name="static")

    # 路由
    from .routers import auth, dashboard, api, admin, login_flow, ai
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(admin.router)
    app.include_router(api.router)
    app.include_router(ai.router)
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
    from fastapi.responses import PlainTextResponse
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
        # /api/* 要的是 JSON —— 重定向会让前端 fetch 跟到 /login 拿回 HTML，
        # 再 r.json() 解析失败，用户只看到「非 JSON 响应」这种莫名其妙的报错。
        is_api = request.url.path.startswith("/api/")
        if code == 401:
            if is_api:
                return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
            return RedirectResponse("/login", status_code=302)
        if code == 402:
            if is_api:
                return JSONResponse({"ok": False, "error": "账户未激活或已过期"},
                                    status_code=402)
            return RedirectResponse("/activate", status_code=302)
        if code in (403, 404):
            if is_api:
                return JSONResponse({"ok": False, "error": exc.detail}, status_code=code)
            template = f"errors/{code}.html"
            user = None
            try:
                from .deps import SESSION_COOKIE, resolve_session_user
                from .db import SessionLocal as SL
                with SL() as db:
                    user = resolve_session_user(
                        db, request.cookies.get(SESSION_COOKIE))
                    if user:
                        db.expunge(user)
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

    def _get_site_cfg():
        """站点配置（域名 / 闲鱼链接）。模板里直接 site_cfg() 取。

        配置很少变，用短缓存避免每次渲染都查库。
        """
        import time as _t
        now = _t.monotonic()
        cached = getattr(app.state, "_site_cfg_cache", None)
        if cached and cached[0] > now:
            return cached[1]
        from .site_settings import load as _load_site
        try:
            with SessionLocal() as db:
                cfg = _load_site(db)
        except Exception:
            cfg = {"site_url": "", "xianyu_url": "", "xianyu_note": ""}
        app.state._site_cfg_cache = (now + 30, cfg)
        return cfg

    tmpl.env.globals["site_cfg"] = _get_site_cfg
    app.state.invalidate_site_cfg = lambda: setattr(app.state, "_site_cfg_cache", None)
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
