#!/usr/bin/env python3
"""
启动入口：python3 run.py [--host HOST] [--port PORT]
"""
import argparse
import os
import uvicorn


def main():
    # License 检查（容器/生产环境强校验；设 SKIP_LICENSE_CHECK=1 跳过，仅供开发）
    if os.environ.get("SKIP_LICENSE_CHECK") not in ("1", "true", "True"):
        from app.license import verify_license_or_exit
        verify_license_or_exit()

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
