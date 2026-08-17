"""实时消息轮询 + 分发。

抖音网页版的实时消息走 WebSocket 长连接（frontier），逆向它要设备注册、
心跳、专有鉴权，风控风险高且工作量大 —— 这里改用轮询
get_message_by_init（1.5MB / ~800ms 一次）。

代价是请求量，所以三条约束必须守住：
1. 只有真的有人开着聊天页才轮询，最后一个人关掉立刻停 —— 没人看时对抖音
   的额外请求为 0，和加这个功能之前完全一样。
2. 自适应间隔：刚聊完 5 秒一轮，安静下来逐步退到 30 秒。
3. 同一个账号只跑一个轮询线程，多人同时看共享同一份结果。
"""
from __future__ import annotations

import itertools
import queue
import threading

MIN_INTERVAL = 5          # 秒，对话进行中
MAX_INTERVAL = 30         # 秒，长时间没动静
# 头 12 个空轮（约 1 分钟）保持最快档。
# Why: 用户会打开聊天页盯着等回复，这段时间必须跟手。
# 原来 5→10→20→30 退避太急，静默 35 秒就固定 30 秒一轮，
# 朋友回条消息最长等 30 秒才出现，体感就是「必须手动刷新」。
FAST_ROUNDS = 12
QUEUE_MAX = 200           # 单个订阅者的积压上限，超了丢最旧的
# 连续这么多次拉取失败就停掉轮询（多半是 cookies 失效，再打也是白打）
MAX_CONSECUTIVE_ERRORS = 10

# 用户可以在聊天页直接指定「几秒一轮」。给个下限是因为一次 init 就是
# 1.5MB/800ms，1 秒一轮既跑不完也在给抖音送风控素材。
MIN_ALLOWED = 3
MAX_ALLOWED = 300
AUTO = "auto"


def normalize_interval(value) -> int | None:
    """把用户传的刷新间隔收敛成秒数；auto / 非法值返回 None（走自适应）。"""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", AUTO):
        return None
    try:
        seconds = int(float(text))
    except (TypeError, ValueError):
        return None
    return max(MIN_ALLOWED, min(MAX_ALLOWED, seconds))

_LOCK = threading.RLock()
_WATCHERS: dict[int, "_Watcher"] = {}
_SUB_IDS = itertools.count(1)


def next_interval(current: int = 0, idle_rounds: int = 0,
                  fixed: int | None = None) -> int:
    """算下一次轮询间隔（秒）。

    fixed 是用户在聊天页选的固定秒数；None 表示自适应：
    先保持一段快档（用户正盯着等回复），确认没人说话了才指数退避。
    current 作为下限，避免退避过程中忽大忽小。
    """
    if fixed:
        return max(MIN_ALLOWED, min(MAX_ALLOWED, int(fixed)))
    if idle_rounds <= 0 or idle_rounds <= FAST_ROUNDS:
        return MIN_INTERVAL
    steps = idle_rounds - FAST_ROUNDS
    target = MIN_INTERVAL * (2 ** (steps - 1))
    return int(min(MAX_INTERVAL, max(MIN_INTERVAL, current or 0, target)))


class Subscription:
    """一个浏览器连接。内部是有界队列，客户端读不动就丢最旧的，
    不能让一个卡住的页面把内存吃穿、也不能拖住其它订阅者。

    system=True 是 AI 自动回复用的「常驻订阅」：它不对应任何浏览器，
    只是为了让轮询在没人开聊天页时也继续跑。它不收消息（broadcast 跳过它），
    否则队列会被永远没人消费的消息填满、白白搬运一遍。
    """

    def __init__(self, account_id: int, interval: int | None = None,
                 system: bool = False):
        self.id = next(_SUB_IDS)
        self.account_id = account_id
        self.interval = normalize_interval(interval)
        self.system = system
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)

    def put(self, item) -> None:
        try:
            self._q.put_nowait(item)
        except queue.Full:
            try:
                self._q.get_nowait()          # 丢最旧的，给新消息腾位
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(item)
            except queue.Full:
                pass

    def get_nowait(self):
        return self._q.get_nowait()

    def empty(self) -> bool:
        return self._q.empty()

    def qsize(self) -> int:
        return self._q.qsize()


