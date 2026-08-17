"""短信登录发码结果判定。

背景：sms_login_browser 点完「获取验证码」后，只 try/except 等一下倒计时文案，
等不到就打印一句警告继续走，然后照样调 send_sink() 告诉前端「验证码已发送」。
弹了滑块 → 短信根本没发出 → 用户盯着「✓ 已发送」干等，这就是「收不到短信」。
"""
import pytest

import douyin_im as dy


# ── 发码结果分类 ─────────────────────────────────────────────────

def test_countdown_means_sent():
    state, msg = dy._classify_send_state(has_countdown=True, has_captcha=False)
    assert state == "sent"


def test_captcha_is_not_success():
    """核心回归：弹滑块绝不能算发送成功。"""
    state, msg = dy._classify_send_state(has_countdown=False, has_captcha=True)
    assert state == "captcha"
    assert state != "sent"
    assert "验证" in msg


def test_countdown_wins_when_both_present():
    """滑块过了之后倒计时也会出现，此时应判成功。"""
    state, _ = dy._classify_send_state(has_countdown=True, has_captcha=True)
    assert state == "sent"


def test_neither_is_unknown_failure():
    state, msg = dy._classify_send_state(has_countdown=False, has_captcha=False)
    assert state == "failed"
    assert msg


def test_error_text_is_surfaced():
    """页面上的报错（频率限制、手机号有误）要原样带给用户。"""
    state, msg = dy._classify_send_state(
        has_countdown=False, has_captcha=False,
        error_text="操作过于频繁，请稍后再试")
    assert state == "failed"
    assert "操作过于频繁" in msg


@pytest.mark.parametrize("state", ["captcha", "failed"])
def test_non_sent_states_are_actionable(state):
    """失败消息必须告诉用户下一步怎么办，而不是一句「失败」。"""
    _, msg = dy._classify_send_state(
        has_countdown=False, has_captcha=(state == "captcha"))
    assert len(msg) > 8


# ── 手机号规范化 ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("18812345678", "18812345678"),
    ("+8618812345678", "18812345678"),
    ("8618812345678", "18812345678"),
    (" 188 1234 5678 ", "18812345678"),
    ("188-1234-5678", "18812345678"),
])
def test_normalize_mobile(raw, expected):
    assert dy._normalize_mobile(raw) == expected


@pytest.mark.parametrize("bad", ["", "   ", "abc", "123", "1881234567890123"])
def test_normalize_mobile_rejects_garbage(bad):
    with pytest.raises(ValueError):
        dy._normalize_mobile(bad)


def test_normalize_mobile_rejects_non_mainland_format():
    """11 位、以 1 开头才是合法国内号；早点拦住比让浏览器空跑 20 秒好。"""
    with pytest.raises(ValueError):
        dy._normalize_mobile("28812345678")
