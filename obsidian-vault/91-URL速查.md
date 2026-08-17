---
tags:
  - 续火花
  - 速查
created: 2026-04-19
---

# URL 速查

> [!info] 配套 [[90-命令速查]]

## 业务

| 用途 | URL |
|---|---|
| **服务器 Web 后台** | `http://服务器IP:8765/login` |
| 健康检查 | `http://服务器IP:8765/healthz` |
| robots.txt | `http://服务器IP:8765/robots.txt` |
| sitemap.xml | `http://服务器IP:8765/sitemap.xml` |

## GitHub

| 用途 | URL |
|---|---|
| 私有源码仓库 | https://github.com/mrsxs/douyin-spark |
| Actions 构建 | https://github.com/mrsxs/douyin-spark/actions |
| Secrets 配置 | https://github.com/mrsxs/douyin-spark/settings/secrets/actions |
| Variables 配置 | https://github.com/mrsxs/douyin-spark/settings/variables/actions |
| Packages（GHCR） | https://github.com/mrsxs?tab=packages |

## 镜像仓库

| 仓库 | 地址 | 公私 |
|---|---|---|
| Docker Hub | https://hub.docker.com/r/mrsxs/douyin-spark | 公开 |
| GHCR | https://ghcr.io/mrsxs/douyin-spark | 私有 |

拉镜像：
```bash
docker pull mrsxs/douyin-spark:latest                    # Docker Hub
docker pull ghcr.io/mrsxs/douyin-spark:latest             # GHCR (需登录)
```

## Gist 部署文件

| 文件 | URL |
|---|---|
| 主菜单脚本 | https://gist.githubusercontent.com/mrsxs/eb80f17ecee1944c83deb5e0c33d2d78/raw/manage.sh |
| 一键部署 | https://gist.githubusercontent.com/mrsxs/eb80f17ecee1944c83deb5e0c33d2d78/raw/deploy.sh |
| docker-compose | https://gist.githubusercontent.com/mrsxs/eb80f17ecee1944c83deb5e0c33d2d78/raw/docker-compose.yml |
| 续期脚本 | https://gist.githubusercontent.com/mrsxs/6df87a305750bda46f721edff49c7df2/raw/renew-license.sh |
| Gist 主页 | https://gist.github.com/mrsxs/eb80f17ecee1944c83deb5e0c33d2d78 |

## 营销

| 用途 | URL |
|---|---|
| 闲鱼商品 | https://m.tb.cn/h.iJuxIwu （口令 **HU287**） |
| 闲鱼宝贝口令 | `HU287` |

## 第三方服务

| 服务 | URL |
|---|---|
| 抖音滑块识别 (DDDocr) | http://ocr.example.com/slide_match |
| Docker Hub Token 管理 | https://hub.docker.com/settings/security |
| GitHub Token 管理 | https://github.com/settings/tokens |

## 环境变量速查

`.env` 必填：

```ini
LICENSE_KEY=eyJ...xx==.yyy...   # 必填，卖家签发
DOUYIN_CAPTCHA_KEY=xxx           # 推荐，滑块识别用
```

`.env` 可选：

```ini
ADMIN_USERNAME=admin             # 默认 admin
ADMIN_PASSWORD_HASH='$2b$12$...' # 留空则首次启动自动生成随机
SECRET_KEY=                      # 留空自动持久化
COOKIE_SECURE=false              # 生产 HTTPS 改 true
SESSION_MAX_AGE=2592000          # 30 天
SITE_NAME=续火花
DOUYIN_CAPTCHA_URL=http://ocr.example.com
DOUYIN_CAPTCHA_ENDPOINT=/slide_match
LICENSE_STRICT=                  # 已废弃并忽略；绑定随 License 自动生效
SKIP_LICENSE_CHECK=              # 1 跳过 license 检查（仅源码态开发有效，正式镜像里已编译关闭）
SMTP_HOST=                       # 邮件通知（也可后台 /admin/smtp 配）
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
SMTP_TLS=true
```
