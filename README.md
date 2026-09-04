# 续火花（douyin-spark）

抖音私信自动续火花的自托管 Web 面板。每天定时给火花联系人发消息，火花不断；也能接 LLM 做自动回复。

除首次扫码登录外**全程纯 HTTP**，不依赖常驻浏览器 —— 签名（`a_bogus` / `req_sign` / `ts_sign` / `sdk_cert`）由 Node 子进程算，消息走抖音 IM 的 protobuf 接口发。

> 自部署、自己用。没有授权码、没有订阅、没有联网校验 —— 拉起来注册第一个账号就能用。

---

## 目录

- [它能做什么](#它能做什么)
- [快速开始](#快速开始)
  - [方式一：Docker（推荐）](#方式一docker推荐)
  - [方式二：源码运行](#方式二源码运行)
- [第一次使用](#第一次使用)
- [配置项](#配置项)
- [架构](#架构)
- [目录结构](#目录结构)
- [开发](#开发)
- [常见问题](#常见问题)
- [已知限制](#已知限制)
- [致谢](#致谢)
- [许可](#许可)
- [免责声明](#免责声明)

---

## 它能做什么

**账号管理**
- 一个面板管多个抖音号，每个号独立 cookies / 模板 / 定时配置
- 扫码登录（Playwright 拉一次 Chromium）或短信登录（纯 HTTP，需自建 ddddocr 滑块服务）
- Cookies 失效自动检测 + 站内通知 / 邮件提醒

**自动续火花**
- 每个号设一个每日触发时间，到点自动给所有启用的火花联系人发消息
- 每个联系人可配独立消息列表（多条则随机选一条），也可走 `default` 兜底
- 发送间隔可调（默认 4.5–5.5s 随机），降低风控概率
- 三类联系人分别可开关：有火花的、已断火花的（发消息能续上）、没火花的普通好友
- 「重燃中」进度展示（N/M 天）

**聊天**
- 面板内直接看聊天记录、发消息，历史消息可回溯拉取
- 分享的视频原生播放、语音消息转文字（需配 ASR）

**AI 自动回复**（可选）
- 接任意 OpenAI 兼容接口（`base_url` + `model` + key，后台填）
- 知识库、回复策略、few-shot、拒答策略都能在界面上改
- 思考过程开关；可按联系人单独覆盖账号级设置

**多用户 + 后台**
- 注册 / 登录 / 会话管理，管理员可停用用户、调整每人的抖音号配额、重置密码
- 审计日志、站内公告、SMTP 配置、7 天触发趋势

---

## 快速开始

### 方式一：Docker（推荐）

**一键脚本**（Linux 服务器）：

```bash
curl -fsSL https://raw.githubusercontent.com/mrsxs/douyin-spark/main/deploy.sh -o deploy.sh
chmod +x deploy.sh && ./deploy.sh
```

向导两步（都能跳过），跑完直接访问 `http://服务器IP:8765`。再执行一次就是升级。

**手动**：

```bash
mkdir -p ~/douyin-spark && cd ~/douyin-spark
curl -fsSL https://raw.githubusercontent.com/mrsxs/douyin-spark/main/docker-compose.yml -o docker-compose.yml

# 不写 .env 也能起：容器首启会生成强随机 admin 密码并打印一次
docker compose up -d
docker compose logs | grep -A 8 "管理员账户"    # ← 密码在这里，只打印这一次
```

**自己构建镜像**：把 compose 里的 `image:` 换成 `build: .`，然后 `docker compose up -d --build`。

### 方式二：源码运行

需要 **Python 3.13** 和 **Node.js 18+**（Node 是必须的，签名靠它算）。

```bash
git clone https://github.com/mrsxs/douyin-spark.git
cd douyin-spark

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npm install                        # 装 jsrsasign，签名用
playwright install chromium        # 只有扫码登录需要

cp .env.example .env
python -m app.cli gen-secret                  # 填进 .env 的 SECRET_KEY
python -m app.cli hash-password 你的密码       # 填进 .env 的 ADMIN_PASSWORD_HASH

python run.py --port 8765
```

打开 <http://127.0.0.1:8765>。

> ⚠️ `npm install` 不能省。`jsrsasign` 缺失时签名会失败，而抖音接口**仍然返回成功**，消息却根本没投递（"幽灵发送"）。

---

## 第一次使用

1. 用 `.env` 里配的管理员账号登录（Docker 首启的随机密码在日志里）。
   注册默认是关的，要开放注册设 `ALLOW_REGISTER=true`
2. Dashboard → **添加账号**，给它起个名字
3. 进账号页 → **登录抖音**
   - **扫码**：页面出二维码，用抖音 App 扫（服务端起一次 Chromium）
   - **短信**：纯 HTTP，但滑块验证需要自建的 [ddddocr](https://github.com/sml2h3/ddddocr) 服务，地址填在 `DOUYIN_CAPTCHA_URL`
4. 登录成功后自动拉取火花联系人列表
5. 配置每个联系人的消息模板，或只设一个 `default`
6. 设定每日触发时间 → 打开定时开关

之后每天到点自动跑。跑完的结果在「运行记录」里，失败会推站内通知（配了 SMTP 还会发邮件）。

---

## 配置项

全部走环境变量或 `.env`（见 `.env.example`）。**所有项都有默认值**，最少只需要设管理员密码。

| 变量 | 默认 | 说明 |
|---|---|---|
| `ALLOW_REGISTER` | `false` | **是否开放公开注册**，见下方说明 |
| `ADMIN_USERNAME` | `admin` | 启动时 upsert 的管理员用户名 |
| `ADMIN_PASSWORD_HASH` | 空 | bcrypt 哈希，`python -m app.cli hash-password <密码>` 生成 |
| `SECRET_KEY` | 自动生成 | session 签名 + 敏感字段加密。留空则落盘到 `data/.secret_key` |
| `SESSION_MAX_AGE` | `2592000` | 会话有效期（秒），默认 30 天 |
| `COOKIE_SECURE` | `false` | 生产 HTTPS 环境设 `true` |
| `DATA_DIR` | `./data` | cookies / DB / 日志 / 密钥全在这里，**备份这个目录 = 备份全部** |
| `DB_URL` | 空 | 留空 → `sqlite:///$DATA_DIR/app.db` |
| `SITE_NAME` | `续火花` | 页面标题 |
| `TZ` | `Asia/Shanghai` | ⭐ Docker 里不设会走 UTC，定时时间差 8 小时 |
| `CRYPT_STRICT` | 空 | 设 `1` 则没有加密密钥时拒绝保存敏感字段（生产建议开） |
| `DOUYIN_CAPTCHA_URL` | 空 | 自建 ddddocr 服务地址，仅短信登录用 |
| `DOUYIN_CAPTCHA_ENDPOINT` | `/slide_match` | ddddocr 的滑块识别端点 |
| `DOUYIN_CAPTCHA_KEY` | 空 | ddddocr 的 API Key（走 `X-API-Key`） |
| `DOUYIN_NODE_BIN` | 自动查找 | Node 可执行文件路径 |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_TLS` | — | 邮件通知，也可以在 `/admin/smtp` 后台填 |

> ⚠️ **`ALLOW_REGISTER` 默认是关的**，`/register` 直接返回 404。
> 注册成功即拿到 10 个抖音号槽位和完整 API 权限 —— 面板一旦对公网可见，
> 陌生人注册完就能用你的服务器和 IP 去打抖音接口，风控算在你头上。
> 自己一个人用就保持关闭，用 `.env` 里配的管理员账号登录。
> 要给朋友开号，用 `python -m app.cli reset-password --username <名字>` 直接建，
> 或临时打开 `ALLOW_REGISTER=true` 注册完再关掉。

LLM 和 ASR 的配置**不走环境变量**，在每个账号的 AI 面板里填（key 加密入库）。

---

## 架构

```
                 ┌──────────── 首次登录（唯一用浏览器的地方）────────────┐
  扫码登录 ────→ │  cookies  ·  init_req.bin(session token)  ·  私钥   │
                 └───────────────────────────────────────────────────┘
                                        │
                    ↓ 之后全程纯 HTTP，Node 子进程算签名 ↓

   get_message_by_init  ──→  火花天数 · conv_short_id · ticket
   spotlight/relation   ──→  备注名 · 昵称 · 头像
   /v1/message/send     ──→  发送（token + ts_sign + sdk_cert + reuqest_sign）


   ┌─ FastAPI ────────────────────────────────────────────────┐
   │  routers/   薄，只做参数校验和响应                          │
   │      ↓                                                    │
   │  trigger.py · *_service.py   业务逻辑                      │
   │      ↓                                                    │
   │  douyin_im.py   抖音协议的唯一出口（签名/proto/接口全在这）   │
   │      ↓                                                    │
   │  lib/*.js   Node 子进程：a_bogus / req_sign                │
   └──────────────────────────────────────────────────────────┘

   scheduler.py  后台线程，每分钟扫一次 schedules 表
   ai_worker.py  后台线程，轮询新消息 → LLM → 回复
```

**技术栈**：Python 3.13 · FastAPI · SQLAlchemy 2.0 · SQLite(WAL) · Jinja2 + Alpine.js + Tailwind CDN（无前端构建）· bcrypt / itsdangerous / cryptography(Fernet) · Playwright（仅扫码）· Node 子进程签名。

**没有** Redis、没有消息队列、没有前端构建步骤。外部依赖只有抖音 API 和（可选的）自建 ddddocr。

DB 迁移是手写的，全在 `app/db.py:init_db()` —— 幂等加列 + 补索引，启动时自动跑。

---

## 目录结构

```
app/
  main.py            FastAPI app factory、路由挂载、异常处理
  config.py          .env → Settings（pydantic-settings）
  db.py              engine / SessionLocal / init_db（含手写迁移）
  models.py          全部 ORM 模型
  deps.py            current_user / require_user / require_admin
  csrf_mw.py         纯 ASGI 的 CSRF 中间件 + user 注入
  routers/           auth · dashboard · api · admin · login_flow · ai
  trigger.py         续火花主流程（拉联系人 → 逐个发 → 记录）
  scheduler.py       定时触发线程
  ai_worker.py       AI 自动回复轮询线程
  *_service.py       contacts / messages / templates / video / voice / knowledge
douyin_im.py         抖音协议唯一出口（签名、proto、所有抖音接口）
lib/*.js             Node 签名脚本
templates/           Jinja2 模板
tests/               pytest（60+ 文件）
obsidian-vault/      项目知识库：架构、部署、协议细节、故障排查
.harness/rules/      本仓库的编码/流程/安全规范
```

**依赖方向是单向的**：router 薄 → 业务下沉到 `trigger.py` / `*_service.py` → 抖音协议只在 `douyin_im.py`。`app/` 下不直接拼抖音请求、不写签名逻辑。

---

## 开发

```bash
pip install -r requirements.txt -r requirements-dev.txt

pytest -q                                # 全量测试
ruff check app/ tests/ run.py            # lint（规则钉在 ruff.toml）
python run.py --port 8765 --reload       # 开发模式，改代码自动重载
```

改代码前请先读 [`.harness/rules/`](.harness/rules/)：

- [工程结构](.harness/rules/工程结构.md) — 新代码放哪、依赖方向
- [编码规范](.harness/rules/编码规范.md) — 命名、SQLAlchemy 2.0 风格、错误脱敏、日志、DB 迁移
- [开发流程](.harness/rules/开发流程.md) — TDD、任务分级、Conventional Commits
- [安全红线](.harness/rules/安全红线.md) — 凭证只进 `.env`，提交前扫密钥

业务细节（抖音协议、签名链、续火花算法、故障排查）在 [`obsidian-vault/`](obsidian-vault/)。

CI（`.github/workflows/docker.yml`）跑 ruff + pytest，通过后构建镜像推 GHCR，并在镜像内做一次自检（起 uvicorn 打 `/healthz` + 走一遍表单登录）。

---

## 常见问题

**Q：消息显示发送成功，对方却没收到？**
签名失败的典型症状。抖音接口在签名无效时照样返回 OK。检查 `npm install` 是否装上了 `jsrsasign`，以及 Node 是否可执行（`DOUYIN_NODE_BIN`）。

**Q：定时任务时间差了 8 小时？**
容器没设 `TZ`，走了 UTC。compose 里已默认 `Asia/Shanghai`，自己写 compose 的记得加。

**Q：Cookies 多久失效？**
一般 30 天。失效后面板会标红并推通知，重新扫码登录即可。

**Q：扫码登录一直转圈？**
Playwright 的 Chromium 没装或缺系统依赖。源码运行执行 `playwright install chromium`；Docker 镜像已内置。

**Q：短信登录卡在滑块？**
需要自建 ddddocr 服务并配 `DOUYIN_CAPTCHA_URL`。不想搭就用扫码登录。

**Q：怎么备份？**
整个 `DATA_DIR`（Docker 默认是宿主机的 `./data`）打包即可，里面含 DB、cookies、私钥、日志。

**Q：升级会丢数据吗？**
不会。`docker compose pull && docker compose up -d` 即可，DB 迁移在启动时自动跑。

**Q：从旧的授权码版本升级过来？**
直接升级。首次启动会自动删掉 `license_codes` 表，并把此前因未激活而 `max_accounts=0` 的用户抬到默认配额。`.env` 里残留的 `LICENSE_KEY` 会被忽略，可以删掉。

---

## 已知限制

1. **单进程**。限流是进程内计数，scheduler / ai_worker 是线程。多副本部署需要换共享存储并重做调度选主。
2. **SQLite**。开了 WAL，个人到小团队规模够用；再往上要换 Postgres（`DB_URL` 支持，但迁移脚本是按 SQLite 写的）。
3. **抖音接口会变**。签名算法和 proto 结构跟着抖音走，上游一改就得跟着更新 `douyin_im.py`。
4. **私钥提取依赖浏览器**。首次登录时从 localStorage 拿；localStorage 被清空需要重新登录。
5. **风控是真实存在的**。发送间隔可调不等于绝对安全，账号异常的风险自负。

---

## 致谢

- [JoeanAmier/TikTokDownloader](https://github.com/JoeanAmier/TikTokDownloader) — a_bogus / X-Bogus JS 实现
- [cv-cat/DouYin_Spider](https://github.com/cv-cat/DouYin_Spider) — 消息发送签名算法 + proto 结构
- [leonfang-dev/NewMediaOperator](https://github.com/leonfang-dev/NewMediaOperator) — 官方 proto schema
- [sml2h3/ddddocr](https://github.com/sml2h3/ddddocr) — 滑块识别

## 许可

[MIT](LICENSE)

## 免责声明

本项目仅供学习和技术研究。使用者需自行遵守抖音的服务条款与相关法律法规，因使用本项目导致的账号异常、封禁或其他任何后果，均由使用者自行承担。作者不对使用本项目产生的任何直接或间接损失负责。

请勿用于骚扰、批量营销或任何未经对方同意的自动化行为。
