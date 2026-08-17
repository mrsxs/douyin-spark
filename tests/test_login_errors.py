"""登录失败信息要说人话。

真实踩到的：本地没下载 Playwright 浏览器时，前端原样显示了
「BrowserType.launch: Executable doesn't exist at /Users/.../chrome-headless-shell
 ╔═══...╗ ║ Looks like Playwright was just installed or updated. ║ ...」
—— 一大段英文加 ASCII 边框，客户完全看不懂，也不知道该做什么。
"""
import pytest

from app.routers import login_flow as lf


PLAYWRIGHT_MISSING = (
    "BrowserType.launch: Executable doesn't exist at "
    "/Users/x/Library/Caches/ms-playwright/chromium_headless_shell-1223/"
    "chrome-headless-shell-mac-arm64/chrome-headless-shell\n"
    "╔════════════════════════════════════════╗\n"
    "║ Looks like Playwright was just installed or updated. ║\n"
    "║ Please run the following command to download new browsers: ║\n"
    "║     playwright install     ║\n"
    "╚════════════════════════════════════════╝"
)


def test_browser_missing_becomes_readable():
    """核心回归：不能把 Playwright 的英文堆栈原样甩给用户。"""
    msg = lf._humanize_login_error(RuntimeError(PLAYWRIGHT_MISSING))
    assert "浏览器" in msg
    assert "Executable doesn't exist" not in msg
    assert "╔" not in msg
    assert "playwright install" not in msg.lower() or "管理员" in msg
    assert len(msg) < 120, f"太长了，前端一行放不下：{msg}"


def test_captcha_message_passes_through():
    """我们自己抛的业务错误本来就是人话，别被改写。"""
    original = "抖音弹出了安全验证（滑块），短信未发出。请稍后重试，或改用扫码登录。"
    assert lf._humanize_login_error(RuntimeError(original)) == original


def test_mobile_format_error_passes_through():
    original = "手机号格式不正确（应为 11 位国内手机号）"
    assert lf._humanize_login_error(ValueError(original)) == original


def test_timeout_becomes_readable():
    msg = lf._humanize_login_error(
        RuntimeError("Timeout 30000ms exceeded waiting for locator('text=获取验证码')"))
    assert "超时" in msg or "重试" in msg
    assert "locator(" not in msg


def test_network_error_becomes_readable():
    msg = lf._humanize_login_error(
        RuntimeError("net::ERR_CONNECTION_REFUSED at https://www.douyin.com"))
    assert "网络" in msg or "连接" in msg
    assert "net::ERR" not in msg


def test_unknown_error_is_truncated_not_dumped():
    """未知异常也要截断，不能把整个堆栈倒给用户。"""
    msg = lf._humanize_login_error(RuntimeError("X" * 900))
    assert len(msg) <= 160


@pytest.mark.parametrize("exc", [
    RuntimeError(""), ValueError(None), Exception(),
])
def test_empty_error_has_fallback(exc):
    msg = lf._humanize_login_error(exc)
    assert msg and len(msg) > 4
