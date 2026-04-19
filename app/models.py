"""
ORM 模型：User / LicenseCode / DouyinAccount / Schedule / AuditLog
"""
from datetime import datetime
from sqlalchemy import (
    Integer, String, Boolean, DateTime, Text, ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


class User(Base):
    __tablename__ = "users"

    id:            Mapped[int]       = mapped_column(Integer, primary_key=True)
    username:      Mapped[str]       = mapped_column(String(32), unique=True, index=True)
    email:         Mapped[str | None] = mapped_column(String(120), nullable=True)
    password_hash: Mapped[str]       = mapped_column(String(120))
    is_admin:      Mapped[bool]      = mapped_column(Boolean, default=False)
    is_active:     Mapped[bool]      = mapped_column(Boolean, default=True)

    expires_at:    Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    max_accounts:  Mapped[int]       = mapped_column(Integer, default=0)

    created_at:    Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    accounts: Mapped[list["DouyinAccount"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class LicenseCode(Base):
    __tablename__ = "license_codes"

    id:             Mapped[int]     = mapped_column(Integer, primary_key=True)
    code:           Mapped[str]     = mapped_column(String(24), unique=True, index=True)
    duration_days:  Mapped[int]     = mapped_column(Integer)
    max_accounts:   Mapped[int]     = mapped_column(Integer)
    note:           Mapped[str | None] = mapped_column(String(200), nullable=True)

    created_by:     Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at:     Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    used_by:        Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    used_at:        Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    revoked_at:     Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DouyinAccount(Base):
    __tablename__ = "douyin_accounts"

    id:            Mapped[int]     = mapped_column(Integer, primary_key=True)
    user_id:       Mapped[int]     = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label:         Mapped[str]     = mapped_column(String(40))         # 用户自定义名字

    dy_uid:        Mapped[str | None] = mapped_column(String(32), nullable=True)
    nickname:      Mapped[str | None] = mapped_column(String(80), nullable=True)

    status:        Mapped[str]     = mapped_column(String(24), default="pending_login")
    # 'pending_login' | 'active' | 'cookies_expired' | 'login_failed'
    cookies_exist: Mapped[bool]    = mapped_column(Boolean, default=False)

    created_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="accounts")
    schedule: Mapped["Schedule | None"] = relationship(back_populates="account", cascade="all, delete-orphan", uselist=False)

    __table_args__ = (UniqueConstraint("user_id", "label", name="uq_user_label"),)


class Schedule(Base):
    __tablename__ = "schedules"

    id:                Mapped[int]   = mapped_column(Integer, primary_key=True)
    douyin_account_id: Mapped[int]   = mapped_column(ForeignKey("douyin_accounts.id", ondelete="CASCADE"), unique=True)
    enabled:           Mapped[bool]  = mapped_column(Boolean, default=False)
    time_hhmm:         Mapped[str]   = mapped_column(String(5), default="09:00")
    last_ran_date:     Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_result:       Mapped[str | None] = mapped_column(Text, nullable=True)

    account: Mapped["DouyinAccount"] = relationship(back_populates="schedule")

    __table_args__ = (
        # scheduler 每 30s 扫表，按 enabled + time_hhmm 过滤
        Index("idx_schedule_enabled_time", "enabled", "time_hhmm"),
    )


class Announcement(Base):
    __tablename__ = "announcements"

    id:         Mapped[int]     = mapped_column(Integer, primary_key=True)
    title:      Mapped[str]     = mapped_column(String(120))
    content:    Mapped[str]     = mapped_column(Text)
    enabled:    Mapped[bool]    = mapped_column(Boolean, default=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class JobRun(Base):
    """一次执行任务（定时触发、手动批量、自动续火花等）的顶层记录"""
    __tablename__ = "job_runs"

    id:               Mapped[int]     = mapped_column(Integer, primary_key=True)
    douyin_account_id: Mapped[int]    = mapped_column(ForeignKey("douyin_accounts.id", ondelete="CASCADE"), index=True)
    kind:             Mapped[str]     = mapped_column(String(16))        # "auto" | "manual_single" | "manual_batch"
    triggered_by:     Mapped[str]     = mapped_column(String(16))        # "user" | "scheduler"

    started_at:       Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at:      Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sent:             Mapped[int]     = mapped_column(Integer, default=0)
    skipped:          Mapped[int]     = mapped_column(Integer, default=0)
    failed:           Mapped[int]     = mapped_column(Integer, default=0)

    status:           Mapped[str]     = mapped_column(String(16), default="running")  # running|done|error
    error:            Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list["JobRunItem"]] = relationship(back_populates="run", cascade="all, delete-orphan")

    __table_args__ = (
        # 健康评分常用：按 account + kind 倒序取最近几次
        Index("idx_jobrun_acc_kind_started", "douyin_account_id", "kind", "started_at"),
    )


class JobRunItem(Base):
    """一次任务内针对单个联系人的执行明细"""
    __tablename__ = "job_run_items"

    id:           Mapped[int]     = mapped_column(Integer, primary_key=True)
    job_run_id:   Mapped[int]     = mapped_column(ForeignKey("job_runs.id", ondelete="CASCADE"), index=True)

    uid:          Mapped[str]     = mapped_column(String(32))
    nickname:     Mapped[str | None] = mapped_column(String(80), nullable=True)
    conv_id:      Mapped[str | None] = mapped_column(String(64), nullable=True)
    message:      Mapped[str | None] = mapped_column(Text, nullable=True)

    ok:           Mapped[bool]    = mapped_column(Boolean, default=False)
    detail:       Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at:      Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped["JobRun"] = relationship(back_populates="items")


class MessageTemplate(Base):
    """消息模板（入库版）
    - uid='default' 表示兜底模板（原 templates.json 的 default 键）
    - uid=其它 表示某个联系人的独立模板
    - 跨联系人共享的"模板库"保留到下个版本
    """
    __tablename__ = "message_templates"

    id:                Mapped[int]     = mapped_column(Integer, primary_key=True)
    douyin_account_id: Mapped[int]     = mapped_column(ForeignKey("douyin_accounts.id", ondelete="CASCADE"), index=True)
    uid:               Mapped[str]     = mapped_column(String(40))   # "default" / 联系人 uid
    name:              Mapped[str | None] = mapped_column(String(80), nullable=True)  # 展示名（联系人昵称缓存）
    enabled:           Mapped[bool]    = mapped_column(Boolean, default=True)
    messages_json:     Mapped[str]     = mapped_column(Text, default="[]")   # JSON list[str]
    updated_at:        Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("douyin_account_id", "uid", name="uq_template_uid"),)


class Contact(Base):
    """抖音联系人缓存（实时源仍是抖音 API，DB 做冷备 + 支持标签/分组/搜索等后续扩展）"""
    __tablename__ = "contacts"

    id:                Mapped[int]     = mapped_column(Integer, primary_key=True)
    douyin_account_id: Mapped[int]     = mapped_column(ForeignKey("douyin_accounts.id", ondelete="CASCADE"), index=True)
    uid:               Mapped[str]     = mapped_column(String(40))
    nickname:          Mapped[str | None] = mapped_column(String(80), nullable=True)
    remark:            Mapped[str | None] = mapped_column(String(80), nullable=True)
    conv_id:           Mapped[str | None] = mapped_column(String(80), nullable=True)
    days:              Mapped[int | None] = mapped_column(Integer, nullable=True)
    tags:              Mapped[str | None] = mapped_column(String(200), nullable=True)  # 逗号分隔
    last_synced_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("douyin_account_id", "uid", name="uq_contact_uid"),)


