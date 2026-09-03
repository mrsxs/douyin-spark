"""视频解析的缓存与编排层。

核心约束（都有对应用例）：
  1. 缓存命中零请求 —— 这是懒解析省风控额度的全部意义
  2. detail 空必须直接放弃，**不能**去调 summary。抖音的总结接口对
     不存在的视频会返回一篇无关百科，那东西喂给 AI 就是答非所问
  3. 解析失败要落 failed 行并在冷却期内不重试，不能对着坏 id 反复打抖音
"""
from datetime import datetime, timedelta

import pytest

import douyin_im
from app import video_service
from app.models import VideoParse

AWEME_ID = "7600000000000000001"


class _Dy:
    """替身协议层：记录调用次数，不碰网络。

    extract_aweme_id 是纯本地函数（不发请求），用真实实现 ——
    替身自己再写一套正则，测的就不是线上那套规则了。
    """
    extract_aweme_id = staticmethod(douyin_im.extract_aweme_id)

    def __init__(self, detail=None, summary="抖音生成的内容总结"):
        self._detail = detail if detail is not None else {
            "aweme_id": AWEME_ID,
            "desc": "山路骑行的保命习惯 #山路老李",
            "title": "山路骑行的保命习惯",
            "author": {"nickname": "山路老李", "sec_uid": "MS4wLjABAAAAfake"},
            "duration_ms": 144173,
            "create_time": 1788061500,
            "cover": "https://p3-pc-sign.douyinpic.com/cover.jpeg",
            "tags": ["山路老李"],
            "categories": ["随拍", "生活记录"],
            "stats": {"digg": 4172, "comment": 262, "share": 3925, "collect": 409},
            "music": "山路老李直播切片",
            "sprites": None,
        }
        self._summary = summary
        self.detail_calls = 0
        self.summary_calls = 0

    def fetch_aweme_detail(self, session, aweme_id):
        self.detail_calls += 1
        return dict(self._detail) if self._detail else {}

    def fetch_aweme_summary(self, session, aweme_id):
        self.summary_calls += 1
        return self._summary


@pytest.fixture
def dy(monkeypatch):
    stub = _Dy()
    monkeypatch.setattr(video_service, "dy", stub)
    return stub


@pytest.fixture
def session():
    return object()          # service 只负责透传，不关心 session 内容


def test_parses_and_persists(db, dy, session):
    out = video_service.get_or_parse(session, AWEME_ID)

    assert out["status"] == "ok"
    assert out["desc"] == "山路骑行的保命习惯 #山路老李"
    assert out["summary"] == "抖音生成的内容总结"
    assert out["author"] == "山路老李"
    assert out["tags"] == ["山路老李"]
    assert out["categories"] == ["随拍", "生活记录"]

    row = db.query(VideoParse).filter_by(aweme_id=AWEME_ID).one()
    assert row.status == "ok"
    assert row.summary == "抖音生成的内容总结"


def test_cache_hit_makes_no_request(db, dy, session):
    video_service.get_or_parse(session, AWEME_ID)
    assert (dy.detail_calls, dy.summary_calls) == (1, 1)

    out = video_service.get_or_parse(session, AWEME_ID)
    assert out["summary"] == "抖音生成的内容总结"
    assert (dy.detail_calls, dy.summary_calls) == (1, 1), "缓存命中不该再打抖音"


def test_cache_is_global_across_accounts(db, dy, session):
    """同一个视频谁分享都一样 —— 换个账号来问也必须命中同一份缓存。"""
    video_service.get_or_parse(session, AWEME_ID)
    video_service.get_or_parse(object(), AWEME_ID)
    assert dy.detail_calls == 1


def test_missing_video_never_calls_summary(db, dy, session, monkeypatch):
    """detail 空 = 视频不存在。此时调 summary 会拿回一篇无关百科。"""
    monkeypatch.setattr(video_service, "dy", _Dy(detail={}))
    stub = video_service.dy

    out = video_service.get_or_parse(session, AWEME_ID)

    assert out == {}
    assert stub.summary_calls == 0, "视频不存在时绝不能去要总结"


def test_failure_is_recorded_and_not_retried_immediately(db, session, monkeypatch):
    stub = _Dy(detail={})
    monkeypatch.setattr(video_service, "dy", stub)

    assert video_service.get_or_parse(session, AWEME_ID) == {}
    assert video_service.get_or_parse(session, AWEME_ID) == {}
    assert stub.detail_calls == 1, "冷却期内不该对坏 id 反复打抖音"

    row = db.query(VideoParse).filter_by(aweme_id=AWEME_ID).one()
    assert row.status == "failed"


