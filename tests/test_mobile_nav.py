"""移动端底部 Tab 导航。

目标用户多半用手机访问（闲鱼买家），顶部 navbar 在小屏上够不着。
"""
from datetime import datetime, timedelta

import pytest

from app.models import DouyinAccount, User
from app.security import hash_password

NAV_MARK = "md:hidden fixed bottom-0"


@pytest.fixture
def user(db):
    u = User(username="mob", password_hash=hash_password("x"),
             expires_at=datetime.utcnow() + timedelta(days=30), max_accounts=3)
    db.add(u); db.commit(); db.refresh(u)
    return u


def test_nav_absent_when_logged_out(client):
    """登录/注册页不该出现底部导航。"""
    for url in ("/login", "/register"):
        assert NAV_MARK not in client.get(url).text


def test_nav_present_on_user_pages(user, login):
    c = login(user)
    for url in ("/dashboard", "/activate"):
        assert NAV_MARK in c.get(url).text, f"{url} 缺底部导航"


def test_nav_highlights_current_section(user, login, db):
    acc = DouyinAccount(user_id=user.id, label="a", status="active",
                        cookies_exist=True)
    db.add(acc); db.commit(); db.refresh(acc)
    c = login(user)

    # 「火花」tab 在 dashboard 和账号页都应高亮
    for url in ("/dashboard", f"/accounts/{acc.id}"):
        html = c.get(url).text
        nav = html[html.index(NAV_MARK):]
        assert "text-ios-tint" in nav[:1400], f"{url} 未高亮当前 tab"


def test_content_has_bottom_padding(user, login):
    """底部导航是 fixed 的，正文必须留出空间，否则最后一行被挡住。"""
    html = login(user).get("/dashboard").text
    assert "pb-24 md:pb-8" in html


def test_nav_shows_unread_badge(user, login, db):
    from app.models import Notification
    for i in range(3):
        db.add(Notification(user_id=user.id, kind="info", title=f"n{i}"))
    db.commit()

    html = login(user).get("/dashboard").text
    nav = html[html.index(NAV_MARK):]
    assert "bg-ios-red" in nav[:1600], "未读角标没渲染"


def test_notifications_tab_opens_panel(user, login):
    """底部「通知」要能触发顶部那个通知面板。"""
    html = login(user).get("/dashboard").text
    assert "open-notifications" in html
    assert "@open-notifications.window" in html, "通知面板没监听该事件"
