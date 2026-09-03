"""
ORM 模型：User / LicenseCode / DouyinAccount / Schedule / AuditLog
"""
from datetime import datetime
from sqlalchemy import (
    BigInteger, Integer, Float, String, Boolean, DateTime, Text, ForeignKey,
    UniqueConstraint, Index,
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

    # 参与 session 签名：+1 即让该用户已签发的所有 cookie 立即失效。
    # 改密码 / 管理员重置 / 强制下线时递增。
    session_version: Mapped[int] = mapped_column(Integer, default=0)

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
    avatar:        Mapped[str | None] = mapped_column(String(512), nullable=True)

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
    # 火花已断(broken)的人：默认发。断了的火花直接发消息就能续上，
    # 跳过他们等于白白少续一批人。
    send_to_broken:    Mapped[bool]  = mapped_column(Boolean, default=True)
    # 从来没有火花(none)的普通好友：默认不发。那是主动去搭讪没在互动的人，
    # 风控面和用户预期都和「续火花」不是一回事，必须显式打开。
    send_to_no_spark:  Mapped[bool]  = mapped_column(Boolean, default=False)
    last_ran_date:     Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_result:       Mapped[str | None] = mapped_column(Text, nullable=True)

    # 两条消息之间的间隔，秒。用户自己调 —— 3 秒对老号可能过快，
    # 20 秒对小号又太磨叽，合适的节奏只有用号的人知道。
    # 默认 4.5~5.5 复刻改这个功能之前的 `5.0 ± random(-0.5, 0.5)`，
    # 升级不该悄悄改变谁的发送节奏。风控降速倍数在这个区间之上叠加。
    send_min_sec:      Mapped[float] = mapped_column(Float, default=4.5)
    send_max_sec:      Mapped[float] = mapped_column(Float, default=5.5)

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
    # 预计要处理的联系人数 —— 进度百分比的分母。
    # 任务启动时还不知道（要先调抖音 API 拉联系人），由后台线程拿到后回填。
    total:            Mapped[int]     = mapped_column(Integer, default=0)

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
    avatar:            Mapped[str | None] = mapped_column(String(512), nullable=True)
    conv_id:           Mapped[str | None] = mapped_column(String(80), nullable=True)
    # 拉历史消息（imapi cmd=301）必须带的会话数字 id。
    # parse_fire_streaks 本来就解析得出，存下来省得每次重打 1.5MB 的 init 去要。
    conv_short_id:     Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    days:              Mapped[int | None] = mapped_column(Integer, nullable=True)
    # active（还在烧）| recovering（断过、正在重燃窗口期内）
    # | broken（已断）| none（从来没火花的普通好友）
    status:            Mapped[str]     = mapped_column(String(16), default="active")
    # 重燃进度「N/M」：已连上 N 天、需要 M 天才能把 days 那个数救回来。
    # 非 recovering 时都是 0。取自 flame_infos 当前段的 text「重燃中 N/M」
    recover_days:      Mapped[int | None] = mapped_column(Integer, nullable=True)
    recover_need_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tags:              Mapped[str | None] = mapped_column(String(200), nullable=True)  # 逗号分隔
    last_synced_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("douyin_account_id", "uid", name="uq_contact_uid"),)


class ChatMessage(Base):
    """聊天消息冷备。

    数据源是 get_message_by_init 响应 —— 拉联系人时顺手解析，
    不额外请求抖音。抖音只在响应里给每个会话最近 ~21 条，
    所以这张表是「越攒越全」：每次同步把新消息累积进来，
    历史不会因为抖音只回最近若干条而丢失。
    """
    __tablename__ = "chat_messages"

    id:                Mapped[int]     = mapped_column(Integer, primary_key=True)
    douyin_account_id: Mapped[int]     = mapped_column(ForeignKey("douyin_accounts.id", ondelete="CASCADE"), index=True)
    peer_uid:          Mapped[str]     = mapped_column(String(40))       # 对方 uid，对应 Contact.uid
    conv_id:           Mapped[str | None] = mapped_column(String(80), nullable=True)
    # 抖音的 server_message_id，全局唯一，做幂等去重用
    server_msg_id:     Mapped[int]     = mapped_column(BigInteger)
    sender:            Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_me:             Mapped[bool]    = mapped_column(Boolean, default=False)
    msg_type:          Mapped[int]     = mapped_column(Integer, default=0)
    # text / image / audio / emoji / share / system / other
    kind:              Mapped[str]     = mapped_column(String(16), default="other")
    text:              Mapped[str | None] = mapped_column(Text, nullable=True)
    # 可内嵌的媒体（视频封面+id / 图片原图），JSON。没有则为空
    media:             Mapped[str | None] = mapped_column(Text, nullable=True)
    # 抖音给的是毫秒时间戳，原样存 —— 转本地时间交给前端，避免时区来回踩坑
    created_ms:        Mapped[int]     = mapped_column(BigInteger, default=0, index=True)
    synced_at:         Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("douyin_account_id", "server_msg_id", name="uq_chatmsg_srvid"),
    )


