"""
统一通知入口：站内通知（DB）+ 可选邮件。

使用方式：
    from app.notify import notify
    notify(db, user_id=u.id, kind="send_failed", title="...", content="...", url="...")

SMTP 在 .env 配置：SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM / SMTP_TLS
任一必填字段缺失 → 只落站内通知，不发邮件。
"""
from __future__ import annotations

import json
import smtplib
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from datetime import datetime

from .config import settings
from .db import SessionLocal
from .models import Notification, User, AppSetting


# 哪些 kind 默认同时发邮件（其他 kind 只落站内）
_EMAIL_KINDS = {"send_failed", "cookies_expired", "license_expiring"}


def get_smtp_config() -> dict:
    """优先读 DB 的 AppSetting(key='smtp')，降级到 .env 的 settings.smtp_*。
    password 字段从 ENC1: 密文解密为明文再返回（SMTP 客户端需要明文认证）。
    """
    from .crypto import decrypt
    try:
        with SessionLocal() as db:
            row = db.get(AppSetting, "smtp")
            if row and row.value:
                cfg = json.loads(row.value)
                if cfg.get("enabled"):
                    # 把密文解回明文（只在内存里短暂存在）
                    cfg["password"] = decrypt(cfg.get("password", ""))
                    return cfg
    except Exception:
        traceback.print_exc()
    # .env 兜底
    if settings.smtp_host and settings.smtp_user and settings.smtp_password:
        return {
            "enabled": True,
            "host": settings.smtp_host,
            "port": settings.smtp_port,
            "user": settings.smtp_user,
            "password": settings.smtp_password,
            "from":  settings.smtp_from or settings.smtp_user,
            "tls":   settings.smtp_tls,
        }
    return {"enabled": False}


def _smtp_ready(cfg: dict | None = None) -> bool:
    c = cfg if cfg is not None else get_smtp_config()
    return bool(c.get("enabled") and c.get("host") and c.get("user") and c.get("password"))


def _send_email_blocking(to_addr: str, subject: str, body: str,
                         cfg: dict | None = None) -> tuple[bool, str]:
    """同步发送邮件 — 返回 (ok, error_msg)"""
    c = cfg if cfg is not None else get_smtp_config()
    if not _smtp_ready(c):
        return False, "SMTP 未配置或未启用"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = c.get("from") or c["user"]
    msg["To"]      = to_addr
    msg.set_content(body)
    try:
        if c.get("tls", True):
            with smtplib.SMTP(c["host"], int(c.get("port", 587)), timeout=15) as s:
                s.starttls()
                s.login(c["user"], c["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(c["host"], int(c.get("port", 465)), timeout=15) as s:
                s.login(c["user"], c["password"])
                s.send_message(msg)
        return True, ""
    except Exception as e:
        print(f"[notify] SMTP 发送失败: {e}")
        traceback.print_exc()
        return False, str(e)


# 有界邮件发送线程池：最多 4 并发，超出的任务排队
# 比之前每封邮件一个 daemon 线程更可控（避免 SMTP 连接风暴）
_email_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="smtp")


def _send_email_async(to_addr: str, subject: str, body: str,
                      cfg: dict | None = None) -> None:
    _email_pool.submit(_send_email_blocking, to_addr, subject, body, cfg)


def send_test_email(to_addr: str, cfg: dict | None = None) -> tuple[bool, str]:
    """同步发测试邮件，返回 (ok, message)。管理员测试用。"""
    return _send_email_blocking(
        to_addr,
        f"[{settings.site_name}] SMTP 测试邮件",
        f"这是一封来自 {settings.site_name} 管理后台的 SMTP 配置测试邮件。\n"
        f"如果您收到此邮件，说明 SMTP 配置正常工作。\n\n"
        f"发送时间：{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        cfg,
    )


def notify(db, *, user_id: int, kind: str, title: str,
           content: str | None = None, url: str | None = None,
           email: bool | None = None) -> Notification:
    """创建站内通知；按策略可选同时发邮件。

    email:
        None (默认) → 根据 kind 决定（_EMAIL_KINDS 里的自动发）
        True        → 强制发
        False       → 强制不发
    """
    n = Notification(
        user_id=user_id, kind=kind, title=title,
        content=content, url=url,
    )
    db.add(n)
    # 注意：此时 n.id 还没生成，调用方需要 commit

    want_email = email if email is not None else (kind in _EMAIL_KINDS)
    if want_email and _smtp_ready():
        try:
            u = db.get(User, user_id)
            if u and u.email:
                body_lines = []
                if content:
                    body_lines.append(content)
                if url:
                    body_lines.append(f"\n详情：{url}")
                body_lines.append(f"\n\n—— {settings.site_name} "
                                  f"· {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
                _send_email_async(u.email, title, "\n".join(body_lines))
        except Exception:
            traceback.print_exc()

    return n
