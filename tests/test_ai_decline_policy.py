"""弃权策略的回归测试。

线上真实事故：连续 3 条私信全被 `model_declined` 拦下 ——
"问"、"one"、"今天晚上能不能给我打个电话或者发个语音呀"。
清洗层一次都没误伤，是契约把口子开太大：原文里有
「对方消息是纯表情或看不懂」和「需要账号主人本人拍板的事」两条，
日常私信条条命中，模型顺着台阶就全弃权了。

这些测试盯的是**提示词本身的取向**，不是模型输出 ——
提示词是这个功能唯一的行为杠杆，改坏了没有任何编译期信号。
"""
from app import ai_reply


def _sys(max_chars: int = 60, persona: str = "", knowledge: str = "") -> str:
    return ai_reply.build_system_prompt(persona, knowledge, max_chars)


# ── 取向：默认要回，不是默认弃权 ──────────────────────────

def test_契约明确写了默认必须回复():
    """模型天然保守，只要给台阶就弃权。必须先立「默认要回」。"""
    assert "默认必须回复" in _sys()
    assert "默认就是要回" in _sys()


def test_策略只说什么该弃权不说怎么表达():
    """怎么表达由【输出格式】管 —— 两种模式的表达方式不同
    （JSON 的 should_reply / 纯文本的 [SKIP]）。策略里写死一种就会打架。"""
    assert "should_reply" not in ai_reply.DECLINE_POLICY
    assert "[SKIP]" not in ai_reply.DECLINE_POLICY
    assert "按【输出格式】里规定的方式弃权" in ai_reply.DECLINE_POLICY


def test_短消息要接住而不是弃权():
    """线上就是被这类消息全拦了："问"、"one"、"？"。"""
    s = _sys()
    assert "短消息" in s or "语气词" in s
    assert "看不懂就问，不要沉默" in s


def test_不再有看不懂就弃权这条():
    """这条口子最大 —— 中文私信里大量短消息都会被judge成看不懂。"""
    s = _sys()
    assert "纯表情或看不懂" not in s
    assert "需要账号主人本人拍板" not in s


def test_约电话约见面走婉拒而不是弃权():
    """真实案例：「今晚能不能打个电话」被弃权了，正确行为是用话术挡回去。"""
    s = _sys()
    assert "不要弃权" in s
    assert "打字聊" in s


# ── 红线：该弃权的还得弃权 ────────────────────────────────

def test_四类红线一条都不能少():
    """收窄不等于放开。这四类放出去是要出事的。"""
    s = _sys()
    for redline in ("转账", "红包", "借钱", "手机号", "微信",
                    "验证码", "辱骂", "诈骗", "政治敏感"):
        assert redline in s, f"红线「{redline}」从契约里掉了"


def test_弃权条件是封闭列表():
    """写成「只有这四类」而不是「以下情况」—— 后者是开放式的，
    模型会自己往里加条目，那正是这次事故的根因。"""
    s = _sys()
    assert "只有这四类才弃权" in s
    assert "除这四类之外，一律回复" in s


# ── 示例：校准阈值最有效的手段 ────────────────────────────

def test_示例覆盖了出过事的那几条消息():
    s = _sys()
    assert '"问"' in s              # 曾被判「看不懂」
    assert "打个电话" in s          # 曾被判「需要本人拍板」


def test_示例里正反两面都有():
    """只给正例，模型会学成"什么都回"；红线例子必须在场。"""
    s = _sys()
    assert '"should_reply": true' in s
    assert '"should_reply": false' in s
    assert "借我500" in s


# ── 格式契约没被改坏 ──────────────────────────────────────

def test_字数上限被正确渲染():
    assert "不超过 40 个字" in _sys(max_chars=40)
    assert "不超过 120 个字" in _sys(max_chars=120)


def test_格式硬约束仍在():
    s = _sys()
    for rule in ("只输出一个 JSON 对象", "纯文本单行", "无链接", "禁止自称 AI"):
        assert rule in s


def test_人设与知识库都进了提示词():
    s = _sys(persona="你是水果店老板", knowledge="[通用] 营业时间：早九晚六")
    assert "你是水果店老板" in s
    assert "早九晚六" in s
    # 知识库垫底：它最长，不该抢走格式契约的注意力
    assert s.index("只输出一个 JSON 对象") < s.index("早九晚六")


def test_没有知识库时不留空标题():
    """空的「已知事实」区块会让模型以为资料被清空了，反而更爱说不知道。"""
    assert "已知事实" not in _sys(knowledge="")
