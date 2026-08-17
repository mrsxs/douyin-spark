#!/usr/bin/env python3
"""
抖音IM纯API脚本
- 扫码登录（终端显示二维码）
- 查看火花联系人列表
- 发送文字消息

依赖: pip install requests qrcode websocket-client
"""

import base64
import binascii
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zlib
from collections import Counter
from urllib.parse import urlencode

import requests

# ── 多租户存储（每用户/账户一个目录 data/users/<user_id>/accounts/<account_id>/）──
# SaaS 化后：上下文用线程局部存储，每个请求/任务独立切换
from dataclasses import dataclass
_ACCOUNTS_ROOT = os.environ.get("DOUYIN_DATA_ROOT", os.path.expanduser("~/.douyin"))

_ctx_local = threading.local()


@dataclass
class AccountCtx:
    """一个用户的某个抖音账户上下文（由 web 层构造传入）"""
    user_id: int
    account_id: int
    def dir(self) -> str:
        return os.path.join(_ACCOUNTS_ROOT, "users", str(self.user_id),
                            "accounts", str(self.account_id))


def set_account_ctx(ctx: AccountCtx | None):
    """设置当前线程的账户上下文（入口必调）"""
    _ctx_local.ctx = ctx
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


def get_account_ctx() -> AccountCtx | None:
    return getattr(_ctx_local, "ctx", None)


def _account_dir() -> str:
    """返回当前上下文的账户目录（自动创建）"""
    ctx = get_account_ctx()
    if ctx is None:
        # 兼容遗留：允许单机 CLI 通过 env 指定旧式 account name
        legacy_name = os.environ.get("DOUYIN_ACCOUNT")
        if legacy_name:
            d = os.path.join(_ACCOUNTS_ROOT, legacy_name)
            os.makedirs(d, exist_ok=True)
            return d
        raise RuntimeError("AccountCtx 未设置；请先调 set_account_ctx(AccountCtx(user_id, account_id))")
    d = ctx.dir()
    os.makedirs(d, exist_ok=True)
    return d


# ── 兼容老 CLI 的遗留函数（web 层不再使用）──

# 老 CLI 的默认账户名。原来只有 `--help` 文案和 `--list-accounts` 引用这个名字，
# 却从没定义过：纯 Python 下这两条分支一跑就 NameError（平时走不到，没人发现），
# 而 cythonize 时是编译错误，会让整个 .so 编不出来。
_DEFAULT_ACCOUNT = "default"


def set_account(account: str):
    """【遗留】按账户名切换（老 CLI 用）"""
    os.environ["DOUYIN_ACCOUNT"] = account or _DEFAULT_ACCOUNT
    set_account_ctx(None)


def list_accounts() -> list[str]:
    """【遗留】列出旧式 account 名（web 层改为查 DB）"""
    root = os.path.expanduser("~/.douyin")
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "cookies.json")):
            out.append(name)
    return out


def _migrate_legacy_files():
    """【遗留】旧式 ~/.douyin_*.json → ~/.douyin/default/（仅 CLI 用）"""
    pass


# 运行时路径代理：访问时解析到当前上下文的目录
class _AccountPath:
    def __init__(self, name: str):
        self._name = name
    def __repr__(self) -> str:
        return self._resolve()
    def __str__(self) -> str:
        return self._resolve()
    def __fspath__(self) -> str:
        return self._resolve()
    def __eq__(self, other) -> bool:
        return self._resolve() == other
    def __hash__(self) -> int:
        return hash(self._resolve())
    def startswith(self, s):
        return self._resolve().startswith(s)
    def endswith(self, s):
        return self._resolve().endswith(s)
    def _resolve(self) -> str:
        return os.path.join(_account_dir(), self._name)


COOKIE_FILE    = _AccountPath("cookies.json")
INIT_REQ_BIN   = _AccountPath("init_req.bin")
CONTACTS_FILE  = _AccountPath("contacts.json")
TEMPLATES_FILE = _AccountPath("templates.json")
SECURITY_FILE  = _AccountPath("security.json")      # 覆盖之前定义，下面会重新 export
# 全局文件（不分账户）
CONFIG_FILE    = os.path.expanduser("~/.douyin_config.json")
LOG_DIR        = os.path.expanduser("~/.douyin_logs")
CHROME_BIN     = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEBUG_PORT     = 9223   # 避免与已有 Chrome 调试端口冲突
NODE_ABOGUS_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib", "abogus.js")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://www.douyin.com/", "Origin": "https://www.douyin.com"}

_CONFIG_CACHE: dict | None = None


def _config() -> dict:
    """读取 ~/.douyin_config.json；首次不存在则写模板并退出"""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    if not os.path.exists(CONFIG_FILE):
        # 默认从环境变量读（生产部署走 .env / docker-compose environment）
        tpl = {
            "captcha_solver_url":      os.environ.get("DOUYIN_CAPTCHA_URL", ""),
            "captcha_solver_endpoint": os.environ.get("DOUYIN_CAPTCHA_ENDPOINT", "/slide_match"),
            "captcha_solver_api_key":  os.environ.get("DOUYIN_CAPTCHA_KEY", ""),
            "node_bin":                os.environ.get("DOUYIN_NODE_BIN") or shutil.which("node") or "/usr/local/bin/node",
            "default_mobile":          "",
        }
        json.dump(tpl, open(CONFIG_FILE, "w"), ensure_ascii=False, indent=2)
        print(f"已生成 {CONFIG_FILE}，确认后重新运行（可修改 default_mobile / node_bin）")
        sys.exit(0)
    _CONFIG_CACHE = json.load(open(CONFIG_FILE))
    return _CONFIG_CACHE


# ── Douyin a_bogus 签名（通过 Node.js 子进程）──────────────────────────

def _sign_params(params: dict) -> str:
    """给 params 追加 a_bogus 签名，返回完整 query string（URL 编码过）"""
    qs = urlencode(params)
    try:
        out = subprocess.check_output(
            [_config()["node_bin"], NODE_ABOGUS_JS, qs, UA],
            timeout=8,
            stderr=subprocess.PIPE,
        )
        return out.decode().strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"a_bogus 签名失败: {e.stderr.decode(errors='replace')}")
    except FileNotFoundError:
        raise RuntimeError(f"找不到 Node.js: {_config()['node_bin']}，请在 {CONFIG_FILE} 更新 node_bin")


def _signed_url(base_url: str, params: dict) -> str:
    """组装带签名的完整 URL"""
    return base_url + ("&" if "?" in base_url else "?") + _sign_params(params)


# ── Douyin IM 发消息签名（dy_ab.js via Node.js）────────────────────────

NODE_DY_SIGNER_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib", "dy_signer.js")
# SECURITY_FILE 已在顶部 _AccountPath 定义，这里不再重复


def _call_dy_signer(cmd: str, *args) -> str:
    """调用 lib/dy_signer.js，返回 stdout 字符串"""
    try:
        out = subprocess.check_output(
            [_config()["node_bin"], NODE_DY_SIGNER_JS, cmd, *args],
            timeout=10,
            stderr=subprocess.PIPE,
        )
        return out.decode().strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"dy_signer {cmd} 失败: {e.stderr.decode(errors='replace')}")


def _im_req_sign(sign_data: str, private_key: str) -> str:
    """计算 IM 请求签名（field 25 reuqest_sign）"""
    payload = {"sign_data": sign_data, "certType": "cookie", "scene": "web_protect"}
    return _call_dy_signer("req_sign", json.dumps(payload, ensure_ascii=False), private_key)


# ── 加密辅助（cookies / private_key 敏感文件 Fernet 加密存储）────────
# 密钥从环境变量 SAAS_CRYPT_KEY 读（由 app.config 启动时设置，取自 settings.secret_key）
# 向后兼容：解密失败时降级读明文 JSON，首次写入转为密文

_ENCRYPTED_MAGIC = b"ENCv1:"   # 密文文件前缀，用于识别格式


def _get_cipher():
    """返回 Fernet 实例；没配置则返回 None（自动降级到明文）"""
    try:
        from cryptography.fernet import Fernet
        import base64, hashlib
        raw = os.environ.get("SAAS_CRYPT_KEY", "")
        if not raw:
            return None
        # 任意长度字符串 → SHA-256 → urlsafe_b64（Fernet 要求 32 字节 base64）
        key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
        return Fernet(key)
    except Exception as e:
        print(f"[crypt] 初始化失败，将退回明文存储: {e}")
        return None