class VideoParse(Base):
    """分享视频的解析结果 —— **全局缓存，刻意不挂 account_id**。

    同一个视频谁分享都是同一份内容，按账号分表存等于把同一份东西
    重复问抖音 N 次。这是给真人号新增的一类请求，省下的每一次都是风控额度。

    只在 AI 真要回复某条分享视频时才写（懒解析）；聊天页浏览不触发。
    结果不放进 ChatMessage.media —— 那个字段有 2000 字节硬上限。
    """
    __tablename__ = "video_parses"

    id:          Mapped[int] = mapped_column(Integer, primary_key=True)
    # 抖音的 item_id，19 位左右的雪花 id
    aweme_id:    Mapped[str] = mapped_column(String(24), unique=True, index=True)

    # "ok"（有内容）| "failed"（拉不到，短期内不重试）
    status:      Mapped[str] = mapped_column(String(16), default="ok")
    err:         Mapped[str] = mapped_column(String(200), default="")

    desc_text:   Mapped[str] = mapped_column(Text, default="")      # 作者写的文案
    title:       Mapped[str] = mapped_column(String(200), default="")
    # 抖音自己生成的内容总结（AI抖音「视频总结」），是喂给 AI 的主要素材
    summary:     Mapped[str] = mapped_column(Text, default="")
    author_nick: Mapped[str] = mapped_column(String(80), default="")
    author_sec:  Mapped[str] = mapped_column(String(120), default="")
    music:       Mapped[str] = mapped_column(String(120), default="")
    cover_url:   Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    create_time: Mapped[int] = mapped_column(Integer, default=0)    # 抖音的发布秒级时间戳
    tags:        Mapped[str] = mapped_column(Text, default="[]")    # JSON: 话题标签
    categories:  Mapped[str] = mapped_column(Text, default="[]")    # JSON: 抖音打的三级内容分类
    stats:       Mapped[str] = mapped_column(Text, default="{}")    # JSON: 点赞/评论/转发/收藏

    parsed_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                  onupdate=datetime.utcnow)


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


class AiReplyConfig(Base):
    """AI 自动回复 — 账号级配置（每个抖音账号一行）。

    api_key 用 app.crypto 加密存（`ENC1:` 前缀），任何接口都不回明文。
    enabled_at 是「启用时刻」：早于它的历史消息一律不回 ——
    否则开关一打开，库里攒的几百条老消息会被一次性全回一遍，号当天就废。
    """
    __tablename__ = "ai_reply_configs"

    id:                Mapped[int]  = mapped_column(Integer, primary_key=True)
    douyin_account_id: Mapped[int]  = mapped_column(
        ForeignKey("douyin_accounts.id", ondelete="CASCADE"), unique=True, index=True)

    enabled:      Mapped[bool] = mapped_column(Boolean, default=False)
    # 开关打开的时刻（naive UTC）。关掉再打开会刷新，不会翻旧账。
    enabled_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # "openai" = OpenAI 兼容（DeepSeek/通义/Kimi/vLLM…）| "anthropic"
    provider:     Mapped[str]  = mapped_column(String(16), default="openai")
    base_url:     Mapped[str]  = mapped_column(String(255), default="")
    model:        Mapped[str]  = mapped_column(String(80), default="")
    api_key_enc:  Mapped[str]  = mapped_column(Text, default="")

    persona:        Mapped[str] = mapped_column(Text, default="")   # 人设
    prompt_template: Mapped[str] = mapped_column(Text, default="")  # 含 {{userinput}}
    reply_format:   Mapped[str] = mapped_column(Text, default="{{message}}")
    # 「什么该回、什么该弃权」和校准用的示例。空 = 用 ai_reply 里的默认值。
    # 做成可编辑的原因：这是纯判断题，每个人的尺度不一样 ——
    # 写死在代码里的那版对「问」「能打个电话吗」全部弃权，用户完全无从下手改。
    decline_policy: Mapped[str] = mapped_column(Text, default="")
    fewshot:        Mapped[str] = mapped_column(Text, default="")

    max_chars:    Mapped[int] = mapped_column(Integer, default=60)    # 回复字数硬上限
    # 让模型「思考」再回答。开=贵而稳，关=快而省。
    # 这个网关上 thinking 和 response_format 不能并存，所以它同时决定了
    # 输出格式：开→强制 JSON（模型能表达弃权），关→纯文本（靠哨兵词表达弃权）。
    thinking:     Mapped[bool] = mapped_column(Boolean, default=True)
    cooldown_sec: Mapped[int] = mapped_column(Integer, default=20)    # 同会话最短间隔
    daily_limit:  Mapped[int] = mapped_column(Integer, default=100)   # 每日回复上限
    history_turns: Mapped[int] = mapped_column(Integer, default=6)    # 带几条上下文
    # 常驻轮询间隔（秒）。没人开聊天页时用它，别和聊天页的 5 秒一个量级 —— 那是给抖音送风控素材
    poll_interval: Mapped[int] = mapped_column(Integer, default=30)
    # 用户自定义禁词，逗号分隔。命中即不发
    banned_words: Mapped[str] = mapped_column(Text, default="")
    # 是否回复对方分享的视频。默认关：回复分享要先去抖音解析视频，
    # 那是给真人号新增的一类请求，得让用户显式点头才花这个风控额度。
    reply_share:  Mapped[bool] = mapped_column(Boolean, default=False)
    # 是否回复对方发的语音。默认关，且要先配好 ASR 才有意义 ——
    # 抖音的 IM 语音不带转写，不转成文字模型就只看得到「[语音] 3.2″」
    reply_voice:  Mapped[bool] = mapped_column(Boolean, default=False)

    # 语音转写（OpenAI 兼容 /audio/transcriptions）。独立于主网关：
    # 主网关常是 DeepSeek，它没有这个接口。三项缺一即视为未启用。
    asr_base_url: Mapped[str] = mapped_column(String(255), default="")
    asr_model:    Mapped[str] = mapped_column(String(80), default="")
    asr_key_enc:  Mapped[str] = mapped_column(Text, default="")

    # 连续失败计数，达阈值自动关开关 + 站内通知，不无限烧钱
    fail_streak:  Mapped[int] = mapped_column(Integer, default=0)
    updated_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AiReplyPeer(Base):
    """AI 自动回复 — 联系人级开关与覆盖。

    白名单模式：这张表里 enabled=True 的联系人才会被自动回复，
    默认谁都不回 —— 一个手滑让所有好友收到 AI 回复是不可逆的。

    persona / reply_format 为 NULL 表示继承账号级配置，
    填了就只对这个人生效（「对客户正式、对朋友随意」）。
    """
    __tablename__ = "ai_reply_peers"

    id:                Mapped[int]  = mapped_column(Integer, primary_key=True)
    douyin_account_id: Mapped[int]  = mapped_column(
        ForeignKey("douyin_accounts.id", ondelete="CASCADE"), index=True)
    uid:               Mapped[str]  = mapped_column(String(40))

    enabled:      Mapped[bool] = mapped_column(Boolean, default=False)
    persona:      Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_format: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NULL = 跟随账号级 reply_share；True/False = 只对这个人生效
    # （「对客户回视频、对朋友不回」）
    reply_share:  Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 同上，针对语音
    reply_voice:  Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    updated_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("douyin_account_id", "uid", name="uq_aipeer_uid"),)