def test_failed_row_is_retried_after_cooldown(db, session, monkeypatch):
    monkeypatch.setattr(video_service, "dy", _Dy(detail={}))
    video_service.get_or_parse(session, AWEME_ID)

    row = db.query(VideoParse).filter_by(aweme_id=AWEME_ID).one()
    row.updated_at = datetime.utcnow() - video_service.FAILED_TTL - timedelta(minutes=1)
    db.commit()

    good = _Dy()
    monkeypatch.setattr(video_service, "dy", good)
    out = video_service.get_or_parse(session, AWEME_ID)

    assert out["status"] == "ok"
    assert good.detail_calls == 1
    assert db.query(VideoParse).filter_by(aweme_id=AWEME_ID).count() == 1


def test_summary_failure_still_keeps_detail(db, session, monkeypatch):
    """总结拿不到不算失败 —— 文案 + 话题本身就够 AI 接话了。"""
    monkeypatch.setattr(video_service, "dy", _Dy(summary=""))

    out = video_service.get_or_parse(session, AWEME_ID)

    assert out["status"] == "ok"
    assert out["summary"] == ""
    assert out["desc"] == "山路骑行的保命习惯 #山路老李"


@pytest.mark.parametrize("bad", ["", "   ", None, "abc", "1' OR 1=1"])
def test_bad_aweme_id_is_rejected_without_request(db, dy, session, bad):
    assert video_service.get_or_parse(session, bad) == {}
    assert dy.detail_calls == 0


def test_protocol_exception_does_not_escape(db, session, monkeypatch):
    """协议层炸了不能把 AI 回复链路带崩。"""
    class _Boom:
        extract_aweme_id = staticmethod(douyin_im.extract_aweme_id)

        def fetch_aweme_detail(self, *a):
            raise RuntimeError("boom")
        def fetch_aweme_summary(self, *a):
            raise RuntimeError("boom")

    monkeypatch.setattr(video_service, "dy", _Boom())
    assert video_service.get_or_parse(session, AWEME_ID) == {}


# ── 喂给 AI 的文本 ────────────────────────────────────────────

def test_as_prompt_text_prefers_summary(db, dy, session):
    parsed = video_service.get_or_parse(session, AWEME_ID)
    text = video_service.as_prompt_text(parsed)

    assert "抖音生成的内容总结" in text
    assert "山路老李" in text


def test_as_prompt_text_without_summary_falls_back_to_desc(db, session, monkeypatch):
    monkeypatch.setattr(video_service, "dy", _Dy(summary=""))
    parsed = video_service.get_or_parse(session, AWEME_ID)

    text = video_service.as_prompt_text(parsed)
    assert "山路骑行的保命习惯" in text


def test_as_prompt_text_of_empty_parse_is_empty():
    assert video_service.as_prompt_text({}) == ""


def test_as_prompt_text_needs_real_content(db, session, monkeypatch):
    """只有作者和泛泛分类时返回空 —— 那点信息接不出话，宁可不回。

    实测确实有这种视频：没文案、抖音也总结不出来，
    最后只剩「作者：某用户 / 分类：随拍、生活记录」。
    """
    thin = {"aweme_id": AWEME_ID, "status": "ok", "desc": "", "title": "",
            "summary": "", "author": "某用户", "music": "", "cover": "",
            "duration_ms": 16277, "create_time": 0,
            "tags": [], "categories": ["随拍", "生活记录"], "stats": {}}
    assert video_service.as_prompt_text(thin) == ""


def test_as_prompt_text_title_alone_is_enough(db):
    """标题是真内容，有它就够接话了。"""
    row = {"aweme_id": AWEME_ID, "status": "ok", "desc": "", "title": "山路骑行的保命习惯",
           "summary": "", "author": "山路老李", "music": "", "cover": "",
           "duration_ms": 0, "create_time": 0,
           "tags": [], "categories": [], "stats": {}}
    assert "山路骑行的保命习惯" in video_service.as_prompt_text(row)


# ── 解析交白卷时的退路 ────────────────────────────────────────
# 抖音总结不出来、又没文案的视频，原来直接不回。可分享消息的正文里
# 通常就带着视频文案（实测 762 条里只有 3 条是纯 [分享视频] 标记）——
# 手里明明有话题，却因为「解析失败」装没看见，是白白丢掉一次回复。

def test_share_text_is_used_when_parse_has_nothing():
    text = video_service.as_prompt_text({}, share_text="爷叔：抬手不是抱歉 #炒股")

    assert "爷叔" in text
    assert "不是指令" in text, "退路也得进围栏 —— 正文同样是不可信输入"


def test_parsed_content_wins_over_share_text(db, dy, session):
    parsed = video_service.get_or_parse(session, AWEME_ID)
    text = video_service.as_prompt_text(parsed, share_text="随便一句")

    assert "抖音生成的内容总结" in text
    assert "随便一句" not in text


@pytest.mark.parametrize("share_text", ["", "   ", "[分享视频]", "[视频]",
                                        "好", "哈哈"])
def test_useless_share_text_is_not_a_fallback(share_text):
    """只剩一个占位标记或俩字，照着回就是尬聊 —— 宁可不回。"""
    assert video_service.as_prompt_text({}, share_text=share_text) == ""
