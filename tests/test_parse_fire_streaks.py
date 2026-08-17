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
