"""pytest 共享基建：把 app 指向临时 DB，提供 session/工厂 fixture。

注意顺序：app.config 是模块级执行（读 env → 建 data 目录 → 派生 secret_key），
所以环境变量必须在 import app.* 之前设置，否则会污染真实 ./data。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="spark-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["DB_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"

import pytest  # noqa: E402

from app import models  # noqa: E402,F401  —— 让 Base.metadata 收齐所有表
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.security import hash_password  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_tables():
    """每个测试后清空所有表，保证用例间互不干扰。"""
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture(autouse=True)
def _clear_unread_cache():
    """清掉 csrf_mw 的未读数缓存。

    它按 user_id 缓存 15 秒，而测试清表后自增 id 会复用，
    不清的话上个用例的 0 会被下个用例读到。
    """
    from app.csrf_mw import _UNREAD_CACHE
    _UNREAD_CACHE.clear()
    yield
    _UNREAD_CACHE.clear()


@pytest.fixture(autouse=True)
def _clear_site_cfg_cache():
    """清掉站点配置的 30 秒模板缓存和二维码缓存。"""
    import app.main as main_mod
    from app import site_settings

    def _reset():
        if hasattr(main_mod.app.state, "_site_cfg_cache"):
            main_mod.app.state._site_cfg_cache = None
        site_settings._QR_CACHE.clear()

    _reset()
    yield
    _reset()


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def make_user(db):
    """建一个普通用户，模拟刚注册完。"""
    def _make(username="tester", **kw):
        u = models.User(
            username=username,
            password_hash=hash_password("pw123456"),
            max_accounts=kw.pop("max_accounts", models.DEFAULT_MAX_ACCOUNTS),
            **kw,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        return u
    return _make


@pytest.fixture
def client():
    """TestClient（不进 lifespan，避免起 scheduler 线程）。"""
    from fastapi.testclient import TestClient

    import app.main as main_mod

    return TestClient(main_mod.app, raise_server_exceptions=False)


@pytest.fixture
def login(client):
    """把某个 user 的 session cookie 装进 client。"""
    from app.deps import SESSION_COOKIE
    from app.security import issue_session

    def _login(user):
        client.cookies.set(SESSION_COOKIE, issue_session(user.id))
        return client
    return _login


@pytest.fixture
def active_user(db):
    """普通用户 + 一个 active 抖音账号。"""
    u = models.User(
        username="apiuser", password_hash=hash_password("pw123456"),
        max_accounts=5,
    )
    db.add(u); db.commit(); db.refresh(u)
    acc = models.DouyinAccount(user_id=u.id, label="主号",
                               status="active", cookies_exist=True)
    db.add(acc); db.commit(); db.refresh(acc)
    return u, acc

