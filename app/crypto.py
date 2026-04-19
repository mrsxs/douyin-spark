"""
字段级加密（Fernet）— 复用 SAAS_CRYPT_KEY 派生。

用途：DB 里敏感字段（如 SMTP 密码、API key 等）的对称加密。
存储格式：`ENC1:<base64-ciphertext>`；明文不带前缀，读时按前缀识别 → 懒迁移。
"""
from __future__ import annotations

import base64
import hashlib
import os
import traceback

_PREFIX = "ENC1:"


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
        return plain  # 没配 key 时降级存明文（会有日志告警）
    try:
        return _PREFIX + cipher.encrypt(plain.encode("utf-8")).decode("ascii")
    except Exception:
        traceback.print_exc()
        return plain


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
