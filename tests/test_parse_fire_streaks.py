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


# ── include_all：把从来没有火花的会话也带出来 ──────────────

def _build_blob_with_bare() -> bytes:
    """在三个有火花的会话之外，多一个完全没有火花标记的裸会话 444。"""
    sep = b"\x00" * 16
    return _build_blob() + b"0:1:999:444" + sep


def test_默认不返回无火花会话(monkeypatch):
    """回归护栏：默认参数必须逐字节保持老行为，老用户升级后火花人数不能变。"""
    monkeypatch.setattr(dy, "_log", lambda *a, **k: None)
    uids = {c["uid"] for c in dy.parse_fire_streaks(_build_blob_with_bare())}
    assert uids == {"111", "222", "333"}


def test_include_all_带出无火花会话(monkeypatch):
    monkeypatch.setattr(dy, "_log", lambda *a, **k: None)
    by_uid = {c["uid"]: c
              for c in dy.parse_fire_streaks(_build_blob_with_bare(), include_all=True)}

    assert set(by_uid) == {"111", "222", "333", "444"}
    assert by_uid["444"]["status"] == "none"
    assert by_uid["444"]["days"] == 0
    # 有火花的那三个分类不能因为开了 include_all 就变
    assert by_uid["111"]["status"] == "active"
    assert by_uid["222"]["status"] == "broken"
    assert by_uid["333"]["status"] == "broken"
    assert by_uid["111"]["days"] == 5


def test_include_all_排序为_active_broken_none(monkeypatch):
    monkeypatch.setattr(dy, "_log", lambda *a, **k: None)
    contacts = dy.parse_fire_streaks(_build_blob_with_bare(), include_all=True)
    rank = {"active": 0, "broken": 1, "none": 2}
    order = [rank[c["status"]] for c in contacts]
    assert order == sorted(order)
    assert contacts[0]["uid"] == "111"
    assert contacts[-1]["uid"] == "444"


def test_include_all_全裸会话也能出结果(monkeypatch):
    """一个火花都没有时，老路径早退返回 []；include_all 不能跟着早退。"""
    monkeypatch.setattr(dy, "_log", lambda *a, **k: None)
    sep = b"\x00" * 16
    blob = b"0:1:999:111" + sep + b"0:1:999:222" + sep
    assert dy.parse_fire_streaks(blob) == []
    got = dy.parse_fire_streaks(blob, include_all=True)
    assert {c["uid"] for c in got} == {"111", "222"}
    assert all(c["days"] == 0 and c["status"] == "none" for c in got)


def test_include_all_不改变无火花会话的窗口隔离(monkeypatch):
    """开了 include_all 后，裸会话依然不能认领下一个会话的天数。"""
    monkeypatch.setattr(dy, "_log", lambda *a, **k: None)
    sep = b"\x00" * 16
    blob = (
        b"0:1:999:111" + sep
        + b"0:1:999:222" + sep + _consecutive(510) + sep + _chat_data(9999999999) + sep
    )
    by_uid = {c["uid"]: c for c in dy.parse_fire_streaks(blob, include_all=True)}
    assert by_uid["111"]["days"] == 0
    assert by_uid["111"]["status"] == "none"
    assert by_uid["222"]["days"] == 510
    assert by_uid["222"]["status"] == "active"


def test_include_all_每条都带完整字段(monkeypatch):
    monkeypatch.setattr(dy, "_log", lambda *a, **k: None)
    for c in dy.parse_fire_streaks(_build_blob_with_bare(), include_all=True):
        for k in ("conv_id", "uid", "sec_uid", "days", "nickname",
                  "avatar", "status", "conversation_short_id", "ticket"):
            assert k in c, f"{c['uid']} 缺字段 {k}"


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
