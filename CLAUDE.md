# 抖音续火花 — 项目说明（Claude Code 入口）

> **执行任何任务前，先读 `.harness/rules/` 下全部规范并持续遵守。**

## 规范（必读，按需深读）

- [工程结构与落点](.harness/rules/工程结构.md) — 目录布局、新代码放哪、依赖方向（router 薄、业务下沉 trigger、抖音协议只在 douyin_im.py）
- [编码规范](.harness/rules/编码规范.md) — 命名、SQLAlchemy 2.0 风格、错误处理(`_safe_err` 脱敏)、日志(`print("[模块]…")`)、API 响应、DB 迁移、原子写、前端
- [开发流程](.harness/rules/开发流程.md) — 本地运行/自证、**强制 TDD（pytest，80% 门槛）**、任务分级、Conventional Commits、CI
- [安全红线](.harness/rules/安全红线.md) — 凭证只进 .env、私钥/cookies/data 永不入 git、脱敏、提交前扫密钥

## 最高优先级硬约束（违反即停）

1. **凭证零泄露**：cookies、`license_private_key.pem`、`SECRET_KEY`、SMTP 密码、抖音 token 绝不进 git / 镜像 / 日志 / 前端响应。提交前扫 `git diff --cached`。
2. **抖音协议只在 `douyin_im.py`**：`app/` 不直接拼抖音请求、不写签名逻辑；通过 `import douyin_im as dy` 调用。
3. **动 license / 加密 / CSRF·session / scheduler 触发 / douyin 签名链 = Heavy 任务**：先说清影响面再改，必须有测试——这些错了会静默风控或留漏洞。

## 技术栈速记

Python 3.13 · FastAPI · SQLAlchemy 2.0 · SQLite(WAL，迁移手写在 `app/db.py:init_db`) · Jinja2+Alpine.js+Tailwind CDN(无构建) · bcrypt/itsdangerous/cryptography(Fernet+RSA) · Playwright(仅扫码登录) · Node 子进程签名(`lib/`)。**无 Redis、无消息队列**。外部依赖：抖音 API、自建 ddddocr 验证码服务。

启动：`SKIP_LICENSE_CHECK=1 python run.py --port 8765`

## 知识层（业务/架构详解）

`obsidian-vault/` 是项目知识库（架构、部署流程、抖音协议与签名、续火花算法、故障排查、命令速查）。**改业务逻辑前先查对应笔记**；规则层不重复这些内容。

## CodeGraph 代码索引

已建本地索引（`codegraph init -i`，32 文件 / 675 节点 / 1236 边），可用于跨文件符号定位与调用/引用关系检索。`.codegraph/*.db` 是本地产物、不入 git（自带 .gitignore）。代码结构变化较大后可 `codegraph init -i` 重建。

## 安全红线（一句话版）

密钥只进 `.env`（`.env.example` 只留空占位）；`data/`、`*.pem`、`*.douyin_*` 全部 gitignore；提交前 `git diff --cached | grep -iE 'secret|password|private.?key|cookie'` 应无真实值命中。详见 [安全红线](.harness/rules/安全红线.md)。
