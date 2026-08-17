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
COPY setup_ext.py run.py douyin_im.py ./

# ⭐ 关闭开发用的 SKIP_LICENSE_CHECK 后门，再编译进 license.so
# 必须在 cythonize 之前做；替换后立即断言，避免 sed 静默失配导致保护失效
RUN sed -i 's/^_ALLOW_SKIP_LICENSE = True  # BUILD_FLAG$/_ALLOW_SKIP_LICENSE = False  # BUILD_FLAG/' app/license.py \
    && grep -qx '_ALLOW_SKIP_LICENSE = False  # BUILD_FLAG' app/license.py \
    && echo "[build] SKIP_LICENSE_CHECK 已在镜像中关闭"

# 编译成 .so，然后删除所有被编译的 .py 源文件（除保留列表）
#
# ⚠️ 不要在这条命令里加管道（如 `| tail -20`）：管道会把退出码换成最后一个
# 命令的，cythonize 编译失败会被静默吞掉，构建照样 DONE，
# 结果是镜像里一个 .so 都没有、全量源码明文发给客户。实际踩过这个坑。
RUN python setup_ext.py build_ext --inplace && \
    python - <<'PYEOF'
import os, pathlib, sys
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
# 编译失败就绝不能继续打包。cythonize 本身的退出码已经拦一道，
# 这里再按「关键模块必须存在 .so」核一遍 —— 保护失效必须是构建期红灯，
# 而不是等客户拿到镜像才发现源码是明文的。
MUST_COMPILE = ["app/license.py", "app/csrf_mw.py", "app/trigger.py", "douyin_im.py"]


def compiled(rel: str) -> bool:
    """有没有编出对应的 .so。

    不能硬编码 `cpython-313-x86_64-linux-gnu.so`：ABI 标签跟 Python 版本
    和 CPU 架构走，arm64 机器上编出来的是 aarch64，硬编码会一个都匹配不上，
    于是源码全部留在镜像里 —— 同样是静默失效。用 glob 按文件名前缀找。
    """
    p = pathlib.Path(rel)
    return any(p.parent.glob(p.stem + "*.so"))


missing = [m for m in MUST_COMPILE if not compiled(m)]
if missing:
    sys.exit(f"[build] ✗ 这些模块没有编译产物，拒绝打包: {missing}")

deleted = 0
for p in pathlib.Path(".").rglob("*.py"):
    rel = str(p).replace("\\", "/")
    if rel in KEEP:
        continue
    if rel.startswith("build/"):
        continue
    if rel == "setup_ext.py":
        # 构建脚本不用留在运行镜像
        p.unlink(); print("deleted:", rel); continue
    if compiled(rel):
        p.unlink(); deleted += 1
# 清理 .c 中间文件
for c in pathlib.Path(".").rglob("*.c"):
    c.unlink()
# 清理 build/ 目录
import shutil
shutil.rmtree("build", ignore_errors=True)
print(f"[build] ✓ 已删除 {deleted} 个已编译的 .py 源文件")
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
    && playwright install chromium


# ─── 阶段 3：运行时镜像 ────────────────────────────────────────
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \
    DATA_DIR=/data

# 运行时系统依赖（Node.js + npm 给 douyin_im 的签名 JS 用 + Playwright 浏览器运行时）
RUN apt-get update && apt-get install -y --no-install-recommends \
      nodejs npm \
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

# ⭐ 关键：装 node 包（jsrsasign），否则 dy_signer.js 签名会失败
# 签名失败抖音虽返回 OK 但消息实际不投递（"幽灵发送"）
COPY package.json /app/package.json
RUN cd /app && npm install --omit=dev --no-audit --no-fund 2>&1 | tail -5 \
    && npm cache clean --force \
    && rm -rf /root/.npm

# 入口脚本
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 数据目录（volume 挂载点）
VOLUME ["/data"]

EXPOSE 8765

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8765"]
