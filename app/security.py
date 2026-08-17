"""
密码哈希、session 签名、CSRF。
"""
import hmac
import secrets
import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from .config import settings


# ── 密码 ──────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    # bcrypt 限制密码最多 72 字节
    pw = plain.encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        pw = plain.encode("utf-8")[:72]
        return bcrypt.checkpw(pw, hashed.encode())
    except Exception:
        return False


# ── Session cookie（itsdangerous 签名）────────────────────────────────

_session_serializer = URLSafeTimedSerializer(settings.secret_key, salt="session")
_csrf_serializer    = URLSafeTimedSerializer(settings.secret_key, salt="csrf")


def issue_session(user_id: int, version: int = 0) -> str:
    """签发 session cookie。version 来自 User.session_version。"""
    return _session_serializer.dumps({"uid": user_id, "v": version})


def read_session_full(cookie_val: str | None) -> tuple[int, int] | None:
    """解出 (user_id, session_version)。

    升级前签发的 cookie 没有 v 字段，按版本 0 处理 ——
    否则上线瞬间会把所有在线用户踢下线。
    """
    if not cookie_val:
        return None
    try:
        data = _session_serializer.loads(cookie_val, max_age=settings.session_max_age)
        uid = int(data.get("uid", 0))
        if not uid:
            return None
        return uid, int(data.get("v", 0))
    except (BadSignature, SignatureExpired, ValueError, TypeError, AttributeError):
        return None


def read_session(cookie_val: str | None) -> int | None:
    """只取 user_id。注意：不校验 session_version，
    需要完整校验请用 deps.resolve_session_user。"""
    parsed = read_session_full(cookie_val)
    return parsed[0] if parsed else None


# ── CSRF Token ────────────────────────────────────────────────────────

def issue_csrf() -> str:
    """生成 CSRF token（与 session 绑不绑都可；此处用 per-request 随机）"""
    return _csrf_serializer.dumps({"n": secrets.token_hex(8)})


def verify_csrf(token: str | None) -> bool:
    if not token:
        return False
    try:
        _csrf_serializer.loads(token, max_age=3600)
        return True
    except (BadSignature, SignatureExpired):
        return False


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
