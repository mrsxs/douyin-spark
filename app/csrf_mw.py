"""
CSRF 防护 ASGI 中间件。

必须是纯 ASGI middleware（不是 BaseHTTPMiddleware），否则读取 body 后
无法正确把 body 透传给下游 route handler — FastAPI 的 Form(...) 依赖会拿不到字段。

流程：
  1. 非 GET/HEAD/OPTIONS 请求 → 读完 ASGI body 消息
  2. 先尝试 X-CSRF-Token header；若是 form-urlencoded 再从 body 解 csrf_token
  3. 和 cookie 里的 csrf 对照（double-submit）+ itsdangerous 签名校验
  4. 校验通过 → 用 body bytes 构造新的 receive 协程交给下游
  5. 失败 → 返回 403（HTML 或 JSON 按 Accept 分流）
"""
from __future__ import annotations

import html
from urllib.parse import parse_qs, urlparse

from .security import issue_csrf, verify_csrf, constant_time_equals


def _safe_referer(referer: str) -> str:
    """把跨域/脏 Referer 净化为安全的站内相对路径。
    - 空或 None → "/"
    - 含 scheme/netloc（跨域）→ "/"
    - 不以 / 开头 → "/"
    - 通过则 html.escape，避免属性注入
    """
    if not referer:
        return "/"
    try:
        parsed = urlparse(referer)
        if parsed.scheme or parsed.netloc:
            return "/"
        path = parsed.path or "/"
        if not path.startswith("/"):
            return "/"
        # 限制长度避免被塞长字符串
        safe = path[:300]
        if parsed.query:
            safe += "?" + parsed.query[:200]
        return html.escape(safe, quote=True)
    except Exception:
        return "/"


import time as _time

CSRF_COOKIE = "csrf"
EXEMPT_PATHS = ("/login", "/register", "/logout", "/healthz",
                "/robots.txt", "/sitemap.xml")
# 不需要注入 user / 查未读数的静态路径（减少每请求 DB 负担）
NO_USER_INJECT_PREFIXES = ("/static/", "/favicon.ico", "/robots.txt",
                           "/sitemap.xml", "/healthz")
MAX_BODY_BYTES = 2 * 1024 * 1024   # 2 MiB：CSRF middleware 缓存 body 的上限

# 未读通知数短期内存缓存：uid → (expire_ts, count)
_UNREAD_CACHE: dict[int, tuple[float, int]] = {}
_UNREAD_TTL = 15   # 秒；点击通知铃铛会主动 fetch /api/notifications 立即更新


def invalidate_unread_cache(user_id: int) -> None:
    """外部在改读状态后可以调用这个让缓存立即失效"""
    _UNREAD_CACHE.pop(user_id, None)


def _parse_cookie(cookie_header: str) -> dict[str, str]:
    out = {}
    for piece in (cookie_header or "").split(";"):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        k, _, v = piece.partition("=")
        out[k.strip()] = v.strip()
    return out


def _csrf_error_response(is_form_html: bool, referer: str):
    """返回 ASGI-compatible 响应字典：(status, headers, body)"""
    if is_form_html:
        html_body = (
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>会话已过期</title>"
            "<style>body{font-family:system-ui;max-width:480px;margin:80px auto;"
            "padding:32px;background:#fff1f2;border-radius:16px;border:1px solid #fecdd3;"
            "color:#881337}h1{margin:0 0 12px;font-size:20px}p{margin:8px 0;line-height:1.6}"
            "a{display:inline-block;margin-top:20px;padding:10px 20px;"
            "background:#4f46e5;color:#fff;border-radius:8px;text-decoration:none}"
            "a.sec{background:#e2e8f0;color:#1e293b;margin-left:8px}</style>"
            "<h1>⚠️ 会话已过期</h1>"
            "<p>你的页面缓存过旧，或 CSRF 令牌已失效。</p>"
            "<p>这通常发生在：页面打开时间过长、或最近更新了登录方式。"
            "请返回上一页并<b>硬刷新（Cmd+Shift+R / Ctrl+Shift+R）</b>后重试。</p>"
            f"<a href='{_safe_referer(referer)}'>返回上一页</a>"
            "<a class='sec' href='/'>回到首页</a>"
            "</html>"
        )
        return 403, [(b"content-type", b"text/html; charset=utf-8")], html_body.encode()
    body = b'{"ok":false,"error":"CSRF token \\u65e0\\u6548\\u6216\\u8fc7\\u671f\\uff0c\\u8bf7\\u5237\\u65b0\\u9875\\u9762"}'
    return 403, [(b"content-type", b"application/json; charset=utf-8")], body


