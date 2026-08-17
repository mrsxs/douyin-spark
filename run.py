#!/usr/bin/env python3
"""
启动入口：python3 run.py [--host HOST] [--port PORT]
"""
import argparse
import uvicorn


def main():
    # License 校验统一走 app.license.license_gate()
    # （app.main 的 lifespan 也会调一次，幂等；覆盖 CMD 绕开 run.py 同样挡得住）
    from app.license import license_gate
    license_gate()

    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--reload", action="store_true", help="开发模式自动重载")
    args = p.parse_args()

    print(f"启动 Web 面板: http://{args.host}:{args.port}")
    uvicorn.run(
        "app.main:app",
        host=args.host, port=args.port,
        reload=args.reload,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
