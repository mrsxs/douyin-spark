"""AI 自动回复 —— 清洗、渲染与提示词组装。

# 为什么清洗比提示词重要

提示词只是「请求」模型守规矩，它随时会不守：加 ```json 围栏、加一句
「好的，这是我的回复」、自称 AI、把回复写成 markdown、幻觉出一个链接、
一口气写两百字。这些原样发进抖音私信，轻则露馅，重则风控。

所以真正的保证在 `sanitize_reply`：它默认不信任模型的任何输出，
只有走完全部检查才返回文本，其余一律返回 None。
**宁可不回，也不能回错** —— 不回只是少一条消息，回错可能把号搭进去。

# 变量渲染为什么不用 format / Jinja2

`str.format` 会被对方消息里的 `{` `}` 直接炸成 KeyError；
Jinja2 渲染用户可控的模板等于开了个 SSTI 入口。
这里只做白名单变量的单次正则替换：未知变量原样保留，
变量值里的 `{{...}}` 不会被二次展开（否则对方发个 `{{userinput}}` 就能套娃）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# ── 输出契约：写死在这里，方便 grep 和测试引用 ──────────────

OUTPUT_CONTRACT = """【输出格式 — 违反即丢弃】
1. 只输出一个 JSON 对象。禁止 ```、禁止任何解释、禁止 JSON 之外的字符。
2. 严格为：{"should_reply": true, "reply": "回复内容"}
3. reply 必须是纯文本单行：无换行、无 markdown(* # > -)、无链接、
   无手机号、无微信/QQ 号、无 @。
4. reply 不超过 %(max_chars)d 个字，宁短不长，像真人随手打的。
5. 禁止自称 AI / 机器人 / 语言模型；禁止提到"知识库""根据资料"。
6. 只回应一件事，最多一个反问。"""

# 关掉思考时用的契约。
#
# 为什么要两份：这个网关上 thinking=disabled 和 response_format=json_object
# 不能并存 —— 同时给会返回一串空白。关思考就只能收纯文本，
# 于是「我不想回」这个信号没地方放，改用哨兵词表达。
#
# 哨兵选 [SKIP] 而不是中文：中文回复里可能真的出现「不回复」三个字，
# 而一句正常的私信不可能整条就是 [SKIP]。方括号也不在 _strip_pair
# 的成对符号表里，不会被当成包裹符剥掉。
DECLINE_SENTINEL = "[SKIP]"

OUTPUT_CONTRACT_PLAIN = """【输出格式 — 违反即丢弃】
1. 直接输出回复正文本身。禁止 JSON、禁止 ```、禁止任何解释或前缀。
2. 纯文本单行：无换行、无 markdown(* # > -)、无链接、
   无手机号、无微信/QQ 号、无 @。
3. 不超过 %(max_chars)d 个字，宁短不长，像真人随手打的。
4. 禁止自称 AI / 机器人 / 语言模型；禁止提到"知识库""根据资料"。
5. 只回应一件事，最多一个反问。
6. 判断这条不该回时，**整条只输出** [SKIP]，不要加任何别的字。"""

# 弃权策略。**默认必须回**是第一句，位置很关键 ——
# 原来这段写成一串"遇到 XX 就弃权"，里面还有「看不懂」「需要本人拍板」
# 这种口子极大的条目，结果模型对「问」「one」「能打个电话吗」这类
# 日常私信全部弃权，功能等于没开。
#
# 模型本来就偏保守，只要给了台阶就往下走。所以这里必须反过来写：
# 先立"默认要回"，再把弃权收窄到真正有害的那几条红线。
#
# 这段只说**什么**该弃权，绝不说**怎么**表达弃权 —— 后者由【输出格式】
# 负责，两种模式的表达方式不同（JSON 的 should_reply / 纯文本的 [SKIP]）。
# 混着写的话，关思考时这里会教模型吐 JSON，和契约直接打架。
# 用户自定义这段时也不用关心当前是哪种模式。
DECLINE_POLICY = """【默认必须回复】
默认就是要回。这是私信闲聊，不是客服工单 —— 沉默比回得平淡更奇怪。
对方发短消息、语气词、单个字、看不太懂的话（"问"、"one"、"？"、"在"），
也要用一句轻松的话接住，比如反问"咋啦"、"啥事呀"。看不懂就问，不要沉默。

【只有这四类才弃权】
只有命中下面任意一条，才按【输出格式】里规定的方式弃权：
1. 涉及钱款：转账、红包、借钱、付款、收款
2. 索要或提供隐私：手机号、微信、住址、身份证、验证码
3. 辱骂、威胁、投诉，或明显的诈骗话术
4. 政治敏感或违法内容
除这四类之外，一律回复。

【要婉拒的用话术挡，不要弃权】
对方约打电话、约语音、约见面、催你现在办某件事 —— 这些不算红线。
不要弃权，用一句自然的话挡回去，例如
"现在不太方便，打字聊呗"、"这两天有点忙，回头说"。"""

# 少量示例比任何规则描述都管用 —— 弃权阈值这种"感觉"，
# 用例子校准一次胜过写十行说明。
FEWSHOT = """【示例】
对方："问"
输出：{"should_reply": true, "reply": "咋啦"}

对方："今天晚上能不能给我打个电话或者发个语音呀"
输出：{"should_reply": true, "reply": "今晚不太方便，打字聊呗"}

对方："在吗"
输出：{"should_reply": true, "reply": "在的，咋啦"}

对方："先借我500，明天还你"
输出：{"should_reply": false, "reply": ""}"""

# 关思考模式下的示例。示例的输出形态必须和契约要求的一致，
# 否则模型会照着示例吐 JSON，而那边根本没开 response_format。
FEWSHOT_PLAIN = """【示例】
对方："问"
输出：咋啦

对方："今天晚上能不能给我打个电话或者发个语音呀"
输出：今晚不太方便，打字聊呗

对方："在吗"
输出：在的，咋啦

对方："先借我500，明天还你"
输出：[SKIP]"""

DEFAULT_PERSONA = "你是账号主人本人，在抖音私信里跟朋友聊天。语气自然随意，像平时发消息那样。"

# 用户可改的提示词模板默认值。{{userinput}} 是对方原话的占位符。
DEFAULT_PROMPT_TEMPLATE = "{{nickname}} 发来消息：{{userinput}}"

DEFAULT_REPLY_FORMAT = "{{message}}"


# ── 变量渲染 ──────────────────────────────────────────────

# 模板里认识的变量。不在表里的原样保留，让用户能看见自己写错了，
# 而不是被静默吃掉变成空白。
ALLOWED_VARS = frozenset({
    "userinput",   # 对方发来的原话（已清洗）
    "message",     # 模型生成的回复（只在 reply_format 里用）
    "nickname",    # 对方昵称
    "days",        # 火花连续天数
    "time",        # 当前时间 HH:MM
    "history",     # 最近几轮对话
    "knowledge",   # 检索到的知识库片段
})

_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_vars(template: str, values: dict[str, str]) -> str:
    """把 {{var}} 换成 values 里的值。单次替换，不递归。

    单次是关键：对方在私信里发一句 `{{nickname}}`，如果递归渲染，
    它就会被展开成真实昵称 —— 模板注入的第一步。
    """
    def _sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in ALLOWED_VARS:
            return m.group(0)          # 未知变量原样保留
        return str(values.get(name, "") or "")
    return _VAR_RE.sub(_sub, template or "")


# ── 对方输入的清洗（防提示词注入）──────────────────────────

# 零宽字符 + 双向控制符：肉眼看不见，但会混进模型输入、也会让长度判断失真。
# 必须写成转义 —— 直接贴字面量等于往源码里埋隐形字符，
# 谁也看不出这行到底匹配了什么，review 时更是完全隐身。
_ZERO_WIDTH = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")

# 整行命中就整行丢掉。对方能往私信里发任何东西，
# 这些是想改写我们系统指令的典型句式。
_INJECTION = re.compile(
    r"(忽略(以上|上面|之前|前面|所有)"
    r"|无视(以上|上述|之前|前面)"
    r"|你现在是|从现在开始你是|重复(你的|上面的)(系统)?(提示|指令)"
    r"|【(输出格式|系统提示|指令|规则|角色|人设)"
    r"|^\s*(system|assistant|user|developer)\s*[:：]"
    r"|ignore\s+(all\s+)?(previous|above|prior)"
    r"|disregard\s+(all\s+)?(previous|above)"
    r"|forget\s+(all\s+)?(previous|above)"
    r"|system\s+prompt|prompt\s+injection)",
    re.I,
)

USER_INPUT_LIMIT = 500


def sanitize_user_input(text: str, limit: int = USER_INPUT_LIMIT) -> str:
    """把对方的原话清洗成可以安全塞进 prompt 的一行文本。"""
    t = _ZERO_WIDTH.sub("", text or "")
    kept = [ln for ln in t.splitlines() if not _INJECTION.search(ln)]
    return " ".join(" ".join(kept).split())[:limit]


# ── 回复清洗流水线 ────────────────────────────────────────

@dataclass(frozen=True)
class ReplyPolicy:
    """一次清洗用到的全部策略。冻结的 —— 清洗过程不该改配置。"""
    max_chars: int = 60
    reply_format: str = DEFAULT_REPLY_FORMAT
    banned_words: tuple[str, ...] = field(default_factory=tuple)


_FENCE_OPEN = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*")
_FENCE_CLOSE = re.compile(r"\s*```\s*$")
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)

# 模型爱加的自报家门前缀
_ROLE_PREFIX = re.compile(
    r"^\s*(回复|答复|回答|输出|assistant|ai|bot|机器人|助手|我)\s*[:：]\s*", re.I)

# markdown 标记。行首标记要按行匹配，所以在折叠换行之前做。
_MD_INLINE = re.compile(r"(\*\*|__|\*|_{2,}|`)")
_MD_LINE = re.compile(r"^\s*(#{1,6}\s*|>\s*|[-+]\s+)", re.M)

# 成对包裹符：整体被裹住时才剥，句中出现的不动
_QUOTE_PAIRS = {
    '"': '"', "'": "'", "`": "`",
    "“": "”", "‘": "’", "「": "」", "『": "』",
    "【": "】", "《": "》", "(": ")", "（": "）",
}

# 句末标点，超长时优先在这里断句
_SENTENCE_END = "。！？!?~～…；;"
# 断句后至少要留这么多字，否则宁可硬截到上限 ——
# 「好。」后面接六十个字，截在第一个句号只剩两个字，那还不如硬截。
_MIN_SENTENCE = 8

# 关思考模式下模型用它表达"这条不该回"。允许模型漏掉方括号或加空格 ——
# 只要整条就是这个词就算弃权；夹在正文里的不算，那多半是模型在复述规则。
_DECLINE_RE = re.compile(r"^\[?\s*SKIP\s*\]?[。.！!]?$", re.I)

# ── 硬拦截：命中即不发。顺序即优先级（决定 reason 取哪个）──

_BLOCKERS: tuple[tuple[str, re.Pattern], ...] = (
    # 抖音私信发链接是触发风控的高频行为，模型幻觉一个 URL 就可能让号进 shadowban
    ("link", re.compile(
        r"(https?://|www\.[a-z0-9-]+\.|[\w-]+\.(com|cn|net|org|io|me|top|xyz|vip|shop)(/|\s|$))",
        re.I)),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("contact", re.compile(
        r"((微信|威信|薇信|扣扣|企鹅)|\b(weixin|wechat|vx|wx|qq)\b)"
        r"\s*[号:：]?\s*[A-Za-z0-9_-]{5,}", re.I)),
    # 一旦露馅，对面立刻知道在跟机器人聊天，这个功能就白做了
    ("ai_persona", re.compile(
        r"(作为(一个|一名)?\s*(AI|A\.I|人工智能|语言模型|机器人|智能助手|聊天助手)"
        r"|我(只)?是(一个|一名|个)?\s*(AI|人工智能|语言模型|机器人|聊天机器人|智能助手)"
        r"|(大型)?语言模型)", re.I)),
    # 把内部机制说漏嘴同样等于露馅
    ("meta_leak", re.compile(
        r"(根据(知识库|资料库|以上资料|提供的资料|系统提示)"
        r"|知识库(中|里)?(显示|提到|说)"
        r"|系统提示词|我的(提示词|指令是))", re.I)),
)


def sanitize_reply(raw: str, policy: ReplyPolicy) -> tuple[str | None, str]:
    """把模型输出洗成可以直接发出去的文本。

    返回 (文本, "ok") 或 (None, 原因)。返回 None 就是**不发**，
    调用方不要试图补救 —— 拿不准的回复不发才是正确行为。
    """
    text, declined = _extract_reply(raw)
    if declined:
        return None, "model_declined"
    if not text:
        return None, "empty"

    text = _strip_noise(text)
    if not text:
        return None, "empty"

    # 关思考模式下没有 JSON 可以放 should_reply，弃权只能靠这个哨兵词。
    # 判定放在剥噪声之后：模型爱把它写成 `[SKIP]` 或 "[SKIP]"。
    if _DECLINE_RE.match(text):
        return None, "model_declined"

    for reason, pattern in _BLOCKERS:
        if pattern.search(text):
            return None, reason

    for word in policy.banned_words:
        w = (word or "").strip()
        if w and w in text:
            return None, "banned_word"

    # 格式模板自己占的字数要先从预算里扣，否则套完超出上限还是发出去了
    fmt = policy.reply_format or ""
    use_fmt = "{{" in fmt and "message" in fmt and _VAR_RE.search(fmt)
    overhead = len(render_vars(fmt, {"message": ""})) if use_fmt else 0
    budget = max(_MIN_SENTENCE, policy.max_chars - overhead)

    text = _truncate(text, budget)
    if not text:
        return None, "empty"

    final = render_vars(fmt, {"message": text}) if use_fmt else text
    final = " ".join(final.split())
    if not final:
        return None, "empty"
    return final, "ok"


def _extract_reply(raw: str) -> tuple[str, bool]:
    """从模型输出里挖出 reply 文本。返回 (文本, 是否主动弃权)。

    三级兜底：直接 JSON → 文本里抓 JSON 块 → 当纯文本用。
    最后一级是必须的：不支持 json_object 的模型会直接吐文本，
    不能因为格式不合就一条都发不出去。
    """
    s = _ZERO_WIDTH.sub("", raw or "").strip()
    if not s:
        return "", False
    s = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", s)).strip()

    for candidate in (s, (_JSON_BLOCK.search(s).group(0) if _JSON_BLOCK.search(s) else "")):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, str):
            return data.strip(), False
        if isinstance(data, dict):
            if data.get("should_reply") is False:
                return "", True
            reply = data.get("reply")
            return (reply.strip() if isinstance(reply, str) else ""), False
    return s, False


def _strip_noise(text: str) -> str:
    """剥掉围栏之外的一切装饰：markdown、角色前缀、包裹引号、多余空白。"""
    t = _MD_LINE.sub("", text)
    t = _MD_INLINE.sub("", t)
    t = " ".join(t.split())          # 换行折叠成空格：私信里多行没意义
    t = _ROLE_PREFIX.sub("", t).strip()
    for _ in range(3):               # 可能套了好几层，如 "「好的」"
        stripped = _strip_pair(t)
        if stripped == t:
            break
        t = stripped
    return _ROLE_PREFIX.sub("", t).strip()


def _strip_pair(t: str) -> str:
    if len(t) < 2:
        return t
    closer = _QUOTE_PAIRS.get(t[0])
    if closer and t.endswith(closer):
        return t[1:-1].strip()
    return t


def _truncate(text: str, limit: int) -> str:
    """超长时优先断在句末标点，断不出足够长度就硬截。

    不加省略号：私信里一句「…」看起来像没打完，比直接结束更怪。
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(ch) for ch in _SENTENCE_END)
    if cut >= _MIN_SENTENCE - 1:
        return head[:cut + 1].strip()
    return head.strip()


