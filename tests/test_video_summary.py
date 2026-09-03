"""抖音自带「AI 视频总结」接口的解析。

Why 用它而不是自己抽帧识别：抖音自己就有这个能力（客户端「AI抖音 → 视频总结」），
输出是结构化的内容梗概，比我们拿 136x240 的雪碧图猜画面准得多，
也省掉 ffmpeg 依赖和一次多模态调用的钱。

接口特征（实测）：
  - so-landing.douyin.com/douyin/select/v1/ai/stream/，**不带 a_bogus 签名**，
    只认 cookie + Referer
  - text/event-stream，正文逐词流式追加
  - 正文路径 data[].display.display.generation_spans[].text.content

夹具是按真实响应结构手写的合成 SSE：真实响应里带 search_id / device_id /
真实作者内容，按安全红线不进 git。
"""
import json

import pytest

import douyin_im as dy

AWEME_ID = "7600000000000000001"


def _span_event(*contents, span_type=2):
    """造一个 SSE data 事件，携带若干正文 span。"""
    payload = {
        "status_code": 200,
        "data": [{
            "cmd": "Append",
            "display": {"display": {
                "generation_spans": [
                    {"type": span_type, "text": {"content": c}} for c in contents
                ],
            }},
        }],
    }
    return f"data:{json.dumps(payload, ensure_ascii=False)}"


def _sse(*lines) -> list[bytes]:
    out = []
    for ln in lines:
        out.append(b"event:message")
        out.append(ln.encode("utf-8"))
        out.append(b"")
    return out


class _StreamResp:
    def __init__(self, lines, status=200, ctype="text/event-stream"):
        self._lines = lines
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.text = ""

    def iter_lines(self, **kw):
        for ln in self._lines:
            if isinstance(ln, Exception):
                raise ln
            yield ln

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Session:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_summary_joins_streamed_spans():
    s = _Session(_StreamResp(_sse(
        _span_event("这段视频", "是摩旅博主"),
        _span_event("的直播切片。"),
    )))
    assert dy.fetch_aweme_summary(s, AWEME_ID) == "这段视频是摩旅博主的直播切片。"


def test_summary_strips_highlight_markup():
    """抖音会给重点词包 <mark>，进 prompt 前必须剥掉，否则模型会学着输出标签。"""
    s = _Session(_StreamResp(_sse(
        _span_event("核心观点是", "<mark>避免撞击</mark>", "，控速。"),
    )))
    assert dy.fetch_aweme_summary(s, AWEME_ID) == "核心观点是避免撞击，控速。"


def test_summary_request_is_unsigned_but_carries_referer():
    """这个接口不走 a_bogus，认的是 cookie + Referer；Referer 缺了会被拒。"""
    s = _Session(_StreamResp(_sse(_span_event("摘要"))))
    dy.fetch_aweme_summary(s, AWEME_ID)

    url, kw = s.calls[0]
    assert url.startswith("https://so-landing.douyin.com/douyin/select/v1/ai/stream/?")
    assert f"ai_search_enter_from_group_id={AWEME_ID}" in url
    assert "a_bogus" not in url
    assert kw["stream"] is True
    assert AWEME_ID in kw["headers"]["Referer"]
    assert kw["headers"]["Referer"].startswith("https://so-landing.douyin.com/")


def test_summary_generates_fresh_conversation_id_each_call():
    """conversation_id 复用会被当成同一轮对话，第二次可能直接返回空。"""
    s = _Session(_StreamResp(_sse(_span_event("a"))),
                 _StreamResp(_sse(_span_event("b"))))
    dy.fetch_aweme_summary(s, AWEME_ID)
    dy.fetch_aweme_summary(s, AWEME_ID)

    def _conv(url):
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(url).query)["conversation_id"][0]

    assert _conv(s.calls[0][0]) != _conv(s.calls[1][0])


@pytest.mark.parametrize("bad_id", ["", "   ", "abc", "1' OR 1=1"])
def test_summary_rejects_bad_aweme_id_without_requesting(bad_id):
    s = _Session()
    assert dy.fetch_aweme_summary(s, bad_id) == ""
    assert s.calls == []


def test_summary_returns_empty_on_http_error():
    s = _Session(_StreamResp([], status=403))
    assert dy.fetch_aweme_summary(s, AWEME_ID) == ""


def test_summary_returns_empty_when_no_spans():
    """接口 200 但没生成内容（风控/未授权）也得干净返回空。"""
    s = _Session(_StreamResp(_sse('data:{"status_code":200,"data":[]}')))
    assert dy.fetch_aweme_summary(s, AWEME_ID) == ""


def test_summary_swallows_network_error():
    s = _Session(RuntimeError("connection reset"))
    assert dy.fetch_aweme_summary(s, AWEME_ID) == ""


def test_summary_survives_broken_lines_midstream():
    """流到一半坏掉时，保住已经收到的部分 —— 半份摘要也比没有强。"""
    s = _Session(_StreamResp([
        b"event:message",
        _span_event("前半段内容").encode("utf-8"),
        b"data:{not valid json",
        b"data:null",
        _span_event("后半段内容").encode("utf-8"),
    ]))
    assert dy.fetch_aweme_summary(s, AWEME_ID) == "前半段内容后半段内容"


def test_summary_is_clamped():
    s = _Session(_StreamResp(_sse(_span_event("啊" * 5000))))
    out = dy.fetch_aweme_summary(s, AWEME_ID)
    assert 0 < len(out) <= dy.AWEME_SUMMARY_MAX + 1


def test_summary_decodes_utf8_split_across_lines():
    """按 bytes 分行再解码：decode_unicode 会在 chunk 边界把中文切坏。"""
    s = _Session(_StreamResp(_sse(_span_event("中文摘要正常"))))
    assert dy.fetch_aweme_summary(s, AWEME_ID) == "中文摘要正常"


# ── 抖音的两种「假总结」，都必须当没有 ──────────────────────

@pytest.mark.parametrize("bogus", [
    # 实测：无文案的短视频会拿到这段道歉模板，而不是真总结
    "由于无法直接访问视频内容，无法提供该视频的总结。您可以使用抖音的“豆包”"
    "AI总结功能获取视频摘要：打开视频，点击右下角分享按钮，选择“AI总结”即可生成要点。",
    "抱歉，我无法获取视频的具体内容，建议您直接观看视频。",
    "无法提供该视频的总结，可尝试提供视频链接或描述视频内容。",
    # 实测：aweme_id 不存在时，抖音把「视频总结」当搜索词回的百科
    "视频总结是将视频精简为简短文本，保留核心内容和关键信息的过程，"
    "能帮你快速了解视频主旨、节省时间。",
])
def test_apology_and_encyclopedia_are_rejected(bogus):
    """这两种文本读起来都很正常，喂给 AI 却会让它彻底跑偏。"""
    s = _Session(_StreamResp(_sse(_span_event(bogus))))
    assert dy.fetch_aweme_summary(s, AWEME_ID) == ""


def test_real_summary_mentioning_video_is_kept():
    """别误伤：正常总结里出现「视频」两个字是家常便饭。"""
    real = ("这段视频是摩旅博主“山路老李”的直播切片，核心围绕公路摩托车保命技巧展开。"
            "他结合自身摔车经验强调：只要不撞固定物体，单纯摔车滑行通常无生命危险。")
    s = _Session(_StreamResp(_sse(_span_event(real))))
    assert dy.fetch_aweme_summary(s, AWEME_ID) == real
