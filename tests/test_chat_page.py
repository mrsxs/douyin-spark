"""聊天页渲染。

首屏必须纯读冷备 —— 和联系人页同一条红线：碰网络就是 5~20 秒白屏。
"""

import pytest

from app import messages_service as ms
from app.db import SessionLocal
from app.models import DouyinAccount, User
from app.security import hash_password


def _seed(account_id, peer="2000000002", n=3, nickname_uid=None):
    with SessionLocal() as db:
        from app import contacts_service as cs
        cs.upsert_cache(db, account_id, [{
            "uid": peer, "nickname": "小明", "conv_id": f"0:1:1:{peer}",
            "days": 30, "status": "active", "avatar": "",
        }])
        ms.sync_messages(db, account_id, [{
            "conv_id": f"0:1:1:{peer}", "peer_uid": peer,
            "server_msg_id": i, "conv_short_id": 1, "msg_type": 7,
            "kind": "text", "sender": peer, "is_me": i % 2 == 0,
            "text": f"消息{i}", "created_at": i * 100,
        } for i in range(1, n + 1)])
        db.commit()


def test_chat_page_renders(active_user, login):
    user, acc = active_user
    _seed(acc.id)
    r = login(user).get(f"/accounts/{acc.id}/chat")
    assert r.status_code == 200, r.text
    assert "小明" in r.text
    assert "消息3" in r.text


def test_chat_page_does_not_touch_douyin(active_user, login, monkeypatch):
    """核心回归：首屏不能走网络。"""
    from app import trigger

    def boom(*a, **k):
        raise AssertionError("聊天页首屏不该调用抖音 API")

    monkeypatch.setattr(trigger, "get_contacts", boom)
    monkeypatch.setattr(trigger, "_ensure_active", boom)
    monkeypatch.setattr(trigger, "poll_new_messages", boom)

    user, acc = active_user
    _seed(acc.id)
    assert login(user).get(f"/accounts/{acc.id}/chat").status_code == 200


def test_chat_page_empty_account(active_user, login):
    """没联系人也要能打开，不能 500。"""
    user, acc = active_user
    r = login(user).get(f"/accounts/{acc.id}/chat")
    assert r.status_code == 200
    assert "还没有联系人" in r.text


def test_chat_page_rejects_foreign_account(active_user, login, db):
    other = User(username="outsider", password_hash=hash_password("pw123456"))
    db.add(other); db.commit(); db.refresh(other)
    acc = DouyinAccount(user_id=other.id, label="别人的", status="active")
    db.add(acc); db.commit(); db.refresh(acc)

    user, _ = active_user
    assert login(user).get(f"/accounts/{acc.id}/chat").status_code == 404


def test_uid_param_selects_conversation(active_user, login):
    user, acc = active_user
    _seed(acc.id, peer="111")
    _seed(acc.id, peer="222")
    r = login(user).get(f"/accounts/{acc.id}/chat?uid=222")
    assert r.status_code == 200
    assert '"222"' in r.text


def test_account_page_links_to_chat(active_user, login):
    user, acc = active_user
    r = login(user).get(f"/accounts/{acc.id}")
    assert f"/accounts/{acc.id}/chat" in r.text, "账号页没有聊天入口"


# ── tojson 的 XSS 转义 ────────────────────────────────────────────
# 联系人昵称和消息正文都来自抖音，是外部可控输入，直接内嵌进 <script>。
# main.py 把 tojson 的 ensure_ascii 关掉了（中文不转义），这里盯住
# 关标签的转义没被一起关掉 —— 否则一个昵称就能注入脚本。

@pytest.mark.parametrize("payload,must_escape", [
    ("</script><script>alert(1)</script>", "\\u003c"),
    ("<img src=x onerror=alert(1)>", "\\u003c"),
    ("'; alert(1); //", "\\u0027"),
    ("a & b", "\\u0026"),
])
def test_hostile_nickname_cannot_break_out_of_script(active_user, login,
                                                     payload, must_escape):
    user, acc = active_user
    with SessionLocal() as db:
        from app import contacts_service as cs
        cs.upsert_cache(db, acc.id, [{
            "uid": "666", "nickname": payload, "conv_id": "0:1:1:666",
            "days": 1, "status": "active", "avatar": "",
        }])
        db.commit()

    body = login(user).get(f"/accounts/{acc.id}/chat").text
    assert payload not in body, "危险字符原样进了页面"
    assert must_escape in body, "tojson 的 HTML 转义没生效"