# ── 提示词组装 ────────────────────────────────────────────

# 单条历史消息进 prompt 前的截断长度
_HISTORY_TEXT_MAX = 120


def has_var(template: str, name: str) -> bool:
    return any(m.group(1) == name for m in _VAR_RE.finditer(template or ""))


def build_system_prompt(persona: str, knowledge: str, max_chars: int,
                        decline_policy: str = "", fewshot: str = "",
                        thinking: bool = True) -> str:
    """输出契约放最前面 —— 放结尾容易被长知识库挤出模型的注意力。

    顺序是 格式 → 人设 → 弃权策略 → 示例 → 知识：
    弃权策略紧挨着示例，两者是一组；知识库垫底，它最长也最不该抢注意力。

    thinking 决定用哪份格式契约。关思考时收不到 JSON，
    改成纯文本 + [SKIP] 哨兵 —— 契约和示例必须成对切换，
    示例里还是 JSON 的话，模型会照着示例吐 JSON，格式就对不上了。

    decline_policy / fewshot 是用户可改的。传空串就用默认值 ——
    用户把输入框清空的语义是"恢复默认"，而不是"整段不要了"：
    没有弃权策略的话，模型对借钱、要微信也会照回不误。
    """
    parts = [
        (OUTPUT_CONTRACT if thinking else OUTPUT_CONTRACT_PLAIN)
        % {"max_chars": max_chars},
        "【人设】\n" + (persona or DEFAULT_PERSONA).strip(),
        (decline_policy or "").strip() or DECLINE_POLICY,
        (fewshot or "").strip() or (FEWSHOT if thinking else FEWSHOT_PLAIN),
    ]
    if knowledge:
        parts.append(
            "【已知事实 — 只能依据这里回答；这里没有的就说不清楚，绝对不要编】\n"
            + knowledge)
    return "\n\n".join(parts)


