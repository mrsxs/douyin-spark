"""从 get_message_by_init 响应里解析聊天消息。

Why 不新增接口：init 响应（1.6MB）里本来就带着每个会话最近 ~21 条消息，
而 trigger._ensure_active 每次拉联系人都已经拿到这份响应。
多打一个「收消息」接口等于凭空给抖音多一次请求 —— 风控风险白送。

字段号来自对真实响应的实测（不是猜的）：
  f1=conversation_id  f3=server_message_id  f5=conversation_short_id
  f6=message_type     f7=sender(uid)        f8=content(JSON)
  f10=create_time(ms)

f3/f5 一度搞反过：f5 是会话级的，拿它当消息主键会把一个会话里的 21 条消息
去重成 1 条（真实响应 342 条被压成 32 条）。语义由现有的 _parse_conv_info
交叉验证过。

测试夹具全部用合成 protobuf 构造 —— 真实响应含真实 uid / 昵称 / 聊天内容，
按安全红线绝不能进 git。
"""
import json

import pytest

import douyin_im as dy


MY_UID = "1000000001"
PEER = "2000000002"
CONV = f"0:1:{MY_UID}:{PEER}"


def _msg(conv_id=CONV, msg_id=111, mtype=7, sender=MY_UID,
         content=None, created_ms=1778517748571, short_id=999):
    """按实测字段号拼一条 Message。"""
    if content is None:
        content = {"mention_users": [], "aweType": 700,
                   "richTextInfos": [], "text": "你好"}
    body = (
        dy._pb_b(1, conv_id)
        + dy._pb_v(2, 1)
        + dy._pb_v(3, msg_id)        # server_message_id（每条唯一）
        + dy._pb_v(5, short_id)      # conversation_short_id（同会话相同）
        + dy._pb_v(6, mtype)
        + dy._pb_v(7, int(sender))
        + dy._pb_b(8, json.dumps(content, ensure_ascii=False))
        + dy._pb_v(10, created_ms)
    )
    return body


def _resp(*messages):
    """把消息包成 init 响应的真实嵌套：Response.6 → body.2043 → conv.1 → msg.2"""
    conv = b"".join(dy._pb_b(2, m) for m in messages)
    return dy._pb_b(6, dy._pb_b(2043, dy._pb_b(1, conv)))


# ── 基本解析 ─────────────────────────────────────────────────────

def test_extracts_text_message():
    out = dy.parse_messages(_resp(_msg()), my_uid=MY_UID)
    assert len(out) == 1
    m = out[0]
    assert m["text"] == "你好"
    assert m["conv_id"] == CONV
    assert m["server_msg_id"] == 111
    assert m["msg_type"] == 7
    assert m["kind"] == "text"


def test_direction_is_detected():
    """核心：靠 f7(sender) 和自己的 uid 比对判断收发方向。"""
    out = dy.parse_messages(
        _resp(_msg(msg_id=1, sender=MY_UID),
              _msg(msg_id=2, sender=PEER)),
        my_uid=MY_UID)
    by_id = {m["server_msg_id"]: m for m in out}
    assert by_id[1]["is_me"] is True
    assert by_id[2]["is_me"] is False


def test_peer_uid_derived_from_conv_id():
    """conv_id 形如 0:1:me:peer —— 对方是另一半，用来归到联系人。"""
    out = dy.parse_messages(_resp(_msg()), my_uid=MY_UID)
    assert out[0]["peer_uid"] == PEER


def test_peer_uid_when_my_uid_is_second_half():
    conv = f"0:1:{PEER}:{MY_UID}"
    out = dy.parse_messages(_resp(_msg(conv_id=conv)), my_uid=MY_UID)
    assert out[0]["peer_uid"] == PEER


def test_created_at_is_parsed():
    out = dy.parse_messages(_resp(_msg(created_ms=1778517748571)), my_uid=MY_UID)
    assert out[0]["created_at"] == 1778517748571


def test_sorted_by_time():
    out = dy.parse_messages(
        _resp(_msg(msg_id=3, created_ms=300),
              _msg(msg_id=1, created_ms=100),
              _msg(msg_id=2, created_ms=200)),
        my_uid=MY_UID)
    assert [m["server_msg_id"] for m in out] == [1, 2, 3]