class KnowledgeEntry(Base):
    """知识库条目。

    uid='*' 是**通用知识库**（对所有联系人生效）；
    uid=某个联系人 uid 是**该联系人专属**。
    两者相互独立：检索时各取各的再拼接，改通用不会动到单独的，反之亦然。
    """
    __tablename__ = "knowledge_entries"

    id:                Mapped[int]  = mapped_column(Integer, primary_key=True)
    douyin_account_id: Mapped[int]  = mapped_column(
        ForeignKey("douyin_accounts.id", ondelete="CASCADE"), index=True)
    uid:               Mapped[str]  = mapped_column(String(40), default="*", index=True)

    title:    Mapped[str] = mapped_column(String(120), default="")
    content:  Mapped[str] = mapped_column(Text, default="")
    # 逗号分隔的命中关键词，命中直接加权（比 n-gram 相似度更可控）
    keywords: Mapped[str] = mapped_column(String(255), default="")
    enabled:  Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_kb_acc_uid", "douyin_account_id", "uid", "enabled"),
    )


class AiReplyLog(Base):
    """每条被处理的来信一行 —— 既是审计，也是**幂等锁**。

    唯一键 (account, server_msg_id)：处理前先插占位行，插入冲突即代表
    这条消息已经处理过。重启、SSE 重连、多线程重复投递都不会回第二次。
    """
    __tablename__ = "ai_reply_logs"

    id:                Mapped[int]  = mapped_column(Integer, primary_key=True)
    douyin_account_id: Mapped[int]  = mapped_column(
        ForeignKey("douyin_accounts.id", ondelete="CASCADE"), index=True)
    peer_uid:      Mapped[str] = mapped_column(String(40))
    server_msg_id: Mapped[int] = mapped_column(BigInteger)

    # pending | sent | skipped | blocked | llm_error | send_failed
    status:       Mapped[str] = mapped_column(String(16), default="pending", index=True)
    incoming:     Mapped[str | None] = mapped_column(Text, nullable=True)   # 对方原话（截断）
    raw_output:   Mapped[str | None] = mapped_column(Text, nullable=True)   # 模型原始输出，排查用
    final_text:   Mapped[str | None] = mapped_column(Text, nullable=True)   # 实际发出去的
    reason:       Mapped[str | None] = mapped_column(String(64), nullable=True)  # 跳过/拦截原因
    tokens:       Mapped[int] = mapped_column(Integer, default=0)
    latency_ms:   Mapped[int] = mapped_column(Integer, default=0)
    created_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("douyin_account_id", "server_msg_id", name="uq_ailog_srvid"),
        # 日配额统计：按 (账号, 时间) 数当天 sent 条数
        Index("idx_ailog_acc_created", "douyin_account_id", "created_at"),
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