def _secure_write(path: str, data: dict) -> None:
    """原子写 JSON，若 cipher 可用则加密。
    写同目录的 tmp 文件 → fsync → chmod 0600 → os.replace → fsync 目录。
    进程崩溃时目标文件保持旧内容不会被破坏。
    """
    import tempfile
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    cipher = _get_cipher()
    blob = (_ENCRYPTED_MAGIC + cipher.encrypt(payload)) if cipher else payload

    dir_path = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, prefix=".tmp_", suffix=".swap")
    try:
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            fd = None  # 已被 fdopen 接管
            raise
        try:
            os.chmod(tmp_path, 0o600)
        except Exception:
            pass
        os.replace(tmp_path, path)
        # fsync 父目录，确保 rename 元数据落盘
        try:
            dfd = os.open(dir_path, os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        except Exception:
            pass
    except Exception:
        # 清理残留的 tmp 文件
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass
        raise


def _secure_read(path: str) -> dict:
    """读 JSON，自动识别明文 / 加密格式"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return {}
    if raw.startswith(_ENCRYPTED_MAGIC):
        cipher = _get_cipher()
        if not cipher:
            print(f"[crypt] 文件 {path} 是密文但未配置密钥，无法解密")
            return {}
        try:
            payload = cipher.decrypt(raw[len(_ENCRYPTED_MAGIC):])
            return json.loads(payload.decode("utf-8"))
        except Exception as e:
            print(f"[crypt] 解密 {path} 失败: {e}")
            return {}
    # 明文旧格式：读一次并转为密文（惰性迁移）
    try:
        data = json.loads(raw.decode("utf-8"))
        if _get_cipher():
            _secure_write(path, data)
            print(f"[crypt] 已把 {path} 从明文迁移为加密存储")
        return data
    except Exception:
        return {}


def _load_security() -> dict:
    if not os.path.exists(SECURITY_FILE):
        return {}
    return _secure_read(SECURITY_FILE)


def _save_security(data: dict):
    _secure_write(SECURITY_FILE, data)


# ── 消息模板（续火花）─────────────────────────────────────────────────

def _load_templates() -> dict:
    """加载或创建默认模板: {uid: {"name": ..., "enabled": bool, "messages": [...]}}"""
    if os.path.exists(TEMPLATES_FILE):
        try:
            return json.load(open(TEMPLATES_FILE))
        except Exception:
            pass
    return {}


def _save_templates(tpl: dict):
    json.dump(tpl, open(TEMPLATES_FILE, "w"), ensure_ascii=False, indent=2)


def _pick_message(uid: str, name: str, templates: dict) -> str | None:
    """按优先级选择消息：
    - 联系人无 entry → 跳过（新联系人默认不自动续，必须用户显式启用）
    - entry.enabled=False → 跳过
    - 有 messages → 随机一条
    - 已启用但 messages 空 → 兜底到 default 模板
    """
    entry = templates.get(uid) or templates.get(name)
    if not entry:
        return None
    if entry.get("enabled") is False:
        return None
    msgs = entry.get("messages") or []
    if msgs:
        return random.choice(msgs)
    fallback = templates.get("default")
    if fallback and fallback.get("enabled", True):
        fb_msgs = fallback.get("messages") or []
        if fb_msgs:
            return random.choice(fb_msgs)
    return None


def _ensure_templates_exist(contacts: list[dict]) -> dict:
    """首次运行：为每个联系人生成默认模板，并写文件"""
    tpl = _load_templates()
    dirty = False
    if "default" not in tpl:
        tpl["default"] = {"enabled": True, "messages": ["早"]}
        dirty = True
    for c in contacts:
        if c["uid"] not in tpl:
            tpl[c["uid"]] = {
                "name":     c.get("nickname") or c["uid"],
                "enabled":  True,
                "messages": [],          # 空表示用 default
            }
            dirty = True
    if dirty:
        _save_templates(tpl)
    return tpl


# ── 日志（结构化）─────────────────────────────────────────────────────

def _log(line: str, tag: str = "INFO"):
    """写到 data/users/<uid>/accounts/<aid>/logs/YYYY-MM-DD.log 并同时 print"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    ctx = get_account_ctx()
    label = f"u{ctx.user_id}/a{ctx.account_id}" if ctx else "default"
    entry = f"[{ts}] [{label}] {tag} {line}"
    print(entry)
    try:
        log_dir = os.path.join(_account_dir(), "logs")
        os.makedirs(log_dir, exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        with open(os.path.join(log_dir, f"{day}.log"), "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass


# ── 登录态检测 ────────────────────────────────────────────────────────

def _verify_session_alive(session: requests.Session) -> bool:
    """用 get_message_by_init 快速检查 session 是否有效"""
    if not os.path.exists(INIT_REQ_BIN):
        return False
    try:
        r = session.post(
            "https://imapi.douyin.com/v1/message/get_message_by_init",
            data=open(INIT_REQ_BIN, "rb").read(),
            headers={**HEADERS, "Content-Type": "application/x-protobuf"},
            timeout=8,
        )
        return r.status_code == 200 and len(r.content) > 1024
    except Exception:
        return False


def _extract_security_sdk(cdp) -> dict:
    """通过 CDP 读 localStorage 抽出 IM 签名所需的私钥等数据"""
    out = {}
    # 私钥（crypt）
    r1 = cdp.cmd(
        "Runtime.evaluate",
        expression='localStorage["security-sdk/s_sdk_crypt_sdk"] || ""',
        returnByValue=True, timeout=5,
    )
    crypt = (r1.get("result") or {}).get("value", "") or ""
    if crypt:
        try:
            inner = json.loads(json.loads(crypt)["data"])
            out["private_key"] = inner.get("ec_privateKey", "")
        except Exception:
            pass
    # ticket/ts_sign/client_cert（sign_data_key/web_protect 有时为空，走 init_req.bin 获取）
    r2 = cdp.cmd(
        "Runtime.evaluate",
        expression='localStorage["security-sdk/s_sdk_sign_data_key/web_protect"] || ""',
        returnByValue=True, timeout=5,
    )
    wp = (r2.get("result") or {}).get("value", "") or ""
    if wp:
        try:
            inner = json.loads(json.loads(wp)["data"])
            out["ticket"]      = inner.get("ticket", "")
            out["ts_sign"]     = inner.get("ts_sign", "")
            out["client_cert"] = inner.get("client_cert", "")
        except Exception:
            pass
    # s_v_web_id cookie（verifyFp/fp 用）
    r3 = cdp.cmd("Network.getAllCookies", timeout=5)
    for c in (r3.get("cookies") or []):
        if c.get("name") == "s_v_web_id" and "douyin" in c.get("domain", ""):
            out["s_v_web_id"] = c.get("value", "")
            break
    return out


# ── Douyin 滑块验证码（对接 DDDocr）────────────────────────────────────

def _fake_trajectory(x_target: int) -> list[dict]:
    """生成拟人滑块轨迹: ease-in-out + 抖动, 总时长 500~900ms"""
    pts: list[dict] = []
    total_ms = random.randint(500, 900)
    steps    = random.randint(30, 50)
    t_acc    = 0
    for i in range(steps):
        p = i / max(1, steps - 1)
        eased = 3 * p * p - 2 * p * p * p
        x = int(x_target * eased + random.uniform(-1.5, 1.5))
        y = int(random.uniform(-3, 3))
        t_acc += total_ms // steps + random.randint(-5, 5)
        pts.append({"x": max(0, x), "y": y, "relative_time": t_acc})
    if pts:
        pts[-1]["x"] = x_target
    return pts


def _solve_slider(bg_url: str, slider_url: str) -> tuple[int, list[dict]]:
    """下载两图 → POST DDDocr /slide_match → 解 x 偏移 + 拟人轨迹"""
    bg_b64     = base64.b64encode(requests.get(bg_url,     timeout=10).content).decode()
    slider_b64 = base64.b64encode(requests.get(slider_url, timeout=10).content).decode()
    cfg = _config()
    url = cfg["captcha_solver_url"].rstrip("/") + cfg.get("captcha_solver_endpoint", "/slide_match")
    r = requests.post(
        url,
        headers={"X-API-Key": cfg.get("captcha_solver_api_key", "")},
        json={"target": slider_b64, "background": bg_b64, "simple_target": False},
        timeout=20,
    )
    body = r.json()
    code = body.get("code")
    if code not in (0, 200):
        raise RuntimeError(f"DDDocr 返回错误: code={code} msg={body.get('msg') or body.get('message')}")
    # 兼容 ocr.mrsong.top 的 result 字段 与 旧服务的 data 字段
    data = body.get("result")
    if data is None:
        data = body.get("data") or {}
    # 兼容多种返回: {"target":[x1,y1,x2,y2]} / {"x":123} / {"target_y":..,"x":..}
    if isinstance(data, dict) and isinstance(data.get("target"), list) and len(data["target"]) >= 1:
        x = int(data["target"][0])
    elif isinstance(data, dict) and "x" in data:
        x = int(data["x"])
    else:
        raise RuntimeError(f"DDDocr 响应格式未识别: {body}")
    return x, _fake_trajectory(x)


def _handle_douyin_captcha(session: requests.Session, aid: str = "1128") -> str:
    """获取 Douyin captcha 挑战 → DDDocr 解 → 提交，返回 verify_ticket"""
    qs = _sign_params({
        "aid": aid, "iid": "0", "did": "0",
        "os_type": "0", "lang": "zh",
        "app_key": "6383", "service": "verify_ticket",
    })
    get_r = session.get(
        f"https://verify.snssdk.com/captcha/get?{qs}",
        timeout=10,
    ).json()
    data = get_r.get("data") or {}
    ch_id = data.get("id")
    question = data.get("question") or {}
    url1 = question.get("url1") or question.get("big_image")        # bg
    url2 = question.get("url2") or question.get("small_image")      # slider
    if not (ch_id and url1 and url2):
        raise RuntimeError(f"captcha/get 响应异常: {get_r}")

    print(f"  拿到挑战 id={ch_id}，调用 DDDocr 识别...")
    x, reply = _solve_slider(url1, url2)
    print(f"  识别偏移 x={x} px, 轨迹 {len(reply)} 点")

    qs2 = _sign_params({"aid": aid})
    v_r = session.post(
        f"https://verify.snssdk.com/captcha/verify?{qs2}",
        json={
            "id": ch_id,
            "modified_img_width": 552,
            "reply": reply,
            "mode": "slide",
        },
        timeout=10,
    ).json()
    ticket = (v_r.get("data") or {}).get("verify_ticket", "")
    if not ticket:
        raise RuntimeError(f"captcha/verify 失败: {v_r}")
    return ticket


# ── 短信登录 ──────────────────────────────────────────────────────────

def _encrypt_mix_mode(s: str) -> str:
    """Douyin mix_mode=1: 每字节 XOR 5 后 hex 编码"""
    return "".join(f"{b ^ 0x05:02x}" for b in s.encode())


def _webid() -> str:
    """生成 19 位雪花风格 webid（纯随机）"""
    return str(random.randint(10 ** 18, 10 ** 19 - 1))


def _prime_session(s: requests.Session) -> None:
    """访问 douyin.com 主页，让 msToken/ttwid cookie 就位"""
    try:
        s.get("https://www.douyin.com/", timeout=10)
    except Exception:
        pass


_SMS_SEND_ENDPOINTS = [
    "https://login.douyin.com/passport/web/send_code/",
    "https://www.douyin.com/passport/web/send_code/",
]
_SMS_LOGIN_ENDPOINTS = [
    "https://login.douyin.com/passport/web/sms_login/",
    "https://www.douyin.com/passport/web/sms_login/",
]


def _debug_http(tag: str, r: requests.Response) -> None:
    ct = r.headers.get("Content-Type", "")
    body = r.text[:300]
    print(f"  [{tag}] HTTP {r.status_code}  CT={ct}  body[:300]={body!r}")


def _post_try_endpoints(s: requests.Session, endpoints: list[str],
                        params: dict, body: dict | None = None) -> dict:
    """依次尝试多个 URL，第一个返回可解析 JSON 的即返回"""
    last_resp = None
    for ep in endpoints:
        qs = _sign_params(params)
        url = f"{ep}?{qs}"
        try:
            if body:
                r = s.post(url, data=body, timeout=10)
            else:
                r = s.post(url, timeout=10)
        except Exception as e:
            print(f"  {ep} 异常: {e}")
            continue
        last_resp = r
        try:
            return r.json()
        except ValueError:
            _debug_http(ep.split("/")[2] + ep.split("/", 3)[-1], r)
    # 全部失败，抛出最后一个响应的内容
    raise RuntimeError(f"所有接口均未返回 JSON (最后: HTTP {last_resp.status_code if last_resp else '?'})")


def _mobile_with_prefix(raw: str) -> str:
    """Douyin 要求 mix_mode 加密前 mobile 必须是 "+86 13800138000" 格式（含空格）"""
    m = raw.lstrip("+").replace(" ", "").strip()
    if m.startswith("86") and len(m) > 11:
        m = m[2:]
    return f"+86 {m}"


def _call_send_sms(s: requests.Session, mobile: str, captcha_ticket: str = "") -> dict:
    # URL query（aid / device_platform 等）
    url_params = {
        "aid": "6383", "device_platform": "web_app", "language": "zh",
        "account_sdk_source": "web", "is_from_ttaccountsdk": "1",
    }
    # POST body (参考浏览器抓包)
    body = {
        "is6Digits":       "1",
        "mix_mode":        "1",
        "mobile":          _encrypt_mix_mode(_mobile_with_prefix(mobile)),
        "type":            _encrypt_mix_mode("24"),    # 24=登录
        "fixed_mix_mode":  "1",
    }
    if captcha_ticket:
        body["verify_ticket"] = captcha_ticket
    return _post_try_endpoints(s, _SMS_SEND_ENDPOINTS, url_params, body)


def _call_sms_login(s: requests.Session, mobile: str, code: str) -> dict:
    url_params = {
        "aid": "6383", "device_platform": "web_app", "language": "zh",
        "account_sdk_source": "web", "is_from_ttaccountsdk": "1",
    }
    body = {
        "mix_mode":        "1",
        "mobile":          _encrypt_mix_mode(_mobile_with_prefix(mobile)),
        "code":            _encrypt_mix_mode(code),
        "fixed_mix_mode":  "1",
    }
    return _post_try_endpoints(s, _SMS_LOGIN_ENDPOINTS, url_params, body)


def sms_login() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    _prime_session(s)

    cfg = _config()
    mobile = cfg.get("default_mobile", "").strip() or input("手机号（纯数字，含区号可省去 +）: ").strip()
    mobile = mobile.lstrip("+").replace(" ", "")

    # Step 1: 发短信（必要时解滑块）
    ticket = ""
    for attempt in range(4):
        resp = _call_send_sms(s, mobile, ticket)
        data = resp.get("data") or {}
        err  = data.get("error_code", resp.get("error_code", -1))
        if err == 0:
            print("✓ 验证码已发送，查看手机")
            break
        if err in (1104, 2046, 8) or "captcha" in str(resp).lower() or "verify" in str(resp).lower():
            print(f"  ⚠ 触发滑块验证 (err={err})，调用 DDDocr...")
            ticket = _handle_douyin_captcha(s)
            print(f"  ✓ 拿到 verify_ticket (len={len(ticket)})，重试发短信")
            continue
        raise RuntimeError(f"发送短信失败: {resp}")
    else:
        raise RuntimeError("重试多次仍失败，放弃")

    code = input("收到的 6 位验证码: ").strip()

    # Step 2: 提交验证码
    lr = _call_sms_login(s, mobile, code)
    data = lr.get("data") or {}
    if data.get("error_code", -1) not in (0,):
        raise RuntimeError(f"登录失败: {lr}")

    # 保存 cookies
    cookies = {c.name: c.value for c in s.cookies if "douyin" in (c.domain or "")}
    if not cookies.get("sessionid"):
        # 服务端把 cookie 设置在其他域（如 .snssdk.com），尝试全收
        cookies = {c.name: c.value for c in s.cookies}
    _secure_write(COOKIE_FILE, cookies)
    print(f"✓ 登录成功，Cookie 已保存至 {COOKIE_FILE}")

    # 补齐 init_req.bin（后续 get_message_by_init 需要）
    if not os.path.exists(INIT_REQ_BIN):
        print("  构造 init_req.bin ...")
        if _bootstrap_init_req(s):
            print(f"  ✓ init_req.bin 已保存")
        else:
            print("  ⚠ 合成 init_req 失败，下次首次使用时需要走扫码路径补齐")
    return s


def _bootstrap_init_req(session: requests.Session) -> bool:
    """构造最小 get_message_by_init 请求体并用 session 发送；成功则保存到 INIT_REQ_BIN"""
    webid    = _webid()
    trace_id = f"{int(time.time() * 1000)}-{random.randint(10**9, 10**10)}"
    hash_val = base64.b64encode(os.urandom(16)).decode().rstrip("=")[:22]

    body = (_pb_v(1, 100) + _pb_v(2, random.randint(10**6, 10**7))
            + _pb_b(3, "1.1.3") + _pb_b(4, hash_val)
            + _pb_v(5, 3) + _pb_v(6, 0) + _pb_b(7, trace_id))
    for k, v in [
        ("session_aid", "6383"), ("session_did", "0"), ("app_name", "douyin_pc"),
        ("priority_region", "cn"), ("user_agent", UA), ("cookie_enabled", "true"),
        ("browser_language", "zh-CN"), ("browser_platform", "MacIntel"),
        ("browser_name", "Mozilla"), ("browser_online", "true"),
        ("screen_width", "1440"), ("screen_height", "900"),
        ("timezone_name", "Asia/Shanghai"), ("deviceId", "0"),
        ("webid", webid), ("is-retry", "0"),
    ]:
        body += _pb_b(15, _pb_b(1, k) + _pb_b(2, v))

    try:
        r = session.post(
            "https://imapi.douyin.com/v1/message/get_message_by_init",
            data=body,
            headers={**HEADERS, "Content-Type": "application/x-protobuf"},
            timeout=15,
        )
        # 响应 > 1KB 说明服务端认可了我们的 init_req（正常是 1MB+）
        if r.status_code == 200 and len(r.content) > 1024:
            open(INIT_REQ_BIN, "wb").write(body)
            return True
        print(f"    bootstrap init_req 响应: HTTP {r.status_code}, {len(r.content)} bytes")
    except Exception as e:
        print(f"    bootstrap init_req 异常: {e}")
    return False

# ── CDP 客户端（同步，基于 websocket-client）──────────────────────────

class CDP:
    """同步 CDP 客户端，使用 websocket.create_connection。"""

    def __init__(self, ws_url: str, port: int = DEBUG_PORT):
        import websocket
        self._ws   = websocket.create_connection(
            ws_url, timeout=15,
            header={"Origin": f"http://localhost:{port}"}
        )
        self._responses: dict        = {}
        self._events: queue.Queue    = queue.Queue()
        self._lock  = threading.Lock()
        self._seq   = 0
        self._alive = True

        t = threading.Thread(target=self._recv_loop, daemon=True)
        t.start()

    def _recv_loop(self):
        import websocket
        while self._alive:
            try:
                raw = self._ws.recv()
                if not raw:
                    break
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid:
                    self._responses[mid] = msg.get("result", {})
                else:
                    self._events.put(msg)
            except websocket.WebSocketConnectionClosedException:
                break
            except Exception:
                break

    def cmd(self, method: str, timeout: float = 15, **params) -> dict:
        with self._lock:
            self._seq += 1
            mid = self._seq
            self._ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if mid in self._responses:
                return self._responses.pop(mid)
            time.sleep(0.05)
        return {}

    def poll_event(self, timeout: float = 1) -> dict | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self._alive = False
        try:
            self._ws.close()
        except Exception:
            pass

# ── 扫码登录 ───────────────────────────────────────────────────────────

def _wait_chrome(port: int, retries: int = 40):
    for _ in range(retries):
        try:
            urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=1)
            time.sleep(0.5)   # 额外等待确保 tab 就绪
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("Chrome 启动超时")

def _display_qr(qr_url: str, qr_b64: str):
    """优先终端 ASCII，否则打开图片"""
    try:
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(qr_url)
        qr.make(fit=True)
        print("\n请用「抖音 App → 左上角 ≡ → 扫一扫」扫码：\n")
        qr.print_ascii(invert=True)
        print()
        return
    except ImportError:
        pass
    # 保存为图片
    img_path = "/tmp/douyin_qr.png"
    with open(img_path, "wb") as f:
        f.write(base64.b64decode(qr_b64))
    subprocess.Popen(["open", img_path])
    print(f"二维码已在图片预览中打开: {img_path}")

def _capture_init_and_contacts(cdp: "CDP", timeout: float = 60) -> tuple[bytes | None, dict]:
    """监听页面网络请求：捕获 init_req 请求体 + 用户信息 API 响应（含备注名）。"""
    deadline = time.time() + timeout
    init_body: bytes | None = None
    contacts: dict = {}   # uid → {"nick": str, "remark": str}
    verifying = False

    while time.time() < deadline:
        if init_body and contacts and time.time() > deadline - 10:
            break

        msg = cdp.poll_event(timeout=1)
        if not msg:
            continue
        method = msg.get("method")

        # ── 检测手机/滑块验证页 ──
        if method == "Page.frameNavigated":
            nav_url = msg["params"]["frame"]["url"]
            if any(k in nav_url for k in ["verify", "passport/web/mobile", "sms", "security"]):
                if not verifying:
                    verifying = True
                    print("\n⚠️  检测到验证码验证，请在浏览器窗口完成操作（手机验证码 / 滑块）...")
                    print("   完成后脚本会自动继续。")
                    deadline = max(deadline, time.time() + 180)  # 最多再等 3 分钟
            elif verifying and "douyin.com" in nav_url:
                verifying = False
                print("✓ 验证完成，继续捕获...")

        # ── 捕获 init_req 请求体 ──
        elif method == "Network.requestWillBeSent" and not init_body:
            req = msg["params"].get("request", {})
            url = req.get("url", "")
            if "get_message_by_init" in url:
                post_data = req.get("postData", "")
                if post_data:
                    init_body = post_data.encode("latin-1") if isinstance(post_data, str) else post_data
                else:
                    req_id = msg["params"]["requestId"]
                    time.sleep(0.3)
                    res = cdp.cmd("Network.getRequestPostData", requestId=req_id, timeout=8)
                    raw = res.get("postData", "")
                    if raw:
                        init_body = raw.encode("latin-1") if isinstance(raw, str) else raw

        # ── 捕获昵称 / 备注（监听用户信息类接口）──
        elif method == "Network.responseReceived":
            url = msg["params"]["response"]["url"]
            if any(k in url for k in ["user/info", "user/profile", "im/user", "imfriend",
                                       "aweme/v1/web/user", "im/contact", "relation/follow"]):
                req_id = msg["params"]["requestId"]
                time.sleep(0.2)
                body_res = cdp.cmd("Network.getResponseBody", requestId=req_id, timeout=8)
                try:
                    data = json.loads(body_res.get("body", "{}"))
                    _extract_contacts(data, contacts)
                except Exception:
                    pass

    return init_body, contacts


def _valid_name(s: str) -> bool:
    """名字有效性检验：非空、非纯数字、非纯下划线/短横线、长度 1-50"""
    s = s.strip()
    if not s or len(s) > 50:
        return False
    if re.fullmatch(r'[\d\s]+', s):        # 纯数字
        return False
    if re.fullmatch(r'[_\-\s]+', s):       # 纯下划线/短横线
        return False
    return True


def _extract_contacts(data: dict, out: dict):
    """从各种 Douyin API 响应格式中提取 uid → {nick, remark}"""
    def _walk(obj):
        if isinstance(obj, list):
            for item in obj:
                _walk(item)
        elif isinstance(obj, dict):
            _extract_one_contact(obj, out)
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    _walk(v)

    _walk(data)


def _extract_one_contact(u: dict, out: dict):
    uid = str(u.get("uid") or u.get("user_id") or "")
    if not uid or not uid.isdigit() or len(uid) < 5:
        return
    nick   = u.get("nickname") or u.get("nick_name") or ""
    remark = (u.get("remark_name") or u.get("remarkName") or
              u.get("remark")     or u.get("alias")      or "")
    if not ((_valid_name(nick)) or (_valid_name(remark))):
        return
    entry = out.get(uid, {"nick": "", "remark": ""})
    if _valid_name(remark): entry["remark"] = remark.strip()
    if _valid_name(nick):   entry["nick"]   = nick.strip()
    out[uid] = entry


def _handle_mfa_verify(page, verify_sink=None, code_provider=None) -> bool:
    """扫码确认后抖音可能强制二次验证（身份验证 → 短信验证码，mfa_web SDK）。
    检测到「身份验证」弹窗 → 点「接收短信验证码」→ 等短信发出 → 取码 → 填入 → 点「验证」。

    verify_sink(masked_phone):  通知外层"需要短信码 + 掩码手机号"（如 130******73）。
    code_provider() -> str|None: 阻塞拿用户提交的验证码；None=超时/取消。
    返回 True 表示检测到弹窗并已驱动（成败都算），调用方据此避免重复触发。
    无回调（CLI 有头）时只打印提示，靠人在窗口里手动完成。
    """
    import re as _re
    try:
        has_modal = (page.get_by_text("身份验证").count() > 0
                     or page.get_by_text("发送短信验证").count() > 0
                     or page.get_by_text("短信已发送至").count() > 0)
    except Exception:
        return False
    if not has_modal:
        return False

    if verify_sink is None or code_provider is None:
        print("\n⚠️  检测到二次验证（身份验证），请在浏览器窗口完成短信验证码...")
        return True

    print("  检测到二次验证，自动驱动短信验证码流程...")
    try:
        # 1. 未发码则点「接收短信验证码」让抖音下发短信
        if page.get_by_text("短信已发送至").count() == 0:
            page.get_by_text("接收短信验证码", exact=True).first.click(timeout=8000)
        # 2. 等短信发出，抓掩码手机号（如 130******73）
        page.wait_for_selector("text=短信已发送至", timeout=15000)
        masked = ""
        try:
            masked = page.get_by_text(_re.compile(r"\d{2,3}\*{2,}\d{2}")).first.inner_text(timeout=3000).strip()
        except Exception:
            pass
    except Exception as e:
        print(f"  [verify] 触发短信验证失败: {e}")
        return True

    # 3. 通知前端 + 阻塞取码
    try: verify_sink(masked)
    except Exception: pass
    code = None
    try: code = code_provider()
    except Exception as e: print(f"  [verify] 取码异常: {e}")
    if not code:
        print("  [verify] 未拿到验证码（超时/取消）")
        return True

    # 4. 填码 + 点「验证」（弹窗内可见的那个输入框/按钮）
    try:
        page.locator("input[placeholder='请输入验证码']:visible").last.fill(str(code).strip(), timeout=8000)
        page.get_by_text("验证", exact=True).last.click(timeout=8000)
        print("  [verify] 验证码已提交")
    except Exception as e:
        print(f"  [verify] 填码/提交失败: {e}")
    return True


def _capture_and_finish(page, context, cookies: dict, captured_nicks: dict,
                        on_logged_in=None) -> requests.Session:
    """登录拿到 sessionid 后的统一收尾（扫码/短信共用）：
    回调外层 → 抓 init_req → 抓 localStorage 私钥/签名 → 落盘 cookies/contacts/security → 返回 session。
    须在 Playwright with 块内调用（用到 page/context）；不负责关 browser（调用方关）。"""
    print("✓ 登录成功，正在捕获 IM 会话数据和联系人昵称...")
    # 立即写 cookies + 回调外层，让前端尽早感知成功
    try:
        _secure_write(str(COOKIE_FILE), cookies)
    except Exception as _e:
        print(f"  预落 cookies 失败: {_e}")
    if on_logged_in is not None:
        try: on_logged_in(cookies)
        except Exception as _e: print(f"  on_logged_in 回调异常: {_e}")

    # 导航到 douyin.com，同步等 get_message_by_init 请求
    init_body = None
    try:
        with page.expect_request(
            lambda r: "get_message_by_init" in r.url and r.method == "POST",
            timeout=30000,
        ) as init_info:
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        init_body = init_info.value.post_data_buffer
    except Exception as e:
        print(f"  警告: 抓取 init_req 失败: {e}")
    # 多等 10 秒让前端拉完 spotlight/relation 等昵称接口
    try:
        page.wait_for_timeout(10000)
    except Exception:
        pass

    # 抓 localStorage 私钥等
    security: dict = {}
    try:
        crypt = page.evaluate('localStorage["security-sdk/s_sdk_crypt_sdk"] || ""') or ""
        if crypt:
            inner = json.loads(json.loads(crypt)["data"])
            security["private_key"] = inner.get("ec_privateKey", "")
    except Exception as e:
        print(f"  抓私钥失败: {e}")
    try:
        wp = page.evaluate('localStorage["security-sdk/s_sdk_sign_data_key/web_protect"] || ""') or ""
        if wp:
            inner = json.loads(json.loads(wp)["data"])
            security["ticket"]      = inner.get("ticket", "")
            security["ts_sign"]     = inner.get("ts_sign", "")
            security["client_cert"] = inner.get("client_cert", "")
    except Exception:
        pass
    for c in context.cookies():
        if c["name"] == "s_v_web_id":
            security["s_v_web_id"] = c["value"]
            break

    # 落盘
    if init_body:
        if isinstance(init_body, str):
            init_body = init_body.encode("latin-1")
        open(str(INIT_REQ_BIN), "wb").write(init_body)
        print(f"  IM 会话数据已保存至 {INIT_REQ_BIN}")
    else:
        print("  警告：未能捕获 IM 会话数据")
    if captured_nicks:
        json.dump(captured_nicks, open(str(CONTACTS_FILE), "w"), ensure_ascii=False)
        print(f"  已保存 {len(captured_nicks)} 个联系人昵称/备注")
    if security:
        _save_security(security)
        print(f"  私钥/签名数据已保存至 {SECURITY_FILE}")
    _secure_write(str(COOKIE_FILE), cookies)
    print(f"  Cookie 已保存至 {COOKIE_FILE}")

    s = requests.Session()
    s.headers.update(HEADERS)
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".douyin.com")
    return s


def qr_login(headless: bool = False, qr_sink=None, on_logged_in=None,
             verify_sink=None, code_provider=None) -> requests.Session:
    """
    扫码登录 — 基于 Playwright + 独立 Chromium（不依赖系统 Chrome）。
    headless:     True 则不弹出窗口（Web 面板用）
    qr_sink:      可选回调 fn(qr_png_b64, qr_url) → 拿到二维码后通知外部
    on_logged_in: 可选回调 fn(cookies: dict) → 刚拿到 sessionid 就触发（让外层尽早告知前端登录成功，
                  init_req/私钥等后续抓取在后台继续）
    verify_sink:  可选回调 fn(masked_phone) → 扫码后需二次验证时通知外层（带掩码手机号）
    code_provider: 可选回调 fn() -> str|None → 阻塞拿用户提交的短信验证码
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "未安装 Playwright，请先运行：\n"
            "  pip install playwright && python3 -m playwright install chromium"
        )

    captured_nicks: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=[
            "--disable-blink-features=AutomationControlled",
        ])
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 800, "height": 600},
        )
        page = context.new_page()

        # 监听用户信息接口，顺便抓联系人昵称/备注
        def on_response(resp):
            url = resp.url
            if any(k in url for k in
                   ["user/info", "user/profile", "im/user", "imfriend",
                    "aweme/v1/web/user", "im/contact", "relation/follow"]):
                try:
                    _extract_contacts(resp.json(), captured_nicks)
                except Exception:
                    pass
        page.on("response", on_response)

        # 进入登录页，同步等 get_qrcode 响应
        print("正在加载登录页面...")
        with page.expect_response(
            lambda r: "get_qrcode" in r.url and r.status == 200,
            timeout=45000,
        ) as qr_info:
            page.goto("https://www.douyin.com/login_page?service=https%3A%2F%2Fwww.douyin.com",
                      wait_until="domcontentloaded")
        qr_resp = qr_info.value
        try:
            qr_data = qr_resp.json().get("data", {}) or {}
        except Exception:
            qr_data = {}
        qr_b64 = qr_data.get("qrcode", "")
        qr_url = qr_data.get("qrcode_index_url", "")
        if not qr_b64:
            browser.close()
            raise RuntimeError(f"get_qrcode 响应异常: {qr_data}")

        if qr_sink is not None:
            try: qr_sink(qr_b64, qr_url)
            except Exception: pass
        else:
            _display_qr(qr_url, qr_b64)
        print("等待扫码确认（最长 2 分钟）...")

        # 轮询 cookies 里是否出现 sessionid；其间检测/处理二次验证（MFA 身份验证）
        cookies: dict = {}
        deadline = time.time() + 120
        verify_done = False
        while time.time() < deadline:
            all_cookies = context.cookies()
            dy_cookies = {c["name"]: c["value"] for c in all_cookies
                          if "douyin" in c.get("domain", "")}
            if dy_cookies.get("sessionid"):
                cookies = {c["name"]: c["value"] for c in all_cookies}  # 全收
                break
            # 扫码确认后抖音可能弹「身份验证」要求短信码 → 自动驱动
            if not verify_done:
                try:
                    if _handle_mfa_verify(page, verify_sink, code_provider):
                        verify_done = True
                        deadline = max(deadline, time.time() + 120)  # 验证后多给时间出 sessionid
                except Exception as _ve:
                    print(f"  [verify] 处理异常: {_ve}")
            time.sleep(1)

        if not cookies.get("sessionid"):
            browser.close()
            raise RuntimeError("登录超时，未检测到 sessionid")

        s = _capture_and_finish(page, context, cookies, captured_nicks, on_logged_in)
        browser.close()
    return s


def _normalize_mobile(raw: str) -> str:
    """规范化国内手机号；非法直接抛 ValueError。

    早点拦住比让浏览器空跑 20 秒再报「未检测到发码提示」强得多。
    """
    s = "".join(ch for ch in (raw or "") if ch.isdigit())
    if s.startswith("86") and len(s) == 13:
        s = s[2:]
    if len(s) != 11 or not s.startswith("1"):
        raise ValueError("手机号格式不正确（应为 11 位国内手机号）")
    return s


# 发码后页面上可能出现的滑块 / 验证弹窗特征
_CAPTCHA_HINTS = ("captcha", "验证码验证", "拖动滑块", "完成安全验证",
                  "请完成验证", "安全验证")


def _classify_send_state(has_countdown: bool, has_captcha: bool,
                         error_text: str = "") -> tuple[str, str]:
    """判断「获取验证码」点下去之后到底发出没有。

    Why: 原实现等不到倒计时就只打印一句警告继续走，还照样通知前端
    「已发送」—— 弹了滑块时短信根本没发出，用户只能盯着假的成功提示干等。

    返回 (state, 用户可读消息)，state ∈ {sent, captcha, failed}。
    """
    if has_countdown:
        return "sent", "验证码已发送"
    if has_captcha:
        return ("captcha",
                "抖音弹出了安全验证（滑块），短信未发出。"
                "请稍后重试，或改用扫码登录。")
    if error_text:
        return "failed", f"发送验证码失败：{error_text.strip()[:80]}"
    return ("failed",
            "未能确认验证码是否发出（可能被风控或手机号有误）。"
            "请稍后重试，或改用扫码登录。")


def sms_login_browser(mobile: str, headless: bool = True,
                      send_sink=None, code_provider=None, on_logged_in=None) -> requests.Session:
    """短信验证码登录 — Playwright 驱动浏览器（避开纯 HTTP 的 msToken/滑块问题）。
    流程：开登录页 → 切「验证码登录」→ 填手机号 → 点「获取验证码」→ send_sink() 通知前端已发码
         → code_provider() 阻塞拿码 → 填码点「登录」→（必要时 _handle_mfa_verify）→ 拿 sessionid。
    mobile:        纯数字手机号（不含 +86）
    send_sink():   可选回调 → 验证码已下发、可让前端显示输入框
    code_provider() -> str|None: 阻塞拿用户提交的验证码
    on_logged_in: fn(cookies) → 刚拿到 sessionid 即触发
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("未安装 Playwright，请先 pip install playwright && python3 -m playwright install chromium")

    mobile = _normalize_mobile(mobile)

    captured_nicks: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(user_agent=UA, viewport={"width": 800, "height": 600})
        page = context.new_page()

        def on_response(resp):
            if any(k in resp.url for k in ["user/info", "user/profile", "im/user", "imfriend",
                                            "aweme/v1/web/user", "im/contact", "relation/follow"]):
                try: _extract_contacts(resp.json(), captured_nicks)
                except Exception: pass
        page.on("response", on_response)

        print("正在加载登录页面（短信登录）...")
        page.goto("https://www.douyin.com/login_page?service=https%3A%2F%2Fwww.douyin.com",
                  wait_until="domcontentloaded")
        # 切到「验证码登录」（默认可能是扫码）
        try:
            page.get_by_text("验证码登录", exact=True).first.click(timeout=8000)
        except Exception:
            pass
        # 填手机号
        page.get_by_placeholder("请输入手机号").first.fill(mobile, timeout=10000)
        # 点「获取验证码」
        page.get_by_text("获取验证码", exact=True).first.click(timeout=8000)

        # 判定到底发出没有 —— 不能等不到就假装成功
        has_countdown = False
        try:
            page.wait_for_selector("text=后重新发送", timeout=20000)
            has_countdown = True
        except Exception:
            pass

        has_captcha = False
        error_text = ""
        if not has_countdown:
            try:
                body = (page.inner_text("body", timeout=3000) or "")
                has_captcha = any(h in body for h in _CAPTCHA_HINTS)
                for line in body.splitlines():
                    line = line.strip()
                    if any(w in line for w in ("频繁", "错误", "失败", "不正确", "稍后")):
                        error_text = line
                        break
            except Exception:
                pass

        state, msg = _classify_send_state(has_countdown, has_captcha, error_text)
        _log(f"[sms] 发码结果: {state} — {msg}", "SMS")
        if state != "sent":
            browser.close()
            raise RuntimeError(msg)

        print("  验证码已发送")
        if send_sink is not None:
            try: send_sink()
            except Exception: pass

        # 阻塞拿用户验证码
        if code_provider is None:
            browser.close()
            raise RuntimeError("缺少 code_provider，无法拿验证码")
        code = code_provider()
        if not code:
            browser.close()
            raise RuntimeError("未拿到验证码（超时/取消）")

        # 填验证码（登录页那个验证码框）+ 点「登录」
        page.get_by_placeholder("请输入验证码").first.fill(str(code).strip(), timeout=10000)
        page.get_by_text("登录", exact=True).first.click(timeout=8000)

        # 轮询 sessionid；其间兜底处理可能的二次验证
        cookies: dict = {}
        deadline = time.time() + 90
        verify_done = False
        while time.time() < deadline:
            all_cookies = context.cookies()
            if {c["name"]: c["value"] for c in all_cookies if "douyin" in c.get("domain", "")}.get("sessionid"):
                cookies = {c["name"]: c["value"] for c in all_cookies}
                break
            if not verify_done:
                try:
                    if _handle_mfa_verify(page, (lambda _m: None) if send_sink else None, code_provider):
                        verify_done = True
                        deadline = max(deadline, time.time() + 90)
                except Exception as _ve:
                    print(f"  [verify] 处理异常: {_ve}")
            time.sleep(1)

        if not cookies.get("sessionid"):
            browser.close()
            raise RuntimeError("登录超时，未检测到 sessionid（验证码可能错误或被风控）")

        s = _capture_and_finish(page, context, cookies, captured_nicks, on_logged_in)
        browser.close()
    return s


def refresh_contacts() -> dict:
    """
    已废弃：联系人刷新现在通过纯 HTTP 的 fetch_nicknames() 完成（signed spotlight/relation + profile/other）。
    保留此函数仅为向后兼容；不再启动浏览器。
    """
    return {}


def _load_session() -> requests.Session | None:
    if not os.path.exists(COOKIE_FILE):
        return None
    cookies = _secure_read(COOKIE_FILE)
    if not cookies:
        return None
    s = requests.Session()
    s.headers.update(HEADERS)
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".douyin.com")
    return s