class CSRFMiddleware:
    """顺带做 user + unread_notifications 注入，避免和 BaseHTTPMiddleware 堆叠导致 body 被消耗"""
    def __init__(self, app, secure_cookie: bool = False):
        self.app = app
        self.secure_cookie = secure_cookie

    async def _inject_user(self, scope):
        """把 request.state.user / unread_notifications 设好。
        静态/健康检查路径直接跳过避免每请求查 DB。
        未读数使用 15s 短缓存，大幅减少 Notification count 查询。
        """
        from .security import read_session
        from .deps import SESSION_COOKIE
        from .db import SessionLocal
        from .models import User, Notification

        scope.setdefault("state", {})
        scope["state"]["user"] = None
        scope["state"]["unread_notifications"] = 0

        # 静态/健康检查：不查 DB
        path = scope.get("path", "")
        if any(path.startswith(p) for p in NO_USER_INJECT_PREFIXES):
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        cookies = _parse_cookie(headers.get("cookie", ""))
        session_val = cookies.get(SESSION_COOKIE)
        if not session_val:
            return
        try:
            uid = read_session(session_val)
            if not uid:
                return
            with SessionLocal() as db:
                u = db.query(User).filter(User.id == uid,
                                          User.is_active == True).first()
                if not u:
                    return
                scope["state"]["user"] = u
                db.expunge(u)
                # 未读数缓存：命中则直接用，避免频繁 count
                now_ts = _time.time()
                cached = _UNREAD_CACHE.get(uid)
                if cached and cached[0] > now_ts:
                    scope["state"]["unread_notifications"] = cached[1]
                else:
                    count = (db.query(Notification)
                               .filter(Notification.user_id == uid,
                                       Notification.read_at.is_(None))
                               .count())
                    _UNREAD_CACHE[uid] = (now_ts + _UNREAD_TTL, count)
                    scope["state"]["unread_notifications"] = count
        except Exception:
            pass

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 先做 user 注入 — 这样下游路由即使是 GET 也能拿到 request.state.user
        await self._inject_user(scope)

        method = scope["method"]
        path   = scope["path"]

        # ── 读 header / cookie ──
        raw_headers = scope.get("headers", [])
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in raw_headers}
        cookies = _parse_cookie(headers.get("cookie", ""))
        csrf_cookie = cookies.get(CSRF_COOKIE, "")

        # ── 确保 cookie 存在，下发新的 ──
        issue_new_cookie = False
        if not csrf_cookie or not verify_csrf(csrf_cookie):
            csrf_cookie = issue_csrf()
            issue_new_cookie = True

        # ── GET / 豁免路径 / Websocket：直接透传（可能夹带 set-cookie） ──
        exempt = (
            method in ("GET", "HEAD", "OPTIONS")
            or any(path == p or path.startswith(p + "/") for p in EXEMPT_PATHS)
        )

        # 包装 send 以便首次响应时附加 set-cookie
        sent_set_cookie = [False]
        async def send_wrapper(message):
            if (issue_new_cookie and not sent_set_cookie[0]
                    and message["type"] == "http.response.start"):
                hdrs = list(message.get("headers", []))
                secure_attr = "; Secure" if self.secure_cookie else ""
                cookie_val = (f"{CSRF_COOKIE}={csrf_cookie}; Path=/; Max-Age=3600; "
                              f"SameSite=Strict{secure_attr}").encode("latin-1")
                hdrs.append((b"set-cookie", cookie_val))
                message["headers"] = hdrs
                sent_set_cookie[0] = True
            await send(message)

        if exempt:
            # 仍然挂上 request.state.csrf_token 给模板用 — ASGI 中间件通过 scope
            scope.setdefault("state", {})
            scope["state"]["csrf_token"] = csrf_cookie
            await self.app(scope, receive, send_wrapper)
            return

        # ── 非豁免写请求：读 body + 校验 ──
        ctype = headers.get("content-type", "")
        is_form = "application/x-www-form-urlencoded" in ctype

        body_chunks = []
        total_size = 0
        more_body = True
        while more_body:
            msg = await receive()
            if msg["type"] == "http.request":
                chunk = msg.get("body", b"")
                total_size += len(chunk)
                if total_size > MAX_BODY_BYTES:
                    await send({"type": "http.response.start", "status": 413,
                                "headers": [(b"content-type",
                                             b"application/json; charset=utf-8")]})
                    await send({"type": "http.response.body",
                                "body": b'{"ok":false,"error":"Payload too large"}'})
                    return
                body_chunks.append(chunk)
                more_body = msg.get("more_body", False)
            elif msg["type"] == "http.disconnect":
                return
        body_bytes = b"".join(body_chunks)

        # 取 token
        token = headers.get("x-csrf-token", "")
        if not token and is_form and body_bytes:
            try:
                parsed = parse_qs(body_bytes.decode("utf-8"))
                vals = parsed.get("csrf_token", [])
                token = vals[0] if vals else ""
            except Exception:
                token = ""

        # 校验：token 本身合法 + 和 cookie 相等
        ok = (bool(token) and verify_csrf(token)
              and constant_time_equals(token, csrf_cookie))

        if not ok:
            accept = headers.get("accept", "")
            is_html_form = is_form and "text/html" in accept
            referer = headers.get("referer", "/")
            status, err_headers, err_body = _csrf_error_response(is_html_form, referer)
            # 即使错误也下发 csrf cookie（下一次请求用）
            if issue_new_cookie:
                secure_attr = "; Secure" if self.secure_cookie else ""
                err_headers = list(err_headers) + [(
                    b"set-cookie",
                    (f"{CSRF_COOKIE}={csrf_cookie}; Path=/; Max-Age=3600; "
                     f"SameSite=Strict{secure_attr}").encode("latin-1"),
                )]
            await send({"type": "http.response.start", "status": status,
                        "headers": err_headers})
            await send({"type": "http.response.body", "body": err_body})
            return

        # ── 校验通过：重放 body 给下游 ──
        # 把 scope.state 里的 token 暴露给模板（main.py 的 tmpl globals 读这里）
        scope.setdefault("state", {})
        scope["state"]["csrf_token"] = csrf_cookie

        replay_sent = [False]
        async def receive_replay():
            if replay_sent[0]:
                return {"type": "http.disconnect"}
            replay_sent[0] = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        await self.app(scope, receive_replay, send_wrapper)
