# 部署指南

面向自己在服务器上跑一套的场景。想先在本机试试看 → [README 的源码运行](README.md#方式二源码运行)。

## 目录

- [架构总览](#架构总览)
- [1. 部署（三选一）](#1-部署三选一)
- [2. 配置 HTTPS](#2-配置-https)
- [3. 镜像源](#3-镜像源)
- [4. 升级](#4-升级)
- [5. 备份与恢复](#5-备份与恢复)
- [6. 日常运维](#6-日常运维)
- [7. 从旧的授权码版本迁移](#7-从旧的授权码版本迁移)
- [8. 故障排查](#8-故障排查)

---

## 架构总览

```
┌──────────┐   HTTPS   ┌──────────── 你的服务器 ────────────┐
│ 浏览器    │ ────────→ │  Caddy / Nginx 反代               │
└──────────┘           │        ↓                          │
                       │  Docker 容器 douyin-spark :8765   │
                       │    ├─ FastAPI + scheduler 线程     │
                       │    ├─ Node 子进程（签名）           │
                       │    └─ Chromium（仅扫码登录时起）    │
                       │        ↓                          │
                       │  bind mount → ./data              │
                       │    app.db · cookies · 私钥 · 日志  │
                       └───────────────────────────────────┘
                                    │
                                    ↓
                          抖音 API · 自建 ddddocr（可选）
```

单容器、单进程、SQLite。**所有状态都在 `/data`**，容器本身随时可以删了重建。

---

## 1. 部署（三选一）

### 1.1 一键脚本（最省事）

```bash
curl -fsSL https://raw.githubusercontent.com/mrsxs/douyin-spark/main/deploy.sh -o deploy.sh
chmod +x deploy.sh && ./deploy.sh
```

脚本会：检查 Docker → 下载 compose → 问两个可跳过的问题（ddddocr key、管理员密码）→ 拉镜像 → 写 `.env`（600 权限）→ 起容器 → 等健康检查 → 打印访问地址和密码。

默认装到 `/opt/douyin-spark`（非 root 时是 `~/douyin-spark`），改 `INSTALL_DIR` 环境变量可以换位置。

### 1.2 手动 compose

```bash
mkdir -p /opt/douyin-spark && cd /opt/douyin-spark
curl -fsSL https://raw.githubusercontent.com/mrsxs/douyin-spark/main/docker-compose.yml -o docker-compose.yml

# .env 可以完全不写。要自定义就照 .env.example 填
docker compose up -d
docker compose logs | grep -A 8 "管理员账户"
```

首次启动时如果没配 `ADMIN_PASSWORD_HASH`，`docker-entrypoint.sh` 会生成一个 20 位强随机密码、bcrypt 之后注入，**并且只在日志里打印这一次**。立刻存下来。

之后要改密码：

```bash
docker compose exec app python -m app.cli reset-password --username admin
```

### 1.3 自己构建镜像

```bash
git clone https://github.com/mrsxs/douyin-spark.git
cd douyin-spark
docker compose build          # 先把 compose 里的 image: 换成 build: .
docker compose up -d
```

镜像分两阶段：一层单独装 Playwright 的 Chromium（改业务代码时命中缓存，不用重下浏览器），一层是运行时。没有编译/混淆步骤。

### 1.4 运维菜单（可选）

`manage.sh` 是个交互式菜单，包了安装 / 改密码 / 看状态三件事：

```bash
curl -fsSL https://raw.githubusercontent.com/mrsxs/douyin-spark/main/manage.sh -o manage.sh
chmod +x manage.sh && ./manage.sh
```

Windows 用户看 [Windows安装指南.md](Windows安装指南.md)（`manage.ps1` 是同一套东西的 PowerShell 版）。

---

## 2. 配置 HTTPS

容器只监听 8765 明文 HTTP，生产环境务必套反代。Caddy 最省事（自动签证书）：

```caddyfile
# /etc/caddy/Caddyfile
spark.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Nginx：

```nginx
server {
    listen 443 ssl http2;
    server_name spark.example.com;

    ssl_certificate     /etc/letsencrypt/live/spark.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/spark.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE（运行进度、新消息推送）不能被缓冲，否则页面一直转圈
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

**上了 HTTPS 之后两件事必须做**：

1. `.env` 里设 `COOKIE_SECURE=true`，重启容器。否则 session cookie 不带 `Secure` 标记。
2. 确认反代确实在设 `X-Forwarded-For`。限流按这个头取客户端 IP —— 它是客户端可伪造的，只有在可信反代后面才有意义。**直接把 8765 暴露到公网时反而应该关掉这个分支**（见 `app/ratelimit.py:_client_key`），否则攻击者换个 header 就绕过限流了。

另外把 8765 从公网防火墙上关掉，只留反代进得来：

```bash
ufw deny 8765
```

> ⚠️ **面板对公网可见时，务必确认 `ALLOW_REGISTER` 是关的**（默认就是关的）。
> 注册成功即拿到 10 个抖音号槽位和完整 API 权限，陌生人注册完就能用你的
> 服务器和 IP 打抖音接口 —— 封号和风控算在你头上。

---

---

## 3. 镜像源

compose 默认用 **Docker Hub**（`mrsxs/douyin-spark:latest`），公开、pull 不需要登录。

CI 同时会推 GHCR（`ghcr.io/mrsxs/douyin-spark`），但 **GitHub 的 container package 默认是私有的** ——
匿名 `docker pull` 会 403。要用 GHCR 得二选一：

- 在 GitHub → Packages → 该 package → Package settings → Change visibility 改成 Public
- 或者部署机上先 `docker login ghcr.io -u <用户名> -p <带 read:packages 的 token>`

没确认过 visibility 就别把 compose 里的 `image:` 换成 GHCR，否则新机器一部署就卡在 pull。

验证某个 GHCR 包是不是公开：

```bash
curl -s "https://ghcr.io/token?scope=repository:mrsxs/douyin-spark:pull&service=ghcr.io"
# 公开包返回 {"token":"..."}；私有包拿不到 token，后续 pull 就是 403
```

---

## 4. 升级

```bash
cd /opt/douyin-spark
docker compose pull
docker compose up -d
```

或者再跑一次 `./deploy.sh`（检测到已有 `.env` 会直接走升级分支，不重新问配置）。

DB 迁移在启动时自动跑（`app/db.py:init_db()`，幂等加列 + 补索引），**不需要手动执行任何迁移命令**。升级前建议先备份一次 `data/`。

看迁移有没有正常跑完：

```bash
docker compose logs --tail 50 | grep init_db
```

---

## 5. 备份与恢复

**要备份的只有一个目录**：`data/`（compose 里的 `DATA_VOLUME_PATH`，默认是 compose 文件旁边的 `./data`）。里面有：

| 内容 | 说明 |
|---|---|
| `app.db` | SQLite 主库：用户、账号、模板、定时、聊天记录、审计日志 |
| `.secret_key` | session 签名 + 敏感字段加密密钥。**丢了等于所有加密字段解不开** |
| `users/<uid>/accounts/<aid>/` | 每个抖音号的 cookies、私钥、init_req、联系人缓存 |
| 日志 | 结构化运行日志 |

```bash
# 备份（停机备份最干净；SQLite 开了 WAL，热备也基本安全）
cd /opt/douyin-spark
docker compose stop
tar czf spark-backup-$(date +%F).tar.gz data/
docker compose start

# 恢复
docker compose down
rm -rf data/
tar xzf spark-backup-2026-09-04.tar.gz
docker compose up -d
```

`data/` 里全是凭证，备份文件请当成密钥对待 —— 别丢进公开的对象存储。

---

## 6. 日常运维

```bash
cd /opt/douyin-spark

docker compose ps                    # 状态
docker compose logs -f               # 实时日志
docker compose logs --tail 200       # 最近 200 行
docker compose restart               # 重启
docker compose down                  # 停止并删容器（数据不动）

# 健康检查
curl -s http://127.0.0.1:8765/healthz

# 用户管理（CLI）
docker compose exec app python -m app.cli list-users
docker compose exec app python -m app.cli reset-password --username <名字>
docker compose exec app python -m app.cli set-admin --username <名字>        # 提权
docker compose exec app python -m app.cli set-admin --username <名字> --off  # 撤销

# 进容器
docker compose exec app bash
```

管理员在 `/admin` 有网页后台：用户列表、账号配额、审计日志、公告、SMTP、站点域名。

**开号给别人**（注册默认关闭）：

```bash
# 直接建一个普通用户
docker compose exec app python -m app.cli reset-password --username 朋友的名字

# 或临时开放注册，让对方自己注册完再关掉
# .env 里 ALLOW_REGISTER=true → docker compose up -d → 注册 → 改回 false → up -d
```

**关于管理员账号**：`.env` 里的 `ADMIN_USERNAME` 每次启动都会被 upsert 成管理员并覆盖密码。如果这个名字撞上了某个已注册的普通用户，他会被静默提权 —— 日志里会喊出来，注意看启动日志的 `[bootstrap] ⚠️` 行。

---

## 7. 从旧的授权码版本迁移

旧版本有两层授权，现在都没了：

| 旧机制 | 现状 |
|---|---|
| `LICENSE_KEY`（RSA 签名的部署许可 + 机器码绑定） | 已删除。`.env` 里残留这一行会被忽略，可以删掉 |
| 用户授权码 / `/activate` / `expires_at` 到期 | 已删除。注册完直接能用 |
| `license_codes` 表 | 首次启动自动 `DROP TABLE` |
| Cython 混淆 + `SKIP_LICENSE_CHECK` | 已删除，镜像里就是明文 Python |

**直接 `docker compose pull && docker compose up -d` 即可**，不需要任何手动步骤。启动时的一次性迁移会：

- 把旧版本里因"未激活"而 `max_accounts = 0` 的用户抬到默认配额（`app/models.py:DEFAULT_MAX_ACCOUNTS`，默认 10）
- 删掉 `license_codes` 表
- 在 `app_settings` 写一个标记位，保证只跑这一次 —— 之后管理员故意把某人配额调回 0，重启不会被再抬上去

`users` 表里的 `expires_at` 列不会被删（SQLite 删列麻烦且没必要），代码不再读它，留着是无害的死列。

日志里会看到：

```
[init_db] 授权码体系已移除：3 个用户的账号配额从 0 抬到 10
```

如果本地还留着 `license_private_key.pem`（旧版签发 License 用的私钥），现在可以删了 —— 它一直被 gitignore，不会进仓库，但留在服务器上也没意义了。

---

## 8. 故障排查

**容器起不来**

```bash
docker compose logs --tail 100
```

常见原因：`.env` 里的 `ADMIN_PASSWORD_HASH` 没做 `$` → `$$` 转义（compose 会把 `$2b$12$...` 当变量插值，结果密码对不上）；`DATA_VOLUME_PATH` 指向的目录没权限。

**定时任务时间差 8 小时** — 容器没设 `TZ`，走了 UTC。compose 默认是 `Asia/Shanghai`，自己写 compose 的记得加。

**消息显示发送成功但对方没收到** — 签名失败。抖音接口在签名无效时**照样返回 OK**，这是最坑的一个坑。检查容器里 `node_modules/jsrsasign` 在不在、`node` 可执行。

**扫码登录一直转圈** — Chromium 起不来。`docker compose exec app playwright install chromium` 补一次，或看日志里 Playwright 的报错。

**页面进度条一直不动 / 新消息不推送** — 反代把 SSE 缓冲了。Nginx 加 `proxy_buffering off`。

**登录后立刻被踢回登录页** — `SECRET_KEY` 变了（比如 `data/.secret_key` 丢了又重新生成），所有已签发的 session 失效。重新登录即可。

更细的排查记录在 [`obsidian-vault/50-故障排查.md`](obsidian-vault/50-故障排查.md)。
