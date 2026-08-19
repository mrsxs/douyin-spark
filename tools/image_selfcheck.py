"""正式镜像的自检 —— 在**镜像内部**跑，CI 挂载进去执行。

为什么必须有这个：
镜像通过「编译成功」不等于能跑。Cython 默认把 PEP-484 注解当运行时类型
声明，FastAPI 的 `username: str = Form(...)` 会在模块 init 时抛
`TypeError: Expected str, got Form`，所有 router 一个都 import 不进来。
这样的镜像照样能通过「.so 是否存在」的检查，然后在客户机器上崩溃循环 ——
真发生过一次，是客户日志报上来才知道的。

为什么不直接 `docker run` 起容器打 /healthz：
正式镜像里 SKIP_LICENSE_CHECK 已被编译关闭，没有有效 LICENSE_KEY 就
拒绝启动，而私钥不能进 CI。所以这里在进程内把闸门标记置位后起 uvicorn，
只验「应用能不能正常服务」，不验授权（授权由另外两条检查覆盖）。

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
    # ① 所有 router 都能 import —— 这一条就能拦住 Form/Depends 那类编译不兼容
    from app import license as lic
    lic._LICENSE_OK = True          # 放行 csrf_mw 里的运行时断言
    # lifespan 里还会再调一次 license_gate()，没有有效 LICENSE_KEY 会 sys.exit(2)。
    # main.py 是在函数内 `from .license import license_gate`，所以打模块属性有效。
    # 这里只是让应用能起来跑自检；「授权是否真的生效」由 CI 的 ①② 两条覆盖。
    lic.license_gate = lambda: None

    try:
        from app.routers import (admin, ai, api, auth,  # noqa: F401
                                 dashboard, login_flow)
    except Exception as e:
        fail(f"router import 失败（编译不兼容）: {type(e).__name__}: {e}")

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
            fail(f"POST /login 返回 {e.code} —— Form 解析可能没编译对")
    except Exception as e:
        fail(f"POST /login 失败: {type(e).__name__}: {e}")
    print("✓ 表单登录路径正常（Form 解析没问题）")

    print("✓ 镜像自检全部通过")


if __name__ == "__main__":
    main()
