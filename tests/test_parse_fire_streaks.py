"""parse_fire_streaks 分类 + 头像解析单元测试（用合成 protobuf 字节，无外部依赖）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import douyin_im as dy  # noqa: E402


def _consecutive(days: int) -> bytes:
    # 匹配 r'a:consecutive_chat\x12.{1,4}?(\d{1,4}):'
    return b"a:consecutive_chat\x12\x01" + str(days).encode() + b":"


def _rekindled(days: int) -> bytes:
    return b"a:rekindled_chat\x12\x01" + str(days).encode() + b":"


def _chat_data(expire_ts: int) -> bytes:
    blob = ('{"expire_time": %d}' % expire_ts).encode()
    return b"a:consecutive_chat_data\x12" + bytes([len(blob)]) + blob


def _build_blob() -> bytes:
    """三个会话：active(续) / expired(需重燃) / rekindled-only(需重燃)。"""
    sep = b"\x00" * 16
    future = 9999999999
    past = 1000000000
    return (
        b"0:1:999:111" + sep + _consecutive(5) + sep + _chat_data(future) + sep
        + b"0:1:999:222" + sep + _consecutive(8) + sep + _chat_data(past) + sep
        + b"0:1:999:333" + sep + _rekindled(3) + sep
    )


def test_classifies_active_and_broken(monkeypatch):
    monkeypatch.setattr(dy, "_log", lambda *a, **k: None)
    contacts = dy.parse_fire_streaks(_build_blob())
    by_uid = {c["uid"]: c for c in contacts}

    assert set(by_uid) == {"111", "222", "333"}
    assert by_uid["111"]["status"] == "active"   # 火花在燃烧
    assert by_uid["222"]["status"] == "broken"   # expire_time 过期 → 需重燃
    assert by_uid["333"]["status"] == "broken"   # 仅 rekindled → 需重燃
    assert by_uid["111"]["days"] == 5
    assert by_uid["222"]["days"] == 8
    assert by_uid["333"]["days"] == 3


def test_active_sorted_before_broken(monkeypatch):
    monkeypatch.setattr(dy, "_log", lambda *a, **k: None)
    contacts = dy.parse_fire_streaks(_build_blob())
    statuses = [c["status"] for c in contacts]
    # active 全部排在 broken 之前
    assert statuses == sorted(statuses, key=lambda s: s != "active")
    assert contacts[0]["uid"] == "111"


def test_every_contact_has_avatar_field(monkeypatch):
    monkeypatch.setattr(dy, "_log", lambda *a, **k: None)
    for c in dy.parse_fire_streaks(_build_blob()):
        assert "avatar" in c
        assert isinstance(c["avatar"], str)


def test_empty_when_no_streaks():
    assert dy.parse_fire_streaks(b"no streaks here") == []


# ── 窗口越界：没火花的会话偷走别人的天数 ──────────────────

def test_没有火花的会话不会认领下一个会话的天数(monkeypatch):
    """线上真实现象：「甜豆包」「Momo」根本没有火花，
    却显示成 510 天和 353 天 —— 正好等于紧随其后的「周大明」「吴天成」。

    根因是搜索窗口只有下界（conv_pos < p < conv_pos + 60000），
    没有上界，于是前一个会话把后一个会话的 consecutive_chat 认领走了。
    """
    monkeypatch.setattr(dy, "_log", lambda *a, **k: None)
    sep = b"\x00" * 16
    future = 9999999999
    blob = (
        # 没有任何火花标记的会话，紧跟着一个 510 天的
        b"0:1:999:111" + sep
        + b"0:1:999:222" + sep + _consecutive(510) + sep + _chat_data(future) + sep
    )
    by_uid = {c["uid"]: c for c in dy.parse_fire_streaks(blob)}

    assert "111" not in by_uid, \
        f"没火花的会话被算出了 {by_uid.get('111', {}).get('days')} 天"
    assert by_uid["222"]["days"] == 510


def test_相邻会话的天数不会串(monkeypatch):
    """两个都有火花时，各拿各的，不能都取到第一个。"""
    monkeypatch.setattr(dy, "_log", lambda *a, **k: None)
    sep = b"\x00" * 16
    future = 9999999999
    blob = (
        b"0:1:999:111" + sep + _consecutive(100) + sep + _chat_data(future) + sep
        + b"0:1:999:222" + sep + _consecutive(200) + sep + _chat_data(future) + sep
        + b"0:1:999:333" + sep + _consecutive(300) + sep + _chat_data(future) + sep
    )
    by_uid = {c["uid"]: c for c in dy.parse_fire_streaks(blob)}
    assert by_uid["111"]["days"] == 100
    assert by_uid["222"]["days"] == 200
    assert by_uid["333"]["days"] == 300


def test_同一会话id重复出现不影响取数(monkeypatch):
    """conv_id 在数据块里会重复出现，不能因此把窗口截成 0 长度。"""
    monkeypatch.setattr(dy, "_log", lambda *a, **k: None)
    sep = b"\x00" * 16
    future = 9999999999
    blob = (
        b"0:1:999:111" + sep + b"0:1:999:111" + sep
        + _consecutive(77) + sep + _chat_data(future) + sep
        + b"0:1:999:222" + sep + _consecutive(88) + sep + _chat_data(future) + sep
    )
    by_uid = {c["uid"]: c for c in dy.parse_fire_streaks(blob)}
    assert by_uid["111"]["days"] == 77
    assert by_uid["222"]["days"] == 88


def test_过期判定也不会串到下一个会话(monkeypatch):
    """expire_time 的取值窗口有同样的问题：
    前一个会话会读到后一个会话的过期时间，把 active 误判成 broken。"""
    monkeypatch.setattr(dy, "_log", lambda *a, **k: None)
    sep = b"\x00" * 16
    blob = (
        b"0:1:999:111" + sep + _consecutive(10) + sep + _chat_data(9999999999) + sep
        + b"0:1:999:222" + sep + _consecutive(20) + sep + _chat_data(1000000000) + sep
    )
    by_uid = {c["uid"]: c for c in dy.parse_fire_streaks(blob)}
    assert by_uid["111"]["status"] == "active"   # 不该被 BBB 的过期时间影响
    assert by_uid["222"]["status"] == "broken"
