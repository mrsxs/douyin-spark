# ─── 阶段 1：Playwright browser ─────────────────────────────────
# 只有扫码登录用得上 Chromium；单独一层是为了让它命中缓存，
# 改业务代码时不用重下浏览器。
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


# ─── 阶段 2：运行时镜像 ────────────────────────────────────────
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
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

# 依赖单独一层：requirements.txt 没变就不重装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ⭐ 关键：装 node 包（jsrsasign），否则 dy_signer.js 签名会失败。
# 签名失败抖音虽返回 OK 但消息实际不投递（"幽灵发送"）。
COPY package.json /app/package.json
RUN npm install --omit=dev --no-audit --no-fund 2>&1 | tail -5 \
    && npm cache clean --force \
    && rm -rf /root/.npm

# 从 playwright-setup 拷入 chromium 浏览器
COPY --from=playwright-setup /root/.cache/ms-playwright /opt/playwright-browsers

# 应用代码
COPY app/       app/
COPY lib/       lib/
COPY templates/ templates/
COPY run.py douyin_im.py ./

# 入口脚本
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 数据目录（volume 挂载点）
VOLUME ["/data"]

EXPOSE 8765

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8765"]
