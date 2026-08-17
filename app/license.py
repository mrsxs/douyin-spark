"""
软件授权验证（离线 License）。

License 格式：`<base64(payload_json)>.<base64(rsa_signature)>`
payload 内容：{expires_at, tier, machine (可选), issued_at, note}

环境变量：
  LICENSE_KEY      —— 必填，客户购买后拿到的字符串
  SKIP_LICENSE_CHECK —— 仅源码态（开发）有效；正式镜像里被编译期关闭

机器绑定不由环境变量控制：签发时若在 payload 里写了 machine，
校验就无条件生效（payload 有 RSA 签名，客户改不了）。

启动时调用 `license_gate()`（不要直接调 verify_license_or_exit）；
每请求必经的 csrf_mw 会调 `assert_licensed()` 兜底，
这样即使有人改掉未编译的 run.py / app/main.py 也拿不到服务。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime


# 公钥 — 内嵌在代码里（和 douyin_im.py 一起 cythonize 后更难提取）
# 对应私钥存在卖家本地，用 issue_license.py 签发
_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqvOoEBDoNYN/A4Uy6r+W
hnXuK6c+7450L40CQZQ1lw8H/Tp43LFbJXreL+rlUW28GUHBapakUWStqSt4r8mD
Z+Qkt0TUuv9cgSh08HCqqo1rX6Mm7A74fyjAfi4BTfAx0N+Lzk7wCJagX4Jra7Ov
4zrVz2yQYOdS4A7r90toOnLx1a7v7k++Ac+dJYCyBp4MwyIvub0VB0FgXdt6YUHC
8QpY1FvQr/P3L2I8KQSMEhhYRJW9/GOZd2NyU6FNs3XICWxqC0WaiX8QEI4CBZ5B
JK6Dj9mbvIq2FX9AmtdxUvCiAON+224Pb9ykysFTmafoH1/TraaESxEWcgCLj0yR
7wIDAQAB
-----END PUBLIC KEY-----"""


# 构建期常量：Dockerfile 在 cythonize 之前把这行替换成 False，
# 于是正式镜像的 license.so 里 SKIP_LICENSE_CHECK 彻底失效。
# ⚠️ 改动这一行的写法必须同步改 Dockerfile 的 sed（有测试锁住形状）。
_ALLOW_SKIP_LICENSE = True  # BUILD_FLAG

# 启动闸门是否已放行。assert_licensed() 用它做运行时兜底。
_LICENSE_OK = False


def _install_id_paths() -> list[str]:
    """`.install_id` 的候选位置，按优先级排列。

    第一个是权威位置：settings.data_dir 走 pydantic-settings，会读 `.env`。
    只读 os.environ 是不够的 —— 只在 `.env` 里配 DATA_DIR 的源码部署，
    环境变量里根本没有它，机器码就会写到 CWD 下的 ./data。
    那不是客户备份的目录，换个工作目录重启机器码就变了，
    绑定的 License 直接启动失败 —— 正是持久化 install_id 要避免的事。
    （Docker 里 compose 设了真环境变量，所以一直没暴露。）

    第二个是历史位置，保证存量安装升级后机器码不变。
    """
    paths: list[str] = []
    try:
        from .config import settings
        paths.append(os.path.join(settings.data_dir, ".install_id"))
    except Exception:
        pass
    legacy = os.path.join(os.environ.get("DATA_DIR", "./data"), ".install_id")
    if legacy not in paths:
        paths.append(legacy)
    return paths


def _install_id() -> str | None:
    """持久化安装 ID —— 存在数据卷里，跨容器重建/镜像升级都稳定。

    这是新签发 License 推荐绑定的标识：容器 hostname 会随重建变化，
    只靠 MAC+hostname 会让客户在 `docker compose up --force-recreate` 后
    突然启动失败。

    读的时候把所有候选位置都找一遍（存量安装的 id 可能在老位置），
    只有一个都没有时才在权威位置新建。
    """
    paths = _install_id_paths()
    for path in paths:
        try:
            if os.path.exists(path):
                val = open(path).read().strip()
                if val:
                    return val
        except Exception:
            continue

    try:
        path = paths[0]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        val = hashlib.sha256(os.urandom(32)).hexdigest()[:16]
        with open(path, "w") as f:
            f.write(val)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return val
    except Exception:
        return None


def _mac_id() -> str:
    """仅基于 MAC 的指纹（compose 固定了 mac_address，故跨重建稳定）。"""
    try:
        return hashlib.sha256(f"{uuid.getnode()}".encode()).hexdigest()[:16]
    except Exception:
        return "unknown"


def _legacy_machine_id() -> str:
    """旧算法：MAC + hostname。保留用于兼容存量已签发的绑定 License。"""
    try:
        mac = uuid.getnode()
        host = os.uname().nodename if hasattr(os, "uname") else ""
        return hashlib.sha256(f"{mac}:{host}".encode()).hexdigest()[:16]
    except Exception:
        return "unknown"


def _machine_id() -> str:
    """对外展示/签发用的机器码 —— 优先持久化安装 ID。"""
    return _install_id() or _legacy_machine_id()


