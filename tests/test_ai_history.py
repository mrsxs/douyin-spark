"""上下文构建：别让 AI 看起来失忆。

两个真实症状：
1. 上午 11:43 对方问「咋了」，AI 回「晚安！陈小舟」——
   续火花的每日模板混在历史里当成了「我说过的话」，模型学舌。
2. 对方发表情、我方分享视频，模型全看不见，
   于是「视频不是」这种话没有指代对象，整段对话看着是断的。
"""
from app import ai_reply


def _m(text, is_me=False, kind="text"):
    return {"text": text, "is_me": is_me, "kind": kind}


def _content(rows, turns=10, **kw):
    return [h["content"] for h in ai_reply.build_history(rows, turns, **kw)]


# ── 续火花模板不该进上下文 ────────────────────────────────

def test_剔除续火花模板():
    """不剔的话，模型会在上午十一点回一句「晚安」—— 线上真发生过。"""
    rows = [_m("晚安！陈小舟", is_me=True), _m("咋了"), _m("在的", is_me=True)]
    got = _content(rows, exclude_texts={"晚安！陈小舟"})
    assert "晚安！陈小舟" not in got
    assert got == ["咋了", "在的"]


def test_模板只剔完全一致的():
    """对方真说了「晚安」不该被吃掉，那是真实对话。"""
    rows = [_m("晚安"), _m("晚安！陈小舟", is_me=True), _m("晚安啦，明天见", is_me=True)]
    got = _content(rows, exclude_texts={"晚安！陈小舟"})
    assert got == ["晚安", "晚安啦，明天见"]


def test_模板前后空白不影响剔除():
    rows = [_m("  晚安！陈小舟  ", is_me=True), _m("在吗")]
    assert _content(rows, exclude_texts={"晚安！陈小舟"}) == ["在吗"]


def test_没有模板时不误伤():
    rows = [_m("晚安！陈小舟", is_me=True), _m("在吗")]
    assert len(_content(rows, exclude_texts=set())) == 2
    assert len(_content(rows)) == 2


# ── 非文本消息压成标记，而不是整条丢掉 ────────────────────

def test_表情保留():
    """对方发表情是一个对话节拍，丢了模型就不知道对方回应过。"""
    rows = [_m("这也是自动回复吗"), _m("[表情]", kind="emoji"), _m("哈哈", is_me=True)]
    assert _content(rows) == ["这也是自动回复吗", "[表情]", "哈哈"]


def test_分享带标题进上下文():
    """「视频不是」指的就是这条分享，丢了这句话就没有指代对象。"""
    rows = [_m("#飞机上的美景 #探索云端", is_me=True, kind="share"),
            _m("视频不是", is_me=True)]
    got = _content(rows)
    assert got[0] == "[分享] #飞机上的美景 #探索云端"
    assert got[1] == "视频不是"


def test_图片语音压成短标记():
    rows = [_m("", kind="image"), _m("", kind="audio")]
    assert _content(rows) == ["[图片]", "[语音]"]


def test_系统消息和无意义消息仍然丢掉():
    """这些是真没信息量，塞进去纯烧 token。"""
    rows = [_m("对方撤回了一条消息", kind="system"),
            _m("[消息]", kind="other"), _m("在吗")]
    assert _content(rows) == ["在吗"]


def test_空文本的文本消息不进上下文():
    assert _content([_m("   "), _m("在吗")]) == ["在吗"]


# ── 顺序与角色 ───────────────────────────────────────────

def test_角色映射正确():
    rows = [_m("在吗"), _m("在的", is_me=True)]
    h = ai_reply.build_history(rows, 10)
    assert [x["role"] for x in h] == ["user", "assistant"]


def test_按时间正序且只取最近若干条():
    rows = [_m(f"m{i}") for i in range(10)]
    assert _content(rows, turns=3) == ["m7", "m8", "m9"]


def test_取数在过滤之后():
    """先过滤再取 turns —— 否则表情一多，实际喂进去的远不够 turns。"""
    rows = ([_m("噪声", kind="system")] * 5) + [_m("a"), _m("b"), _m("c")]
    assert _content(rows, turns=3) == ["a", "b", "c"]


def test_零轮上下文返回空():
    assert ai_reply.build_history([_m("在吗")], 0) == []


def test_单条消息过长被截断():
    got = _content([_m("啊" * 500)])
    assert len(got[0]) <= 120


# ── 编排层：取数要留足余量 ────────────────────────────────

def test_编排层多取以抵消过滤损耗(db, active_user, monkeypatch):
    """非文本会被压标记、模板会被剔，按 turns*2 取根本不够。"""
    from app import ai_worker, messages_service
    _, a = active_user
    seen = {}

    def _spy(db_, account_id, peer_uid, limit=50, **kw):
        seen["limit"] = limit
        return []
    monkeypatch.setattr(messages_service, "load_conversation", _spy)
    monkeypatch.setattr(ai_worker, "_spark_templates", lambda *a_: set())

    from app import ai_reply_config
    ai_reply_config.save(db, a.id, {"model": "m", "api_key": "k",
                                    "history_turns": 6, "enabled": True})
    ai_reply_config.set_peer(db, a.id, "1001", {"enabled": True})
    db.commit()
    cfg = ai_reply_config.load(db, a.id)
    cfg.enabled_at = None
    db.commit()

    from app import llm
    monkeypatch.setattr(llm, "chat", lambda *a_, **k: llm.LLMResult(
        text='{"should_reply":true,"reply":"好"}'))
    monkeypatch.setattr(ai_worker, "_send", lambda *a_: "ok")
    ai_worker.handle(a.user_id, a.id, {
        "server_msg_id": 1, "peer_uid": "1001", "is_me": False,
        "kind": "text", "text": "在吗", "created_at": 9_999_999_999_999})

    assert seen["limit"] >= 20


def test_模板读取失败不影响回复(db, active_user, monkeypatch):
    """剔模板是锦上添花，读不到最差就是回到剔除前的行为，不能让回复挂掉。"""
    from app import ai_worker, templates_service
    _, a = active_user

    def _boom(*args, **kw):
        raise RuntimeError("DB 挂了")
    monkeypatch.setattr(templates_service, "load_templates", _boom)
    assert ai_worker._spark_templates(a.id, "1001") == set()


def test_收集默认模板和联系人模板(db, active_user):
    from app import ai_worker, templates_service
    _, a = active_user
    templates_service.upsert_template(db, a.id, "default", messages=["晚安！"])
    templates_service.upsert_template(db, a.id, "1001", messages=["早安！", " "])
    templates_service.upsert_template(db, a.id, "2002", messages=["别人的"])
    db.commit()

    got = ai_worker._spark_templates(a.id, "1001")
    assert got == {"晚安！", "早安！"}          # 空白条目和别人的模板都不要