def _check_login(s: requests.Session) -> bool:
    """只检查 cookie 文件里有没有 sessionid，不发网络请求。"""
    if not os.path.exists(COOKIE_FILE):
        return False
    cookies = _secure_read(COOKIE_FILE)
    return bool(cookies.get("sessionid"))

# ── protobuf 工具 ──────────────────────────────────────────────────────

def _enc_varint(v: int) -> bytes:
    out = b""
    while True:
        bits = v & 0x7F; v >>= 7
        out += bytes([bits | (0x80 if v else 0)])
        if not v: break
    return out

def _pb_v(field: int, v: int) -> bytes:
    return _enc_varint(field << 3) + _enc_varint(v)

def _pb_b(field: int, v: bytes | str) -> bytes:
    if isinstance(v, str): v = v.encode()
    return _enc_varint((field << 3) | 2) + _enc_varint(len(v)) + v

def _read_vi(data: bytes, pos: int) -> tuple[int, int]:
    r = 0; s = 0
    while True:
        b = data[pos]; pos += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80): break
        s += 7
    return r, pos

def _pb_flat(data: bytes) -> dict:
    fields: dict = {}
    pos = 0
    while pos < len(data):
        try: tag, pos = _read_vi(data, pos)
        except IndexError: break
        f = tag >> 3; w = tag & 0x7
        if w == 0:   val, pos = _read_vi(data, pos)
        elif w == 2:
            l, pos = _read_vi(data, pos); val = data[pos:pos+l]; pos += l
        elif w == 5: val = data[pos:pos+4]; pos += 4
        elif w == 1: val = data[pos:pos+8]; pos += 8
        else: break
        if f in fields:
            if not isinstance(fields[f], list): fields[f] = [fields[f]]
            fields[f].append(val)
        else: fields[f] = val
    return fields

