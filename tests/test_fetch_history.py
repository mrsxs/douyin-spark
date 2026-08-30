"""从抖音云端拉历史消息（imapi cmd=301 / get_by_conversation）。

Why: get_message_by_init 每个会话只回最近 ~21 条，两次同步之间聊得多一点，
中间就是永久空档 —— 真实库里出现过 9 天的窟窿。cmd=301 带 cursor + limit，
能把整个会话的历史翻完，已经丢的也追得回来。

协议是 2026-08-29 从抖音网页版抓包确认的，见 obsidian-vault/60-抖音协议与签名。
请求体 (cmd=301)：1=conversation_id, 2=conversation_type, 3=conversation_short_id,
4=direction, 5=cursor(微秒), 6=limit；响应 2=next_cursor, 3=has_more。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import douyin_im as dy  # noqa: E402


# ── 请求体构造 ───────────────────────────────────────────────────

def _fake_init_req() -> bytes:
    """仿 init_req.bin：外层环境字段 + cmd=1 + field 8 的 body。

    真实文件里还有几十个字段，这里保留结构关键的那几个就够验证
    「外层原样复用、只换 cmd 和 body」这条契约。
    """
    return (
        dy._pb_v(1, 1)                       # cmd（要被换成 301）
        + dy._pb_v(2, 10008)                 # sequence_id
        + dy._pb_b(3, "0.1.8")
        + dy._pb_b(7, "0d50935:feat/pc-im-group")
        + dy._pb_b(8, b"\x01\x02\x03")       # 原 body（要被整个换掉）
        + dy._pb_b(11, "douyin_pc")
        + dy._pb_b(15, dy._pb_b(1, "session_aid") + dy._pb_b(2, "6383"))
        + dy._pb_v(18, 1)
        + dy._pb_b(21, "douyin_web")
        + dy._pb_b(22, "web_sdk")
    )


def _build(**kw):
    return dy.build_history_body(_fake_init_req(), **kw)


BASE = dict(conv_id="0:1:10000000001:20000000002",
            conv_short_id=7610461610200629770,
            cursor=1672502418040000, limit=50)


def test_cmd_is_301():
    """cmd 必须换成 301，否则打到的还是 init 那条老路。"""
    assert dy._pb_flat(_build(**BASE))[1] == 301


def test_request_body_fields():
    top = dy._pb_flat(_build(**BASE))
    req = dy._pb_flat(dy._pb_flat(top[8])[301])

    assert req[1] == b"0:1:10000000001:20000000002"   # conversation_id
    assert req[2] == 1                                  # conversation_type（conv_id 第二段）
    assert req[3] == 7610461610200629770                # conversation_short_id
    assert req[4] == 1                                  # direction
    assert req[5] == 1672502418040000                   # cursor
    assert req[6] == 50                                 # limit


def test_conversation_type_comes_from_conv_id():
    """conv_id 形如 0:<type>:<a>:<b>，第二段就是会话类型，不能写死。"""
    body = _build(**{**BASE, "conv_id": "0:2:1:2"})
    req = dy._pb_flat(dy._pb_flat(dy._pb_flat(body)[8])[301])
    assert req[2] == 2


def test_environment_fields_are_preserved():
    """外层是账户自己的设备环境（device_id/webid 等），必须原样保留 ——
    换成别人的等于串号。"""
    top = dy._pb_flat(_build(**BASE))
    assert top[3] == b"0.1.8"
    assert top[7] == b"0d50935:feat/pc-im-group"
    assert top[11] == b"douyin_pc"
    assert top[21] == b"douyin_web"
    assert top[22] == b"web_sdk"
    assert top[18] == 1


def test_original_body_is_replaced_not_appended():
    """field 8 要整个换掉。留着旧的会变成 repeated，抖音按第一个解。"""
    top = dy._pb_flat(_build(**BASE))
    assert not isinstance(top[8], list), "field 8 出现了两份"
    assert top[8] != b"\x01\x02\x03"


def test_no_request_sign_needed():
    """拉历史不带 field 23/24/25 —— 抓包确认这条路不需要 RSA 签名，
    别顺手加上，那会引入一次没必要的私钥依赖。"""
    top = dy._pb_flat(_build(**BASE))
    for f in (23, 24, 25):
        assert f not in top


def test_limit_is_clamped():
    """limit 不能无上限，抖音一页就给 50 左右，要太多只会被截或报错。"""
    req = dy._pb_flat(dy._pb_flat(dy._pb_flat(_build(**{**BASE, "limit": 9999}))[8])[301])
    assert req[6] <= 100


# ── 响应解析 ─────────────────────────────────────────────────────

def _fake_response(msgs_blob: bytes, next_cursor: int, has_more: int) -> bytes:
    """仿 cmd=301 响应：外层 f6 → 内层 f301 → {1:消息, 2:cursor, 3:has_more}"""
    inner = msgs_blob + dy._pb_v(2, next_cursor) + dy._pb_v(3, has_more)
    return (dy._pb_v(1, 301) + dy._pb_b(4, "OK")
            + dy._pb_b(6, dy._pb_b(301, inner)))


def test_parses_cursor_and_has_more():
    resp = _fake_response(b"", 1672502430520000, 1)
    cursor, has_more = dy.parse_history_cursor(resp)
    assert cursor == 1672502430520000
    assert has_more is True


def test_has_more_false_ends_paging():
    cursor, has_more = dy.parse_history_cursor(_fake_response(b"", 123, 0))
    assert has_more is False


def test_missing_fields_stop_paging_safely():
    """字段缺失（抖音改版/异常响应）时要当成「没有下一页」，
    不能返回 has_more=True 让调用方无限翻。"""
    cursor, has_more = dy.parse_history_cursor(b"garbage")
    assert has_more is False
    assert cursor == 0


# ── 翻页拉取 ─────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status


class _FakeSession:
    """按预设脚本逐页返回，并记录每次请求的 cursor。"""

    def __init__(self, pages):
        self.pages, self.calls, self.cursors = list(pages), 0, []

    def post(self, url, data=None, headers=None, timeout=None, **kw):
        self.calls += 1
        req = dy._pb_flat(dy._pb_flat(dy._pb_flat(data)[8])[301])
        self.cursors.append(req[5])
        return _FakeResp(self.pages.pop(0) if self.pages else b"")


def _page(n_msgs: int, next_cursor: int, has_more: int) -> bytes:
    blob = b"".join(dy._pb_b(1, dy._pb_b(1, f"m{i}")) for i in range(n_msgs))
    inner = blob + dy._pb_v(2, next_cursor) + dy._pb_v(3, has_more)
    return (dy._pb_v(1, 301) + dy._pb_b(4, "OK")
            + dy._pb_b(6, dy._pb_b(301, inner)))


def _fetch(session, **kw):
    kw.setdefault("sleep_between", 0)      # 测试里不真睡
    return dy.fetch_history(session, _fake_init_req(),
                            conv_id="0:1:1:2", conv_short_id=123, **kw)


def test_follows_cursor_across_pages(monkeypatch):
    monkeypatch.setattr(dy, "parse_messages", lambda b, my_uid="": [{"x": 1}] * 3)
    s = _FakeSession([_page(3, 200, 1), _page(3, 300, 1), _page(3, 400, 0)])

    out = _fetch(s, start_cursor=100)

    assert s.calls == 3
    assert s.cursors == [100, 200, 300], "没有把 next_cursor 带到下一页"
    assert len(out) == 9


def test_stops_on_has_more_false(monkeypatch):
    monkeypatch.setattr(dy, "parse_messages", lambda b, my_uid="": [{"x": 1}])
    s = _FakeSession([_page(1, 200, 0), _page(1, 300, 1)])
    _fetch(s, start_cursor=100)
    assert s.calls == 1, "has_more=0 之后还在翻"


def test_max_pages_is_a_hard_stop(monkeypatch):
    """抖音一直说 has_more 也得停 —— 防止死循环把接口打爆。"""
    monkeypatch.setattr(dy, "parse_messages", lambda b, my_uid="": [{"x": 1}])
    # cursor 必须递增，否则会先撞上「游标不前进」那道刹车
    s = _FakeSession([_page(1, 100 + i * 10, 1) for i in range(200)])
    _fetch(s, start_cursor=1, max_pages=5)
    assert s.calls == 5


def test_stops_when_cursor_does_not_advance(monkeypatch):
    """游标原地不动 = 抖音在重复发同一页，再翻就是无限循环。"""
    monkeypatch.setattr(dy, "parse_messages", lambda b, my_uid="": [{"x": 1}])
    s = _FakeSession([_page(1, 100, 1) for _ in range(10)])
    _fetch(s, start_cursor=100, max_pages=10)
    assert s.calls <= 2, f"游标不前进却翻了 {s.calls} 页"


def test_http_error_stops_without_raising(monkeypatch):
    monkeypatch.setattr(dy, "parse_messages", lambda b, my_uid="": [{"x": 1}])

    class _Bad(_FakeSession):
        def post(self, *a, **kw):
            return _FakeResp(b"", status=500)

    assert _fetch(_Bad([]), start_cursor=1) == []


def test_empty_page_stops(monkeypatch):
    """解析不出消息就别再翻了，多半是响应结构变了。"""
    monkeypatch.setattr(dy, "parse_messages", lambda b, my_uid="": [])
    s = _FakeSession([_page(0, 200, 1), _page(1, 300, 1)])
    _fetch(s, start_cursor=100)
    assert s.calls == 1


# ── init 请求的会话数上限（cmd=2043 field 3）────────────────────────

def _init_req_bytes() -> bytes:
    """仿真实 init_req.bin：外层环境字段 + field8={2043:{field2:0}}。"""
    return (
        dy._pb_v(1, 2043)
        + dy._pb_v(2, 10001)
        + dy._pb_b(3, "0.1.8")
        + dy._pb_b(8, dy._pb_b(2043, dy._pb_v(2, 0)))
        + dy._pb_b(11, "douyin_pc")
        + dy._pb_b(15, dy._pb_b(1, "session_aid") + dy._pb_b(2, "6383"))
        + dy._pb_b(21, "douyin_web")
    )


def _init_body_fields(limit=None):
    raw = (dy.build_init_body(_init_req_bytes(), limit=limit)
           if limit is not None else dy.build_init_body(_init_req_bytes()))
    return dy._pb_flat(raw), dy._pb_flat(dy._pb_flat(dy._pb_flat(raw)[8])[2043])


def test_init_body_sets_conversation_limit():
    """核心：不传 field 3 时抖音只回 ~25 个会话，2.47MB 就截断了 ——
    真实账号 37 个会话里，「王女士 906 天」「顺风吖 905 天」被截在外面。
    传 200 就能拿全（实测 200 和 500 结果一致）。
    """
    top, body = _init_body_fields()
    assert body[3] == dy.INIT_CONV_LIMIT
    assert body[2] == 0, "全量标志被弄丢了"


def test_init_body_keeps_cmd_2043():
    top, _ = _init_body_fields()
    assert top[1] == 2043


def test_init_body_preserves_environment():
    """外层是账户自己的设备环境，必须原样保留 —— 换成别人的等于串号。"""
    top, _ = _init_body_fields()
    assert top[3] == b"0.1.8"
    assert top[11] == b"douyin_pc"
    assert top[21] == b"douyin_web"


def test_init_body_custom_limit():
    _, body = _init_body_fields(limit=50)
    assert body[3] == 50


def test_init_body_replaces_not_appends():
    """field 8 要整个换掉，留着旧的会变成 repeated。"""
    top, _ = _init_body_fields()
    assert not isinstance(top[8], list)
