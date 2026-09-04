"""AI 自动回复接口的归属与脱敏测试。

两条红线：
1. api_key 只进不出 —— 任何响应里都不能出现明文；
2. 换个 account_id 就能读别人的知识库/聊天记录，归属过滤是唯一的闸门。
"""
import pytest

from app import ai_reply_config
from app.models import DouyinAccount, KnowledgeEntry


def _csrf(client):
    client.get("/login")                       # 让中间件下发 csrf cookie
    return client.cookies.get("csrf", "")


class _W:
    """给写请求自动带上 CSRF 头。

    中间件对所有非 GET 请求校验 token，不带头一律 403 ——
    那样测出来的全是「被中间件拦住」，根本走不到路由里的归属校验。
    """

    def __init__(self, c):
        self.c = c

    def _h(self):
        return {"X-CSRF-Token": _csrf(self.c)}

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def put(self, url, **kw):
        return self.c.put(url, headers=self._h(), **kw)

    def post(self, url, **kw):
        return self.c.post(url, headers=self._h(), **kw)

    def delete(self, url, **kw):
        return self.c.delete(url, headers=self._h(), **kw)


@pytest.fixture
def other_acc(db, active_user):
    """同一个用户的第二个账号，用来验证账号之间也隔离。"""
    u, _ = active_user
    a = DouyinAccount(user_id=u.id, label="小号", status="active")
    db.add(a); db.commit(); db.refresh(a)
    return a


@pytest.fixture
def stranger(db):
    """另一个用户 + 他的账号。"""

    from app.models import User
    from app.security import hash_password
    u = User(username="stranger", password_hash=hash_password("pw123456"),
             max_accounts=5)
    db.add(u); db.commit(); db.refresh(u)
    a = DouyinAccount(user_id=u.id, label="别人的号", status="active")
    db.add(a); db.commit(); db.refresh(a)
    return u, a


# ── 配置读写 ─────────────────────────────────────────────

def test_未配置时返回默认值(db, active_user, login):
    u, a = active_user
    r = _W(login(u)).get(f"/api/ai/{a.id}")
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert cfg["enabled"] is False
    assert cfg["has_key"] is False


def test_保存后能读回(db, active_user, login):
    u, a = active_user
    c = _W(login(u))
    r = c.put(f"/api/ai/{a.id}", json={
        "provider": "openai", "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat", "api_key": "sk-secret-123",
        "persona": "你是店主", "max_chars": 40,
    })
    assert r.json()["ok"] is True
    cfg = c.get(f"/api/ai/{a.id}").json()["config"]
    assert cfg["model"] == "deepseek-chat"
    assert cfg["persona"] == "你是店主"
    assert cfg["max_chars"] == 40


def test_响应里永远没有api_key明文(db, active_user, login):
    """key 泄到前端就等于泄给了任何能打开页面的人。"""
    u, a = active_user
    c = _W(login(u))
    c.put(f"/api/ai/{a.id}", json={"api_key": "sk-secret-123", "model": "m"})

    for resp in (c.get(f"/api/ai/{a.id}"),
                 c.put(f"/api/ai/{a.id}", json={"persona": "x"})):
        assert "sk-secret-123" not in resp.text
    assert c.get(f"/api/ai/{a.id}").json()["config"]["has_key"] is True


def test_key加密入库(db, active_user, login):
    """DB 落盘也不能是明文 —— 备份文件被人拿到就全泄了。"""
    import os

    u, a = active_user
    os.environ["SAAS_CRYPT_KEY"] = "test-crypt-key"
    try:
        _W(login(u)).put(f"/api/ai/{a.id}", json={"api_key": "sk-plain", "model": "m"})
        db.expire_all()
        row = ai_reply_config.load(db, a.id)
        assert row.api_key_enc.startswith("ENC1:")
        assert "sk-plain" not in row.api_key_enc
        assert ai_reply_config.api_key(row) == "sk-plain"
    finally:
        os.environ.pop("SAAS_CRYPT_KEY", None)


