"""回复策略可视化与可编辑的测试。

用户反馈「策略看不到啊」—— 原来 decline_policy / fewshot 硬编码在
ai_reply.py 里，页面上既看不到也改不了，而它恰恰是最需要按口味调的部分
（写死的那版对「问」「在吗」全部弃权）。

盯三件事：
1. 存进去的策略真的进了发给模型的 prompt（不然改了等于没改）；
2. 清空 = 恢复默认，而不是「整段不要了」—— 没有弃权策略，
   模型对借钱、要微信也会照回不误；
3. 接口把默认原文吐给界面，保证「看到的」就是「发出去的」。
"""

from app import ai_reply, ai_reply_config
from app.models import AiReplyPeer


def _csrf(client):
    client.get("/login")
    return client.cookies.get("csrf", "")


class _W:
    def __init__(self, c):
        self.c = c

    def _h(self):
        return {"X-CSRF-Token": _csrf(self.c)}

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def put(self, url, **kw):
        return self.c.put(url, headers=self._h(), **kw)


# ── 自定义策略要真的生效 ──────────────────────────────────

def test_自定义弃权策略进入系统提示词(db, active_user):
    _, a = active_user
    ai_reply_config.save(db, a.id, {"decline_policy": "只有骂人才不回"})
    db.commit()
    eff = ai_reply_config.resolve(ai_reply_config.load(db, a.id), None)
    s = eff.system_prompt("")
    assert "只有骂人才不回" in s
    assert ai_reply.DECLINE_POLICY not in s      # 自定义顶掉默认，不是两份都塞


def test_自定义示例进入系统提示词(db, active_user):
    _, a = active_user
    ai_reply_config.save(db, a.id, {"fewshot": '对方："在" → {"reply":"嗯"}'})
    db.commit()
    eff = ai_reply_config.resolve(ai_reply_config.load(db, a.id), None)
    assert '对方："在"' in eff.system_prompt("")
    assert ai_reply.FEWSHOT not in eff.system_prompt("")


def test_没自定义时用内置默认(db, active_user):
    _, a = active_user
    ai_reply_config.save(db, a.id, {"model": "m"})
    db.commit()
    s = ai_reply_config.resolve(ai_reply_config.load(db, a.id), None).system_prompt("")
    assert ai_reply.DECLINE_POLICY in s
    assert ai_reply.FEWSHOT in s


def test_清空等于恢复默认而不是整段删掉(db, active_user):
    """输入框清空的语义是「恢复默认」。

    要是当成「不要这段」，模型就没有任何红线约束 ——
    借钱、要微信也会照回不误，这是能出事的。
    """
    _, a = active_user
    ai_reply_config.save(db, a.id, {"decline_policy": "自定义的"})
    db.commit()
    ai_reply_config.save(db, a.id, {"decline_policy": ""})
    db.commit()

    s = ai_reply_config.resolve(ai_reply_config.load(db, a.id), None).system_prompt("")
    assert ai_reply.DECLINE_POLICY in s
    for redline in ("转账", "借钱", "微信", "诈骗"):
        assert redline in s


def test_编排层和试跑用同一个拼装入口(db, active_user):
    """三处各拼一遍的话，界面预览显示的和真发出去的迟早对不上。"""
    _, a = active_user
    ai_reply_config.save(db, a.id, {"decline_policy": "只认这一条"})
    db.commit()
    cfg = ai_reply_config.load(db, a.id)
    eff = ai_reply_config.resolve(cfg, None)
    assert eff.system_prompt("知识X") == ai_reply.build_system_prompt(
        eff.persona, "知识X", eff.max_chars,
        decline_policy="只认这一条", fewshot="")


def test_联系人级人设覆盖后策略仍生效(db, active_user):
    _, a = active_user
    ai_reply_config.save(db, a.id, {"decline_policy": "自定义策略"})
    db.add(AiReplyPeer(douyin_account_id=a.id, uid="1001",
                       enabled=True, persona="对他正式一点"))
    db.commit()

    cfg = ai_reply_config.load(db, a.id)
    peer = ai_reply_config.get_peer(db, a.id, "1001")
    s = ai_reply_config.resolve(cfg, peer).system_prompt("")
    assert "对他正式一点" in s
    assert "自定义策略" in s


