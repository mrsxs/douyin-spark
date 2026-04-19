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
    ]
    with engine.begin() as conn:
        for sql in ddl:
            try:
                conn.execute(text(sql))
            except Exception as e:
                print(f"[init_db] 创建索引失败 ({sql[:60]}...): {e}")
    try:
        _migrate_templates_json()
    except Exception as e:
        print(f"[init_db] 模板迁移失败: {e}")


def _migrate_templates_json():
    """把每个账户目录下的 templates.json 一次性导入 message_templates 表。
    已存在的 (account_id, uid) 不覆盖 — 幂等。"""
    import os
    import json
    from .models import DouyinAccount, MessageTemplate
    from .storage import AccountCtx
    import douyin_im as dy

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