def test_hostile_message_text_is_escaped(active_user, login):
    user, acc = active_user
    with SessionLocal() as db:
        ms.sync_messages(db, acc.id, [{
            "conv_id": "0:1:1:777", "peer_uid": "777", "server_msg_id": 1,
            "conv_short_id": 1, "msg_type": 7, "kind": "text",
            "sender": "777", "is_me": False,
            "text": "</script><script>alert('xss')</script>",
            "created_at": 100,
        }])
        db.commit()
    body = login(user).get(f"/accounts/{acc.id}/chat?uid=777").text
    assert "</script><script>" not in body


def test_chinese_is_not_unicode_escaped(active_user, login):
    """ensure_ascii=False 生效：中文原样输出，不是 \\uXXXX。"""
    user, acc = active_user
    _seed(acc.id)
    assert "小明" in login(user).get(f"/accounts/{acc.id}/chat").text


# ── 内嵌视频 ─────────────────────────────────────────────────────

def _seed_video(account_id, peer="333", vid="7668476126852136795"):
    with SessionLocal() as db:
        from app import contacts_service as cs
        cs.upsert_cache(db, account_id, [{
            "uid": peer, "nickname": "视频君", "conv_id": f"0:1:1:{peer}",
            "days": 5, "status": "active", "avatar": "",
        }])
        ms.sync_messages(db, account_id, [{
            "conv_id": f"0:1:1:{peer}", "peer_uid": peer, "server_msg_id": 77,
            "conv_short_id": 1, "msg_type": 110, "kind": "share",
            "sender": peer, "is_me": False, "text": "[分享视频]小猫",
            "media": {"kind": "video", "vid": vid,
                      "cover": "https://p26.douyinpic.com/cover.webp"},
            "created_at": 1000,
        }])
        db.commit()


def test_video_media_reaches_the_page(active_user, login):
    user, acc = active_user
    _seed_video(acc.id)
    body = login(user).get(f"/accounts/{acc.id}/chat?uid=333").text
    assert "7668476126852136795" in body, "视频 id 没传到前端，播放不了"
    assert "p26.douyinpic.com/cover.webp" in body, "封面没传到前端"


def test_player_is_native_and_inline(active_user, login):
    """就在气泡里原生播，不再开弹窗、不再用官方 iframe。

    直链走自己的反代：抖音的播放地址带时效签名又认 Referer，
    塞进页面既会过期又等于把只有登录态才看得到的内容散出去。
    """
    user, acc = active_user
    _seed_video(acc.id)
    body = login(user).get(f"/accounts/{acc.id}/chat?uid=333").text

    assert "<video" in body
    assert f"/api/videos/{acc.id}/" in body
    assert "open.douyin.com/player/video" not in body, "官方 iframe 应该已经退场"
    assert "douyinvod.com" not in body, "直链绝不能进页面"


def test_video_poster_has_a_fallback(active_user, login):
    """<video poster> 没有 error 事件，签名封面过期就只剩一块死黑。

    图片走 onCoverError 回退到去签名版，视频这条路得自己探。
    """
    user, acc = active_user
    _seed_video(acc.id)
    body = login(user).get(f"/accounts/{acc.id}/chat?uid=333").text

    assert "probeVideoPoster" in body
    assert "cover_alt" in body, "回退地址没传到前端，探了也没得换"


def test_video_is_not_preloaded(active_user, login):
    """preload=none：不点就一个字节都不拉。

    一屏几十条视频，预加载能把流量和抖音那边的请求量一起打爆。
    """
    user, acc = active_user
    _seed_video(acc.id)
    body = login(user).get(f"/accounts/{acc.id}/chat?uid=333").text
    assert 'preload="none"' in body


def test_cover_url_is_escaped_in_page(active_user, login):
    """封面 URL 来自抖音，直接进 <img src>，不能带出脚本。"""
    user, acc = active_user
    with SessionLocal() as db:
        ms.sync_messages(db, acc.id, [{
            "conv_id": "0:1:1:444", "peer_uid": "444", "server_msg_id": 88,
            "conv_short_id": 1, "msg_type": 110, "kind": "share",
            "sender": "444", "is_me": False, "text": "x",
            "media": {"kind": "video", "vid": "1",
                      "cover": "https://x/a.png\"></script><script>alert(1)</script>"},
            "created_at": 1,
        }])
        db.commit()
    body = login(user).get(f"/accounts/{acc.id}/chat?uid=444").text
    assert "</script><script>" not in body
