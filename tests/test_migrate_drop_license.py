"""拆掉授权码体系后的一次性迁移。

旧版本里 `max_accounts=0` 表示「注册了但没兑换授权码」。去掉授权码后这些用户
能登录却一个抖音号都加不了 —— 升级即变砖。所以要把 0 抬到默认配额。

这条路径此前完全没有测试覆盖：conftest 直接 `Base.metadata.create_all`，
从不走 `init_db()`。第一版实现就因此带着一个 NOT NULL 崩溃合进来了 ——
裸 INSERT 绕过了 `app_settings.updated_at` 的 ORM 层 default，
结果标记位永远写不进去、迁移每次启动重跑，恰好破坏了它要防的那件事。
"""
from sqlalchemy import text

from app import db as db_mod
from app.models import DEFAULT_MAX_ACCOUNTS, AppSetting, User

MARK = "migration.drop_license"


def _clear_mark(db):
    db.query(AppSetting).filter(AppSetting.key == MARK).delete()
    db.commit()


def test_zero_quota_users_get_default(db):
    """核心需求：旧库里未激活的用户升级后能正常加账号。"""
    _clear_mark(db)
    db.add(User(username="legacy", password_hash="x", max_accounts=0))
    db.commit()

    n = db_mod._migrate_drop_license()

    assert n == 1
    db.expire_all()
    assert db.query(User).filter(User.username == "legacy").one().max_accounts \
        == DEFAULT_MAX_ACCOUNTS


def test_existing_quota_is_untouched(db):
    """已有配额的用户不受影响 —— 只抬 0，不动别人。"""
    _clear_mark(db)
    db.add(User(username="paid", password_hash="x", max_accounts=3))
    db.commit()

    db_mod._migrate_drop_license()

    db.expire_all()
    assert db.query(User).filter(User.username == "paid").one().max_accounts == 3


def test_mark_is_actually_persisted(db):
    """回归：标记位必须真的写进去。

    第一版用裸 INSERT 漏了 updated_at，撞 NOT NULL 被 init_db 的 try/except
    吞掉，表现是「看起来没报错但每次启动都重跑」。
    """
    _clear_mark(db)
    db_mod._migrate_drop_license()

    row = db.query(AppSetting).filter(AppSetting.key == MARK).one_or_none()
    assert row is not None, "标记位没写进去，迁移会每次启动重跑"
    assert row.updated_at is not None


def test_runs_only_once(db):
    """跑过一次之后，管理员故意调回 0 的配额不该被再抬上去。"""
    _clear_mark(db)
    db_mod._migrate_drop_license()

    # 管理员事后把某人配额收到 0（封他的加号权限）
    db.add(User(username="restricted", password_hash="x", max_accounts=0))
    db.commit()

    n = db_mod._migrate_drop_license()

    assert n == 0, "标记位没挡住，迁移重跑了"
    db.expire_all()
    assert db.query(User).filter(User.username == "restricted").one().max_accounts == 0


def test_license_codes_table_is_dropped(db):
    """旧库里的 license_codes 表要被丢掉。"""
    _clear_mark(db)
    with db_mod.engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS license_codes "
            "(id INTEGER PRIMARY KEY, code VARCHAR(24))"))
        conn.execute(text(
            "INSERT INTO license_codes (code) VALUES ('LEFTOVER12345678')"))

    db_mod._migrate_drop_license()

    with db_mod.engine.connect() as conn:
        found = conn.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='license_codes'")).first()
    assert found is None, "license_codes 表还在"


def test_survives_missing_license_codes_table(db):
    """新装的库根本没有这张表，DROP 不能炸。"""
    _clear_mark(db)
    db_mod._migrate_drop_license()   # 不抛异常即通过


def test_init_db_is_idempotent(db):
    """init_db() 连跑两次不报错 —— 每次容器重启都会跑它。"""
    db_mod.init_db()
    db_mod.init_db()
