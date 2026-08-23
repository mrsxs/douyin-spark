"""自动 location.reload() 必须走闸门。

线上连着出过两次同一类事故，都是「无条件重载」撞上「重载后状态不变」：

  1. CDN 缓存了没有 Set-Cookie 的页面 → 没 csrf → POST 403
     → api() 无条件 reload → 又是那份没 cookie 的副本 → 无限刷屏
  2. CDN 发 9.7 小时前的 HTML → 首屏 SSR 快照永远对不上新同步的数据
     → syncContacts 判定 stale → 无条件 reload → 无限刷屏 +「同步中」一直转

外因（客户的 CDN 怎么配）不可控，但「刷不出结果就一直刷」是我们的锅：
用户除了关标签页毫无办法，也看不到任何提示。

所以规则是：**任何非用户点击触发的 location.reload() 都必须先过
window._reloadOnce(key)**。这里既验行为，也扫模板挡住以后新写的。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASE_HTML = ROOT / "templates" / "base.html"
TEMPLATE_DIR = ROOT / "templates"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="没装 node，跳过 JS 行为验证")


def _guard_js() -> str:
    """抠出 base.html 里的 _reloadOnce / _reloadClear。"""
    src = BASE_HTML.read_text(encoding="utf-8")
    start = src.index("window._reloadOnce = ")
    end = src.index("window._getCsrf = ", start)
    return src[start:end]


def _run_node(script: str) -> dict:
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0, f"node 执行失败:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


HARNESS = """
global.window = global;
const store = {};
global.sessionStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
%s
const out = {
  first:  window._reloadOnce('acct-sync'),
  second: window._reloadOnce('acct-sync'),
  third:  window._reloadOnce('acct-sync'),
  otherKey: window._reloadOnce('csrf'),
};
window._reloadClear('acct-sync');
out.afterClear = window._reloadOnce('acct-sync');
console.log(JSON.stringify(out));
"""

BROKEN_STORAGE = """
global.window = global;
global.sessionStorage = {
  getItem: () => { throw new Error('SecurityError'); },
  setItem: () => { throw new Error('SecurityError'); },
  removeItem: () => { throw new Error('SecurityError'); },
};
%s
window._reloadClear('x');   // 不能抛
console.log(JSON.stringify({ first: window._reloadOnce('x') }));
"""


@pytest.fixture(scope="module")
def guard():
    return _run_node(HARNESS % _guard_js())


def test_first_reload_allowed(guard):
    assert guard["first"] is True, "第一次重载是正当补救，不能拦"


def test_second_reload_blocked(guard):
    assert guard["second"] is False, "第二次必须拦住，否则就是无限刷屏"


def test_third_reload_still_blocked(guard):
    assert guard["third"] is False


def test_keys_are_independent(guard):
    """CSRF 那条路和账户同步那条路各用各的额度，别互相吃掉。"""
    assert guard["otherKey"] is True


def test_clear_restores_quota(guard):
    """用户主动点「同步」时会 clear，得能再放行一次。"""
    assert guard["afterClear"] is True


def test_broken_storage_does_not_throw():
    """隐私模式下 sessionStorage 会抛 —— 不能因此把整个页面搞崩。"""
    res = _run_node(BROKEN_STORAGE % _guard_js())
    assert res["first"] is True, "storage 不可用时退回旧行为，至少刷得动"


# ── 扫模板：不许有裸的自动 reload ─────────────────────────────────────

def _js_lines(path: Path):
    """去掉 HTML 注释和 JS 行注释后按行返回。"""
    src = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)
    return [ln for ln in src.splitlines() if not ln.strip().startswith("//")]


ALL_TEMPLATES = sorted(TEMPLATE_DIR.rglob("*.html"))


@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=lambda p: p.name)
def test_no_unguarded_auto_reload(path):
    """每处 location.reload() 上方 12 行内必须出现 _reloadOnce。

    例外：写在 @click 里的（用户亲手点的，点一次刷一次，不会失控）。
    """
    lines = _js_lines(path)
    for i, ln in enumerate(lines):
        if "location.reload()" not in ln:
            continue
        window = "\n".join(lines[max(0, i - 12):i + 1])
        if "@click" in ln or "@click" in window:
            continue
        assert "_reloadOnce" in window, (
            f"{path.name} 第 {i + 1} 行有裸的自动 reload，"
            f"外部原因导致重载后状态不变时会无限刷屏：\n{window}")


def test_the_scan_actually_sees_the_known_sites():
    """防止扫描本身失效（比如正则写错、路径变了）而静默全绿。"""
    hits = sum("location.reload()" in ln
               for p in ALL_TEMPLATES for ln in _js_lines(p))
    assert hits >= 2, f"只扫到 {hits} 处 reload，扫描逻辑可能已经失效"
