# douyin_im — 抖音 IM 续火花工具

纯 HTTP 实现（仅首次登录使用浏览器）的抖音私信续火花工具。支持每日自动给所有火花联系人发消息。

## 功能

- ✅ 扫码登录（一次性）+ 短信登录（纯 HTTP，需 ddddocr 服务）
- ✅ 纯 HTTP 拉取火花联系人列表（含备注名/昵称，从 Douyin spotlight/relation 接口）
- ✅ 纯 HTTP 发送消息（完整 proto 签名：token + ts_sign + sdk_cert + reuqest_sign）
- ✅ 自动续火花（CLI `--auto`，按模板发，防风控间隔 3-10s 随机）
- ✅ 个性化消息模板（每个联系人可配置独立消息列表）
- ✅ Cookies 失效检测
- ✅ 结构化日志（`~/.douyin_logs/`）
- ⚠️  浏览器仅在首次登录时使用（抓 cookies + private_key + init_req）

## 依赖

- **Python 3.10+**
- **Node.js 18+**（子进程生成 a_bogus / req_sign 签名）
- Chrome（仅首次登录扫码用）
- Python 包: `requests`, `qrcode`, `websocket-client`
- Node 包: `jsrsasign`（会在首次运行时从 `npm install` 装上）
- 可选: 自建 [ddddocr](https://github.com/sml2h3/ddddocr) 服务（短信登录滑块识别）

```bash
pip install requests qrcode websocket-client
cd /path/to/item && npm install jsrsasign
```

## 首次使用

**1. 登录**
```bash
python3 douyin_im.py
# 选 [2] 扫码登录 → 用 Douyin App 扫码
```

**2. 抓取私钥**（一次性）
```bash
python3 fetch_security.py
# Chrome 短暂打开 → 自动读 localStorage 私钥 → 保存
```

**3. 查看联系人**
```bash
python3 douyin_im.py --list
```

**4. 配置模板**（可选，默认每人发"早"）
```bash
python3 douyin_im.py --config
```

会进入交互式向导：
```
============================================================
  续火花配置向导
============================================================
   [0] ✓ [default 兜底]         "早"
   [1] ✓ 小周                562天  "老板好"
   [2] ✗ 周大明               402天  (禁用, 不发)
   [3] ✓ 27                254天  (走 default 兜底)
   [4] ✓ 吴天成               234天  随机 2 条: ['早', '早安']
   [5] ✓ 山高水长.             193天  (走 default 兜底)
   [6] ✓ 陈小舟                31天  "今天忙吗"
============================================================
操作:
  数字 0..N  - 编辑该联系人 (0 = default 兜底)
  A          - 全部启用  /  N - 全部禁用
  M <文本>   - 批量设置：所有联系人消息统一为 <文本>
  Q          - 保存并退出
```

选一个数字进入该联系人的编辑子菜单：
```
── 编辑 小周 (562天) ──
  状态: ✓ 启用
  消息列表 (2 条，发送时随机选一条):
    1. '老板好'
    2. '最近怎么样'

  操作:
    t  切换启用/禁用
    a  添加消息
    d  删除消息
    c  清空并重设
    q  保存返回
```

## CLI 用法

```bash
python3 douyin_im.py              # 交互式（手动选联系人发消息）
python3 douyin_im.py --list       # 仅列出火花联系人
python3 douyin_im.py --auto       # 自动续火花（按模板，所有启用的联系人）
python3 douyin_im.py --config     # 展示/提示编辑模板
```

## 消息模板（`~/.douyin_templates.json`）

```json
{
  "default": {
    "enabled": true,
    "messages": ["早", "早上好", "在吗"]
  },
  "20000000003": {
    "name": "小周",
    "enabled": true,
    "messages": ["老板好", "今天忙吗"]
  },
  "20000000002": {
    "name": "周大明",
    "enabled": false,
    "messages": []
  }
}
```

- `enabled: false` → 跳过该联系人
- `messages: []` → 走 `default`
- `messages: [...]` → 从数组里随机选一条

## 定时任务（macOS launchd）

把 `contrib/com.douyin.keepalive.plist` 复制到 `~/Library/LaunchAgents/`，修改里面的路径和时间（默认每天早 9 点），然后：

```bash
launchctl load ~/Library/LaunchAgents/com.douyin.keepalive.plist
```

卸载：
```bash
launchctl unload ~/Library/LaunchAgents/com.douyin.keepalive.plist
```

## 文件清单

### 代码
- `douyin_im.py` — 主程序
- `fetch_security.py` — 一次性抓私钥
- `lib/abogus.js` / `a_bogus_core.js` — a_bogus 签名（URL 用）
- `lib/dy_ab.js` / `dy_signer.js` — req_sign 签名（消息体用）

### 运行时数据（`~/` 下，不要提交）
| 文件 | 来源 | 用途 |
|---|---|---|
| `~/.douyin_cookies.json` | 扫码登录 | sessionid 等 |
| `~/.douyin_init_req.bin` | 扫码登录（CDP 拦截） | session 级 token/ts_sign/sdk_cert |
| `~/.douyin_security.json` | `fetch_security.py`（localStorage） | EC 私钥 |
| `~/.douyin_contacts.json` | spotlight/relation + profile/other 接口 | uid→{nick, remark} 缓存 |
| `~/.douyin_templates.json` | 首次 `--auto` 或 `--config` 自动生成 | 消息模板 |
| `~/.douyin_config.json` | 首次运行生成 | ddocr URL + API key + node 路径 |

## 架构

```
                ┌─ 首次浏览器 ─┐
扫码登录  ──→  cookies  ──→│  init_req.bin  │（session token）
                           │  localStorage   │── fetch_security.py ──→ private_key
                           └──────────────┘

日常运行（纯 HTTP）：
                  Node.js 子进程签名
                  (a_bogus / req_sign)
                         │
  get_message_by_init ──→ 火花 + conv_short_id + ticket
                         │
  spotlight/relation   ──→ 备注名 + 昵称
                         │
  /v1/message/send     ──→ 发送（token + ts_sign + sdk_cert + reuqest_sign）
```

## 已知限制

1. **Cookies 过期**：Douyin cookies 一般 30 天。失效后需重新扫码登录。
2. **private_key 仅提取一次**：如果浏览器 localStorage 被清空需重新运行 `fetch_security.py`。
3. **短信登录还未 100% 打通**：Douyin 的 `passport/web/send_code/` 目前还要 `passport_jssdk` 的 sign/qs 签名，未实现。推荐用扫码登录。
4. **仅 macOS**：`CHROME_BIN` 是硬编码的 macOS 路径，跨平台需改代码。

## 致谢

参考了以下开源项目：
- [JoeanAmier/TikTokDownloader](https://github.com/JoeanAmier/TikTokDownloader) — a_bogus/X-Bogus JS 实现
- [cv-cat/DouYin_Spider](https://github.com/cv-cat/DouYin_Spider) — 消息发送签名算法 + proto 结构
- [leonfang-dev/NewMediaOperator](https://github.com/leonfang-dev/NewMediaOperator) — 官方 proto schema
- [sml2h3/ddddocr](https://github.com/sml2h3/ddddocr) — 滑块识别服务

## 免责声明

本项目仅供学习研究使用。使用者应遵守抖音的服务条款，自行承担使用风险。