def test_multiple_conversations_kept_separate():
    other = "0:1:1000000001:3000000003"
    out = dy.parse_messages(
        _resp(_msg(msg_id=1), _msg(conv_id=other, msg_id=2)),
        my_uid=MY_UID)
    assert {m["peer_uid"] for m in out} == {PEER, "3000000003"}


def test_duplicate_message_ids_collapsed():
    """同一条消息可能在响应里出现两次（会话摘要 + 消息列表）。"""
    out = dy.parse_messages(_resp(_msg(msg_id=7), _msg(msg_id=7)), my_uid=MY_UID)
    assert len(out) == 1


def test_whole_conversation_is_kept():
    """核心回归：同一会话里的多条消息不能被去重掉。

    去重键一度用了 f5（conversation_short_id）——它在整个会话里是同一个值，
    结果真实响应的 342 条消息被压成 32 条（正好等于会话数）。
    这里刻意让所有消息共用 short_id、只有 msg_id 不同。
    """
    msgs = [_msg(msg_id=i, short_id=42, created_ms=1000 + i) for i in range(1, 22)]
    out = dy.parse_messages(_resp(*msgs), my_uid=MY_UID)
    assert len(out) == 21, f"同会话消息被去重掉了，只剩 {len(out)} 条"
    assert len({m["server_msg_id"] for m in out}) == 21


def test_conv_short_id_is_read_from_its_own_field():
    """server_msg_id 与 conv_short_id 不能串位。"""
    out = dy.parse_messages(
        _resp(_msg(msg_id=777, short_id=42)), my_uid=MY_UID)
    assert out[0]["server_msg_id"] == 777
    assert out[0]["conv_short_id"] == 42


# ── 非文本消息要有可读摘要 ────────────────────────────────────────

@pytest.mark.parametrize("mtype,content,kind,text", [
    (27, {"aweType": 2702, "cover_width": 750}, "image", "[图片]"),
    (17, {"resource_url": {"uri": "x.mpeg"}}, "audio", "[语音]"),
    (109, {"aweType": 10900, "tkey": "x"}, "audio", "[语音]"),
    (5, {"aweType": 501, "emoji_from": "favorite_emoji"}, "emoji", "[表情]"),
])
def test_non_text_kinds_get_readable_placeholder(mtype, content, kind, text):
    out = dy.parse_messages(_resp(_msg(mtype=mtype, content=content)), my_uid=MY_UID)
    assert out[0]["kind"] == kind
    assert out[0]["text"] == text


# ── 语音带时长 ───────────────────────────────────────────────────
# 只显示「[语音]」等于没解析：一条 3 秒和一条 60 秒的语音看起来一模一样。

def test_audio_shows_duration():
    out = dy.parse_messages(_resp(_msg(mtype=17, content={
        "resource_url": {"uri": "x.mpeg"}, "duration": 3900,
        "md5": "abc"})), my_uid=MY_UID)
    assert out[0]["kind"] == "audio"
    assert "3.9" in out[0]["text"], f"没显示时长: {out[0]['text']}"


def test_audio_without_duration_falls_back():
    out = dy.parse_messages(_resp(_msg(mtype=17, content={"md5": "x"})),
                            my_uid=MY_UID)
    assert out[0]["text"] == "[语音]"


@pytest.mark.parametrize("bad", [0, -1, "abc", None])
def test_audio_bad_duration_does_not_break(bad):
    out = dy.parse_messages(
        _resp(_msg(mtype=17, content={"duration": bad})), my_uid=MY_UID)
    assert out[0]["text"] == "[语音]"


# ── 分享/视频要吐出标题 ──────────────────────────────────────────
# 实测：type 105 有 aweme_title + comment，8/77 有 content_title，
# 之前只看 description/content_name，这些全被丢成了裸「[分享]」。

def test_video_comment_share_uses_title_and_comment():
    out = dy.parse_messages(_resp(_msg(mtype=105, content={
        "aweme_title": "一起祝贺嘉豪和嘉欣呀",
        "comment": "去参加这个婚礼的人才是真正意义上的嘉宾",
    })), my_uid=MY_UID)
    text = out[0]["text"]
    assert "一起祝贺嘉豪和嘉欣呀" in text
    assert "去参加这个婚礼" in text