# ── 会话常量 ──────────────────────────────────────────────────────────

def extract_constants(bin_path: str) -> dict:
    data = open(bin_path, "rb").read()
    pos = 0; c: dict = {}
    while pos < len(data):
        try: tag, pos = _read_vi(data, pos)
        except IndexError: break
        f = tag >> 3; w = tag & 0x7
        if w == 0: _, pos = _read_vi(data, pos)
        elif w == 2:
            l, pos = _read_vi(data, pos); val = data[pos:pos+l]; pos += l
            if f == 4:   c["hash"]        = val.decode()   # token (field 4)
            elif f == 7:  c["trace_id"]   = val.decode()   # build_number
            elif f == 23: c["ts_sign"]    = val.decode()
            elif f == 24: c["sdk_cert"]   = val.decode()
            elif f == 15:
                kv = _pb_flat(val)
                k = kv.get(1, b""); v = kv.get(2, b"")
                if isinstance(k, bytes): k = k.decode(errors="ignore")
                if isinstance(v, bytes): v = v.decode(errors="ignore")
                if k == "webid": c["webid"] = v
        else: pos += 4 if w == 5 else 8
    return c

# ── 按联系人 UID 字符串定位 sec_uid 和昵称 ──────────────────────────────

_SEC_UID_PAT = re.compile(rb'MS4wLjAB[A-Za-z0-9_\-/+=]{40,}')
_CJK_PAT     = re.compile(rb'(?:[\x20-\x7E]|[\xE4-\xE9][\x80-\xBF]{2})+')

def _find_sec_uid_for_uid(data: bytes, uid: str, my_sec_uids: set) -> str:
    """通过联系人数字 uid 字符串（非 conv_id 上下文）找其 sec_uid"""
    uid_b = uid.encode()
    for m in re.finditer(re.escape(uid_b), data):
        pos = m.start()
        if pos > 0 and data[pos - 1:pos] == b':':
            continue  # 在 conv_id 里，跳过
        ws = max(0, pos - 6000)
        we = min(len(data), pos + 6000)
        uid_rel = pos - ws
        best_su, best_d = "", float('inf')
        for sm in _SEC_UID_PAT.finditer(data[ws:we]):
            su = sm.group().decode()
            if su in my_sec_uids:
                continue
            d = abs(sm.start() - uid_rel)
            if d < best_d:
                best_d, best_su = d, su
        if best_su:
            return best_su
    return ""

