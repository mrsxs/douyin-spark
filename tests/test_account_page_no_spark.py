"""账户页对「无火花好友」的渲染契约。

前端是 Jinja + Alpine，没有构建步骤也没有前端测试框架，
所以拿 HTML 文本断言 —— 段控、徽章、总开关这些一旦漏渲染，
用户就是「点了没反应」，光看后端测试发现不了。
"""

import pytest

from app import contacts_service as cs
from app.models import DouyinAccount, Schedule, User
from app.security import hash_password


@pytest.fixture
def acc(db):
    u = User(username="pageuser", password_hash=hash_password("pw123456"),
             max_accounts=3)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="主号", status="active",
                      cookies_exist=True)
    db.add(a); db.commit(); db.refresh(a)
    return u, a


MIXED = [
    {"uid": "111", "nickname": "在烧", "conv_id": "c1", "days": 30, "status": "active"},
    {"uid": "222", "nickname": "已断", "conv_id": "c2", "days": 5, "status": "broken"},
    {"uid": "333", "nickname": "普通好友", "conv_id": "c3", "days": 0, "status": "none"},
]


def _page(db, acc, login):
    u, a = acc
    cs.upsert_cache(db, a.id, MIXED); db.commit()
    r = login(u).get(f"/accounts/{a.id}")
    assert r.status_code == 200, r.text
    return r.text


def test_no_spark_contacts_are_rendered(db, acc, login):
    """核心：没有火花的好友也要出现在列表里，否则根本选不到。"""
    html = _page(db, acc, login)
    assert "普通好友" in html
    assert 'data-spark="none"' in html


def test_all_three_states_have_cards(db, acc, login):
    html = _page(db, acc, login)
    for spark in ("active", "broken", "none"):
        assert f'data-spark="{spark}"' in html, f"{spark} 卡片没渲染"


def test_segment_control_has_no_spark_tab(db, acc, login):
    html = _page(db, acc, login)
    assert "setFilter('none')" in html, "段控缺「无火花」这一段"


def test_counts_split_broken_and_none(db, acc, login):
    """broken_total 不能把 none 算进去，否则红色横幅会为普通好友报警。"""
    from app.routers import dashboard  # noqa: F401

    u, a = acc
    cs.upsert_cache(db, a.id, MIXED); db.commit()
    html = login(u).get(f"/accounts/{a.id}").text
    assert "1 位好友的火花已断" in html, "断火花计数被无火花好友污染了"


def test_send_to_broken_switch_rendered(db, acc, login):
    html = _page(db, acc, login)
    assert 'id="sch-broken"' in html
    assert "也发给火花已断的好友" in html


def test_send_to_no_spark_switch_rendered(db, acc, login):
    html = _page(db, acc, login)
    assert 'id="sch-nospark"' in html
    assert "也发给没有火花的普通好友" in html


def test_switch_defaults_reflected(db, acc, login):
    """已断默认勾上、无火花默认不勾 —— 界面得和后端默认一致。"""
    u, a = acc
    db.add(Schedule(douyin_account_id=a.id)); db.commit()
    cs.upsert_cache(db, a.id, MIXED); db.commit()

    html = login(u).get(f"/accounts/{a.id}").text
    assert 'id="sch-broken" checked' in html
    assert 'id="sch-nospark" checked' not in html


def test_no_spark_switch_reflects_saved_state(db, acc, login):
    u, a = acc
    db.add(Schedule(douyin_account_id=a.id, send_to_no_spark=True)); db.commit()
    cs.upsert_cache(db, a.id, MIXED); db.commit()

    html = login(u).get(f"/accounts/{a.id}").text
    assert 'id="sch-nospark" checked' in html


def test_bulk_buttons_rendered(db, acc, login):
    html = _page(db, acc, login)
    assert "bulkEnabled(true)" in html
    assert "bulkEnabled(false)" in html


def test_no_spark_card_has_its_own_toggle(db, acc, login):
    """无火花好友的卡片也得有「启用自动续火花」开关，不然开不了。"""
    html = _page(db, acc, login)
    body = html.split('data-spark="none"', 1)[1].split("</div>\n  </div>", 1)[0]
    assert "toggle-enabled" in body
