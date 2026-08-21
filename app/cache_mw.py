"""给所有动态响应打 no-store。

为什么必须有：
线上把域名挂在阿里云 ESA 后面，CDN 对 200 的 HTML 默认缓存 30 天
（`X-Swift-CacheTime: 2592000`），而且响应的 Vary 里只有 Accept-Encoding
—— **不按 Cookie 区分**。于是某个用户的已登录页面被缓存后原样发给其他人。

真实故障长这样，三个看起来毫不相干的现象其实是同一件事：
  1. admin 登录后看到的是 adong 的面板（缓存副本）
  2. 点「登录」扫码 → 404 Not Found
     （页面里 `{{ acc.id }}` 是渲染时写死的，指向 adong 的账号，
       admin 的 session 查不到这条 → qr_start 的所有权检查 raise 404）
  3. 短信验证码收到了但没有输入框
     （同理，qr/status 也 404，前端拿不到 waiting_sms_code 状态）

应用一个缓存头都不发 = 把策略交给中间层猜。这里显式声明清楚。

注意：改代码只能防住**以后**的缓存，CDN 里已经存下的旧副本必须去控制台
刷新才会消失。
"""
from __future__ import annotations

# 只有静态资源该被 CDN 缓存；其余全是按登录用户渲染的，一律不能存
CACHEABLE_PREFIXES = ("/static/", "/favicon.ico")

_NO_STORE = b"no-store, no-cache, must-revalidate, private, max-age=0"
_DROP = (b"cache-control", b"pragma", b"expires")


class NoStoreMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http"
                or scope.get("path", "").startswith(CACHEABLE_PREFIXES)):
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                message["headers"] = _rewrite(message.get("headers", []))
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _rewrite(headers) -> list[tuple[bytes, bytes]]:
    """换掉缓存头，并把 Cookie 并进 Vary。

    Vary 要**追加**不能覆盖：上游可能已经写了 Accept-Encoding，
    直接盖掉会让压缩和非压缩版本混用。
    """
    out: list[tuple[bytes, bytes]] = []
    vary_parts: list[bytes] = []
    for k, v in headers:
        lk = k.lower()
        if lk in _DROP:
            continue
        if lk == b"vary":
            vary_parts += [p.strip() for p in v.split(b",") if p.strip()]
            continue
        out.append((k, v))

    if not any(p.lower() == b"cookie" for p in vary_parts):
        vary_parts.append(b"Cookie")

    out.append((b"cache-control", _NO_STORE))
    out.append((b"pragma", b"no-cache"))
    out.append((b"expires", b"0"))
    out.append((b"vary", b", ".join(vary_parts)))
    return out