class _Watcher:
    def __init__(self, user_id: int, account_id: int):
        self.user_id = user_id
        self.account_id = account_id
        self.subs: dict[int, Subscription] = {}
        self.stop = threading.Event()
        # 被 set 时轮询立刻醒来并重置退避（用户刚发消息 / 刚打开会话）
        self.wake_ev = threading.Event()
        self.thread: threading.Thread | None = None


def subscribe(user_id: int, account_id: int,
              interval: int | None = None,
              system: bool = False) -> Subscription:
    """登记一个浏览器连接；首个订阅者会拉起轮询线程。

    interval 是这个连接要求的刷新秒数，None 表示自适应。
    system=True 见 Subscription 的说明（AI 自动回复的常驻订阅）。
    """
    sub = Subscription(account_id, interval, system=system)
    with _LOCK:
        w = _WATCHERS.get(account_id)
        if w is None:
            w = _Watcher(user_id, account_id)
            _WATCHERS[account_id] = w
        w.subs[sub.id] = sub
        if w.thread is None or not w.thread.is_alive():
            w.stop.clear()
            w.thread = threading.Thread(
                target=_poll_loop, args=(w,), daemon=True,
                name=f"rt-poll-{account_id}")
            w.thread.start()
        else:
            # 已有轮询可能正睡在退避里，新来的人不该干等
            w.wake_ev.set()
    return sub


def effective_interval(account_id: int) -> int | None:
    """当前生效的刷新秒数 = 所有订阅者里要求最快的那个；都没指定则 None（自适应）。

    多人共享同一份轮询结果，按最慢的来会让要求快的那个人体验退化。

    有人真在看时，只按浏览器订阅算 —— 常驻订阅那个 30 秒是「没人看时的省电档」，
    要是也参与 min()，用户在聊天页选了「自动」反而会被拖成 30 秒一轮。
    """
    with _LOCK:
        w = _WATCHERS.get(account_id)
        if not w or not w.subs:
            return None
        subs = list(w.subs.values())
        humans = [s for s in subs if not s.system]
        wanted = [s.interval for s in (humans or subs) if s.interval]
        return min(wanted) if wanted else None


# ── AI 自动回复的常驻订阅 ─────────────────────────────────

# account_id → 常驻订阅。进程内状态，重启后由 ai_worker.resume_watchers 重建。
_SYSTEM_SUBS: dict[int, Subscription] = {}


def ensure_system_watch(user_id: int, account_id: int,
                        interval: int | None = None) -> Subscription:
    """让这个账号保持轮询，哪怕没人开聊天页。

    自动回复要 24 小时在线，而轮询默认是「最后一个浏览器关掉就停」。
    幂等：重复调用不会叠加订阅；interval 变了则换一个新的。
    """
    with _LOCK:
        old = _SYSTEM_SUBS.get(account_id)
        if old is not None and old.interval == normalize_interval(interval):
            return old
    if old is not None:
        unsubscribe(old)
    sub = subscribe(user_id, account_id, interval, system=True)
    with _LOCK:
        _SYSTEM_SUBS[account_id] = sub
    return sub


def drop_system_watch(account_id: int) -> None:
    """撤掉常驻订阅。还有人开着聊天页的话轮询继续，只是不再 24h 常驻。"""
    with _LOCK:
        sub = _SYSTEM_SUBS.pop(account_id, None)
    if sub is not None:
        unsubscribe(sub)


def has_system_watch(account_id: int) -> bool:
    with _LOCK:
        return account_id in _SYSTEM_SUBS


def _feed_ai(user_id: int, account_id: int, messages: list[dict]) -> None:
    """把新消息交给 AI 自动回复。

    只入队，实际的 LLM 调用和发送在 ai_worker 自己的线程里做 ——
    在这里同步跑的话，一次十几秒的模型调用会把整条轮询链路卡住，
    所有开着聊天页的人都会觉得消息延迟了。
    自动回复出任何问题都不该影响实时消息，所以整体兜异常。
    """
    try:
        from . import ai_worker
        ai_worker.on_new_messages(user_id, account_id, messages)
    except Exception as e:
        print(f"[realtime] acc#{account_id} 投递 AI 自动回复失败: {e}")