class AppSetting(Base):
    """通用系统配置 KV 表（SMTP、公共开关等）"""
    __tablename__ = "app_settings"

    key:        Mapped[str]     = mapped_column(String(64), primary_key=True)
    value:      Mapped[str]     = mapped_column(Text)       # JSON 序列化
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class Notification(Base):
    """站内通知 — 按 user_id 存储，用户在 navbar 查看"""
    __tablename__ = "notifications"

    id:          Mapped[int]     = mapped_column(Integer, primary_key=True)
    user_id:     Mapped[int]     = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    kind:        Mapped[str]     = mapped_column(String(32))      # "send_failed" | "cookies_expired" | "license_expiring" | "info"
    title:       Mapped[str]     = mapped_column(String(200))
    content:     Mapped[str | None] = mapped_column(Text, nullable=True)
    url:         Mapped[str | None] = mapped_column(String(255), nullable=True)  # 点击跳转链接

    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    read_at:     Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    __table_args__ = (
        # 未读数统计常用：按 user + 未读 条件
        Index("idx_noti_user_unread", "user_id", "read_at"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id:             Mapped[int]     = mapped_column(Integer, primary_key=True)
    ts:             Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    actor_user_id:  Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_kind:     Mapped[str]     = mapped_column(String(16))          # user/admin/system
    action:         Mapped[str]     = mapped_column(String(48), index=True)
    target_type:    Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id:      Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta:           Mapped[str | None] = mapped_column(Text, nullable=True)
    ip:             Mapped[str | None] = mapped_column(String(64), nullable=True)