def build_user_prompt(template: str, values: dict[str, str]) -> str:
    """渲染用户配置的提示词模板。

    模板里漏了 {{userinput}} 是常见配置错误 —— 那样模型收不到对方说了什么，
    会开始自说自话。这里补在末尾，而不是静默让它跑歪。
    """
    t = (template or DEFAULT_PROMPT_TEMPLATE).strip()
    if not has_var(t, "userinput"):
        t = (t + "\n{{userinput}}").strip()
    return render_vars(t, values).strip()


def build_history(rows: list[dict], turns: int,
                  exclude_texts: set[str] | None = None) -> list[dict]:
    """把冷备消息转成 LLM 的 messages 格式（按时间正序，只取最近 turns 条）。

    非文本消息不再整条丢掉，而是压成一个短标记。丢掉的代价实测过：
    对方发了个表情、我方分享了个视频，模型全看不见，
    于是「视频不是」这种话在它眼里没有指代对象，整段对话看着是断的。
    一个标记只占两三个 token，换回来的是完整的对话节奏。

    exclude_texts 用来剔除续火花的每日模板。那些是自动推送，不是对话 ——
    模型看到「我」反复说「晚安！陈小舟」，就会学着在上午十一点回一句晚安。
    实测踩过这个坑。
    """
    if turns <= 0:
        return []
    skip = {t.strip() for t in (exclude_texts or set()) if t and t.strip()}

    usable = []
    for m in rows:
        content = _history_text(m)
        if not content or content.strip() in skip:
            continue
        usable.append((m, content))

    out: list[dict] = []
    for m, content in usable[-turns:]:
        out.append({
            "role": "assistant" if m.get("is_me") else "user",
            "content": sanitize_user_input(content, limit=_HISTORY_TEXT_MAX),
        })
    return [m for m in out if m["content"]]


# 非文本消息在上下文里的样子。system/other 不列 —— 它们真的没有信息量。
_KIND_MARKER = {
    "image": "[图片]",
    "audio": "[语音]",
    "video": "[视频]",
    "share": "[分享]",
}


def _history_text(m: dict) -> str:
    """一条消息进上下文时的文本形态；返回空串表示不进上下文。"""
    kind = m.get("kind") or ""
    text = (m.get("text") or "").strip()
    if kind == "text" or kind == "emoji":
        # 表情的 text 本来就是「[表情]」这类可读占位符，原样用
        return text
    marker = _KIND_MARKER.get(kind)
    if not marker:
        return ""
    # 分享类带标题，标题往往就是对话在聊的东西，必须留
    return f"{marker} {text}".strip() if text else marker

