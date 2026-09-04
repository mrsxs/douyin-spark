---
tags:
  - 续火花
  - 速查
created: 2026-04-19
updated: 2026-09-04
---

# URL 速查

> [!info] 配套 [[90-命令速查]]

## 业务

| 用途 | URL |
|---|---|
| **Web 后台** | `http://服务器IP:8765/login` |
| 健康检查 | `http://服务器IP:8765/healthz` |
| 管理后台 | `http://服务器IP:8765/admin` |
| robots.txt | `http://服务器IP:8765/robots.txt` |
| sitemap.xml | `http://服务器IP:8765/sitemap.xml` |

## GitHub

| 用途 | URL |
|---|---|
| 源码仓库 | https://github.com/mrsxs/douyin-spark |
| Actions 构建 | https://github.com/mrsxs/douyin-spark/actions |
| Secrets 配置 | https://github.com/mrsxs/douyin-spark/settings/secrets/actions |
| Variables 配置 | https://github.com/mrsxs/douyin-spark/settings/variables/actions |
| Packages（GHCR） | https://github.com/mrsxs?tab=packages |

## 镜像

| 仓库 | 地址 | 可见性 |
|---|---|---|
| Docker Hub | `mrsxs/douyin-spark:latest` | **公开**，免登录 —— compose 默认用这个 |
| GHCR | `ghcr.io/mrsxs/douyin-spark:latest` | package 默认**私有**，匿名 pull 403 |

```bash
docker pull mrsxs/douyin-spark:latest              # 免登录
docker pull ghcr.io/mrsxs/douyin-spark:latest      # 需 docker login ghcr.io，或把包改成 public
```

## 部署文件（直接从仓库拉）

| 文件 | URL |
|---|---|
| 一键部署 | https://raw.githubusercontent.com/mrsxs/douyin-spark/main/deploy.sh |
| 运维菜单（Linux） | https://raw.githubusercontent.com/mrsxs/douyin-spark/main/manage.sh |
| 运维菜单（Windows） | https://raw.githubusercontent.com/mrsxs/douyin-spark/main/manage.ps1 |
| docker-compose | https://raw.githubusercontent.com/mrsxs/douyin-spark/main/docker-compose.yml |

## 第三方服务

| 服务 | 说明 |
|---|---|
| ddddocr 滑块识别 | 需**自建**，见 https://github.com/sml2h3/ddddocr 。地址填在 `DOUYIN_CAPTCHA_URL`，只有短信登录用得上 |
| GitHub Token 管理 | https://github.com/settings/tokens |

> [!warning] 别把自建服务的地址写进仓库
> ddddocr 实例的公网地址属于自己的基础设施，只放 `.env`（已 gitignore），
> 不要写进代码默认值、文档或 `.env.example`。

## 环境变量速查

**`.env` 没有任何必填项** —— 全部有默认值。最少配置就是设个管理员密码：

```ini
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH='$2b$12$...'   # python -m app.cli hash-password <密码>
```

常用可选项：

```ini
SECRET_KEY=                      # 留空自动生成并持久化到 data/.secret_key
COOKIE_SECURE=false              # 生产 HTTPS 改 true
SESSION_MAX_AGE=2592000          # 30 天
SITE_NAME=续火花
TZ=Asia/Shanghai                 # ⭐ Docker 里不设会走 UTC，定时差 8 小时

DATA_DIR=./data                  # 备份这个目录 = 备份全部
DATA_VOLUME_PATH=./data          # compose 的 bind mount 宿主机侧路径
DB_URL=                          # 留空 → sqlite:///$DATA_DIR/app.db
CRYPT_STRICT=                    # 1 = 没有加密密钥时拒绝保存敏感字段（生产建议开）

DOUYIN_CAPTCHA_URL=              # 自建 ddddocr 地址，仅短信登录用
DOUYIN_CAPTCHA_ENDPOINT=/slide_match
DOUYIN_CAPTCHA_KEY=
DOUYIN_NODE_BIN=                 # 留空自动查找 node

SMTP_HOST=                       # 邮件通知（也可后台 /admin/smtp 配）
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
SMTP_TLS=true
```

> [!danger] `ADMIN_PASSWORD_HASH` 在 `.env` 里必须转义
> docker compose 会把 `$2b$12$xxx` 当变量插值。写进 `.env` 时要 `$` → `$$`，
> 否则「密码明明设对了却登不上」。部署脚本已经做了这个转义，手写 `.env` 的注意。

LLM 和 ASR 配置**不走环境变量**，在每个账号的 AI 面板里填（key 加密入库）。
