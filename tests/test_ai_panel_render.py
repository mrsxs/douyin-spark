"""聊天页里 AI 面板的渲染测试。

模板层最容易静默出错的两件事，这里各盯一条：
1. Jinja 会把 {{message}} / {{userinput}} 当自己的表达式吃掉 ——
   页面上就只剩空白，用户完全不知道该填什么变量；
2. 面板依赖的 Alpine 状态如果没挂进 chat()，整页 x-data 直接报错，
   聊天功能一起挂掉。
"""
import pytest


@pytest.fixture
def page(active_user, login):
    u, a = active_user
    r = login(u).get(f"/accounts/{a.id}/chat")
    assert r.status_code == 200
    return r.text


def test_ai入口按钮存在(page):
    assert "openAi()" in page
    assert "AI 自动回复" in page


def test_变量占位符没被jinja吃掉(page):
    """页面上必须能看到字面量的 {{userinput}} 和 {{message}}。"""
    assert "{{userinput}}" in page
    assert "{{message}}" in page


def test_面板状态挂进了chat组件(page):
    """漏了这行，x-data="chat()" 里所有 ai* 引用都会 undefined 报错。"""
    assert "function aiMixin()" in page
    assert "aiMixin()" in page


def _code_only(html: str) -> str:
    """去掉注释再检查。

    解释这个坑的注释里就写着 `{...aiMixin()}` 当反例，
    不剥注释的话守卫会被自己的说明文字绊倒。
    """
    import re
    return re.sub(r"/\*.*?\*/|\{#.*?#\}", "", html, flags=re.S)


def test_mixin用描述符合并而不是对象展开(page):
    """线上事故：写成对象展开时，展开会调用 getter 并把返回值拷成普通属性 ——
    peerAiOn / aiEnabledCount 在加载那一刻就被定死。

    表现是「面板永远显示 0 位联系人生效」和「联系人开关只能开、关不掉」
    （want = !peerAiOn 恒为 true）。没有 JS 测试环境，用这条守住。
    """
    assert "...aiMixin()" not in _code_only(page), "对象展开会把 getter 求值成静态值"
    assert "getOwnPropertyDescriptors(aiMixin())" in page


def test_依赖实时求值的两个getter还在(page):
    assert "get peerAiOn()" in page
    assert "get aiEnabledCount()" in page


def test_四个分页都在(page):
    for label in ("设置", "知识库", "试跑", "记录"):
        assert label in page


def test_通用与专属知识库的说明在页面上(page):
    """用户要求的核心语义，必须在界面上讲清楚而不是藏在文档里。"""
    assert "两者互不影响" in page


def test_试跑不会真的发出去有明确提示(page):
    assert "不会真的发出去" in page


def test_每个联系人的开关入口存在(page):
    assert "togglePeerAi()" in page
    assert "peerAiOn" in page


def test_没用不存在的颜色令牌(page):
    """ios-orange 在 Tailwind 配置里不存在，写了会静默不生效。"""
    assert "ios-orange" not in page


def test_key输入框不回显明文(active_user, login, db):
    from app import ai_reply_config
    u, a = active_user
    ai_reply_config.save(db, a.id, {"api_key": "sk-should-never-render", "model": "m"})
    db.commit()

    html = login(u).get(f"/accounts/{a.id}/chat").text
    assert "sk-should-never-render" not in html
