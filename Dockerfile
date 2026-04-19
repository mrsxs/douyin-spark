# ─── 阶段 1：Cython 编译 ───────────────────────────────────────────
FROM python:3.13-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 先装依赖（用于 cythonize + 运行时）
COPY requirements.txt .
RUN pip install --no-cache-dir cython setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# 拷贝源码
COPY app/            app/
COPY lib/            lib/
COPY templates/      templates/
COPY setup_ext.py run.py douyin_im.py fetch_security.py ./

# 编译成 .so，然后删除所有被编译的 .py 源文件（除保留列表）
RUN python setup_ext.py build_ext --inplace 2>&1 | tail -20 && \
    python - <<'PYEOF'
import os, pathlib
KEEP = {
    "run.py",
    "app/__init__.py",
    "app/main.py",
    "app/config.py",
    "app/db.py",
    "app/models.py",
    "app/deps.py",
    "app/cli.py",
    "app/routers/__init__.py",
}
for p in pathlib.Path(".").rglob("*.py"):
    rel = str(p).replace("\\", "/")
    if rel in KEEP:
        continue
    if rel.startswith("build/"):
        continue
    if rel == "setup_ext.py" or rel == "fetch_security.py":
        # 构建脚本不用留在运行镜像
        p.unlink(); print("deleted:", rel); continue
    # 检查是否有对应的 .so
    so = rel[:-3] + ".cpython-313-x86_64-linux-gnu.so"
    if pathlib.Path(so).exists() or pathlib.Path(rel[:-3] + ".so").exists():
        p.unlink(); print("deleted:", rel)
# 清理 .c 中间文件
for c in pathlib.Path(".").rglob("*.c"):
    c.unlink()
# 清理 build/ 目录
import shutil
shutil.rmtree("build", ignore_errors=True)
print("cleanup done")
PYEOF


# ─── 阶段 2：Playwright browser ─────────────────────────────────
FROM python:3.13-slim AS playwright-setup
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
      libgbm1 libpango-1.0-0 libcairo2 libasound2 libx11-xcb1 libxcb-dri3-0 \
      fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir playwright \
    && playwright install chromium --with-deps=false


# ─── 阶段 3：运行时镜像 ────────────────────────────────────────
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \
    DATA_DIR=/data

# 运行时系统依赖（Node.js 给 douyin_im 的签名 JS 用 + Playwright 浏览器运行时）
RUN apt-get update && apt-get install -y --no-install-recommends \
      nodejs \
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
      libgbm1 libpango-1.0-0 libcairo2 libasound2 libx11-xcb1 libxcb-dri3-0 \
      fonts-liberation fonts-noto-cjk \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 从 builder 阶段拷入：依赖 + 已编译源码
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /build /app

# 从 playwright-setup 拷入 chromium 浏览器
COPY --from=playwright-setup /root/.cache/ms-playwright /opt/playwright-browsers

# 入口脚本
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 数据目录（volume 挂载点）
VOLUME ["/data"]

EXPOSE 8765

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8765"]
