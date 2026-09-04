"""正式镜像的自检 —— 在**镜像内部**跑，CI 挂载进去执行。

为什么必须有这个：
「docker run 能进 shell」不等于应用能跑。router import 失败、模板目录
没拷进来、node 包没装上，都能让镜像通过一个天真的 smoke test，
然后在用户机器上崩溃循环 —— 真发生过一次，是用户日志报上来才知道的。
所以这里把应用真起来，打 /healthz，再走一遍表单登录路径。

只用运行时依赖（uvicorn + 标准库）—— 镜像里没有 httpx/pytest。
"""
from __future__ import annotations

import http.cookiejar
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# 脚本是挂载到 /tmp 再执行的，sys.path[0] 会是 /tmp，镜像里的 /app 不在路径上。
# 把工作目录（镜像的 WORKDIR）放到最前面，否则 import app 直接 ModuleNotFound。
sys.path.insert(0, os.getcwd())

PORT = 18799
BASE = f"http://127.0.0.1:{PORT}"
BOOT_TIMEOUT = 40


def fail(msg: str) -> None:
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    # ① 所有 router 都能 import
    try:
        from app.routers import (admin, ai, api, auth,  # noqa: F401
                                 dashboard, login_flow)
    except Exception as e:
        fail(f"router import 失败: {type(e).__name__}: {e}")

    import app.main as m
    from app.db import init_db
    init_db()
    print(f"✓ 应用已加载，{len(m.app.routes)} 条路由")

    # ② 真起 uvicorn，确认进程能对外服务
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(
        m.app, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    deadline = time.monotonic() + BOOT_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if opener.open(f"{BASE}/healthz", timeout=2).status == 200:
                break
        except Exception:
            time.sleep(1)
    else:
        fail(f"{BOOT_TIMEOUT}s 内 /healthz 不通")
    print("✓ /healthz 正常")

    # ③ 表单路径 —— 崩溃那次挂的就是它（Form(...) 解析）
    try:
        opener.open(f"{BASE}/login", timeout=5)
    except Exception as e:
        fail(f"GET /login 失败: {e}")
    csrf = next((c.value for c in jar if c.name == "csrf"), "")
    if not csrf:
        fail("没拿到 csrf cookie，中间件可能没生效")

    body = urllib.parse.urlencode(
        {"username": "selfcheck", "password": "wrong", "csrf_token": csrf}).encode()
    req = urllib.request.Request(f"{BASE}/login", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("X-CSRF-Token", csrf)
    try:
        # 密码错会 302 回登录页；opener 默认跟随重定向，拿到 200 也算通
        opener.open(req, timeout=5)
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            fail(f"POST /login 返回 {e.code} —— Form 解析出问题")
    except Exception as e:
        fail(f"POST /login 失败: {type(e).__name__}: {e}")
    print("✓ 表单登录路径正常")

    print("✓ 镜像自检全部通过")


if __name__ == "__main__":
    main()