def _find_nickname_by_sec_uid(data: bytes, sec_uid: str) -> str:
    """通过 sec_uid（唯一，仅出现在用户资料块）找附近的昵称"""
    if not sec_uid:
        return ""
    su_b = sec_uid.encode()
    for m in re.finditer(re.escape(su_b), data):
        ws = max(0, m.start() - 2000)
        we = min(len(data), m.end() + 2000)
        chunk = data[ws:we]
        su_rel = m.start() - ws
        best, best_d = "", float('inf')
        for nm in _CJK_PAT.finditer(chunk):
            s = nm.group()
            if not re.search(rb'[\xE4-\xE9][\x80-\xBF]{2}', s):
                continue
            try:
                decoded = s.decode('utf-8').strip()
                if 1 <= len(decoded) <= 20 and 'http' not in decoded and '://' not in decoded:
                    d = abs(nm.start() - su_rel)
                    if d < best_d:
                        best_d, best = d, decoded
            except UnicodeDecodeError:
                continue
        if best and best_d < 1500:
            return best
    return ""

def _parse_conv_info(data: bytes, conv_id: str) -> tuple[int, str]:
    """从 get_message_by_init 响应里抽 conv_id 对应的 (short_id, ticket)"""
    cid_b = conv_id.encode()
    idx = data.find(cid_b)
    if idx < 0:
        return 0, ""
    # conv_id 紧邻前有 0a <len> 表示 field 1 string；conv_id 之后紧接 f2(varint) f3(varint) f4(string)
    p = idx + len(cid_b)
    try:
        # field 2 tag + varint (conversation_short_id)
        tag, p = _read_vi(data, p)
        if tag != 0x10:           # 0x10 = field 2 wire 0
            return 0, ""
        short_id, p = _read_vi(data, p)
        # field 3 tag + varint (conv_type) — 跳过
        tag, p = _read_vi(data, p)
        if tag != 0x18:
            return short_id, ""
        _, p = _read_vi(data, p)
        # field 4 tag + length + string (ticket)
        tag, p = _read_vi(data, p)
        if tag != 0x22:           # 0x22 = field 4 wire 2
            return short_id, ""
        length, p = _read_vi(data, p)
        ticket = data[p:p+length].decode('utf-8', errors='replace')
        return short_id, ticket
    except Exception:
        return 0, ""


# ── self uid 识别（独立 helper，复用 parse_fire_streaks 的高频识别思路）─

def extract_my_uid(resp_bytes: bytes) -> str:
    """从 init 响应字节里识别当前账户的 UID（高频出现的就是自己）。

    Why: 抖音 web 接口没有"我是谁"端点，要从 IM init 抓到的 conv_id 列表
    （格式 0:1:uid_a:uid_b）按 UID 频次反推 — 出现最多的就是 self。
    """
    conv_positions = [
        m.group().decode()
        for m in re.finditer(rb'0:[12]:\d+:\d+', resp_bytes)
    ]
    ctr: Counter = Counter()
    for cid in conv_positions:
        parts = cid.split(":")
        if len(parts) == 4:
            ctr[parts[2]] += 1
            ctr[parts[3]] += 1
    return ctr.most_common(1)[0][0] if ctr else ""


def load_my_uid_from_cache() -> str:
    """读 INIT_REQ_BIN（当前 account ctx），抽 self uid。失败返回 ''。"""
    try:
        path = str(INIT_REQ_BIN)
        if not os.path.exists(path):
            return ""
        with open(path, "rb") as f:
            return extract_my_uid(f.read())
    except Exception as e:
        print(f"[load_my_uid] 失败: {e}")
        return ""


# ── 火花解析（正则字节匹配，速度快）────────────────────────────────────

