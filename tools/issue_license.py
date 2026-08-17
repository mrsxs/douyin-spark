"""
License 签发工具 —— 仅卖家本地使用，不得打进镜像。

用法：
  python tools/issue_license.py --days 365 --tier pro
  python tools/issue_license.py --days 30 --tier basic --machine abc123 --note "闲鱼买家-小王"

需要 license_private_key.pem 放在当前目录或通过 --key 指定。
输出 License key 到 stdout，直接复制给客户填到 .env 的 LICENSE_KEY。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path


def issue(private_key_path: str, days: int, tier: str,
          machine: str | None, note: str) -> str:
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    priv_pem = Path(private_key_path).read_bytes()
    private_key = serialization.load_pem_private_key(priv_pem, password=None)

    now = datetime.utcnow()
    payload = {
        "issued_at":  now.replace(microsecond=0).isoformat(),
        "expires_at": (now + timedelta(days=days)).replace(microsecond=0).isoformat(),
        "tier":       tier,
        "machine":    machine,      # None 表示不绑定机器
        "note":       note,
    }
    payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    sig = private_key.sign(payload_json, padding.PKCS1v15(), hashes.SHA256())

    return (base64.b64encode(payload_json).decode()
            + "." + base64.b64encode(sig).decode())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--key", default="license_private_key.pem",
                   help="私钥路径（默认当前目录 license_private_key.pem）")
    p.add_argument("--days", type=int, required=True, help="有效天数")
    p.add_argument("--tier", default="basic",
                   choices=["basic", "pro", "enterprise"],
                   help="License 等级（basic/pro/enterprise）")
    p.add_argument("--machine", default=None,
                   help="机器码（可选，绑定后该 License 只能在此机器运行；"
                        "绑定后校验无条件生效，客户无法用环境变量关闭）")
    p.add_argument("--note", default="", help="客户备注（只卖家可见，显示在启动日志）")
    p.add_argument("-q", "--quiet", action="store_true", help="只输出 license key，不打印 payload 预览")
    args = p.parse_args()

    key = issue(args.key, args.days, args.tier, args.machine, args.note)
    if not args.quiet:
        print("━" * 60, file=sys.stderr)
        print("  License 签发成功", file=sys.stderr)
        print("━" * 60, file=sys.stderr)
        print(f"  Tier    : {args.tier}", file=sys.stderr)
        print(f"  有效期  : {args.days} 天", file=sys.stderr)
        print(f"  绑定机器: {args.machine or '（未绑定）'}", file=sys.stderr)
        print(f"  备注    : {args.note or '（无）'}", file=sys.stderr)
        print("━" * 60, file=sys.stderr)
        print("  请把下面这行完整复制给客户，填到 .env 的 LICENSE_KEY：", file=sys.stderr)
        print("━" * 60, file=sys.stderr)
    print(key)


if __name__ == "__main__":
    main()