def wake(account_id: int) -> None:
    """催一次轮询并重置退避。

    用户刚发出消息、刚打开会话时调用 —— 这些时刻多半马上有回复，
    不该还睡在 30 秒的退避里。没人看的账号调用无副作用（不会凭空起轮询）。
    """
    with _LOCK:
        w = _WATCHERS.get(account_id)
        if w:
            w.wake_ev.set()


def is_awake(account_id: int) -> bool:
    """催醒信号是否待处理（测试用）。"""
    with _LOCK:
        w = _WATCHERS.get(account_id)
        return bool(w and w.wake_ev.is_set())


def unsubscribe(sub: Subscription) -> None:
    """注销连接；最后一个人走了就停轮询。重复调用安全。"""
    with _LOCK:
        w = _WATCHERS.get(sub.account_id)
        if not w:
            return
        w.subs.pop(sub.id, None)
        if not w.subs:
            w.stop.set()
            w.wake_ev.set()          # 一并叫醒，别等退避睡满才退出
            _WATCHERS.pop(sub.account_id, None)


def is_watching(account_id: int) -> bool:
    with _LOCK:
        return account_id in _WATCHERS


def watcher_count(account_id: int) -> int:
    with _LOCK:
        w = _WATCHERS.get(account_id)
        return len(w.subs) if w else 0


def broadcast(account_id: int, messages: list[dict]) -> None:
    """把新消息推给该账号的所有订阅者。

    严格按 account_id 分发 —— 串号就是把别人的私聊推给了不该看的人。
    常驻订阅（system）跳过：它没有消费者，推给它只会填满队列再被丢掉。
    """
    if not messages:
        return
    with _LOCK:
        w = _WATCHERS.get(account_id)
        subs = [s for s in w.subs.values() if not s.system] if w else []
    for s in subs:
        s.put(messages)


def shutdown_all() -> None:
    """停掉所有轮询（测试清理 / 进程退出用）。"""
    with _LOCK:
        watchers = list(_WATCHERS.values())
        _WATCHERS.clear()
        _SYSTEM_SUBS.clear()
    for w in watchers:
        w.stop.set()


def _poll_loop(w: _Watcher) -> None:
    """轮询线程主体。任何异常都不能让线程死掉 —— 死了就再也没有实时了。"""
    from . import trigger

    idle_rounds = 0
    interval = MIN_INTERVAL
    errors = 0
    while not w.stop.is_set():
        try:
            new = trigger.poll_new_messages(w.user_id, w.account_id)
            errors = 0
            if new:
                idle_rounds = 0
                broadcast(w.account_id, new)
                _feed_ai(w.user_id, w.account_id, new)
            else:
                idle_rounds += 1
        except Exception as e:
            errors += 1
            idle_rounds += 1
            # 只在第一次和放弃时说话，否则日志会被刷屏
            if errors == 1:
                print(f"[realtime] acc#{w.account_id} 轮询失败: {e}")
            if errors >= MAX_CONSECUTIVE_ERRORS:
                print(f"[realtime] acc#{w.account_id} 连续 {errors} 次失败，停止轮询")
                break

        interval = next_interval(current=interval, idle_rounds=idle_rounds,
                                 fixed=effective_interval(w.account_id))
        # 睡在 wake 上而不是 stop 上：用户发消息 / 新人加入能立刻把它叫醒，
        # 不用等这一轮退避睡满。停止时也会 set 它，所以退出照样及时。
        woke = w.wake_ev.wait(interval)
        if w.stop.is_set():
            break
        if woke:
            w.wake_ev.clear()
            idle_rounds = 0          # 被催醒 = 预期马上有消息，回到最快档
            interval = MIN_INTERVAL

    with _LOCK:
        if _WATCHERS.get(w.account_id) is w and not w.subs:
            _WATCHERS.pop(w.account_id, None)