def parse_fire_streaks(resp_bytes: bytes) -> list[dict]:
    # 找所有 conv_id — 支持 0:1:（私聊）和 0:2:（可能含重燃/群）
    conv_positions: list[tuple[int, str]] = [
        (m.start(), m.group().decode())
        for m in re.finditer(rb'0:[12]:\d+:\d+', resp_bytes)
    ]

    # 确定 my_uid：统计两侧 UID，出现最多的是自己
    all_uid_ctr: Counter = Counter()
    for _, cid in conv_positions:
        parts = cid.split(":")
        if len(parts) == 4:
            all_uid_ctr[parts[2]] += 1
            all_uid_ctr[parts[3]] += 1
    my_uid = all_uid_ctr.most_common(1)[0][0] if all_uid_ctr else ""

    # 找火花天数：
    # - consecutive_chat = 当前连续聊天（火花未断）
    # - rekindled_chat   = 历史重燃记录（断了也会保留）→ 默认不算
    # 用单独正则，记录每个 entry 的 marker 类型方便诊断
    streak_entries: list[tuple[int, int, str]] = []  # (pos, days, marker)
    for m in re.finditer(
        rb'a:(consecutive_chat|rekindled(?:_chat)?)(?:\x00)*\x12.{1,4}?(\d{1,4}):',
        resp_bytes
    ):
        try:
            marker = m.group(1).decode()
            days = int(m.group(2))
            if days > 0:
                streak_entries.append((m.start(), days, marker))
        except Exception:
            pass

    # 找每个会话的 consecutive_chat_data JSON（含 expire_time / can_recover_days / flame_infos）
    # 形如：a:consecutive_chat_data\x12<varint-len><json>
    # 用 expire_time 判断火花是否还在燃烧；过期的不该被识别为「续火花」目标
    chat_data_entries: list[tuple[int, int]] = []  # (pos, expire_time)
    _now_ts = int(time.time())
    for m in re.finditer(rb'a:consecutive_chat_data\x12', resp_bytes):
        # 读 varint length
        p = m.end()
        try:
            length, p2 = _read_vi(resp_bytes, p)
            blob = resp_bytes[p2:p2+length]
            obj = json.loads(blob.decode('utf-8', 'replace'))
            exp = int(obj.get('expire_time') or 0)
            if exp:
                chat_data_entries.append((m.start(), exp))
        except Exception:
            pass

    # 找所有 sec_uid 及频次，高频的是自己
    sec_uid_freq: Counter = Counter(
        m.group().decode()
        for m in _SEC_UID_PAT.finditer(resp_bytes)
    )
    n_convs = max(len(all_uid_ctr) // 2, 1)
    my_sec_uids = {su for su, f in sec_uid_freq.items() if f >= max(3, n_convs // 2)}

    if not streak_entries:
        return []

    result = []
    seen_uids: set = set()

    for conv_pos, conv_id in conv_positions:
        parts = conv_id.split(":")
        if len(parts) < 4:
            continue
        uid_a, uid_b = parts[2], parts[3]
        if uid_a == my_uid:
            uid = uid_b
        elif uid_b == my_uid:
            uid = uid_a
        else:
            uid = uid_b
        if uid in seen_uids:
            continue

        # 搜索窗口扩大到 60KB，覆盖更大跨度的数据块
        # 取 conv_pos 之后窗口内的 consecutive_chat 标记 + expire_time
        match = next(
            ((d, mk) for p, d, mk in streak_entries
             if conv_pos < p < conv_pos + 60000 and mk == "consecutive_chat"),
            None,
        )
        # 状态分类：
        #   active —— 火花仍在燃烧，正常续即可（有 consecutive_chat 且未过期）
        #   broken —— 火花已断，抖音 web 显示「续火花」按钮，需主动重燃
        if match:
            days, marker = match
            # consecutive_chat_data.expire_time 是火花过期 unix 秒；< 当前时间 → 已断
            exp_ts = next((e for p, e in chat_data_entries
                           if conv_pos < p < conv_pos + 60000), None)
            if exp_ts and exp_ts < _now_ts:
                status = "broken"
                from datetime import datetime as _dt
                _log(f"  [parse] uid={uid}: expire_time={exp_ts}"
                     f" ({_dt.fromtimestamp(exp_ts):%Y-%m-%d %H:%M}) 已过期 → 需重燃", "PARSE")
            else:
                status = "active"
        else:
            # 没有 consecutive_chat，只剩 rekindled 记录 → 火花已断，需重燃
            _rekindled = next(((d, mk) for p, d, mk in streak_entries
                               if conv_pos < p < conv_pos + 60000), None)
            if not _rekindled:
                continue
            days, marker = _rekindled
            status = "broken"
            _log(f"  [parse] uid={uid}: 仅有 {marker}={days}天 → 需重燃", "PARSE")

        sec_uid  = _find_sec_uid_for_uid(resp_bytes, uid, my_sec_uids)
        nickname = _find_nickname_by_sec_uid(resp_bytes, sec_uid)
        short_id, ticket = _parse_conv_info(resp_bytes, conv_id)

        seen_uids.add(uid)
        result.append({
            "conv_id": conv_id, "uid": uid,
            "sec_uid": sec_uid, "days": days,
            "nickname": nickname,
            # 头像走 fetch_nicknames 的 user JSON（按 uid 精确对应）；
            # init 二进制响应里按距离猜头像会张冠李戴，故此处留空待补
            "avatar": "",
            "status": status,
            "conversation_short_id": short_id,
            "ticket": ticket,
        })

    # active 在前、broken 在后，各自按天数倒序
    return sorted(result, key=lambda x: (x["status"] != "active", -x["days"]))


# ── 聊天消息解析 ──────────────────────────────────────────────────────
#
# get_message_by_init 的响应里本来就带着每个会话最近 ~21 条消息，
# 而拉联系人已经打过这个接口了 —— 直接从同一份响应里解析，
# 不额外请求抖音（多一个接口就多一份风控风险）。
#
# 字段号来自对真实响应的实测：
#   f1=conversation_id  f2=conversation_type  f3=server_message_id
#   f5=conversation_short_id  f6=message_type  f7=sender(uid)
#   f8=content(JSON)          f10=create_time(ms)
#
# 注意 f3/f5 别搞反：f5 在整个会话里是同一个值，拿它当消息主键会把
# 一个会话的 21 条消息去重成 1 条。语义由 _parse_conv_info 交叉验证过。

_MSG_KINDS: dict[int, str] = {
    7: "text",
    27: "image",
    17: "audio", 109: "audio",
    5: "emoji",
    1: "system", 15: "system",
    8: "share", 77: "share", 105: "share", 110: "share",
}

_KIND_PLACEHOLDER: dict[str, str] = {
    "image": "[图片]", "audio": "[语音]", "emoji": "[表情]",
    "share": "[分享]", "other": "[消息]",
}

# 分享摘要的长度上限。抖音的视频文案能有上千字（整篇小作文），
# 原样塞进聊天气泡会把整屏顶满，聊天记录没法看。
_SHARE_MAX = 120

# 抖音图片的签名域名（p3-pc-sign / p26-sign 之类），URL 带 x-expires，
# 十来天后就 403。换成同源常规域名后长期有效 —— 头像栽过一次的坑。
_SIGN_HOST_RE = re.compile(r"^(https?://p\d+(?:-pc)?)-sign(\.douyinpic\.com/)",
                           re.I)
# 视频 id 会拼进播放器 URL，只放行纯数字
_VID_RE = re.compile(r"^\d{1,32}$")


def normalize_media_url(url: str | None) -> str:
    """把抖音图片 URL 规范化成长期可访问的形式；非 http(s) 一律丢弃。

    - 去掉域名里的 -sign（签名域名会过期，403）
    - 去掉 x-expires / x-signature 这类时效参数
    - 只放行 http/https：这些 URL 直接进 <img src>，
      javascript: / data: 混进来就是一个 XSS 入口

    ⚠ 只适用于头像。视频封面不能这么处理 —— 实测 14 条封面里
    去签名后只有 6 条还能访问（tos-cn-i-dy、*c001 这些桶必须带签名），
    封面走 safe_media_url。
    """
    u = _http_only(url)
    if not u:
        return ""
    normalized = _SIGN_HOST_RE.sub(r"\1\2", u)
    if normalized != u or "douyinpic.com" in u.lower():
        normalized = normalized.split("?", 1)[0]
    return normalized


def _http_only(url) -> str:
    """只放行 http(s)，其余（javascript: / data: / 协议相对）一律丢弃。"""
    u = url.strip() if isinstance(url, str) else ""
    return u if u and re.match(r"^https?://", u, re.I) else ""


def safe_media_url(url: str | None) -> str:
    """视频封面/原图：只做协议校验，原样保留签名。

    实测签名 URL 14/14 可用、去签名只有 6/14，所以封面必须留签名。
    代价是签名有 x-expires（多数一年，少数十来天），过期后前端回退到
    去签名版本（见 _media 的 cover_alt）。
    """
    return _http_only(url)


def _first_url(node) -> str:
    """从抖音的 {uri, url_list:[...]} 结构里取第一个可用 URL（保留签名）。"""
    if not isinstance(node, dict):
        return ""
    for key in ("large_url_list", "url_list"):
        lst = node.get(key)
        if isinstance(lst, list):
            for item in lst:
                url = safe_media_url(item)
                if url:
                    return url
    return ""


def _cover_pair(node) -> tuple[str, str]:
    """返回 (签名封面, 去签名兜底)。

    签名版现在 100% 能用但有 x-expires；去签名版不过期却只有约四成可用。
    两个都给前端，签名的 403 了就自动回退。
    """
    signed = _first_url(node)
    if not signed:
        return "", ""
    alt = normalize_media_url(signed)
    return signed, ("" if alt == signed else alt)


def _media(kind: str, content: dict) -> dict | None:
    """抽出可内嵌的媒体：分享视频给封面 + 视频 id，图片给原图。

    拿不到任何可展示的东西就返回 None —— 前端据此决定画不画卡片。
    """
    if kind == "image":
        cover, alt = _cover_pair(content.get("resource_url"))
        if not cover:
            return None
        out = {"kind": "image", "cover": cover, "cover_alt": alt, "vid": ""}
        for src, dst in (("cover_width", "width"), ("cover_height", "height")):
            v = content.get(src)
            if isinstance(v, int) and 0 < v < 100000:
                out[dst] = v
        return out

    if kind != "share":
        return None

    # 110 有时把 item_id / cover 藏在 aweme_info 里
    info = content.get("aweme_info")
    info = info if isinstance(info, dict) else {}

    vid = ""
    for src in (content, info):
        for key in ("item_id", "itemId", "aweme_id"):
            v = src.get(key)
            if isinstance(v, (str, int)) and not isinstance(v, bool) \
                    and _VID_RE.match(str(v)):
                vid = str(v)
                break
        if vid:
            break

    cover, alt = _cover_pair(content.get("cover_url"))
    if not cover:
        cover, alt = _cover_pair(info.get("cover_url"))
    if not vid and not cover:
        return None
    return {"kind": "video", "cover": cover, "cover_alt": alt, "vid": vid}


def _pb_fields(data: bytes) -> list[tuple[int, int, object]] | None:
    """把一段 bytes 当 protobuf 逐字段解析。不像是 protobuf 就返回 None。

    这里刻意宽松：抖音随时可能加字段，遇到不认识的整段放弃比抛异常好。
    """
    out: list[tuple[int, int, object]] = []
    pos = 0
    n = len(data)
    while pos < n:
        try:
            tag, pos = _read_vi(data, pos)
        except (IndexError, ValueError):
            return None
        field, wire = tag >> 3, tag & 0x7
        # field 0 不合法；字段号过大基本可以断定不是 protobuf（是字符串/图片字节）
        if field == 0 or field > 50000:
            return None
        try:
            if wire == 0:
                val, pos = _read_vi(data, pos)
            elif wire == 2:
                length, pos = _read_vi(data, pos)
                if length < 0 or pos + length > n:
                    return None
                val = data[pos:pos + length]
                pos += length
            elif wire == 5:
                if pos + 4 > n:
                    return None
                val = data[pos:pos + 4]
                pos += 4
            elif wire == 1:
                if pos + 8 > n:
                    return None
                val = data[pos:pos + 8]
                pos += 8
            else:
                return None
        except (IndexError, ValueError):
            return None
        out.append((field, wire, val))
    return out


def _first_fields(fields: list[tuple[int, int, object]]) -> dict[int, tuple[int, object]]:
    """字段号 → (wire, value)，重复字段取第一个。"""
    out: dict[int, tuple[int, object]] = {}
    for field, wire, val in fields:
        if field not in out:
            out[field] = (wire, val)
    return out


def _is_message(head: dict[int, tuple[int, object]]) -> bool:
    """判断这层结构是不是一条 Message。

    条件取自实测：f1 是 `0:` 开头的会话 id，f6/f7 是 varint，f8 是内容。
    卡死这四个才不会把会话摘要、用户资料块之类误判成消息。
    """
    if 1 not in head or head[1][0] != 2:
        return False
    try:
        conv_id = head[1][1].decode("ascii")          # type: ignore[union-attr]
    except (UnicodeDecodeError, AttributeError):
        return False
    if not conv_id.startswith("0:"):
        return False
    return (6 in head and head[6][0] == 0
            and 7 in head and head[7][0] == 0
            and 8 in head and head[8][0] == 2)


def _s(content: dict, key: str) -> str:
    """取字符串字段。抖音偶尔把这些字段给成 dict/list/数字，直接 .strip() 会炸。"""
    v = content.get(key)
    return v.strip() if isinstance(v, str) else ""


def _share_label(content: dict) -> str:
    """分享类消息的一行摘要。

    字段优先级来自实测：
      105（视频评论分享）→ aweme_title + comment
      8 / 77（视频、用户名片）→ content_title / content_name
      110（分享视频）→ description，常常只有「[分享视频]」
    只看 description/content_name 的话，105 这类会全变成裸「[分享]」。
    """
    title = (_s(content, "aweme_title") or _s(content, "content_title")
             or _s(content, "content_name"))
    desc = _s(content, "description")
    label = title or desc or _KIND_PLACEHOLDER["share"]
    comment = _s(content, "comment")
    if comment:
        # 分享的是「别人的评论」，评论正文才是重点
        label = comment if label == _KIND_PLACEHOLDER["share"] else f"{label}：{comment}"
    # 换行会把气泡撑成一大片，压成一行再截断
    label = " ".join(label.split())
    if len(label) > _SHARE_MAX:
        label = label[:_SHARE_MAX] + "…"
    return label


def _audio_label(content: dict) -> str:
    """语音带上时长 —— 只显示「[语音]」的话，3 秒和 60 秒看起来一模一样。"""
    ms = content.get("duration")
    if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 0:
        return f"{_KIND_PLACEHOLDER['audio']} {ms / 1000:.1f}″"
    return _KIND_PLACEHOLDER["audio"]


def _summarize(kind: str, content: dict) -> str:
    """把各类消息压成一行可读文字 —— 聊天列表里要能扫一眼看懂。"""
    if kind == "text":
        return (content.get("text") or "").strip() if isinstance(
            content.get("text"), str) else ""
    if kind == "system":
        return _s(content, "tips") or _s(content, "hint_text")
    if kind == "share":
        return _share_label(content)
    if kind == "audio":
        return _audio_label(content)
    return _KIND_PLACEHOLDER.get(kind, _KIND_PLACEHOLDER["other"])


def _peer_of(conv_id: str, my_uid: str) -> str:
    """从 `0:1:a:b` 里取出对方 uid。不是单聊返回 ''。"""
    parts = conv_id.split(":")
    if len(parts) != 4 or not (parts[2].isdigit() and parts[3].isdigit()):
        return ""
    if my_uid and parts[3] == my_uid:
        return parts[2]
    return parts[3]


def parse_messages(resp_bytes: bytes, my_uid: str = "") -> list[dict]:
    """从 get_message_by_init 响应里解析出聊天消息，按时间正序。

    my_uid 用来判断收发方向；拿不到时全部当对方发的（is_me=False）。
    任何解析异常都只跳过那一条，绝不让整个会话读不出来。
    """
    found: list[dict] = []
    seen: set = set()

    def walk(data: bytes, depth: int) -> None:
        if depth > 8 or len(data) < 8:
            return
        fields = _pb_fields(data)
        if fields is None:
            return
        head = _first_fields(fields)
        if _is_message(head):
            _collect(head, fields)
            return
        for _field, wire, val in fields:
            if wire == 2:
                walk(val, depth + 1)               # type: ignore[arg-type]

    def _collect(head: dict, fields: list) -> None:
        try:
            conv_id = head[1][1].decode("ascii")
            peer = _peer_of(conv_id, my_uid)
            if not peer:                            # 群聊/异常会话，跳过
                return
            msg_id = head[3][1] if 3 in head and head[3][0] == 0 else 0
            key = msg_id or (conv_id, head[10][1] if 10 in head else 0,
                             bytes(head[8][1])[:64])
            if key in seen:
                return
            seen.add(key)

            raw = head[8][1]
            try:
                content = json.loads(raw.decode("utf-8", errors="replace"))
                if not isinstance(content, dict):
                    content = {}
            except (ValueError, AttributeError):
                content = {}

            mtype = head[6][1]
            kind = _MSG_KINDS.get(mtype, "other")
            sender = str(head[7][1])
            found.append({
                "conv_id": conv_id,
                "peer_uid": peer,
                "server_msg_id": msg_id,
                "conv_short_id": head[5][1] if 5 in head and head[5][0] == 0 else 0,
                "msg_type": mtype,
                "kind": kind,
                "sender": sender,
                "is_me": bool(my_uid) and sender == my_uid,
                "text": _summarize(kind, content) or _KIND_PLACEHOLDER["other"],
                "media": _media(kind, content),
                "created_at": head[10][1] if 10 in head and head[10][0] == 0 else 0,
            })
        except Exception:
            return

    try:
        walk(resp_bytes, 0)
    except RecursionError:
        print("[messages] 响应嵌套过深，已截断解析")
    found.sort(key=lambda m: (m["created_at"], m["server_msg_id"]))
    return found


def _is_group_uid(uid: str, cached_nicks: dict) -> bool:
    """粗略判断是否为群聊。群聊 uid 通常 ≥ 10^15 且不在个人昵称缓存里（或名字含'群'字）"""
    try:
        if int(uid) >= 10 ** 15:
            return True
    except ValueError:
        pass
    name = cached_nicks.get(uid, "")
    if name and any(k in name for k in ["群", "群聊", "Group"]):
        return True
    return False

# ── 获取昵称（备用，缓存优先）─────────────────────────────────────────

def _extract_avatar(u: dict) -> str:
    """从用户对象里抽取头像 URL（优先小图，省流量）。找不到返回 ''。"""
    for key in ("avatar_thumb", "avatar_168x168", "avatar_300x300",
                "avatar_medium", "avatar_larger", "avatar"):
        a = u.get(key)
        if isinstance(a, dict):
            lst = a.get("url_list") or a.get("urlList") or []
            if lst and isinstance(lst[0], str) and lst[0].startswith("http"):
                return lst[0]
        elif isinstance(a, str) and a.startswith("http"):
            return a
    for key in ("avatar_url", "avatarUrl", "avatar_uri"):
        v = u.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


def _extract_user_fields(u: dict) -> tuple[str, str, str, str, str]:
    """从用户对象里抽取 (uid, sec_uid, nick, remark, avatar)"""
    uid    = str(u.get("uid") or u.get("user_id") or u.get("id") or "")
    sec    = u.get("sec_uid") or u.get("sec_user_id") or ""
    nick   = (u.get("nickname") or u.get("nick_name") or "").strip()
    remark = (u.get("remark_name") or u.get("remarkName") or
              u.get("remark")      or u.get("alias")     or "").strip()
    avatar = _extract_avatar(u)
    return uid, sec, nick, remark, avatar


def _walk_users(obj, callback):
    """递归遍历 JSON 响应，找到任何看起来像 user 对象的 dict 就调 callback"""
    if isinstance(obj, list):
        for v in obj:
            _walk_users(v, callback)
    elif isinstance(obj, dict):
        if ("nickname" in obj or "nick_name" in obj) and ("uid" in obj or "sec_uid" in obj or "user_id" in obj):
            callback(obj)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _walk_users(v, callback)


def fetch_self_profile(session: requests.Session, my_uid: str) -> dict:
    """拉自己的 profile（昵称 + 头像）。

    抖音 `/user/profile/other?user_id=<my_uid>` 对自己的 uid 同样返回 self profile。
    返回 {"nickname": str, "avatar": str}，任何失败都静默返回空 dict — 调用方判空。
    """
    if not my_uid:
        return {}
    try:
        params = {
            "device_platform": "webapp",  "aid": "6383",
            "channel": "channel_pc_web",  "version_code": "170400",
            "version_name": "17.4.0",     "cookie_enabled": "true",
            "platform": "PC",             "downlink": "10",
            "browser_language": "zh-CN",  "browser_platform": "MacIntel",
            "browser_name": "Chrome",     "browser_version": "147.0.0.0",
            "os_name": "Mac OS",
            "user_id": str(my_uid),
            "publish_video_strategy_type": "2",
            "personal_center_strategy": "1", "from_page": "user",
        }
        qs = _sign_params(params)
        r = session.get(f"https://www.douyin.com/aweme/v1/web/user/profile/other/?{qs}",
                        timeout=10)
        if not r.text.strip().startswith("{"):
            return {}
        j = r.json()
        if j.get("status_code") != 0:
            return {}
        u = j.get("user") or {}
        return {
            "nickname": (u.get("nickname") or "").strip(),
            "avatar":   _extract_avatar(u),
        }
    except Exception as e:
        print(f"[self_profile] 失败: {e}")
        return {}


def fetch_nicknames(session: requests.Session, contacts: list[dict]) -> dict[str, dict]:
    """批量拉昵称和备注名，返回 {uid: {"nick": str, "remark": str}}"""
    out: dict[str, dict] = {}

    sec_uids = [c["sec_uid"] for c in contacts if c.get("sec_uid")]
    if not sec_uids:
        return out
    sec2uid = {c["sec_uid"]: c["uid"] for c in contacts if c.get("sec_uid")}
    my_uids = {c["uid"] for c in contacts}

    base_params = {
        "device_platform": "webapp",  "aid": "6383",
        "channel": "channel_pc_web",  "version_code": "170400",
        "version_name": "17.4.0",     "cookie_enabled": "true",
        "platform": "PC",             "downlink": "10",
        "browser_language": "zh-CN",  "browser_platform": "MacIntel",
        "browser_name": "Chrome",     "browser_version": "147.0.0.0",
        "os_name": "Mac OS",
    }

    def _merge(uid: str, sec: str, nick: str, remark: str, avatar: str = ""):
        if not uid and sec and sec in sec2uid:
            uid = sec2uid[sec]
        if not uid or uid not in my_uids:
            return
        prev = out.get(uid, {"nick": "", "remark": "", "avatar": ""})
        if nick:   prev["nick"]   = nick
        if remark: prev["remark"] = remark
        if avatar: prev["avatar"] = avatar
        out[uid] = prev

    # 1) IM 好友/关系链接口 — 通常含 remark_name（带分页）
    for ep in [
        "/aweme/v1/web/im/spotlight/relation/",
        "/aweme/v1/web/im/resources/get_spotlight_relation_v2/",
        "/aweme/v1/web/im/contact/",
        "/aweme/v1/web/im/fans/",
    ]:
        cursor = "0"
        for page in range(10):          # 最多翻 10 页
            try:
                params = {**base_params, "count": "50", "cursor": cursor}
                qs = _sign_params(params)
                r = session.get(f"https://www.douyin.com{ep}?{qs}", timeout=12)
                if r.status_code != 200 or not r.text.strip().startswith(("{", "[")):
                    if page == 0:
                        print(f"  {ep} HTTP {r.status_code} (跳过)")
                    break
                j = r.json()
                before = len(out)
                _walk_users(j, lambda u: _merge(*_extract_user_fields(u)))
                new = len(out) - before
                print(f"  {ep} p{page} cursor={cursor} → +{new} (累计 {len(out)}/{len(sec2uid)})")
                # 翻页
                has_more = False
                next_cursor = None
                for key in ("data", "extra", "pagination"):
                    blob = j.get(key) if isinstance(j.get(key), dict) else j
                    if isinstance(blob, dict):
                        if blob.get("has_more") or blob.get("hasMore"):
                            has_more = True
                        nc = blob.get("next_cursor") or blob.get("cursor") or blob.get("min_time")
                        if nc and str(nc) != "0":
                            next_cursor = str(nc)
                if not has_more or not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
                # 已覆盖所有联系人就提前结束
                if all(c["uid"] in out for c in contacts):
                    break
            except Exception as e:
                print(f"  {ep} 异常: {e}")
                break
        if all(c["uid"] in out for c in contacts):
            break

    # (跳过 im/user/info — 该接口返回的 nickname 常是聊天场景中涉及到的人如分享视频作者，不是联系人本人)
    # (也不用 sec_user_id 查 profile/other — parse_fire_streaks 挖出的 sec_uid 可能是聊天上下文里的视频作者)

    # 2) 仍缺的按 user_id 查 profile/other（最可靠：按 uid 精确匹配）
    still_missing = [c for c in contacts if c["uid"] not in out][:8]
    for c in still_missing:
        try:
            params = {**base_params, "user_id": c["uid"],
                      "publish_video_strategy_type": "2",
                      "personal_center_strategy": "1", "from_page": "user"}
            qs = _sign_params(params)
            r = session.get(f"https://www.douyin.com/aweme/v1/web/user/profile/other/?{qs}",
                            timeout=10)
            if not r.text.strip().startswith("{"):
                print(f"  profile/other?user_id={c['uid']} 非 JSON")
                continue
            j = r.json()
            status = j.get("status_code")
            u = j.get("user") or {}
            resp_uid = str(u.get("uid") or "")
            if status != 0 or resp_uid != c["uid"]:
                print(f"  profile/other?user_id={c['uid']} status={status} uid={resp_uid} 未匹配")
                continue
            nick   = (u.get("nickname") or "").strip()
            remark = (u.get("remark_name") or "").strip()
            avatar = _extract_avatar(u)
            if nick or remark or avatar:
                out[c["uid"]] = {"nick": nick, "remark": remark, "avatar": avatar}
                # 顺便把 user 里的真实 sec_uid 存回 contacts（如果之前挖错了）
                real_sec = u.get("sec_uid") or ""
                if real_sec and real_sec != c.get("sec_uid"):
                    c["sec_uid"] = real_sec
                print(f"  profile/other?user_id={c['uid']} → nick='{nick}' remark='{remark}'")
        except Exception as e:
            print(f"  profile/other?user_id={c['uid']} 异常: {e}")

    return out

# ── 发送消息 ──────────────────────────────────────────────────────────

def _build_body(conv_id: str, text: str, contact: dict, c: dict, security: dict) -> bytes:
    """
    构造 /message/send 请求体（Request proto）。
    contact: 联系人 dict（parse_fire_streaks 出来的，含 conversation_short_id / ticket）
    c:       会话常量 dict（token/ts_sign/sdk_cert/webid，来自 init_req.bin）
    security: 私钥等安全数据（来自 ~/.douyin_security.json）
    """
    import uuid

    parts      = conv_id.split(":")
    conv_type  = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 1
    short_id   = int(contact.get("conversation_short_id") or 0)
    inner_ticket = contact.get("ticket") or ""
    client_msg_id = str(uuid.uuid4())

    content_obj = {
        "mention_users": [],
        "aweType":       700,
        "richTextInfos": [],
        "text":          text,
    }
    content_json = json.dumps(content_obj, ensure_ascii=False, separators=(',', ':'))

    # SendMessageRequestBody (proto field numbers from leonfang-dev schema)
    inner = (
        _pb_b(1, conv_id)                                              # conversation_id
        + _pb_v(2, conv_type)                                          # conversation_type
        + _pb_v(3, short_id)                                           # conversation_short_id
        + _pb_b(4, content_json)                                       # content (JSON)
        # field 5 = ext entries (ExtValue{key=1, value=2})
        + _pb_b(5, _pb_b(1, "s:client_message_id") + _pb_b(2, client_msg_id))
        + _pb_b(5, _pb_b(1, "s:stime") + _pb_b(2, str(int(time.time() * 1000))))
        + _pb_b(5, _pb_b(1, "s:mentioned_users") + _pb_b(2, ""))
        + _pb_v(6, 7)                                                  # message_type = 7 (text)
        + _pb_b(7, inner_ticket)                                       # ticket (per-conv)
        + _pb_b(8, client_msg_id)                                      # client_message_id
    )

    # RequestBody 里 SendMessageRequestBody 占 field 100
    req_body = _pb_b(100, inner)

    # 外层 Request 常量
    # ⭐ ts_sign / sdk_cert 优先从 security.json（localStorage 抽取）取，
    # 其次从 init_req.bin 取（老抖音协议）。新抖音已不在 init_req 放这俩。
    token      = c.get("hash", "")                                     # session token
    ts_sign    = security.get("ts_sign") or c.get("ts_sign", "")
    sdk_cert   = security.get("client_cert") or c.get("sdk_cert", "")
    build_no   = c.get("trace_id", "5fa6ff1:Detached: 5fa6ff1111fd53aafc4c753505d3c93daad74d27")
    webid      = c.get("webid", "0")
    private_key = security.get("private_key", "")

    # reuqest_sign（注意官方 typo 是 "reuqest"）
    sign_data = (f"content={content_json}"
                 f"&conversation_id={conv_id}"
                 f"&conversation_short_id={short_id}")
    try:
        reuqest_sign = _im_req_sign(sign_data, private_key) if private_key else ""
    except Exception as e:
        print(f"  ⚠ reuqest_sign 生成失败: {e}")
        reuqest_sign = ""

    # 组装 outer Request
    body = (
        _pb_v(1, 100)                                                  # cmd
        + _pb_v(2, random.randint(10000, 11000))                       # sequence_id
        + _pb_b(3, "1.1.3")                                            # sdk_version
        + _pb_b(4, token)                                              # token
        + _pb_v(5, 3)                                                  # refer
        + _pb_v(6, 0)                                                  # inbox_type
        + _pb_b(7, build_no)                                           # build_number
        + _pb_b(8, req_body)                                           # body (RequestBody)
        + _pb_b(9, "0")                                                # device_id
        + _pb_b(11, "douyin_pc")                                       # device_platform
    )

    # field 15: headers map<string,string>
    for k, v in [
        ("session_aid","6383"),("session_did","0"),("app_name","douyin_pc"),
        ("priority_region","cn"),("user_agent",UA),("cookie_enabled","true"),
        ("browser_language","zh-CN"),("browser_platform","MacIntel"),
        ("browser_name","Mozilla"),("browser_online","true"),
        ("screen_width","1440"),("screen_height","900"),
        ("referer",""),("timezone_name","Asia/Shanghai"),("deviceId","0"),
        ("webid", webid), ("fp", security.get("s_v_web_id", "")),
        ("is-retry","0"),
    ]:
        body += _pb_b(15, _pb_b(1, k) + _pb_b(2, v))

    # 剩余字段
    body += (
        _pb_v(18, 4)                                                   # auth_type
        + _pb_b(21, "douyin_web")                                      # biz
        + _pb_b(22, "web_sdk")                                         # access
        + _pb_b(23, ts_sign)                                           # ts_sign
        + _pb_b(24, sdk_cert)                                          # sdk_cert
    )
    if reuqest_sign:
        body += _pb_b(25, reuqest_sign)                                # reuqest_sign
    return body


import threading as _threading

# 线程局部的发送诊断信息，替代原模块级全局变量（避免多账号并发串扰）
_SEND_INFO_TLS = _threading.local()


def get_last_send_info() -> dict:
    """返回当前线程最近一次 send_text 的诊断信息副本"""
    return dict(getattr(_SEND_INFO_TLS, "info", {}))


def _set_send_info(info: dict) -> None:
    _SEND_INFO_TLS.info = dict(info)


def _update_send_info(**kwargs) -> None:
    cur = getattr(_SEND_INFO_TLS, "info", None) or {}
    cur.update(kwargs)
    _SEND_INFO_TLS.info = cur


def send_text(session: requests.Session, conv_id: str, text: str,
              contact: dict, constants: dict) -> bool:
    _set_send_info({"stage": "start", "conv_id": conv_id})
    # 缺少 conv_id / ticket（init_req 没抓全）→ 直接返回，避免无效请求 + 误判风控
    if not conv_id or not contact.get("ticket"):
        _log("  ⚠ 联系人缺 conv_id 或 ticket（init_req 漏抓），请重新登录刷新", "SKIP")
        _set_send_info({"stage": "no_ticket",
                        "error": "联系人 ticket 缺失（init_req 未抓到），需重新登录"})
        return False
    security = _load_security()
    if not security.get("private_key"):
        print("  ⚠ 缺少私钥 (~/.douyin_security.json)，必须先通过扫码登录抓取")
        _set_send_info({"stage": "no_private_key",
                        "error": "缺少私钥，需要重新扫码登录"})
        return False

    body = _build_body(conv_id, text, contact, constants, security)

    # URL 参数: verifyFp / fp / msToken / a_bogus
    s_v_web_id = security.get("s_v_web_id", "") or session.cookies.get("s_v_web_id", "")
    from douyin_im import _call_dy_signer as _sg
    ms_token = session.cookies.get("msToken", "")
    if not ms_token:
        # 生成一个随机 107 字符 msToken
        import string
        alphabet = string.ascii_letters + string.digits + "="
        ms_token = "".join(random.choice(alphabet) for _ in range(107))

    url_params = {
        "verifyFp": s_v_web_id,
        "fp":       s_v_web_id,
        "msToken":  ms_token,
    }
    # a_bogus 签名
    qs = urlencode(url_params)
    try:
        a_bogus = _sg("a_bogus", qs, "")
        url_params["a_bogus"] = a_bogus
    except Exception as e:
        print(f"  ⚠ a_bogus 生成失败: {e}")

    try:
        r = session.post(
            "https://imapi.douyin.com/v1/message/send",
            params=url_params,
            data=body,
            headers={**HEADERS, "Content-Type": "application/x-protobuf"},
            timeout=10,
        )
        print(f"  HTTP {r.status_code}, {len(r.content)} bytes")
        _set_send_info({"stage": "response", "http": r.status_code,
                        "bytes": len(r.content)})
        if r.status_code == 200 and r.content:
            try:
                fields = _pb_flat(r.content)
                # Response proto: f1=cmd echo, f2=seq, f3=error_desc, f4=message
                msg   = fields.get(4, b"")
                edesc = fields.get(3, b"")
                if isinstance(msg, bytes):
                    try: msg = msg.decode('utf-8')
                    except Exception: msg = repr(msg)
                if isinstance(edesc, bytes):
                    try: edesc = edesc.decode('utf-8')
                    except Exception: edesc = repr(edesc)
                _update_send_info(msg=msg, error_desc=edesc)
                # 收集其他字段（非 1/2/3/4 的业务字段）便于诊断
                extras = {}
                for k, v in fields.items():
                    if k in (1, 2, 3, 4):
                        continue
                    if isinstance(v, bytes):
                        try:
                            s = v.decode('utf-8')
                            if s.strip() and len(s) < 300:
                                extras[f"f{k}"] = s
                        except Exception:
                            pass
                    elif isinstance(v, int):
                        extras[f"f{k}"] = v
                if extras:
                    _update_send_info(extras=extras)
                if msg == "OK" and (not edesc or edesc == "0"):
                    # 把 extras 也打出来：OK 但被悄悄丢消息时，风控码往往藏在这里
                    if extras:
                        print(f"  ✓ 发送成功 (server: OK) extras={extras}")
                    else:
                        print(f"  ✓ 发送成功 (server: OK)")
                    _update_send_info(ok=True)
                    return True
                print(f"  ✗ 服务端拒绝: message={msg!r} error_desc={edesc!r}")
                for k, v in extras.items():
                    print(f"    {k}: {v!r}")
            except Exception as e:
                print(f"  解析响应失败: {e}")
                print(f"  响应 hex: {r.content.hex()[:200]}")
                _update_send_info(parse_error=str(e), hex=r.content.hex()[:200])
        _update_send_info(ok=False)
        return False
    except Exception as e:
        print(f"  发送异常: {e}")
        _set_send_info({"stage": "exception", "error": str(e)})
        return False

# ── 主流程 ────────────────────────────────────────────────────────────


def _run_auto_keepalive(session, contacts, constants):
    """自动续火花: 给所有启用的联系人按模板发消息"""
    templates = _ensure_templates_exist(contacts)
    _log(f"自动续火花启动, 共 {len(contacts)} 个联系人")
    sent, skipped, failed = 0, 0, 0
    for i, c in enumerate(contacts):
        name = c["nickname"]
        msg = _pick_message(c["uid"], name, templates)
        if not msg:
            _log(f"  [{i+1}/{len(contacts)}] 跳过 {name}: 模板禁用或无消息", "SKIP")
            skipped += 1
            continue
        # 每条间隔 3-10 秒随机，防风控
        if i > 0:
            delay = random.uniform(3, 10)
            _log(f"  等待 {delay:.1f}s 防风控...", "WAIT")
            time.sleep(delay)
        _log(f"  [{i+1}/{len(contacts)}] 发给 {name}: {msg!r}", "SEND")
        try:
            ok = send_text(session, c["conv_id"], msg, c, constants)
        except Exception as e:
            _log(f"    异常: {e}", "ERR")
            ok = False
        if ok:
            sent += 1
            _log(f"    ✓ 已送达", "OK")
        else:
            failed += 1
            _log(f"    ✗ 失败", "FAIL")
    _log(f"续火花完成: 成功 {sent}, 跳过 {skipped}, 失败 {failed}", "DONE")


def _render_template_line(i: int, contact: dict, entry: dict, is_default: bool = False) -> str:
    name = "[default 兜底]" if is_default else (entry.get("name") or contact.get("nickname", "?"))
    en   = entry.get("enabled", True)
    msgs = entry.get("messages", [])
    tag  = "✓" if en else "✗"
    if not en:
        body = "(禁用, 不发)"
    elif not msgs:
        body = "(走 default 兜底)" if not is_default else "(未设置任何消息)"
    elif len(msgs) == 1:
        body = f'"{msgs[0]}"'
    else:
        body = f'随机 {len(msgs)} 条: {msgs}'
    if is_default:
        return f"   [0] {tag} {name:<20} {body}"
    days = contact.get("days", 0)
    return f"   [{i}] {tag} {name:<16} {days:>4}天  {body}"


def _edit_contact_menu(label: str, entry: dict) -> dict:
    """编辑单个联系人（或 default）的消息设置，返回新的 entry"""
    entry = dict(entry)
    entry.setdefault("enabled", True)
    entry.setdefault("messages", [])
    while True:
        en = entry["enabled"]
        msgs = entry["messages"]
        print(f"\n── 编辑 {label} ──")
        print(f"  状态: {'✓ 启用' if en else '✗ 禁用'}")
        if msgs:
            print(f"  消息列表 ({len(msgs)} 条，发送时随机选一条):")
            for i, m in enumerate(msgs, 1):
                print(f"    {i}. {m!r}")
        else:
            print(f"  消息列表: (空，将走 default 兜底)")
        print("\n  操作:")
        print("    t  切换启用/禁用")
        print("    a  添加消息")
        print("    d  删除消息")
        print("    c  清空并重设")
        print("    q  保存返回")
        op = input("  > ").strip().lower()
        if op == "q":
            return entry
        elif op == "t":
            entry["enabled"] = not entry["enabled"]
        elif op == "a":
            text = input("    输入新消息 (空=取消): ").strip()
            if text:
                msgs.append(text)
        elif op == "d":
            if not msgs:
                print("    消息列表为空"); continue
            idx = input(f"    删除哪条？[1-{len(msgs)}] (空=取消): ").strip()
            try:
                i = int(idx) - 1
                if 0 <= i < len(msgs):
                    removed = msgs.pop(i)
                    print(f"    ✗ 已删: {removed!r}")
            except ValueError:
                pass
        elif op == "c":
            confirm = input("    确认清空所有消息? (y/N): ").strip().lower()
            if confirm == "y":
                entry["messages"] = []
                msgs = entry["messages"]
        else:
            print(f"  未知操作: {op!r}")


def _config_wizard(contacts: list[dict]):
    """交互式配置向导：对每个联系人可启/禁 + 自定义消息列表"""
    templates = _ensure_templates_exist(contacts)

    def _save_and_echo():
        _save_templates(templates)
        print(f"  已保存到 {TEMPLATES_FILE}")

    while True:
        print("\n" + "=" * 60)
        print("  续火花配置向导")
        print("=" * 60)
        default_entry = templates.get("default", {"enabled": True, "messages": ["早"]})
        print(_render_template_line(0, {}, default_entry, is_default=True))
        for i, c in enumerate(contacts, 1):
            entry = templates.get(c["uid"], {"name": c["nickname"], "enabled": True, "messages": []})
            print(_render_template_line(i, c, entry))
        print("=" * 60)
        print("操作:")
        print("  数字 0..N  - 编辑该联系人 (0 = default 兜底)")
        print("  A          - 全部启用  /  N - 全部禁用")
        print("  M <文本>   - 把所有联系人的消息批量设为单条 <文本>")
        print("  Q          - 保存并退出")
        cmd = input("> ").strip()
        if not cmd:
            continue
        lo = cmd.lower()
        if lo == "q":
            _save_and_echo()
            return
        if lo == "a":
            for key in list(templates.keys()):
                templates[key]["enabled"] = True
            print("  ✓ 全部启用")
            _save_and_echo()
            continue
        if lo == "n":
            for key in list(templates.keys()):
                if key == "default": continue
                templates[key]["enabled"] = False
            print("  ✗ 全部联系人已禁用（default 保留）")
            _save_and_echo()
            continue
        if lo.startswith("m "):
            text = cmd[2:].strip()
            if not text:
                print("  用法: M <文本>"); continue
            for c in contacts:
                uid = c["uid"]
                templates.setdefault(uid, {"name": c["nickname"]})
                templates[uid]["enabled"] = True
                templates[uid]["messages"] = [text]
            print(f"  ✓ 所有联系人消息已设为 {text!r}")
            _save_and_echo()
            continue
        # 数字 → 编辑
        try:
            idx = int(cmd)
        except ValueError:
            print(f"  未知操作: {cmd!r}"); continue
        if idx == 0:
            templates["default"] = _edit_contact_menu("default 兜底", default_entry)
            _save_and_echo()
        elif 1 <= idx <= len(contacts):
            c = contacts[idx - 1]
            uid = c["uid"]
            entry = templates.get(uid, {"name": c["nickname"], "enabled": True, "messages": []})
            entry["name"] = c["nickname"]
            templates[uid] = _edit_contact_menu(f"{c['nickname']} ({c['days']}天)", entry)
            _save_and_echo()
        else:
            print(f"  编号超出范围 [0-{len(contacts)}]")


def main():
    # 解析命令行
    import argparse
    p = argparse.ArgumentParser(
        description="Douyin IM 续火花工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python3 douyin_im.py             # 交互式\n"
               "  python3 douyin_im.py --list      # 列出火花联系人就退出\n"
               "  python3 douyin_im.py --auto      # 自动续火花（按模板发）\n"
               "  python3 douyin_im.py --config    # 展示/提示编辑模板"
    )
    p.add_argument("--account", "-a", default=None,
                   help=f"账户名（默认 {_DEFAULT_ACCOUNT}）；可用 --list-accounts 查看已有账户")
    p.add_argument("--list-accounts", action="store_true", help="列出已有账户")
    p.add_argument("--list",   action="store_true", help="列出火花联系人")
    p.add_argument("--auto",   action="store_true", help="自动续火花")
    p.add_argument("--config", action="store_true", help="展示/编辑消息模板")
    args = p.parse_args()

    # 先迁移旧数据，再处理账户相关命令
    _migrate_legacy_files()

    if args.list_accounts:
        accounts = list_accounts()
        if not accounts:
            print("（暂无账户，先登录：python3 douyin_im.py）")
        else:
            print("已有账户：")
            for a in accounts:
                mark = " (当前)" if a == (args.account or _DEFAULT_ACCOUNT) else ""
                print(f"  - {a}{mark}")
        return

    if args.account:
        set_account(args.account)
        # 这里原本打的是 `_current_account`，而那个名字全文件都没定义过 ——
        # 纯 Python 下走到这条 CLI 分支才会 NameError（平时走不到所以没人发现），
        # 但 cythonize 时它是编译错误，会让整个 .so 编译失败。
        print(f"使用账户: {args.account}")

    if args.auto:       mode = "auto"
    elif args.list:     mode = "list"
    elif args.config:   mode = "config"
    else:               mode = "interactive"

    _config()   # 触发首次配置文件生成

    # 登录
    session = _load_session()
    if session and _check_login(session):
        if mode == "auto" and not _verify_session_alive(session):
            _log("cookies 已失效，请重新登录", "ERR")
            sys.exit(2)
        print("使用已保存的登录状态")
    else:
        if mode == "auto":
            _log("无有效登录态，请先手动 python3 douyin_im.py 登录", "ERR")
            sys.exit(2)
        print("=== 抖音登录 ===")
        print("  [1] 短信登录（纯 HTTP）")
        print("  [2] 扫码登录（Playwright Chromium，推荐）")
        choice = input("选择登录方式 [2]: ").strip() or "2"
        if choice == "1":
            try:
                session = sms_login()
            except Exception as e:
                print(f"❌ 短信登录失败: {e}")
                print("降级尝试扫码登录...")
                session = qr_login()
        else:
            session = qr_login()

    # 会话常量
    if not os.path.exists(INIT_REQ_BIN):
        print(f"\n缺少 {INIT_REQ_BIN}（get_message_by_init 请求体）")
        sys.exit(1)
    constants = extract_constants(INIT_REQ_BIN)

    # 获取火花联系人
    print("\n获取火花联系人...")
    resp = session.post(
        "https://imapi.douyin.com/v1/message/get_message_by_init",
        data=open(INIT_REQ_BIN, "rb").read(),
        headers={**HEADERS, "Content-Type": "application/x-protobuf"},
        timeout=15,
    )
    print(f"接口响应: HTTP {resp.status_code}, {len(resp.content)} bytes")

    contacts = parse_fire_streaks(resp.content)
    if not contacts:
        print("未找到火花联系人")
        sys.exit(1)

    # 加载联系人缓存（含备注名）
    contacts_cache: dict = {}
    if os.path.exists(CONTACTS_FILE):
        try:
            contacts_cache = json.load(open(CONTACTS_FILE))
        except Exception:
            pass

    def _display_name(uid: str) -> str:
        info = contacts_cache.get(uid, {})
        if isinstance(info, str):
            return info or uid
        remark = info.get("remark", "").strip()
        nick   = info.get("nick",   "").strip()
        return remark or nick or uid

    for c in contacts:
        c["nickname"] = _display_name(c["uid"])

    # 对没有缓存、或只有缓存没备注的联系人，调用 API 补全（仅 interactive 模式）
    if mode != "auto":
        need_fetch = [c for c in contacts if c["uid"] not in contacts_cache or
                      (isinstance(contacts_cache.get(c["uid"]), dict) and
                       not contacts_cache[c["uid"]].get("remark"))]
        missing = need_fetch if need_fetch else [c for c in contacts if c["nickname"] == c["uid"]]
        if missing:
            print(f"  {len(missing)} 个联系人需要补全备注/昵称，调用 API ...")
            try:
                fetched = fetch_nicknames(session, missing)
            except Exception as e:
                print(f"  API 调用失败: {e}")
                fetched = {}
            for c in missing:
                info = fetched.get(c["uid"], {})
                nick = info.get("nick", "") if isinstance(info, dict) else str(info)
                remark = info.get("remark", "") if isinstance(info, dict) else ""
                avatar = info.get("avatar", "") if isinstance(info, dict) else ""
                if nick or remark or avatar:
                    contacts_cache[c["uid"]] = {"nick": nick, "remark": remark, "avatar": avatar}
                    c["nickname"] = remark or nick
            try:
                json.dump(contacts_cache, open(CONTACTS_FILE, "w"), ensure_ascii=False)
            except Exception:
                pass
            # 重新应用显示名字
            for c in contacts:
                c["nickname"] = _display_name(c["uid"])

    _dispatch(mode, session, contacts, constants)


def _dispatch(mode, session, contacts, constants):
    """按 mode 分发到具体处理逻辑"""
    # 展示列表（所有模式都打印一次）
    print(f"\n{'':=<56}")
    print(f"{'#':>3}  {'备注/昵称':<22} {'火花':>5}  uid")
    print(f"{'':=<56}")
    for i, c in enumerate(contacts, 1):
        print(f"{i:>3}. {c['nickname']:<22} {c['days']:>4}天  {c['uid']}")
    print(f"{'':=<56}")

    if mode == "list":
        return
    if mode == "config":
        _config_wizard(contacts)
        return
    if mode == "auto":
        _run_auto_keepalive(session, contacts, constants)
        return

    # interactive
    while True:
        choice = input("\n输入编号选联系人（0 退出）: ").strip()
        if choice == "0": break
        try:
            contact = contacts[int(choice) - 1]
        except (ValueError, IndexError):
            print("无效编号"); continue
        text = input(f"发给 {contact['nickname']}: ").strip()
        if not text: continue
        ok = send_text(session, contact["conv_id"], text, contact, constants)
        print("✓ 成功" if ok else "✗ 失败")


if __name__ == "__main__":
    main()
