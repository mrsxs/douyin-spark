"""
SQLAlchemy engine + SessionLocal + Base.
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from .config import settings

# SQLite 需要 check_same_thread=False 才能跨线程（scheduler 线程 + FastAPI 请求）
connect_args = {"check_same_thread": False} if settings.db_url.startswith("sqlite") else {}

engine = create_engine(settings.db_url, connect_args=connect_args, echo=False)

# SQLite 并发性能调优：WAL 模式 + 锁等待 5s + 同步级别 NORMAL
if settings.db_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")   # 写锁冲突时等 5 秒
            cur.execute("PRAGMA synchronous=NORMAL")  # WAL 下 NORMAL 安全且更快
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency — 每请求一个 session，请求结束关闭"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """启动时创建表 + 幂等补复合索引 + 迁移 templates.json 进 DB"""
    from . import models  # noqa: F401
    from sqlalchemy import text
    Base.metadata.create_all(bind=engine)
    # 幂等补索引：create_all 不会给已存在的表加新索引
    ddl = [
        "CREATE INDEX IF NOT EXISTS idx_jobrun_acc_kind_started "
        "ON job_runs(douyin_account_id, kind, started_at)",
        "CREATE INDEX IF NOT EXISTS idx_noti_user_unread "
        "ON notifications(user_id, read_at)",
        "CREATE INDEX IF NOT EXISTS idx_schedule_enabled_time "
        "ON schedules(enabled, time_hhmm)",
        # 聊天页按 (账号, 对方) 倒序翻页，没这个索引会全表扫
        "CREATE INDEX IF NOT EXISTS idx_chatmsg_acc_peer_time "
        "ON chat_messages(douyin_account_id, peer_uid, created_ms)",
    ]
    # 幂等加列：SQLite 没 IF NOT EXISTS，try/except
    add_columns = [
        ("douyin_accounts", "avatar", "VARCHAR(512)"),
        ("job_runs", "total", "INTEGER DEFAULT 0"),
        ("users", "session_version", "INTEGER DEFAULT 0"),
        # 首屏改读 Contact 冷备后，渲染需要头像和火花状态
        ("contacts", "avatar", "VARCHAR(512)"),
        ("contacts", "status", "VARCHAR(16) DEFAULT 'active'"),
        # 聊天里内嵌视频封面/图片
        ("chat_messages", "media", "TEXT"),
        # AI 回复策略搬到界面上可编辑（老库里这张表已经建过，create_all 不会补列）
        ("ai_reply_configs", "decline_policy", "TEXT DEFAULT ''"),
        ("ai_reply_configs", "fewshot", "TEXT DEFAULT ''"),
        # 老库里已有的行要默认开着思考，保持升级前后行为一致
        ("ai_reply_configs", "thinking", "BOOLEAN DEFAULT 1"),
        # 已断火花默认照发（直接发消息就能续上）；没火花的普通好友默认不发
        ("schedules", "send_to_broken", "BOOLEAN DEFAULT 1"),
        ("schedules", "send_to_no_spark", "BOOLEAN DEFAULT 0"),
        # 发消息间隔，用户自己调。默认值复刻旧的 5.0±0.5，
        # 升级不该悄悄改变已有账号的发送节奏
        ("schedules", "send_min_sec", "FLOAT DEFAULT 4.5"),
        ("schedules", "send_max_sec", "FLOAT DEFAULT 5.5"),
        # 拉历史消息（cmd=301）要带的会话数字 id
        ("contacts", "conv_short_id", "BIGINT"),
        # 重燃中的进度 N/M
        ("contacts", "recover_days", "INTEGER"),
        ("contacts", "recover_need_days", "INTEGER"),
        # 是否回复对方分享的视频。老库一律默认关 —— 升级不该让谁的号
        # 突然开始自动回视频，那要多打抖音的解析接口
        ("ai_reply_configs", "reply_share", "BOOLEAN DEFAULT 0"),
        # 联系人级覆盖，NULL = 跟随账号级
        ("ai_reply_peers", "reply_share", "BOOLEAN"),
        # 语音转写。老库同样默认关 —— 没配 ASR 时开着也没用
        ("ai_reply_configs", "reply_voice", "BOOLEAN DEFAULT 0"),
        ("ai_reply_configs", "asr_base_url", "VARCHAR(255) DEFAULT ''"),
        ("ai_reply_configs", "asr_model", "VARCHAR(80) DEFAULT ''"),
        ("ai_reply_configs", "asr_key_enc", "TEXT DEFAULT ''"),
        ("ai_reply_peers", "reply_voice", "BOOLEAN"),
    ]
    with engine.begin() as conn:
        for sql in ddl:
            try:
                conn.execute(text(sql))
            except Exception as e:
                print(f"[init_db] 创建索引失败 ({sql[:60]}...): {e}")
        for table, col, col_type in add_columns:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                print(f"[init_db] 列 {table}.{col} 已添加")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    print(f"[init_db] 加列 {table}.{col} 失败: {e}")
    try:
        _migrate_drop_license()
    except Exception as e:
        # 不静默吞：这条迁移专门用来防「升级即变砖」。它失败时旧库里
        # max_accounts=0 的用户能登录却一个抖音号都加不了，界面只显示
        # 「已达上限 0」，没有任何线索指向这里。
        # 也不 raise —— 迁移在同一个事务里写标记位，下次重启会自愈，
        # 为它挡住整个服务启动不划算。所以喊得足够大声。
        border = "!" * 68
        print(f"\n{border}\n"
              f"[init_db] ❌ 授权码遗留数据迁移失败: {type(e).__name__}: {e}\n"
              f"  影响：从旧版升级上来、此前未激活的用户配额仍是 0，\n"
              f"        他们能登录但加不了抖音号（界面显示「已达上限 0」）。\n"
              f"  处理：重启容器会自动重试；若反复失败，管理员可在 /admin\n"
              f"        逐个调整账号配额，或执行\n"
              f"        python -m app.cli list-users 确认受影响的用户。\n"
              f"{border}\n", flush=True)
    try:
        _migrate_templates_json()
    except Exception as e:
        print(f"[init_db] 模板迁移失败: {e}")


def _migrate_drop_license():
    """一次性迁移：拆掉授权码体系后收拾旧库。

    旧版本里 max_accounts=0 表示「注册了但没兑换授权码」，配额也就是 0。
    去掉授权码后这些用户能登录却一个抖音号都加不了 —— 升级即变砖。
    所以把 0 抬到 DEFAULT_MAX_ACCOUNTS，并丢掉 license_codes 表。

    只跑一次：用 app_settings 里的标记位挡住。否则管理员后来故意把某人
    配额调回 0，下次重启又会被抬上去。

    走裸 SQL 而不是 ORM：这是启动期迁移，不能依赖 models 的当前形状。
    但 app_settings.updated_at 的默认值是 ORM 层的 `default=datetime.utcnow`，
    裸 INSERT 绕过它就会撞 NOT NULL —— 标记位写不进去，迁移每次启动重跑，
    恰好破坏了它要防的那件事。所以这里必须显式带上 updated_at。
    """
    from datetime import datetime

    from sqlalchemy import text

    from .models import DEFAULT_MAX_ACCOUNTS

    MARK = "migration.drop_license"
    with engine.begin() as conn:
        done = conn.execute(
            text("SELECT 1 FROM app_settings WHERE key = :k"), {"k": MARK}
        ).first()
        if done:
            return 0
        n = conn.execute(
            text("UPDATE users SET max_accounts = :d WHERE max_accounts = 0"),
            {"d": DEFAULT_MAX_ACCOUNTS},
        ).rowcount
        conn.execute(text("DROP TABLE IF EXISTS license_codes"))
        conn.execute(
            text("INSERT INTO app_settings (key, value, updated_at) "
                 "VALUES (:k, :v, :ts)"),
            {"k": MARK, "v": '"done"', "ts": datetime.utcnow()},
        )
    if n:
        print(f"[init_db] 授权码体系已移除：{n} 个用户的账号配额从 0 抬到 "
              f"{DEFAULT_MAX_ACCOUNTS}")
    return n


def _migrate_templates_json():
    """把每个账户目录下的 templates.json 一次性导入 message_templates 表。
    已存在的 (account_id, uid) 不覆盖 — 幂等。"""
    import os
    import json
    from .models import DouyinAccount, MessageTemplate
    from .storage import AccountCtx

    with SessionLocal() as db:
        accounts = db.query(DouyinAccount).all()
        migrated_accounts = 0
        migrated_rows = 0
        for acc in accounts:
            tpl_path = os.path.join(AccountCtx(acc.user_id, acc.id).dir(), "templates.json")
            if not os.path.exists(tpl_path):
                continue
            try:
                data = json.load(open(tpl_path))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            existing_uids = {
                t.uid for t in db.query(MessageTemplate).filter(
                    MessageTemplate.douyin_account_id == acc.id).all()
            }
            for uid, entry in data.items():
                if uid in existing_uids:
                    continue
                if not isinstance(entry, dict):
                    continue
                db.add(MessageTemplate(
                    douyin_account_id=acc.id,
                    uid=uid,
                    name=entry.get("name"),
                    enabled=bool(entry.get("enabled", True)),
                    messages_json=json.dumps(entry.get("messages") or [], ensure_ascii=False),
                ))
                migrated_rows += 1
            migrated_accounts += 1
        if migrated_rows:
            db.commit()
            print(f"[init_db] 模板迁移完成：{migrated_accounts} 个账户，{migrated_rows} 条模板入库")