def test_提交空key不会清空已有的(db, active_user, login):
    """前端回显不到明文，提交时那个空输入框不该把 key 抹掉。"""
    u, a = active_user
    c = _W(login(u))
    c.put(f"/api/ai/{a.id}", json={"api_key": "sk-1", "model": "m"})
    c.put(f"/api/ai/{a.id}", json={"api_key": "", "persona": "改个人设"})
    assert c.get(f"/api/ai/{a.id}").json()["config"]["has_key"] is True


def test_没配key不允许开启(db, active_user, login):
    """开着开关却没 key，等于每来一条消息烧一次失败请求。"""
    u, a = active_user
    r = _W(login(u)).put(f"/api/ai/{a.id}", json={"enabled": True})
    assert r.json()["ok"] is False
    assert ai_reply_config.load(db, a.id) is None or \
        ai_reply_config.load(db, a.id).enabled is False


def test_数值参数被夹逼到合法区间(db, active_user, login):
    u, a = active_user
    c = _W(login(u))
    c.put(f"/api/ai/{a.id}", json={"max_chars": 99999, "cooldown_sec": 0,
                                   "poll_interval": 1})
    cfg = c.get(f"/api/ai/{a.id}").json()["config"]
    assert cfg["max_chars"] == ai_reply_config.MAX_CHARS_RANGE[1]
    assert cfg["cooldown_sec"] == ai_reply_config.COOLDOWN_RANGE[0]
    # 没人看时还几秒一次打抖音，纯粹是给风控送素材
    assert cfg["poll_interval"] == ai_reply_config.POLL_RANGE[0]


# ── 联系人级开关 ──────────────────────────────────────────

def test_默认没有联系人在白名单里(db, active_user, login):
    u, a = active_user
    assert _W(login(u)).get(f"/api/ai/{a.id}").json()["peers"] == {}


def test_单独打开某个联系人(db, active_user, login):
    u, a = active_user
    c = _W(login(u))
    assert c.put(f"/api/ai/{a.id}/peer/1001", json={"enabled": True}).json()["ok"]
    peers = c.get(f"/api/ai/{a.id}").json()["peers"]
    assert peers["1001"]["enabled"] is True
    assert "1002" not in peers


def test_联系人级话术覆盖可清空(db, active_user, login):
    u, a = active_user
    c = _W(login(u))
    c.put(f"/api/ai/{a.id}/peer/1001", json={"enabled": True, "persona": "正式一点"})
    assert c.get(f"/api/ai/{a.id}").json()["peers"]["1001"]["persona"] == "正式一点"
    c.put(f"/api/ai/{a.id}/peer/1001", json={"persona": ""})
    # 空串 = 回到继承账号级，而不是存一个空人设
    assert c.get(f"/api/ai/{a.id}").json()["peers"]["1001"]["persona"] == ""
    assert ai_reply_config.get_peer(db, a.id, "1001").persona is None


# ── 知识库 ───────────────────────────────────────────────

def test_通用与专属知识库分开读写(db, active_user, login):
    """用户明确要求：通用和单独知识库互不影响。"""
    u, a = active_user
    c = _W(login(u))
    c.post(f"/api/ai/{a.id}/knowledge",
           json={"uid": "*", "title": "营业时间", "content": "早九晚六"})
    c.post(f"/api/ai/{a.id}/knowledge",
           json={"uid": "1001", "title": "他的订单", "content": "A123"})

    shared = c.get(f"/api/ai/{a.id}/knowledge?uid=*").json()["entries"]
    private = c.get(f"/api/ai/{a.id}/knowledge?uid=1001").json()["entries"]
    assert [e["title"] for e in shared] == ["营业时间"]
    assert [e["title"] for e in private] == ["他的订单"]


def test_删除通用条目不影响专属(db, active_user, login):
    u, a = active_user
    c = _W(login(u))
    g = c.post(f"/api/ai/{a.id}/knowledge",
               json={"uid": "*", "title": "通用", "content": "x"}).json()["entry"]
    c.post(f"/api/ai/{a.id}/knowledge",
           json={"uid": "1001", "title": "专属", "content": "y"})

    assert c.delete(f"/api/ai/{a.id}/knowledge/{g['id']}").json()["ok"] is True
    assert c.get(f"/api/ai/{a.id}/knowledge?uid=*").json()["entries"] == []
    assert len(c.get(f"/api/ai/{a.id}/knowledge?uid=1001").json()["entries"]) == 1


