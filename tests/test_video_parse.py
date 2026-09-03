"""分享视频解析：aweme_id 提取 + aweme/detail 归一化。

Why 懒解析：这是给真人号新增的一类抖音请求，聊天页浏览不触发，
只有 AI 真要回复某条分享视频时才打一次 —— 风控额度花在刀刃上。

字段取自对真实响应的实测（spike 验过两个视频，均 status_code=0）：
  desc / caption      完整文案
  item_title          标题，常为空
  text_extra[].hashtag_name   话题
  video_tag[].tag_name        抖音自己打的三级内容分类
  video.big_thumbs    雪碧图，5x5 网格、2s 一帧 —— 画面识别走它，不用 ffmpeg

夹具全部是合成 JSON：真实响应含真实 uid / 昵称 / 作品，按安全红线不进 git。
"""
import json

import pytest

import douyin_im as dy

AWEME_ID = "7600000000000000001"


# ── extract_aweme_id：纯本地，不发任何请求 ──────────────────────

@pytest.mark.parametrize("text,expected", [
    (AWEME_ID, AWEME_ID),
    (f"https://www.douyin.com/video/{AWEME_ID}", AWEME_ID),
    (f"https://www.douyin.com/video/{AWEME_ID}/", AWEME_ID),
    (f"https://www.douyin.com/video/{AWEME_ID}?previous_page=app_code_link", AWEME_ID),
    (f"https://www.iesdouyin.com/share/video/{AWEME_ID}/?region=CN", AWEME_ID),
    (f"看看这个 https://www.douyin.com/video/{AWEME_ID} 挺好笑", AWEME_ID),
    (f"https://www.douyin.com/discover?modal_id={AWEME_ID}", AWEME_ID),
])
def test_extract_aweme_id_from_common_forms(text, expected):
    assert dy.extract_aweme_id(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", None, "没有链接的普通聊天内容",
    "https://www.douyin.com/user/MS4wLjABAAAA",       # 用户主页不是视频
    "https://evil.com/video/7600000000000000001",     # 非抖音域名
    "12345",                                          # 太短，不是 aweme_id
    "7600000000000000001123456789012345678901",       # 太长
    "76790622711416619ab",                            # 含非数字
])
def test_extract_aweme_id_rejects_junk(text):
    assert dy.extract_aweme_id(text) == ""


def test_extract_aweme_id_ignores_short_link_without_network():
    """v.douyin.com 短链本地提不出 id —— 它要跟随重定向，是另一个函数的事。"""
    assert dy.extract_aweme_id("https://v.douyin.com/iRabcDe/") == ""


# ── fetch_aweme_detail ────────────────────────────────────────

def _detail(**over) -> dict:
    d = {
        "aweme_id": AWEME_ID,
        "desc": "山路骑行的保命习惯 #山路老李",
        "caption": "#山路老李",
        "item_title": "山路骑行的保命习惯",
        "create_time": 1788061500,
        "duration": 144173,
        "text_extra": [
            {"hashtag_name": "山路老李", "type": 1},
            {"hashtag_name": "", "type": 0},          # 空的要被过滤
            {"type": 1},                              # 缺字段不能炸
        ],
        "video_tag": [
            {"level": 1, "tag_name": "随拍"},
            {"level": 2, "tag_name": "生活记录"},
            {"level": 3, "tag_name": "日常vlog"},
        ],
        "statistics": {"digg_count": 4172, "comment_count": 262,
                       "share_count": 3925, "collect_count": 409},
        "author": {"nickname": "山路老李", "sec_uid": "MS4wLjABAAAAfake"},
        "music": {"author": "山路老李直播切片", "title": "@创作的原声"},
        "video": {
            "duration": 144173,
            "cover": {"url_list": ["https://p9-pc-sign.douyinpic.com/cover.jpeg?x-signature=a"]},
            "big_thumbs": [{
                "img_urls": ["https://p11-sign.douyinpic.com/sprite0.jpg?x-signature=b",
                             "https://p11-sign.douyinpic.com/sprite1.jpg?x-signature=c"],
                "img_num": 72, "img_x_len": 5, "img_y_len": 5,
                "img_x_size": 136, "img_y_size": 240, "interval": 2,
                "duration": 144.13333,
            }],
        },
    }
    d.update(over)
    return d


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class _Session:
    """只记录请求并回放预设响应，绝不碰网络。"""
    def __init__(self, *responses):
        self._responses = list(responses)
        self.urls: list[str] = []

    def get(self, url, **kw):
        self.urls.append(url)
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def no_sign(monkeypatch):
    """签名走 Node 子进程，单测里换成可预测的假实现。"""
    def _fake(params):
        from urllib.parse import urlencode
        return urlencode(dict(params, a_bogus="FAKE"))
    monkeypatch.setattr(dy, "_sign_params", _fake)


