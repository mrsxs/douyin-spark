# 部署指南

## 架构总览

```
┌──────────────────┐           ┌──────────────────┐
│ 客户（付费）       │  HTTPS   │ 你的服务器        │
│ Browser          │ ───────→ │ Docker 容器       │
└──────────────────┘           │ ┌──────────────┐ │
                               │ │ 续火花 app    │ │
                               │ │ (Cython .so) │ │
                               │ └──────────────┘ │
                               │ volume: /data   │
                               └──────────────────┘
```

- **镜像**：Cython 编译核心代码为 `.so`，反编译难度大
- **分发**：推到 **GHCR 私有仓库**（只有你能 pull）
- **License**：客户付款后你本地 `python tools/issue_license.py` 签发 License Key，客户填进 `.env`
- **首次启动**：容器自动生成强随机 admin 密码，打印到日志一次

---

## 1. 首次初始化（你本地做一次）

### 1.1 生成 License 签名密钥对

```bash
# 已生成：license_private_key.pem（私钥，绝密！）
#         license_public_key.pem（公钥，已嵌入 app/license.py）

# 如果要重新生成（注意：重新生成后所有旧 License 失效）
python3 -c "
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
open('license_private_key.pem', 'wb').write(priv.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()))
open('license_public_key.pem', 'wb').write(priv.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo))
"
import os; os.chmod('license_private_key.pem', 0o600)
# 然后把 license_public_key.pem 内容粘到 app/license.py 的 _PUBLIC_KEY_PEM
```

**⚠️ `license_private_key.pem` 绝不能进 git 或镜像！** 已在 `.gitignore` / `.dockerignore`。

### 1.2 签发 License

```bash
# 宽松模式（推荐，不绑机器）
python tools/issue_license.py --days 365 --tier pro --note "闲鱼 HU287-客户A"

# 严格模式（绑机器，需要客户先启动报错拿到机器码再签）
# 客户先跑一次容器，会在启动失败时打印 "当前机器码：abc123"
python tools/issue_license.py --days 365 --machine abc123 --note "客户B"

# 输出的长字符串就是 License Key，发给客户
```

---

## 2. 把代码推到 GitHub（私有仓库）

```bash
cd /Users/apple/code/python/抖音续火花

# 初始化 git（如果还没）
git init
git add -A
git status  # ⚠️ 仔细看有没有 .env / license_private_key.pem / data/ — 不应该出现！
git commit -m "init"

# GitHub 新建私有 repo（例：douyin-spark），然后：
git remote add origin git@github.com:YOUR_GITHUB/douyin-spark.git
git branch -M main
git push -u origin main
```

推送后，GitHub Actions 会自动构建 Docker 镜像并推到 GHCR：
`ghcr.io/YOUR_GITHUB/douyin-spark:latest`

**让 GHCR 镜像私有**：GitHub → Packages → douyin-spark → Package settings → Change visibility → Private

---

## 3. 服务器部署

### 3.1 准备

```bash
# 服务器上
apt install docker.io docker-compose-plugin

# 登录 GHCR（首次）
echo YOUR_GITHUB_TOKEN | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
# TOKEN 在 github.com/settings/tokens（需要 read:packages 权限）
```

### 3.2 拉取并启动

```bash
mkdir -p /opt/douyin-spark && cd /opt/douyin-spark

# 复制 docker-compose.yml 过来
wget https://raw.githubusercontent.com/YOUR_GITHUB/douyin-spark/main/docker-compose.yml
# 或手动拷贝

# 创建 .env
cat > .env <<EOF
LICENSE_KEY=客户拿到的 license 字符串
DOUYIN_CAPTCHA_KEY=你的滑块服务 key
EOF

# 拉镜像
docker compose pull

# 首次启动（会随机生成 admin 密码并打印）
docker compose up -d
docker compose logs -f | head -30
# ==> 复制密码保存！此后 logs 里不会再有明文

# 浏览器访问
# http://服务器IP:8765/login
# 用 admin / 刚才打印的密码登录
```

### 3.3 配置 HTTPS（推荐 Caddy 反代）

```bash
# /etc/caddy/Caddyfile
yourdomain.com {
    reverse_proxy localhost:8765
}
```

同时把 `docker-compose.yml` 里的 `COOKIE_SECURE: "true"` 改成 true。

---

## 4. 升级 / 维护

### 4.1 升级到新版本

```bash
docker compose pull
docker compose up -d
```

### 4.2 重置 admin 密码

```bash
# 方式 A：删标记文件后重启，重新生成随机密码
docker exec douyin-spark rm /data/.admin_initialized
docker compose restart

# 方式 B：生成新哈希，手动填 docker-compose.yml 的 ADMIN_PASSWORD_HASH
docker exec douyin-spark python -m app.cli hash-password 新密码
```

### 4.3 续费 License

```bash
# 你本地签新 License
python tools/issue_license.py --days 365 --tier pro --note "客户A 续费 2026"

# 客户更新 .env 的 LICENSE_KEY，重启容器
docker compose restart
```

### 4.4 备份数据

```bash
# 备份 volume（含 cookies/私钥/DB）
docker run --rm -v /opt/douyin-spark_spark-data:/data -v $(pwd):/backup \
  alpine tar czf /backup/backup-$(date +%F).tar.gz -C /data .
```

---

## 5. 常见问题

### Q: 启动报 "License 已过期"
客户的 License Key 过期了，按 4.3 续费。

### Q: 启动报 "License 绑定的机器码与当前不匹配"
客户换服务器了。按 1.2 重新签发不绑机器，或拿新机器码签新 License。

### Q: Chromium 扫码登录无响应
检查：
```bash
docker exec douyin-spark python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(); print('ok'); b.close()"
```

### Q: 如何更新抖音签名逻辑
改 `douyin_im.py` → git push → Actions 自动构建新镜像 → 服务器 `docker compose pull && up -d`

---

## 6. 防破解说明

- ✅ **Cython 编译**：核心代码编译成 `.so`，反编译成本极高
- ✅ **License 过期**：离线 RSA 签名，篡改系统时间无效（签名已固定 `expires_at`）
- ✅ **启动闸门**：验签在 `app.license.license_gate()`，`run.py` 与 `app/main.py` 的 lifespan 都要过；
  每请求还会经过已编译的 `csrf_mw` 调 `assert_licensed()` 兜底，改明文入口也绕不过
- ✅ **后门已关闭**：`SKIP_LICENSE_CHECK` 只在源码态有效；镜像构建时 `_ALLOW_SKIP_LICENSE` 被改成 `False` 再编译进 `license.so`
- ⚠️ **镜像是公开的**：Docker Hub `mrsxs/douyin-spark` 免登录即可 pull（GHCR 那份才是私有）。
  因此**不能把「拿不到镜像」当作防线**，防护完全依赖 License 校验
- ⚠️ **不绑机器时**：客户可以把镜像转给别人用 → 建议对大客户签发时带 `--machine`（绑定后无条件生效，客户无法关闭）
- ⚠️ **改本地系统时间**：理论上可以绕过过期，但会导致其它时间敏感功能异常（如 cookies 认证）

如果要加更强保护（在线心跳验证），参考方案 B：定期回调你的 license 服务器，可远程吊销。
