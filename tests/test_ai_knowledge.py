"""知识库检索：通用池与专属池必须互不影响。

这条不变量是用户明确要求的 —— 「通用和单独知识库不影响」。
测试盯死两点：
1. 两个池子分别取 top-k，通用条目再多也挤不掉专属条目；
2. 归属隔离，别的账号 / 别的联系人的知识永远检索不到。
"""
import pytest

from app import knowledge_service as kb
from app.models import KnowledgeEntry


@pytest.fixture
def acc(db, active_user):
    _, a = active_user
    return a


def _add(db, account_id, uid, title, content="", keywords="", enabled=True):
    e = KnowledgeEntry(douyin_account_id=account_id, uid=uid, title=title,
                       content=content, keywords=keywords, enabled=enabled)
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


# ── 命中 ─────────────────────────────────────────────────

def test_通用知识被关键词命中(db, acc):
    _add(db, acc.id, kb.GLOBAL_UID, "营业时间",
         "每天早九点到晚六点", keywords="营业,几点")
    out = kb.retrieve(db, acc.id, "1001", "你们几点开门呀")
    assert "早九点到晚六点" in out
    assert "[通用]" in out


def test_专属知识被命中(db, acc):
    _add(db, acc.id, "1001", "他的订单", "订单号 A123，已发货", keywords="订单")
    out = kb.retrieve(db, acc.id, "1001", "我订单到哪了")
    assert "A123" in out
    assert "[专属]" in out


def test_完全不相关时返回空(db, acc):
    _add(db, acc.id, kb.GLOBAL_UID, "营业时间", "早九晚六", keywords="营业")
    assert kb.retrieve(db, acc.id, "1001", "哈哈哈哈") == ""


def test_空查询返回空(db, acc):
    _add(db, acc.id, kb.GLOBAL_UID, "营业时间", "早九晚六", keywords="营业")
    assert kb.retrieve(db, acc.id, "1001", "") == ""


def test_禁用的条目不参与检索(db, acc):
    _add(db, acc.id, kb.GLOBAL_UID, "旧价格", "以前卖 99", keywords="价格",
         enabled=False)
    assert kb.retrieve(db, acc.id, "1001", "价格多少") == ""


# ── 核心不变量：两个池子互不挤占 ──────────────────────────

def test_通用条目再多也挤不掉专属条目(db, acc):
    """通用库塞满 10 条都命中的知识，专属那条仍然必须出现。

    如果两个池子混在一起排序，这里就会挂 —— 而用户的感受是
    「我给这个人单独配的知识没生效」，排查起来极其难受。
    """
    for i in range(10):
        _add(db, acc.id, kb.GLOBAL_UID, f"通用{i}", f"通用答案{i}", keywords="价格")
    _add(db, acc.id, "1001", "他的专属价", "给他打八折", keywords="价格")

    out = kb.retrieve(db, acc.id, "1001", "价格多少")
    assert "给他打八折" in out
    assert "[专属]" in out
    assert "[通用]" in out


def test_专属排在通用前面(db, acc):
    """同样命中时，为这个人单独写的更贴切，得先让模型看到。"""
    _add(db, acc.id, kb.GLOBAL_UID, "统一报价", "统一 100", keywords="价格")
    _add(db, acc.id, "1001", "他的专属价", "给他打八折", keywords="价格")
    out = kb.retrieve(db, acc.id, "1001", "价格多少")
    assert out.index("[专属]") < out.index("[通用]")


def test_每个池子各自限制条数(db, acc):
    for i in range(8):
        _add(db, acc.id, kb.GLOBAL_UID, f"通用{i}", f"答案{i}", keywords="价格")
    out = kb.retrieve(db, acc.id, "1001", "价格多少", top_k=2)
    assert out.count("[通用]") == 2


def test_改通用不影响专属(db, acc):
    """两条不变量的另一面：删/停通用条目，专属条目照样命中。"""
    g = _add(db, acc.id, kb.GLOBAL_UID, "统一报价", "统一 100", keywords="价格")
    _add(db, acc.id, "1001", "他的专属价", "给他打八折", keywords="价格")
    kb.delete_entry(db, acc.id, g.id)
    db.commit()
    out = kb.retrieve(db, acc.id, "1001", "价格多少")
    assert "给他打八折" in out
    assert "[通用]" not in out


# ── 归属隔离 ──────────────────────────────────────────────

def test_别的联系人的专属知识检索不到(db, acc):
    _add(db, acc.id, "2002", "别人的订单", "订单 B999", keywords="订单")
    out = kb.retrieve(db, acc.id, "1001", "我订单到哪了")
    assert "B999" not in out


def test_别的账号的知识检索不到(db, acc, active_user):
    from app.models import DouyinAccount
    u, _ = active_user
    other = DouyinAccount(user_id=u.id, label="小号", status="active")
    db.add(other); db.commit(); db.refresh(other)

    _add(db, other.id, kb.GLOBAL_UID, "别号营业时间", "早八晚十", keywords="营业")
    assert kb.retrieve(db, acc.id, "1001", "几点营业") == ""


# ── 预算 ─────────────────────────────────────────────────

def test_超预算的条目被丢掉(db, acc):
    _add(db, acc.id, kb.GLOBAL_UID, "长文", "啊" * 500, keywords="价格")
    out = kb.retrieve(db, acc.id, "1001", "价格多少", budget=50)
    assert len(out) <= 50


# ── CRUD ─────────────────────────────────────────────────

def test_新建与更新条目(db, acc):
    e = kb.upsert_entry(db, acc.id, kb.GLOBAL_UID,
                        {"title": "退货", "content": "七天无理由", "keywords": "退货"})
    db.commit()
    assert e.id and e.enabled is True

    kb.upsert_entry(db, acc.id, kb.GLOBAL_UID, {"content": "十五天无理由"}, entry_id=e.id)
    db.commit()
    assert kb.list_entries(db, acc.id, kb.GLOBAL_UID)[0]["content"] == "十五天无理由"


def test_标题内容都空时报错(db, acc):
    with pytest.raises(ValueError):
        kb.upsert_entry(db, acc.id, kb.GLOBAL_UID, {"title": " ", "content": ""})


def test_不能跨账号编辑条目(db, acc, active_user):
    """改个 id 就能编辑别人的知识库 —— 必须连 account_id 一起校验。"""
    from app.models import DouyinAccount
    u, _ = active_user
    other = DouyinAccount(user_id=u.id, label="小号", status="active")
    db.add(other); db.commit(); db.refresh(other)
    e = _add(db, other.id, kb.GLOBAL_UID, "别人的", "内容")

    with pytest.raises(ValueError):
        kb.upsert_entry(db, acc.id, kb.GLOBAL_UID, {"title": "篡改"}, entry_id=e.id)
    assert kb.delete_entry(db, acc.id, e.id) is False


def test_按uid统计条数(db, acc):
    _add(db, acc.id, kb.GLOBAL_UID, "a")
    _add(db, acc.id, "1001", "b")
    _add(db, acc.id, "1001", "c")
    assert kb.count_entries(db, acc.id) == {kb.GLOBAL_UID: 1, "1001": 2}
