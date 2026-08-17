"""授权码兑换：原子性、幂等、并发安全。

背景：原实现用 `db.query(...).with_for_update()` 选中未使用的码再赋值，
但 SQLite dialect 会静默忽略 FOR UPDATE，两个并发请求能选中同一行、
双双兑换成功 —— 对按码售卖的产品是直接的收入漏损。
"""
import threading

from sqlalchemy import func

from app.codes_service import redeem_code
from app.db import SessionLocal
from app.models import AuditLog, LicenseCode, User


def test_redeem_ok_sets_expiry_and_quota(db, make_user, make_code):
    user = make_user()
    make_code(code="AAAABBBBCCCCDDDD", duration_days=30, max_accounts=3)

    assert redeem_code(db, user.id, "AAAABBBBCCCCDDDD") == "ok"

    db.refresh(user)
    assert user.max_accounts == 3
    assert user.expires_at is not None
    remain = (user.expires_at - __import__("datetime").datetime.utcnow()).days
    assert 29 <= remain <= 30

    lc = db.query(LicenseCode).filter_by(code="AAAABBBBCCCCDDDD").one()
    assert lc.used_by == user.id
    assert lc.used_at is not None


def test_redeem_normalizes_case_and_whitespace(db, make_user, make_code):
    user = make_user()
    make_code(code="AAAABBBBCCCCDDDD")
    assert redeem_code(db, user.id, "  aaaabbbbccccdddd  ") == "ok"


def test_second_redeem_of_same_code_rejected(db, make_user, make_code):
    u1 = make_user("u1")
    u2 = make_user("u2")
    make_code(code="AAAABBBBCCCCDDDD")

    assert redeem_code(db, u1.id, "AAAABBBBCCCCDDDD") == "ok"
    assert redeem_code(db, u2.id, "AAAABBBBCCCCDDDD") == "code_used"

    db.refresh(u2)
    assert u2.expires_at is None       # 第二个用户没白拿到时长
    assert u2.max_accounts == 0


def test_revoked_and_missing_and_malformed(db, make_user, make_code):
    user = make_user()
    make_code(code="REVOKEDCODE12345",
              revoked_at=__import__("datetime").datetime.utcnow())

    assert redeem_code(db, user.id, "REVOKEDCODE12345") == "code_revoked"
    assert redeem_code(db, user.id, "NOSUCHCODE123456") == "code_not_found"
    assert redeem_code(db, user.id, "bad!") == "code_format"
    assert redeem_code(db, user.id, "") == "code_format"


def test_renewal_stacks_on_existing_expiry(db, make_user, make_code):
    from datetime import datetime, timedelta
    future = datetime.utcnow() + timedelta(days=10)
    user = make_user(expires_at=future, max_accounts=5)
    make_code(code="AAAABBBBCCCCDDDD", duration_days=30, max_accounts=2)

    assert redeem_code(db, user.id, "AAAABBBBCCCCDDDD") == "ok"
    db.refresh(user)
    # 从剩余到期日往后叠加，而不是从今天重算
    assert (user.expires_at - future).days == 30
    # 配额取较大值，不因为兑换小额度码而被降级
    assert user.max_accounts == 5


def test_expired_user_renews_from_now(db, make_user, make_code):
    from datetime import datetime, timedelta
    past = datetime.utcnow() - timedelta(days=10)
    user = make_user(expires_at=past)
    make_code(code="AAAABBBBCCCCDDDD", duration_days=30)

    assert redeem_code(db, user.id, "AAAABBBBCCCCDDDD") == "ok"
    db.refresh(user)
    assert (user.expires_at - datetime.utcnow()).days >= 29


def test_concurrent_redeem_only_one_wins(make_user, make_code):
    """核心回归：8 个线程抢同一个码，只能有一个成功。"""
    with SessionLocal() as setup:
        users = []
        for i in range(8):
            u = User(username=f"race{i}", password_hash="x",
                     expires_at=None, max_accounts=0)
            setup.add(u)
            users.append(u)
        setup.add(LicenseCode(code="RACECODE12345678",
                              duration_days=30, max_accounts=2))
        setup.commit()
        user_ids = [u.id for u in users]

    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(user_ids))

    def worker(uid: int):
        barrier.wait()                     # 尽量让 UPDATE 真正撞在一起
        with SessionLocal() as s:          # 每线程独立 session
            r = redeem_code(s, uid, "RACECODE12345678")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(uid,)) for uid in user_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert results.count("ok") == 1, f"码被重复兑换: {results}"
    assert set(results) <= {"ok", "code_used"}, f"意外状态: {results}"

    with SessionLocal() as s:
        # 只有一个用户拿到了时长
        granted = s.query(func.count(User.id)).filter(
            User.expires_at.isnot(None)).scalar()
        assert granted == 1
        lc = s.query(LicenseCode).filter_by(code="RACECODE12345678").one()
        assert lc.used_by is not None
        # 审计只记一次成功兑换
        audits = s.query(func.count(AuditLog.id)).filter(
            AuditLog.action == "activate_code").scalar()
        assert audits == 1
