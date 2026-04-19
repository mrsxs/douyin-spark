"""
软件授权验证（离线 License）。

License 格式：`<base64(payload_json)>.<base64(rsa_signature)>`
payload 内容：{expires_at, tier, machine (可选), issued_at, note}

环境变量：
  LICENSE_KEY      —— 必填，客户购买后拿到的字符串
  LICENSE_STRICT   —— 可选，=1 时开启机器绑定校验；默认宽松模式只校验过期

启动时调用 `verify_license_or_exit()`；验证失败直接 `SystemExit`。
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


def _machine_id() -> str:
    """当前机器指纹：MAC 地址 + hostname 派生的 16 位 hash"""
    try:
        mac = uuid.getnode()
        host = os.uname().nodename if hasattr(os, "uname") else ""
        return hashlib.sha256(f"{mac}:{host}".encode()).hexdigest()[:16]
    except Exception:
        return "unknown"


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

    # 机器绑定校验（strict 模式）
    strict = os.environ.get("LICENSE_STRICT", "0") in ("1", "true", "True", "yes")
    bound_machine = data.get("machine")
    current_machine = _machine_id()
    if strict and bound_machine and bound_machine != current_machine:
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
        f"  机器绑定: {'严格' if strict else '宽松'}"
        + (f" · {bound_machine}" if bound_machine else ""),
    ]
    if note:
        lines.append(f"  备注    : {note}")
    lines.append(banner)
    print("\n".join(lines), flush=True)
    return data