def _machine_candidates() -> list[str]:
    """所有可接受的机器指纹；绑定校验命中任一即通过。

    同时接受三种是为了不误伤：
      - install_id  : 新签发推荐，最稳
      - legacy      : 存量客户已绑定的 MAC+hostname
      - mac_id      : 存量客户容器重建导致 hostname 变化后的兜底
    """
    out = []
    iid = _install_id()
    if iid:
        out.append(iid)
    out.append(_legacy_machine_id())
    out.append(_mac_id())
    return out


def _fail(msg: str) -> None:
    border = "─" * 60
    print("\n" + border, file=sys.stderr)
    print("  ❌ 许可验证失败", file=sys.stderr)
    print(border, file=sys.stderr)
    print(f"  {msg}", file=sys.stderr)
    print(border, file=sys.stderr)
    print(f"  当前机器码：{_machine_id()}", file=sys.stderr)
    print(border + "\n", file=sys.stderr)
    sys.exit(2)


def verify_license_or_exit() -> dict:
    """在应用启动入口调用。验证失败 → 进程退出。返回 license payload dict。

    预期 License 格式（base64 编码的 JSON）：
        {
          "issued_at":  "2026-04-19T00:00:00",
          "expires_at": "2027-04-19T00:00:00",
          "tier":       "pro",            # basic | pro | enterprise
          "machine":    "abc123..." 或 null,
          "note":       "客户备注"
        }
    """
    raw = (os.environ.get("LICENSE_KEY") or "").strip()
    if not raw:
        _fail("未设置 LICENSE_KEY 环境变量。\n"
              "  获取 License 请联系卖家：闲鱼口令 HU287，或 https://m.tb.cn/h.iJuxIwu")
    try:
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import padding
    except Exception as e:
        _fail(f"缺少 cryptography 依赖：{e}")

    try:
        payload_b64, sig_b64 = raw.split(".", 1)
        payload = base64.b64decode(payload_b64)
        sig = base64.b64decode(sig_b64)
    except Exception:
        _fail("License 格式错误（应形如 <payload>.<signature>）")

    # 验签
    try:
        pubkey = serialization.load_pem_public_key(_PUBLIC_KEY_PEM)
        pubkey.verify(sig, payload, padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        _fail("License 签名无效（可能被篡改或非法生成）")

    try:
        data = json.loads(payload)
    except Exception:
        _fail("License payload 不是合法 JSON")

    # 过期检查
    try:
        expires_at = datetime.fromisoformat(data["expires_at"])
    except Exception:
        _fail("License 缺少或损坏的 expires_at 字段")
    now = datetime.utcnow()
    if expires_at < now:
        _fail(f"License 已于 {expires_at.isoformat()} 过期。请续费。")

    # 机器绑定校验
    # payload 受 RSA 签名保护，客户无法篡改其中的 machine 字段；
    # 因此「是否绑定」只由签发方决定 —— 一旦签发时绑了机器就无条件校验。
    # 不能交给客户可控的 LICENSE_STRICT 环境变量，否则删掉它就能解绑。
    bound_machine = data.get("machine")
    current_machine = _machine_id()
    if bound_machine and bound_machine not in _machine_candidates():
        _fail(f"License 绑定的机器码与当前不匹配。\n"
              f"  绑定机器：{bound_machine}\n"
              f"  当前机器：{current_machine}\n"
              f"  请联系卖家解绑或重新签发。")

    # 成功信息
    remain_days = (expires_at - now).days
    tier = data.get("tier", "basic")
    note = data.get("note", "")
    banner = "━" * 60
    lines = [
        banner,
        "  ✓ License 已验证",
        banner,
        f"  等级    : {tier}",
        f"  到期    : {data['expires_at']}  （剩余 {remain_days} 天）",
        f"  机器绑定: {'严格 · ' + bound_machine if bound_machine else '未绑定'}",
    ]
    if note:
        lines.append(f"  备注    : {note}")
    lines.append(banner)
    print("\n".join(lines), flush=True)
    return data


# ── 启动闸门 / 运行时断言 ────────────────────────────────────────────

def license_gate() -> dict | None:
    """统一的启动校验入口，run.py 与 app.main 的 lifespan 都必须调用。

    源码态允许 SKIP_LICENSE_CHECK=1 跳过（方便开发）；
    正式镜像里 _ALLOW_SKIP_LICENSE 已被编译成 False，跳不掉。
    """
    global _LICENSE_OK
    skip = os.environ.get("SKIP_LICENSE_CHECK") in ("1", "true", "True", "yes")
    if skip and _ALLOW_SKIP_LICENSE:
        print("[license] 开发模式：跳过 License 校验（正式镜像无此行为）")
        _LICENSE_OK = True
        return None
    data = verify_license_or_exit()
    _LICENSE_OK = True
    return data


def assert_licensed() -> None:
    """运行时兜底断言 —— 由每请求必经的 csrf_mw（已 cythonize）调用。

    Why: run.py 和 app/main.py 出于 FastAPI/Pydantic 元类兼容不做编译，
    是明文可改的。把最终判定放在编译模块里，改明文入口也绕不过去。
    """
    if not _LICENSE_OK:
        raise RuntimeError(
            "License 未通过校验，服务拒绝提供。"
            "请通过 run.py 正常启动，并配置有效的 LICENSE_KEY。"
        )
