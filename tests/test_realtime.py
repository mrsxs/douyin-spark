"""实时消息轮询器。

抖音网页版的实时消息走 WebSocket 长连接，逆向它需要设备注册 + 心跳 +
专有鉴权，风控风险太高，所以这里用轮询 get_message_by_init（1.5MB / ~800ms）。

关键是别把请求打成筛子：
- 只有真的有人开着聊天页才轮询，没人看就完全停（风控暴露 = 0）
- 自适应间隔：刚聊完 5 秒一轮，安静下来逐步退到 30 秒
- 同一个账号只跑一个轮询线程，多人同时看共享它
"""
import pytest

from app import realtime


# ── 自适应间隔 ────────────────────────────────────────────────────

def test_starts_at_min_interval():
    assert realtime.next_interval(current=0, idle_rounds=0) == realtime.MIN_INTERVAL


def test_new_message_resets_to_fast():
    """有新消息就立刻回到最快档 —— 对话正在进行中。"""
    assert realtime.next_interval(current=30, idle_rounds=0) == realtime.MIN_INTERVAL


def test_stays_fast_while_user_is_watching():
    """核心回归：用户开着聊天页盯着看的头一分钟必须保持最快档。

    原来退避太急（5→10→20→30，静默 35 秒就固定 30 秒一轮），
    朋友回条消息最长要等 30 秒才出现，用户的体感就是「必须手动刷新」。
    """
    for idle in range(1, realtime.FAST_ROUNDS + 1):
        assert realtime.next_interval(current=realtime.MIN_INTERVAL,
                                      idle_rounds=idle) == realtime.MIN_INTERVAL, \
            f"第 {idle} 个空轮就退避了，太早"
    # 覆盖的时长至少一分钟
    assert realtime.FAST_ROUNDS * realtime.MIN_INTERVAL >= 60


def test_backs_off_after_quiet_period():
    """长时间没动静才退避，最终到 MAX。"""
    seq, interval = [], realtime.MIN_INTERVAL
    for idle in range(1, realtime.FAST_ROUNDS + 8):
        interval = realtime.next_interval(current=interval, idle_rounds=idle)
        seq.append(interval)
    assert seq == sorted(seq), f"退避不是单调递增: {seq}"
    assert seq[-1] == realtime.MAX_INTERVAL
    assert seq[0] == realtime.MIN_INTERVAL


def test_never_exceeds_max():
    assert realtime.next_interval(current=999, idle_rounds=100) == realtime.MAX_INTERVAL


def test_never_below_min():
    for idle in range(0, 40):
        assert realtime.next_interval(current=1, idle_rounds=idle) >= realtime.MIN_INTERVAL


# ── 催醒 ─────────────────────────────────────────────────────────
# 用户刚发出一条消息，多半马上就有回复 —— 这时候不该还在 30 秒的退避里睡着。

def test_wake_resets_backoff():
    sub = realtime.subscribe(user_id=1, account_id=7)
    try:
        realtime.wake(7)
        assert realtime.is_awake(7), "催醒信号没生效"
    finally:
        realtime.unsubscribe(sub)


def test_wake_on_unwatched_account_is_safe():
    """没人看的账号被催醒不能炸，也不该凭空起轮询。"""
    realtime.wake(4242)
    assert not realtime.is_watching(4242)


def test_new_subscriber_wakes_existing_watcher():
    """第二个人打开聊天页时，轮询可能正睡在 30 秒里 —— 得叫醒它。"""
    a = realtime.subscribe(user_id=1, account_id=7)
    try:
        b = realtime.subscribe(user_id=1, account_id=7)
        try:
            assert realtime.is_awake(7)
        finally:
            realtime.unsubscribe(b)
    finally:
        realtime.unsubscribe(a)


# ── 用户可选的刷新频率（聊天页里按秒设置）────────────────────────

@pytest.mark.parametrize("raw,want", [
    (5, 5), ("5", 5), ("10", 10), (30, 30), ("30.0", 30),
    (None, None), ("auto", None), ("", None),
    ("abc", None), ("'; drop--", None),          # 来自 URL 参数，乱传不能炸
    (0, realtime.MIN_ALLOWED), (1, realtime.MIN_ALLOWED),   # 太快会给抖音送风控素材
    (99999, realtime.MAX_ALLOWED), (-5, realtime.MIN_ALLOWED),
])
def test_normalize_interval(raw, want):
    assert realtime.normalize_interval(raw) == want


def test_fixed_interval_is_honored():
    for idle in (0, 5, 50, 500):
        assert realtime.next_interval(current=30, idle_rounds=idle, fixed=10) == 10


def test_fixed_interval_is_clamped():
    assert realtime.next_interval(fixed=1) == realtime.MIN_ALLOWED
    assert realtime.next_interval(fixed=99999) == realtime.MAX_ALLOWED


def test_auto_is_the_default():
    assert realtime.next_interval(current=0, idle_rounds=0) == \
        realtime.next_interval(current=0, idle_rounds=0, fixed=None)


def test_interval_is_remembered_on_subscription():
    sub = realtime.subscribe(user_id=1, account_id=7, interval=30)
    try:
        assert realtime.effective_interval(7) == 30
    finally:
        realtime.unsubscribe(sub)


def test_fastest_subscriber_wins():
    """多人共享一份轮询，按慢的来会让选快的那个人体验退化。"""
    slow = realtime.subscribe(user_id=1, account_id=7, interval=60)
    fast = realtime.subscribe(user_id=1, account_id=7, interval=5)
    try:
        assert realtime.effective_interval(7) == 5
    finally:
        realtime.unsubscribe(fast)
        assert realtime.effective_interval(7) == 60, "选快的走了应回落到慢档"
        realtime.unsubscribe(slow)


def test_auto_subscriber_does_not_slow_others():
    """自适应的人不该把明确要求 5 秒的人拖慢。"""
    auto = realtime.subscribe(user_id=1, account_id=7)
    fast = realtime.subscribe(user_id=1, account_id=7, interval=5)
    try:
        assert realtime.effective_interval(7) == 5
    finally:
        realtime.unsubscribe(auto)
        realtime.unsubscribe(fast)


def test_effective_interval_without_watcher():
    assert realtime.effective_interval(4242) is None