def test_share_uses_content_title():
    out = dy.parse_messages(_resp(_msg(mtype=77, content={
        "content_title": "标题在这", "content_name": "作者名"})), my_uid=MY_UID)
    assert "标题在这" in out[0]["text"]


def test_bare_share_video_keeps_its_label():
    """type 110 的 description 只有「[分享视频]」，也比裸「[分享]」强。"""
    out = dy.parse_messages(_resp(_msg(mtype=110, content={
        "description": "[分享视频]", "item_id": "123"})), my_uid=MY_UID)
    assert out[0]["text"] == "[分享视频]"


def test_share_with_nothing_useful():
    out = dy.parse_messages(_resp(_msg(mtype=8, content={"itemId": "1"})),
                            my_uid=MY_UID)
    assert out[0]["text"] == "[分享]"


def test_share_fields_of_wrong_type_do_not_crash():
    """抖音偶尔把这些字段给成 dict/list，取 .strip() 会炸。"""
    out = dy.parse_messages(_resp(_msg(mtype=8, content={
        "content_name": {"nested": 1}, "description": ["a"],
        "comment": 123})), my_uid=MY_UID)
    assert len(out) == 1
    assert out[0]["text"]


def test_very_long_share_is_truncated():
    """抖音视频文案能有上千字（整篇小作文），原样塞进气泡会把整屏顶满。"""
    out = dy.parse_messages(_resp(_msg(mtype=110, content={
        "description": "长" * 3000})), my_uid=MY_UID)
    text = out[0]["text"]
    assert len(text) <= dy._SHARE_MAX + 1
    assert text.endswith("…")


def test_share_newlines_are_flattened():
    """多行文案会把气泡撑成一大片，压成一行。"""
    out = dy.parse_messages(_resp(_msg(mtype=110, content={
        "description": "第一行\n\n第二行\n第三行"})), my_uid=MY_UID)
    assert out[0]["text"] == "第一行 第二行 第三行"


def test_system_tip_uses_its_own_text():
    out = dy.parse_messages(
        _resp(_msg(mtype=1, content={"tips": "你们已互相关注对方", "aweType": 0})),
        my_uid=MY_UID)
    assert out[0]["kind"] == "system"
    assert out[0]["text"] == "你们已互相关注对方"


def test_share_uses_content_name():
    out = dy.parse_messages(
        _resp(_msg(mtype=8, content={"content_name": "冰箱哥生活号", "aweType": 800})),
        my_uid=MY_UID)
    assert out[0]["kind"] == "share"
    assert "冰箱哥生活号" in out[0]["text"]


def test_share_video_uses_description():
    out = dy.parse_messages(
        _resp(_msg(mtype=110, content={"description": "[分享视频]豆豆：多次挑衅饲养员"})),
        my_uid=MY_UID)
    assert "豆豆" in out[0]["text"]


def test_unknown_type_still_returned_with_placeholder():
    """新消息类型不能让整个会话解析失败 —— 未知也要占个位。"""
    out = dy.parse_messages(_resp(_msg(mtype=40001, content={})), my_uid=MY_UID)
    assert len(out) == 1
    assert out[0]["text"]


# ── 健壮性：解析绝不能抛 ──────────────────────────────────────────

def test_empty_bytes():
    assert dy.parse_messages(b"", my_uid=MY_UID) == []


def test_garbage_bytes_do_not_raise():
    assert dy.parse_messages(b"\xff\xfe\x00\x01garbage" * 50, my_uid=MY_UID) == []


def test_truncated_response_does_not_raise():
    full = _resp(_msg())
    for cut in range(1, len(full)):
        dy.parse_messages(full[:cut], my_uid=MY_UID)


def test_malformed_content_json_falls_back():
    """content 不是合法 JSON 时不能整条丢掉。"""
    body = (dy._pb_b(1, CONV) + dy._pb_v(5, 1) + dy._pb_v(6, 7)
            + dy._pb_v(7, int(MY_UID)) + dy._pb_b(8, "{not json")
            + dy._pb_v(10, 1))
    out = dy.parse_messages(_resp(body), my_uid=MY_UID)
    assert len(out) == 1


def test_missing_my_uid_still_parses():
    """拿不到 self uid 时消息照样要能读，只是方向未知。"""
    out = dy.parse_messages(_resp(_msg()), my_uid="")
    assert len(out) == 1
    assert out[0]["is_me"] is False


