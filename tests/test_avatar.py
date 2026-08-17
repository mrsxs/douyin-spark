"""_extract_avatar / _extract_user_fields 单元测试。

头像从用户 JSON 对象（fetch_nicknames 拉到的 relation/profile 数据）按 uid 精确取，
不再从 init 二进制响应按距离猜（会张冠李戴）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import douyin_im as dy  # noqa: E402

AV = "https://p3-sign.douyinpic.com/obj/tos-cn-avt-0015/abc123?x-expires=1"


def test_extract_avatar_from_url_list():
    u = {"avatar_thumb": {"url_list": ["http://low/x", AV]}}
    assert dy._extract_avatar(u) == "http://low/x"


def test_prefers_thumb_over_larger():
    u = {"avatar_larger": {"url_list": ["http://large/x"]},
         "avatar_thumb": {"url_list": [AV]}}
    assert dy._extract_avatar(u) == AV


def test_plain_string_avatar():
    assert dy._extract_avatar({"avatar_url": AV}) == AV


def test_no_avatar_returns_blank():
    assert dy._extract_avatar({"nickname": "x"}) == ""
    assert dy._extract_avatar({"avatar_thumb": {"url_list": []}}) == ""


def test_user_fields_includes_avatar():
    u = {"uid": "42", "sec_uid": "MS4x", "nickname": "小明",
         "remark_name": "老王", "avatar_thumb": {"url_list": [AV]}}
    uid, sec, nick, remark, avatar = dy._extract_user_fields(u)
    assert (uid, sec, nick, remark, avatar) == ("42", "MS4x", "小明", "老王", AV)
