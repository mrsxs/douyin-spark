"""GET 请求必须防中间层缓存。

线上事故：域名挂在阿里云 ESA 后面，边缘节点无视源站的 no-store，
把 `/api/login/qr/status` 的响应缓存了 4 分钟（响应头里 `age: 248`）。
前端每秒轮询拿到的都是那份旧的 `{"status":"waiting_scan"}`，
而后端其实早就进了 waiting_sms_code、短信也发出去了 ——
表现就是「验证码收到了但网页不弹输入框」，backend 干等 180 秒后超时。

服务器端的 no-store 中间件解决不了这个：客户会把服务挂在各种 CDN 后面，
配置由客户掌握。所以前端也得自保 —— GET 的 URL 每次都不一样。

这里用 node 真跑一遍 base.html 里的 api()，而不是只 grep 字符串，
因为「有没有拼对」和「拼在了 ? 还是 &」都能悄悄错。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASE_HTML = ROOT / "templates" / "base.html"
CHAT_HTML = ROOT / "templates" / "dashboard" / "chat.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="没装 node，跳过 JS 行为验证")


def _extract_api_fn() -> str:
    """把 base.html 里的 _getCsrf + window.api 一起抠出来。

    api() 里会调 window._getCsrf，只抠 api 的话 node 直接 TypeError。
    两个函数在文件里紧挨着，从 _getCsrf 开头取到 api 结尾即可。
    """
    src = BASE_HTML.read_text(encoding="utf-8")
    start = src.index("window._getCsrf = ")
    api_at = src.index("window.api = async", start)
    # 函数以 "};" 单独成行结束
    end = src.index("\n};", api_at) + len("\n};")
    return src[start:end]


def _run_node(script: str) -> dict:
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0, f"node 执行失败:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


HARNESS = """
global.window = global;
global.document = { cookie: 'csrf=tok123' };
const calls = [];
global.fetch = async (url, opts) => {
  calls.push({url, opts});
  return { ok: true, status: 200, json: async () => ({ok: true}) };
};
%s
(async () => {
  await window.api('/api/login/qr/status?account_id=1');
  await window.api('/api/login/qr/status?account_id=1');
  await window.api('/api/notifications');
  await window.api('/api/login/qr/start', 'POST', {account_id: 1});
  console.log(JSON.stringify({
    urls: calls.map(c => c.url),
    caches: calls.map(c => c.opts.cache || null),
    methods: calls.map(c => c.opts.method),
    csrf: calls.map(c => (c.opts.headers['X-CSRF-Token'] || null)),
  }));
})();
"""


@pytest.fixture(scope="module")
def result():
    return _run_node(HARNESS % _extract_api_fn())


def test_get_url_gets_cache_buster(result):
    assert "_ts=" in result["urls"][0], "GET 没加一次性参数，会被 CDN 缓存住"


def test_repeated_get_urls_differ(result):
    """两次问同一个状态必须是两个不同 URL，否则边缘节点照样命中同一份。"""
    assert result["urls"][0] != result["urls"][1]


def test_cache_buster_uses_ampersand_when_query_exists(result):
    """原本就有 ?account_id=1，再拼必须用 &，用 ? 会把参数拼废。"""
    url = result["urls"][0]
    assert "?account_id=1&_ts=" in url, url


def test_cache_buster_uses_question_mark_when_no_query(result):
    assert result["urls"][2].startswith("/api/notifications?_ts="), result["urls"][2]


def test_original_query_params_survive(result):
    assert "account_id=1" in result["urls"][0]


def test_get_sets_fetch_no_store(result):
    assert result["caches"][0] == "no-store", "浏览器自己那层缓存也要关"


def test_post_is_not_touched(result):
    """POST 本来就不会被缓存，加参数只会白白弄脏 URL 和审计日志。"""
    post_url = result["urls"][3]
    assert "_ts=" not in post_url
    assert result["caches"][3] is None


def test_post_still_carries_csrf(result):
    """别在改缓存的时候把 CSRF 头弄丢了。"""
    assert result["csrf"][3] == "tok123"
    assert result["csrf"][0] is None, "GET 不需要 CSRF"


# ── SSE ───────────────────────────────────────────────────────────────

def test_sse_url_has_cache_buster():
    src = CHAT_HTML.read_text(encoding="utf-8")
    m = re.search(r"new EventSource\((.*?)\);", src, re.S)
    assert m, "没找到 EventSource 调用"
    assert "_ts=" in m.group(1), "SSE 连接也要带一次性参数"
    assert "interval=" in m.group(1), "别把 interval 参数弄丢了"


def test_sse_keeps_interval_before_buster():
    """拼接顺序：interval 在前用 ?，_ts 在后用 &。"""
    src = CHAT_HTML.read_text(encoding="utf-8")
    m = re.search(r"new EventSource\((.*?)\);", src, re.S)
    frag = m.group(1)
    assert frag.index("interval=") < frag.index("_ts="), frag
    assert "&_ts=" in frag, "_ts 必须用 & 拼，前面已经有 ?interval= 了"


# ── CSRF 失败不能进无限刷新 ───────────────────────────────────────────

LOOP_HARNESS = """
global.window = global;
let cookie = %s;
Object.defineProperty(global, 'document', { value: { get cookie() { return cookie; } } });
const store = {};
global.sessionStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
let reloads = 0;
global.location = { reload: () => { reloads++; }, href: '' };
global.setTimeout = (fn) => fn();
const toasts = [];
global.toast = (m) => toasts.push(m);
global.fetch = async () => ({
  ok: false, status: 403,
  text: async () => '{"ok":false,"error":"CSRF token 无效或过期"}',
  json: async () => ({}),
});
%s
(async () => {
  const r1 = await window.api('/api/x', 'POST', {});
  const r2 = await window.api('/api/x', 'POST', {});
  const r3 = await window.api('/api/x', 'POST', {});
  console.log(JSON.stringify({reloads, errs: [r1.error, r2.error, r3.error], toasts}));
})();
"""


def _run_loop(cookie_js):
    return _run_node(LOOP_HARNESS % (cookie_js, _extract_api_fn()))


@pytest.fixture(scope="module")
def loop_no_cookie():
    return _run_loop("''")


def test_csrf_failure_reloads_at_most_once(loop_no_cookie):
    """三次连续 403，只允许刷新一次。

    旧代码是无条件 location.reload()：中间层缓存了没有 Set-Cookie 的页面时，
    每次加载都拿不到 csrf → POST 403 → 再刷 → 无限循环，
    用户除了关标签页没别的办法。线上真发生过。
    """
    assert loop_no_cookie["reloads"] == 1, \
        f"刷了 {loop_no_cookie['reloads']} 次 —— 会变成无限刷新"


def test_second_failure_explains_missing_cookie(loop_no_cookie):
    assert "csrf cookie" in loop_no_cookie["errs"][1], loop_no_cookie["errs"][1]
    assert "CDN" in loop_no_cookie["errs"][1], "要指出最可能的原因，不然没人查得下去"


def test_third_failure_still_does_not_reload(loop_no_cookie):
    assert loop_no_cookie["reloads"] == 1
    assert loop_no_cookie["errs"][2], "第三次也要返回可读的错误，不能是 undefined"


def test_first_failure_still_reloads_once(loop_no_cookie):
    """别矫枉过正：令牌真过期时，刷一次是对的补救。"""
    assert "过期" in loop_no_cookie["errs"][0]


def test_message_differs_when_cookie_exists():
    """有 cookie 却仍被拒 → 不该甩锅给 CDN。"""
    res = _run_loop("'csrf=abc123'")
    assert "csrf cookie" not in res["errs"][1], res["errs"][1]
    assert "停留过久" in res["errs"][1], res["errs"][1]