def test_group_conversation_ignored():
    """群会话 conv_id 不是 0:1:a:b 结构，不该混进单聊。"""
    out = dy.parse_messages(_resp(_msg(conv_id="1:group:abc")), my_uid=MY_UID)
    assert out == []


# ── 内嵌媒体 ─────────────────────────────────────────────────────
# 分享的视频要能在聊天里直接看到封面并播放，而不是一行干巴巴的标题。
# 封面必须保留签名：实测 14 条封面里，签名原样 14/14 可用，
# 去签名后只剩 6/14（tos-cn-i-dy、*c001 这些桶必须带签名）。
# 头像可以去签名，封面不行 —— 两者规则不同，别想当然套用。
# 签名有 x-expires，所以另存一份去签名的 cover_alt 作过期兜底。

_COVER = ("https://p26-sign.douyinpic.com/tos-cn-p-0015/o4Z1EE"
          "~tplv-dy-resize.webp?lk3s=138a59ce&x-expires=1786687200&x-signature=abc")


def test_video_share_exposes_media():
    out = dy.parse_messages(_resp(_msg(mtype=110, content={
        "description": "[分享视频]小猫",
        "item_id": "7668476126852136795",
        "cover_url": {"url_list": [_COVER]},
    })), my_uid=MY_UID)
    media = out[0]["media"]
    assert media["kind"] == "video"
    assert media["vid"] == "7668476126852136795"
    assert media["cover"] == _COVER, "封面被去了签名，实测会有一半 403"
    assert media["cover_alt"].startswith("https://p26.douyinpic.com/"), \
        "缺少去签名兜底，签名过期后封面就彻底没了"
    assert "x-expires" not in media["cover_alt"]


def test_video_share_reads_item_id_from_either_key():
    """110 用 item_id，8/77/105 用 itemId。"""
    for key in ("item_id", "itemId"):
        out = dy.parse_messages(_resp(_msg(mtype=8, content={
            key: "123", "cover_url": {"url_list": [_COVER]}})), my_uid=MY_UID)
        assert out[0]["media"]["vid"] == "123", f"{key} 没读到"


def test_video_share_falls_back_to_aweme_info():
    """110 有时把 item_id/cover 藏在 aweme_info 里。"""
    out = dy.parse_messages(_resp(_msg(mtype=110, content={
        "description": "[分享视频]",
        "aweme_info": {"item_id": "999", "cover_url": {"url_list": [_COVER]}},
    })), my_uid=MY_UID)
    assert out[0]["media"]["vid"] == "999"
    assert out[0]["media"]["cover"]


def test_image_message_exposes_media():
    out = dy.parse_messages(_resp(_msg(mtype=27, content={
        "resource_url": {"large_url_list": [_COVER]},
        "cover_width": 750, "cover_height": 1494,
    })), my_uid=MY_UID)
    media = out[0]["media"]
    assert media["kind"] == "image"
    assert media["cover"] == _COVER
    assert media["width"] == 750 and media["height"] == 1494


def test_media_is_none_when_nothing_to_show():
    out = dy.parse_messages(_resp(_msg(mtype=7)), my_uid=MY_UID)
    assert out[0]["media"] is None


def test_share_without_cover_still_has_vid():
    """没封面也要能点开播放。"""
    out = dy.parse_messages(_resp(_msg(mtype=8, content={"itemId": "555"})),
                            my_uid=MY_UID)
    assert out[0]["media"]["vid"] == "555"
    assert out[0]["media"]["cover"] == ""


def test_share_with_neither_cover_nor_id_has_no_media():
    out = dy.parse_messages(_resp(_msg(mtype=8, content={"content_name": "x"})),
                            my_uid=MY_UID)
    assert out[0]["media"] is None


@pytest.mark.parametrize("hostile", [
    {"cover_url": "not-a-dict"},
    {"cover_url": {"url_list": "nope"}},
    {"cover_url": {"url_list": []}},
    {"item_id": {"nested": 1}},
    {"aweme_info": "not-a-dict"},
    {"resource_url": []},
])
def test_malformed_media_fields_do_not_crash(hostile):
    out = dy.parse_messages(_resp(_msg(mtype=110, content=hostile)), my_uid=MY_UID)
    assert len(out) == 1