# ── 接口把默认原文给界面 ──────────────────────────────────

def test_接口返回默认策略原文(db, active_user, login):
    """界面拿它做占位和「恢复默认」，必须和代码里的是同一份。"""
    u, a = active_user
    d = _W(login(u)).get(f"/api/ai/{a.id}").json()["defaults"]
    assert d["decline_policy"] == ai_reply.DECLINE_POLICY
    assert d["fewshot"] == ai_reply.FEWSHOT
    assert d["output_contract"] == ai_reply.OUTPUT_CONTRACT


def test_配置里回显自定义策略(db, active_user, login):
    u, a = active_user
    c = _W(login(u))
    c.put(f"/api/ai/{a.id}", json={"decline_policy": "我的策略", "fewshot": "我的示例"})
    cfg = c.get(f"/api/ai/{a.id}").json()["config"]
    assert cfg["decline_policy"] == "我的策略"
    assert cfg["fewshot"] == "我的示例"


def test_未自定义时回空串而不是默认原文(db, active_user, login):
    """回空串前端才分得清「没改过」和「改成了和默认一样」。"""
    u, a = active_user
    cfg = _W(login(u)).get(f"/api/ai/{a.id}").json()["config"]
    assert cfg["decline_policy"] == ""
    assert cfg["fewshot"] == ""


# ── 提示词预览 ────────────────────────────────────────────

def test_预览接口吐出完整提示词(db, active_user, login):
    u, a = active_user
    c = _W(login(u))
    c.put(f"/api/ai/{a.id}", json={"persona": "你是水果店老板",
                                   "decline_policy": "只有骂人才不回"})
    body = c.get(f"/api/ai/{a.id}/prompt").json()
    assert body["ok"] is True
    assert "你是水果店老板" in body["system_prompt"]
    assert "只有骂人才不回" in body["system_prompt"]
    assert body["uses_custom_policy"] is True
    assert body["uses_custom_fewshot"] is False


def test_预览带上联系人级覆盖(db, active_user, login):
    u, a = active_user
    c = _W(login(u))
    c.put(f"/api/ai/{a.id}/peer/1001", json={"enabled": True, "persona": "对他正式"})
    body = c.get(f"/api/ai/{a.id}/prompt?uid=1001").json()
    assert "对他正式" in body["system_prompt"]


def test_没配过也能预览(db, active_user, login):
    """一进来就想看看默认长什么样，不该因为没保存过而报错。"""
    u, a = active_user
    body = _W(login(u)).get(f"/api/ai/{a.id}/prompt").json()
    assert body["ok"] is True
    assert ai_reply.DECLINE_POLICY in body["system_prompt"]


def test_预览不会顺手建配置行(db, active_user, login):
    """GET 不该有副作用 —— 建了行会让「有没有配过」的判断失真。"""
    u, a = active_user
    _W(login(u)).get(f"/api/ai/{a.id}/prompt")
    db.expire_all()
    assert ai_reply_config.load(db, a.id) is None


def test_预览不泄露api_key(db, active_user, login):
    u, a = active_user
    c = _W(login(u))
    c.put(f"/api/ai/{a.id}", json={"api_key": "sk-never-preview", "model": "m"})
    assert "sk-never-preview" not in c.get(f"/api/ai/{a.id}/prompt").text


def test_预览不了别人的号(db, active_user, login):

    from app.models import DouyinAccount, User
    from app.security import hash_password
    u, _ = active_user
    other = User(username="stranger2", password_hash=hash_password("pw123456"),
                 max_accounts=5)
    db.add(other); db.commit(); db.refresh(other)
    acc = DouyinAccount(user_id=other.id, label="别人的", status="active")
    db.add(acc); db.commit(); db.refresh(acc)

    assert _W(login(u)).get(f"/api/ai/{acc.id}/prompt").status_code == 404


# ── 长度上限 ─────────────────────────────────────────────

def test_策略超长被截断(db, active_user):
    """整段提示词给得宽，但不能没有上限 —— 那是直接烧 token。"""
    _, a = active_user
    ai_reply_config.save(db, a.id, {"decline_policy": "啊" * 9999})
    db.commit()
    assert len(ai_reply_config.load(db, a.id).decline_policy) <= 4000
