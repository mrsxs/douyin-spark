---
tags:
  - 续火花
  - License
  - 严格绑定
created: 2026-04-19
---

# License 严格绑定模式

> [!info] 上下文 [[40-开发流程#签发 License]] · 续期 [[30-运维操作#License 续期]]

## 什么时候用

| 场景 | 推荐模式 |
|---|---|
| 自用 / 内部部署 | 宽松（不绑） |
| 闲鱼小客户 ¥5-25 | 宽松（客服压力小） |
| 大客户 / 怀疑转售 | 严格（绑机器） |
| KOC 代运营服务 | 严格（按机器收费） |
| 演示/试用 | 宽松 + 短期（如 7 天） |

> [!tip] 选择原则
> 默认宽松。看到客户机器码异常变化（同一 license 在多个机器签到）时再单独签严格 license 给他更新。

## 工作原理

```
签发                                    启动
─────────────                          ─────────────
license_payload = {                    1. 验证签名（公钥）
  "expires_at": "...",                 2. 检查过期
  "machine": "abc123" 或 null,         3. 严格模式 + 有 machine：
  "tier": "pro",                          对比当前机器码
  ...                                     不一致 → 拒绝
}                                       4. 通过 → 启动
sig = RSA-PKCS1v15(payload)
license = b64(payload).b64(sig)
```

机器码算法（[[10-架构与技术栈]] 详细）：

```python
def _machine_id() -> str:
    mac = uuid.getnode()           # 容器 MAC
    host = os.uname().nodename     # 容器 hostname
    return sha256(f"{mac}:{host}").hexdigest()[:16]
```

> [!warning] 容器 MAC 不固定
> Docker 默认每次重建容器分配新 MAC → 机器码每次重建会变 → 绑机器 license 会失效。
> **必须在 docker-compose.yml 固定 MAC** 才能稳定绑定，本项目已固定为 `02:42:ac:de:ad:01`。

## 完整操作流程

### Step 1：客户拿机器码

让客户在他服务器跑（**优先用这条**，不依赖容器是否在跑）：

```bash
docker run --rm --mac-address "02:42:ac:de:ad:01" \
  mrsxs/douyin-spark:latest \
  python -c "from app.license import _machine_id; print(_machine_id())"
```

> [!note] 为什么指定 `--mac-address`
> 这条命令启动一个临时容器，必须用和正式容器**完全一样的 MAC**，拿到的机器码才匹配未来正式运行时的机器码。

输出：16 位 hex，如 `8f7e6d5c4b3a2918`

如果客户已经有运行中的容器：
```bash
docker exec douyin-spark python -c "from app.license import _machine_id; print(_machine_id())"
```

### Step 2：卖家签 license

```bash
cd /Users/apple/item

python3 tools/issue_license.py \
    --days 365 \
    --tier pro \
    --machine 客户给的机器码 \
    --note "客户姓名 - 严格绑定"
```

输出 license 字符串发给客户。

### Step 3：客户更新 .env

```ini
LICENSE_KEY=新签的绑机器 license
LICENSE_STRICT=1               # 关键这一行
```

> [!important] 必须 `LICENSE_STRICT=1`
> 即使 license 里有 machine 字段，没设 `LICENSE_STRICT=1` 也会被忽略（视作宽松）。

### Step 4：重启验证

```bash
docker compose restart
docker compose logs --tail 30 | grep -A 5 "License 已验证"
```

成功输出：

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✓ License 已验证
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  机器绑定: 严格 · 8f7e6d5c4b3a2918
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

`机器绑定: 严格` = 已生效。

## 失败场景

### 「License 绑定的机器码与当前不匹配」

```
❌ 许可验证失败
─────────────────────────────────────────
  License 绑定的机器码与当前不匹配
  绑定机器：abc123def456
  当前机器：xyz789abc123
─────────────────────────────────────────
```

| 原因 | 解决 |
|---|---|
| 客户换机器了（云迁移） | 客户重新跑 Step 1 拿新机器码 → 卖家重新签 |
| docker-compose.yml 没固定 MAC | 改 compose 加 `mac_address: "02:42:ac:de:ad:01"` → `down && up -d` 重建 → 重新拿机器码 → 重签 |
| 客户复制 license 到第二台机器 | **预期行为，正常拒绝**。如客户合理要求多台，签多份 |

### 临时回退到宽松

如果客户搞坏了机器码 / 急用，**不开 STRICT** 即可绕过：

```ini
# LICENSE_STRICT=1   ← 注释掉
```

`docker compose restart`。系统会跳过机器码校验（payload 里有但不查），其他校验照常（过期/签名）。

## 解绑 / 换机器流程

### 客户场景：服务器迁移

1. 旧机器：`docker compose down`（停了不能拿机器码）
2. 新机器：跑 Step 1 命令拿新机器码
3. 客户发新机器码给卖家
4. 卖家用 issue_license.py 重新签（`--machine 新机器码`），剩余天数沿用旧 license 剩余天数
5. 客户更新 .env `LICENSE_KEY=` 重启

> [!tip] 卖家保留 license 签发记录
> 建议在自己本地维护一个表格：客户 → license 字符串 → 机器码 → 到期日 → 签发日。客户找你换机器时方便查"剩余多少天"。

### 极端：私钥泄露 → 重新发整套

详见 [[40-开发流程#重新生成 License 密钥对（紧急情况）]]。这不是日常操作。

## 完整命令模板（卖家用）

### 严格绑定 + 1 年
```bash
python3 tools/issue_license.py --days 365 --tier pro --machine 客户机器码 --note "客户名"
```

### 宽松不绑 + 1 年
```bash
python3 tools/issue_license.py --days 365 --tier pro --note "客户名"
```

### 试用 7 天
```bash
python3 tools/issue_license.py --days 7 --tier basic --note "试用"
```

### 严格 + 试用
```bash
python3 tools/issue_license.py --days 7 --tier basic --machine xxx --note "试用-客户名"
```

## 风险与限制

> [!danger] 严格模式不能防的
> - 客户给你假机器码（要求他先 `docker run --rm` 跑一次给你看输出截图）
> - 客户用同一个云提供商克隆机器（可能 MAC 派生算法相同）
> - 客户改本地系统时间绕过过期（用[[40-开发流程#在线心跳验证]]才能挡）
> - 客户反编译 .so 跳过 license 检查（Cython 难度高但不是不可能）

> [!info] 严格模式适合的场景
> - 一次性付费 + 长期有效（预防永久白嫖）
> - 大额订单（投入精力对接客服值得）
> - B2B SaaS（机器固定，运维少）

> [!info] 严格模式不适合的场景
> - 高频换机器的客户（家庭网络/动态 IP）
> - 个人用户多设备（电脑+服务器+笔记本）
> - 短期试用（验证太繁琐）

## 卖家操作 checklist

签发严格 license 前：

- [ ] 收到客户付款
- [ ] 客户给了机器码（建议要截图证明是命令输出）
- [ ] 用 `tools/issue_license.py` 签发
- [ ] **本地记录**：客户 / 机器码 / 到期 / 备注
- [ ] License 字符串发客户（推荐微信/邮件，避免 IM 中转截断）
- [ ] 提醒客户 `.env` 同时加 `LICENSE_STRICT=1`
- [ ] 客户报告"已生效，看到机器绑定: 严格" → 完成
