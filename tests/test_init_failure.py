"""get_message_by_init 失败时的报错。

Why 单独一份：这条路失败时 **HTTP 依然是 200**，错误藏在 protobuf 的 field 4。
早先只报「HTTP 200」，唯一有用的线索被丢掉了 —— 真实排查靠手工解包才看到
`unexepcted session length`（抖音自己的拼写），才知道是 init_req.bin 里的
IM 握手过期、只能重新登录。同样的坑不该再踩第二次。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import douyin_im as dy  # noqa: E402
from app import trigger  # noqa: E402


def _err_resp(text: str) -> bytes:
    """仿抖音的错误信封：f1=cmd, f3=1(出错), f4=错误正文。"""
    return (dy._pb_v(1, 2043) + dy._pb_v(2, 10001) + dy._pb_v(3, 1)
            + dy._pb_b(4, text) + dy._pb_v(5, 1))


class _Resp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status


# ── douyin_im.init_error ──────────────────────────────────────

def test_reads_server_error_text():
    assert dy.init_error(_err_resp("unexepcted session length")) \
        == "unexepcted session length"


@pytest.mark.parametrize("blob", [b"", b"garbage", b"\x08\x01"])
def test_unparsable_response_gives_empty(blob):
    assert dy.init_error(blob) == ""


def test_error_text_is_capped():
    """错误正文直接进日志和前端提示，不能让它无限长。"""
    assert len(dy.init_error(_err_resp("x" * 5000))) <= 200


# ── trigger._init_failure ─────────────────────────────────────

def test_stale_im_session_tells_user_to_relogin():
    msg = trigger._init_failure(_Resp(_err_resp("unexepcted session length")))
    assert "重新登录" in msg
    assert "unexepcted session length" in msg, "服务端原话要留着，方便对症"


@pytest.mark.parametrize("err", ["unexpected token=abc",
                                 "crc32 check not pass for xyz"])
def test_other_handshake_errors_also_point_at_relogin(err):
    assert "重新登录" in trigger._init_failure(_Resp(_err_resp(err)))


def test_unknown_error_is_passed_through():
    msg = trigger._init_failure(_Resp(_err_resp("rate limit exceeded")))
    assert "rate limit exceeded" in msg
    assert "重新登录" not in msg, "别把不认识的错误都往重新登录上引"


def test_non_200_still_reports_the_status_code():
    assert "HTTP 503" in trigger._init_failure(_Resp(b"", status=503))


def test_short_body_without_error_text_says_so():
    """有时连错误正文都没有 —— 至少要说清「响应短得不正常」。"""
    msg = trigger._init_failure(_Resp(b"\x08\x01"))
    assert "字节" in msg
    assert "HTTP 200" not in msg, "报 HTTP 200 等于什么都没说"