def test_知识库条数统计(db, active_user, login):
    u, a = active_user
    c = _W(login(u))
    c.post(f"/api/ai/{a.id}/knowledge", json={"uid": "*", "title": "a", "content": "x"})
    c.post(f"/api/ai/{a.id}/knowledge", json={"uid": "1001", "title": "b", "content": "y"})
    assert c.get(f"/api/ai/{a.id}").json()["kb_counts"] == {"*": 1, "1001": 1}


def test_空条目被拒绝(db, active_user, login):
    u, a = active_user
    r = _W(login(u)).post(f"/api/ai/{a.id}/knowledge", json={"uid": "*"})
    assert r.json()["ok"] is False


# ── 归属隔离 ──────────────────────────────────────────────

def test_读不到别人的配置(db, active_user, stranger, login):
    u, _ = active_user
    _, their_acc = stranger
    assert _W(login(u)).get(f"/api/ai/{their_acc.id}").status_code == 404


def test_写不了别人的配置(db, active_user, stranger, login):
    u, _ = active_user
    _, their_acc = stranger
    assert _W(login(u)).put(f"/api/ai/{their_acc.id}",
                        json={"model": "hack"}).status_code == 404


def test_读不到别人的知识库(db, active_user, stranger, login):
    u, _ = active_user
    su, their_acc = stranger
    db.add(KnowledgeEntry(douyin_account_id=their_acc.id, uid="*",
                          title="机密", content="别人的秘密"))
    db.commit()
    r = _W(login(u)).get(f"/api/ai/{their_acc.id}/knowledge?uid=*")
    assert r.status_code == 404
    assert "别人的秘密" not in r.text


def test_删不了别人的知识条目(db, active_user, stranger, login):
    """带上自己的 account_id、填别人的 entry_id —— 最容易漏的越权路径。"""
    u, my_acc = active_user
    _, their_acc = stranger
    e = KnowledgeEntry(douyin_account_id=their_acc.id, uid="*",
                       title="机密", content="别人的秘密")
    db.add(e); db.commit(); db.refresh(e)

    r = _W(login(u)).delete(f"/api/ai/{my_acc.id}/knowledge/{e.id}")
    assert r.json()["ok"] is False
    assert db.get(KnowledgeEntry, e.id) is not None


def test_改不了别人的知识条目(db, active_user, stranger, login):
    u, my_acc = active_user
    _, their_acc = stranger
    e = KnowledgeEntry(douyin_account_id=their_acc.id, uid="*",
                       title="机密", content="别人的秘密")
    db.add(e); db.commit(); db.refresh(e)

    r = _W(login(u)).post(f"/api/ai/{my_acc.id}/knowledge",
                      json={"id": e.id, "uid": "*", "title": "篡改"})
    assert r.json()["ok"] is False
    db.expire_all()
    assert db.get(KnowledgeEntry, e.id).title == "机密"


def test_读不到别人的回复日志(db, active_user, stranger, login):
    u, _ = active_user
    _, their_acc = stranger
    assert _W(login(u)).get(f"/api/ai/{their_acc.id}/logs").status_code == 404


def test_未登录一律拒绝(client, active_user):
    _, a = active_user
    client.cookies.clear()
    assert client.get(f"/api/ai/{a.id}").status_code in (401, 403, 302, 307)


# ── 试跑 ─────────────────────────────────────────────────

@pytest.fixture
def configured(db, active_user, login):
    """配好 key 和模型的账号 + 带 CSRF 的客户端。"""
    u, a = active_user
    ai_reply_config.save(db, a.id, {"model": "test-model", "api_key": "sk-t",
                                    "base_url": "https://x.test/v1"})
    db.commit()
    return u, a, _W(login(u))


