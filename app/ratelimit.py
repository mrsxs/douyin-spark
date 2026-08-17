"""登录 / 注册 / 授权码兑换的限流。

背景：这三个端点原本毫无限流（slowapi 装了却从没用过）。
授权码是 [A-Z0-9]{8,24}，对按码售卖的产品来说可被枚举；
登录端点则可被撞库和针对单账号爆破。

存储用 slowapi 的内存后端 —— 本项目单进程、无 Redis，够用。
多副本部署时限流是按进程算的，需要换共享存储。
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# 各端点的配额。写在一处便于调整，也方便测试引用同一常量。
LOGIN_LIMIT = "10/minute"
REGISTER_LIMIT = "5/minute"
ACTIVATE_LIMIT = "10/minute"


def _client_key(request: Request) -> str:
    """优先取反代传来的真实 IP。

    注意：X-Forwarded-For 是客户端可伪造的头，只有在可信反代后面才有意义。
    直接暴露公网时应关掉这个分支，否则攻击者换 header 就能绕过限流。
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=_client_key, headers_enabled=True)


async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """超限响应：API 返回 JSON，表单页重定向回去带 error 参数。"""
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"ok": False, "error": "操作过于频繁，请稍后再试"}, status_code=429)
    return RedirectResponse(f"{request.url.path}?error=rate_limited",
                            status_code=302)


def reset() -> None:
    """清空计数 —— 仅供测试使用，避免用例之间互相干扰。"""
    limiter.reset()