def test_non_douyin_cover_url_is_left_alone():
    """不是抖音域名的就别乱改。"""
    out = dy.parse_messages(_resp(_msg(mtype=8, content={
        "itemId": "1", "cover_url": {"url_list": ["https://example.com/a.png?x=1"]},
    })), my_uid=MY_UID)
    assert out[0]["media"]["cover"] == "https://example.com/a.png?x=1"
    # 非抖音域名没有签名概念，不需要兜底
    assert out[0]["media"]["cover_alt"] == ""


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "data:text/html,<script>",
                                 "//evil.com/x.png", "ftp://x/y"])
def test_hostile_cover_url_is_rejected(bad):
    """封面直接进 <img src>，非 http(s) 一律不要。"""
    out = dy.parse_messages(_resp(_msg(mtype=8, content={
        "itemId": "1", "cover_url": {"url_list": [bad]}})), my_uid=MY_UID)
    assert out[0]["media"]["cover"] == "", f"危险 URL 进了封面: {bad}"


@pytest.mark.parametrize("bad_vid", ["../../etc", "1'; drop--", "<script>", "a b"])
def test_hostile_vid_is_rejected(bad_vid):
    """vid 会拼进播放器 URL，只允许纯数字。"""
    out = dy.parse_messages(_resp(_msg(mtype=8, content={
        "itemId": bad_vid, "cover_url": {"url_list": [_COVER]}})), my_uid=MY_UID)
    assert (out[0]["media"] or {}).get("vid", "") == ""


# ── 语音与表情包：要能播、能看 ────────────────────────────

def _url_node(u="https://p1-sign.douyinpic.com/obj/xx"):
    return {"uri": "douyin-user-image-file/xx", "url_list": [u]}


def test_表情包解析出图片地址():
    """只显示「[表情]」的话，一连串表情长得一模一样，
    完全看不出对方在表达什么。"""
    md = dy._media("emoji", {"url": _url_node(), "width": 463, "height": 322})
    assert md["kind"] == "emoji"
    assert md["cover"].startswith("https://")
    assert md["width"] == 463 and md["height"] == 322


def test_语音解析出音频地址与时长():
    md = dy._media("audio", {
        "resource_url": _url_node("https://sf26-sign.douyinstatic.com/a.mpeg"),
        "duration": 4899,
        "voice_wave": [0, 0.3081, 0.5918],
    })
    assert md["kind"] == "audio"
    assert md["src"].endswith(".mpeg")
    assert md["duration_ms"] == 4899
    assert md["wave"] == [0.0, 0.31, 0.59]


def test_语音波形被截断():
    """抖音能给几百个点，整段塞进 DB 会把 media 字段撑爆（上限 2000 字节）。"""
    md = dy._media("audio", {
        "resource_url": _url_node("https://x.test/a.mpeg"),
        "voice_wave": [0.5] * 500,
    })
    assert len(md["wave"]) <= 40


def test_语音带上转写文字():
    md = dy._media("audio", {
        "resource_url": _url_node("https://x.test/a.mpeg"),
        "ai_audio_text": "明天一起吃饭吧",
    })
    assert md["asr"] == "明天一起吃饭吧"


def test_没有转写时不带asr字段():
    md = dy._media("audio", {
        "resource_url": _url_node("https://x.test/a.mpeg"), "ai_audio_text": "  "})
    assert "asr" not in md


def test_拿不到地址就不画卡片():
    """返回 None 前端才知道该退回纯文字，而不是渲染一个空播放器。"""
    assert dy._media("audio", {"duration": 3000}) is None
    assert dy._media("emoji", {"width": 100}) is None


def test_非http地址一律丢弃():
    """这些地址直接进 <img src> / new Audio()，javascript: 混进来就是 XSS。"""
    bad = {"url_list": ["javascript:alert(1)"]}
    assert dy._media("emoji", {"url": bad}) is None
    assert dy._media("audio", {"resource_url": bad}) is None


def test_异常时长被忽略():
    for dur in (0, -5, 99_999_999):
        md = dy._media("audio", {"resource_url": _url_node("https://x.test/a.mpeg"),
                                 "duration": dur})
        assert "duration_ms" not in md
