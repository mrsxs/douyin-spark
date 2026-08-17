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
  "machine": "abc123" 或 null,         3. payload 里有 machine：
  "tier": "pro",                          对比当前机器指纹
  ...                                     不一致 → 拒绝
}                                       4. 通过 → 启动
sig = RSA-PKCS1v15(payload)
license = b64(payload).b64(sig)
```

> [!important] 绑定没有客户端开关
> payload 受 RSA 签名保护，客户改不了。签发时绑了机器码，校验就无条件生效。
> 旧的 `LICENSE_STRICT` 环境变量已废弃：它由客户端控制，删掉就能解绑，等于没绑。

机器码算法（2026-07 起）：

```python
def _machine_id() -> str:
    # 持久化安装 ID，存 {DATA_DIR}/.install_id，首次启动生成
    return _install_id() or _legacy_machine_id()
```

校验时接受三种指纹中的任意一种，避免误伤存量客户：

| 指纹 | 来源 | 用途 |
|---|---|---|
| `.install_id` | 数据卷里的随机 16 hex | 新签发推荐，跨容器重建/升级最稳 |
| legacy | `sha256(MAC:hostname)` | 兼容存量已绑定的 License |
| mac-only | `sha256(MAC)` | 存量客户容器重建、hostname 变化后的兜底 |

> [!warning] 容器 MAC / hostname 都要固定
> Docker 默认每次重建容器会分配新 MAC、新 hostname。
> `docker-compose.yml` 已同时固定 `mac_address: "02:42:ac:de:ad:01"` 和 `hostname: douyin-spark`。
> 新算法用 `.install_id` 后已不依赖这两者，但固定它们能让存量 License 继续有效。

## 完整操作流程

### Step 1：客户拿机器码

机器码存在数据卷里，**必须在挂载了 `/data` 的正式容器里取**：

```bash
docker exec douyin-spark python -c "from app.license import _machine_id; print(_machine_id())"
```

输出：16 位 hex，如 `8f7e6d5c4b3a2918`

> [!danger] 不能用 `docker run --rm` 取码
> 临时容器没挂客户的数据卷，会现场生成一个随即被丢弃的 `.install_id`，
> 拿到的码是错的，签发后客户依然启动不了。

> [!note] 客户还没跑起来怎么办
> 先发一个宽松 License（不带 `--machine`）让客户启动一次，
> `.install_id` 会自动生成，再按上面取码换严格 License。

### Step 2：卖家签 license

```bash
cd /Users/apple/code/python/抖音续火花

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
```

> [!note] 不需要额外开关
> 绑定校验随 License 自动生效。旧文档要求的 `LICENSE_STRICT=1` 已废弃并被忽略。

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
| 客户没恢复数据卷就重装 | `.install_id` 丢了 → 重新取码重签；提醒客户备份 `data/` 目录 |
| 客户复制 license 到第二台机器 | **预期行为，正常拒绝**。如客户合理要求多台，签多份 |

### 临时回退到宽松

客户端**没有任何开关**可以关闭绑定校验（这是刻意的）。
客户急用时，唯一办法是卖家重新签一个不带 `--machine` 的 License 发过去：

```bash
python3 tools/issue_license.py --days 剩余天数 --tier pro --note "客户名-临时宽松"
```

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
> - 客户给你假机器码（要求他先跑一次 `docker exec` 命令给你看输出截图）
> - 客户复制整个 `data/` 目录到另一台机器（`.install_id` 跟着走 → 绑定被绕过）
> - 客户改本地系统时间绕过过期（用[[40-开发流程#在线心跳验证]]才能挡）
> - 客户反编译 .so 跳过 license 检查（Cython 难度高但不是不可能）

> [!info] 离线 License 的固有限制
> 客户完全控制运行环境时，任何离线方案都挡不住有心破解，
> 机器绑定挡的是「随手把镜像转给朋友用」这一类。
> 真要防转售，只能上在线心跳校验。

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
- [ ] 提醒客户备份 `data/` 目录（含 `.install_id`，丢了要重新签）
- [ ] 客户报告"已生效，看到机器绑定: 严格" → 完成
