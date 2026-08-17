"""
字段级加密（Fernet）— 复用 SAAS_CRYPT_KEY 派生。

用途：DB 里敏感字段（如 SMTP 密码、API key 等）的对称加密。
存储格式：`ENC1:<base64-ciphertext>`；明文不带前缀，读时按前缀识别 → 懒迁移。

未配置 SAAS_CRYPT_KEY 时默认降级存明文并告警；设 CRYPT_STRICT=1 可改为直接失败。
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys
import traceback

_PREFIX = "ENC1:"

# 只告警一次，避免每次加密都刷屏把其它日志淹没
_WARNED = False


def _no_key_fallback(plain: str) -> str:
    """没配 SAAS_CRYPT_KEY 时的处理。

    默认降级存明文（保持向后兼容，不让升级打挂存量部署），但必须告警 ——
    原实现注释写着「会有日志告警」，代码里却什么都没有，
    结果 SMTP 密码这类字段悄悄明文入库，没人会发现。
    生产可设 CRYPT_STRICT=1 改为直接拒绝。
    """
    global _WARNED
    if os.environ.get("CRYPT_STRICT") in ("1", "true", "True", "yes"):
        raise RuntimeError(
            "未配置 SAAS_CRYPT_KEY，且 CRYPT_STRICT=1 —— 拒绝以明文保存敏感字段")
    if not _WARNED:
        _WARNED = True
        print("[crypto] ⚠️ 未配置 SAAS_CRYPT_KEY，敏感字段将以明文存库。"
              "生产环境请设置该变量（或设 CRYPT_STRICT=1 强制失败）。",
              file=sys.stderr, flush=True)
    return plain


def _get_cipher():
    try:
        from cryptography.fernet import Fernet
        raw = os.environ.get("SAAS_CRYPT_KEY", "")
        if not raw:
            return None
        key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
        return Fernet(key)
    except Exception:
        traceback.print_exc()
        return None


def encrypt(plain: str | None) -> str:
    """加密明文；空串/None 原样返回。"""
    if not plain:
        return plain or ""
    if plain.startswith(_PREFIX):
        return plain  # 已加密，幂等
    cipher = _get_cipher()
    if not cipher:
        return _no_key_fallback(plain)
    try:
        return _PREFIX + cipher.encrypt(plain.encode("utf-8")).decode("ascii")
    except Exception:
        traceback.print_exc()
        return _no_key_fallback(plain)


def decrypt(s: str | None) -> str:
    """解密；没前缀视为明文直接返回（兼容老数据）。"""
    if not s:
        return s or ""
    if not s.startswith(_PREFIX):
        return s
    cipher = _get_cipher()
    if not cipher:
        return ""   # 有前缀但没 key → 无法解密，返回空避免拿到密文当明文用
    try:
        return cipher.decrypt(s[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        traceback.print_exc()
        return ""