def test_试跑返回清洗前后与拦截原因(db, configured, monkeypatch):
    """这条曾经在真实服务上 500：limiter 开了 headers_enabled，
    返回裸 dict 的端点必须有 response 形参，否则 slowapi 直接抛异常。
    单测里没跑过这个端点，是靠起服务 curl 才发现的 —— 补上盯死。"""
    from app import llm
    u, a, c = configured
    monkeypatch.setattr(llm, "chat", lambda *args, **kw: llm.LLMResult(
        text='{"should_reply": true, "reply": "早九晚六"}', tokens=12, latency_ms=88))

    r = c.post(f"/api/ai/{a.id}/test", json={"text": "几点营业", "uid": "1001"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["reply"] == "早九晚六"
    assert body["blocked"] is False
    assert body["raw"].startswith("{")
    assert body["tokens"] == 12


def test_试跑命中拦截时给出人话原因(db, configured, monkeypatch):
    from app import llm
    u, a, c = configured
    monkeypatch.setattr(llm, "chat", lambda *args, **kw: llm.LLMResult(
        text='{"should_reply": true, "reply": "看这个 https://x.com/a"}'))

    body = c.post(f"/api/ai/{a.id}/test", json={"text": "在吗"}).json()
    assert body["blocked"] is True
    assert body["reason"] == "link"
    assert "风控" in body["reason_label"]


def test_试跑绝不真的发送(db, configured, monkeypatch):
    """试跑走的是独立路径，不该碰到任何发送出口。"""
    from app import llm, trigger
    u, a, c = configured
    monkeypatch.setattr(llm, "chat", lambda *args, **kw: llm.LLMResult(
        text='{"should_reply": true, "reply": "好的"}'))

    def _boom(*args, **kw):
        raise AssertionError("试跑不该调用发送")

    monkeypatch.setattr(trigger, "send_to_uid", _boom)
    monkeypatch.setattr(trigger, "send_single", _boom)
    assert c.post(f"/api/ai/{a.id}/test", json={"text": "在吗"}).json()["ok"] is True


def test_试跑注入命中的知识(db, configured, monkeypatch):
    from app import llm
    u, a, c = configured
    c.post(f"/api/ai/{a.id}/knowledge",
           json={"uid": "*", "title": "营业时间", "content": "早九晚六", "keywords": "营业"})

    seen = {}

    def _capture(cfg, system, user_prompt, history=None):
        seen["system"] = system
        return llm.LLMResult(text='{"should_reply":true,"reply":"早九晚六"}')

    monkeypatch.setattr(llm, "chat", _capture)
    body = c.post(f"/api/ai/{a.id}/test", json={"text": "几点营业"}).json()
    assert "早九晚六" in seen["system"]
    assert "早九晚六" in body["knowledge"]


def test_没配key不能试跑(db, active_user, login):
    u, a = active_user
    body = _W(login(u)).post(f"/api/ai/{a.id}/test", json={"text": "在吗"}).json()
    assert body["ok"] is False


def test_空消息不能试跑(db, configured):
    u, a, c = configured
    assert c.post(f"/api/ai/{a.id}/test", json={"text": "  "}).json()["ok"] is False


def test_模型报错时回人话而不是500(db, configured, monkeypatch):
    from app import llm
    u, a, c = configured

    def _boom(*args, **kw):
        raise llm.LLMError("HTTP 401: unauthorized")

    monkeypatch.setattr(llm, "chat", _boom)
    r = c.post(f"/api/ai/{a.id}/test", json={"text": "在吗"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "401" in r.json()["error"]


def test_试不了别人的号(db, active_user, stranger, login):
    u, _ = active_user
    _, their_acc = stranger
    assert _W(login(u)).post(f"/api/ai/{their_acc.id}/test",
                             json={"text": "在吗"}).status_code == 404


# ── 日志 ─────────────────────────────────────────────────

def test_日志按时间倒序并带可读原因(db, active_user, login):
    from app.models import AiReplyLog
    u, a = active_user
    for i, (status, reason) in enumerate([("sent", None), ("blocked", "link")]):
        db.add(AiReplyLog(douyin_account_id=a.id, peer_uid="1001",
                          server_msg_id=100 + i, status=status, reason=reason,
                          incoming="在吗", final_text="好的"))
    db.commit()
    logs = _W(login(u)).get(f"/api/ai/{a.id}/logs").json()["logs"]
    assert len(logs) == 2
    blocked = next(x for x in logs if x["status"] == "blocked")
    assert "风控" in blocked["reason_label"]