def test_fetch_returns_normalized_fields(no_sign):
    s = _Session(_Resp({"status_code": 0, "aweme_detail": _detail()}))
    out = dy.fetch_aweme_detail(s, AWEME_ID)

    assert out["aweme_id"] == AWEME_ID
    assert out["desc"] == "山路骑行的保命习惯 #山路老李"
    assert out["title"] == "山路骑行的保命习惯"
    assert out["duration_ms"] == 144173
    assert out["create_time"] == 1788061500
    assert out["author"]["nickname"] == "山路老李"
    assert out["author"]["sec_uid"] == "MS4wLjABAAAAfake"
    assert out["tags"] == ["山路老李"]
    assert out["categories"] == ["随拍", "生活记录", "日常vlog"]
    assert out["stats"] == {"digg": 4172, "comment": 262,
                            "share": 3925, "collect": 409}
    assert out["music"] == "山路老李直播切片"
    assert out["cover"].startswith("https://p9-pc-sign.douyinpic.com/")


def test_fetch_signs_request_and_passes_aweme_id(no_sign):
    s = _Session(_Resp({"status_code": 0, "aweme_detail": _detail()}))
    dy.fetch_aweme_detail(s, AWEME_ID)

    url = s.urls[0]
    assert url.startswith("https://www.douyin.com/aweme/v1/web/aweme/detail/?")
    assert f"aweme_id={AWEME_ID}" in url
    assert "a_bogus=FAKE" in url


def test_fetch_extracts_sprites_for_frame_recognition(no_sign):
    """雪碧图是画面识别的输入 —— 抖音不给 OCR，这是不装 ffmpeg 的唯一路。"""
    s = _Session(_Resp({"status_code": 0, "aweme_detail": _detail()}))
    sp = dy.fetch_aweme_detail(s, AWEME_ID)["sprites"]

    assert sp["urls"] == ["https://p11-sign.douyinpic.com/sprite0.jpg?x-signature=b",
                          "https://p11-sign.douyinpic.com/sprite1.jpg?x-signature=c"]
    assert sp["cols"] == 5 and sp["rows"] == 5
    assert sp["count"] == 72
    assert sp["interval"] == 2


def test_fetch_without_sprites_is_fine(no_sign):
    d = _detail()
    d["video"].pop("big_thumbs")
    s = _Session(_Resp({"status_code": 0, "aweme_detail": d}))
    assert dy.fetch_aweme_detail(s, AWEME_ID)["sprites"] is None


@pytest.mark.parametrize("payload", [
    {"status_code": 8, "aweme_detail": None},     # 抖音业务错误
    {"status_code": 0},                           # 没有 aweme_detail
    {"status_code": 0, "aweme_detail": {}},       # 空详情
    "<!DOCTYPE html><html>验证码页</html>",         # 被风控挡了，返回 HTML
])
def test_fetch_returns_empty_on_bad_response(no_sign, payload):
    s = _Session(_Resp(payload))
    assert dy.fetch_aweme_detail(s, AWEME_ID) == {}


def test_fetch_swallows_network_error(no_sign):
    """解析失败绝不能把调用方（AI worker）炸掉，静默返回空。"""
    s = _Session(RuntimeError("connection reset"))
    assert dy.fetch_aweme_detail(s, AWEME_ID) == {}


@pytest.mark.parametrize("bad_id", ["", "   ", "abc", "76790622711416619ab",
                                    "1' OR 1=1", "../../etc/passwd"])
def test_fetch_rejects_bad_aweme_id_without_requesting(no_sign, bad_id):
    s = _Session()          # 空响应列表：真发请求就会 IndexError
    assert dy.fetch_aweme_detail(s, bad_id) == {}
    assert s.urls == []


def test_fetch_survives_weird_field_types(no_sign):
    """抖音偶尔把字符串字段给成 dict/None，不能让整条解析崩掉。"""
    d = _detail(desc={"x": 1}, caption=None, item_title=None, text_extra="oops",
                video_tag={"a": 1}, statistics=None, author="nope",
                duration="144173", music=[], video=None)
    s = _Session(_Resp({"status_code": 0, "aweme_detail": d}))
    out = dy.fetch_aweme_detail(s, AWEME_ID)

    assert out["aweme_id"] == AWEME_ID
    assert out["desc"] == "" and out["title"] == ""
    assert out["tags"] == [] and out["categories"] == []
    assert out["stats"] == {"digg": 0, "comment": 0, "share": 0, "collect": 0}
    assert out["author"] == {"nickname": "", "sec_uid": ""}
    assert out["duration_ms"] == 0
    assert out["music"] == ""
    assert out["cover"] == "" and out["sprites"] is None


def test_fetch_falls_back_to_video_duration(no_sign):
    """顶层 duration 给成字符串时回退到 video.duration，别白丢时长。"""
    s = _Session(_Resp({"status_code": 0, "aweme_detail": _detail(duration="144173")}))
    assert dy.fetch_aweme_detail(s, AWEME_ID)["duration_ms"] == 144173


def test_fetch_falls_back_to_caption_when_desc_empty(no_sign):
    d = _detail(desc="", caption="夏天结束了")
    s = _Session(_Resp({"status_code": 0, "aweme_detail": d}))
    assert dy.fetch_aweme_detail(s, AWEME_ID)["desc"] == "夏天结束了"


def test_fetch_clamps_absurd_desc(no_sign):
    """视频文案能有上千字，整段进 prompt 就是白烧 token。"""
    d = _detail(desc="啊" * 5000)
    s = _Session(_Resp({"status_code": 0, "aweme_detail": d}))
    desc = dy.fetch_aweme_detail(s, AWEME_ID)["desc"]
    assert 0 < len(desc) <= dy.AWEME_DESC_MAX + 1
