"""SSE 端到端投递。

单测过了不代表浏览器收得到 —— 订阅登记、事件格式、连接保持
任何一环断了，用户看到的就是「必须手动刷新」。
这个用例真的连上 /stream，从外部 broadcast，断言字节流里收得到。

读取放在独立线程里并设硬超时：SSE 是无限流，直接迭代会把测试挂死。
"""
import json
import queue
import threading
import time

import pytest

from app import realtime


@pytest.fixture(autouse=True)
def _no_real_polling(monkeypatch):
    """别真去打抖音；只测分发链路。"""
    from app import trigger
    monkeypatch.setattr(trigger, "poll_new_messages", lambda *a, **k: [])
    realtime.shutdown_all()
    yield
    realtime.shutdown_all()


def _collect_stream(client, url, want_event, timeout=15):
    """连上 SSE，最多读 timeout 秒，返回收到的原始文本。

    单独开线程读 —— 流不会自己结束，主线程直接迭代会永远卡住。
    """
    out: queue.Queue = queue.Queue()

    def reader():
        buf = ""
        try:
            with client.stream("GET", url) as r:
                out.put(("meta", r.status_code, dict(r.headers)))
                for chunk in r.iter_text():
                    buf += chunk
                    if want_event in buf:
                        break
        except Exception as e:                       # noqa: BLE001
            out.put(("error", repr(e), {}))
        finally:
            out.put(("body", buf, {}))

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    meta, body, err = None, "", None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            kind, val, extra = out.get(timeout=0.2)
        except queue.Empty:
            continue
        if kind == "meta":
            meta = (val, extra)
        elif kind == "error":
            err = val
        else:
            body = val
            break
    return meta, body, err


def _broadcast_when_ready(account_id, payload, timeout=10.0):
    """等订阅登记好再广播 —— 广播早于订阅的话消息会丢，测出来是假阴性。"""
    def worker():
        deadline = time.time() + timeout
        while time.time() < deadline:
            if realtime.watcher_count(account_id) > 0:
                time.sleep(0.1)
                realtime.broadcast(account_id, payload)
                return
            time.sleep(0.02)
    threading.Thread(target=worker, daemon=True).start()


def test_sse_delivers_broadcast_to_browser(active_user):
    """核心：服务端 broadcast 的消息必须真的成帧发出去。

    这里直接消费端点返回的 body_iterator，不走 TestClient ——
    它的 ASGI transport 会把整个响应缓冲完才返回，对无限 SSE 流等于永远不返回
    （真实服务器是增量推送的，已用 curl 验证过）。
    """
    import asyncio

    from app.routers.api import api_messages_stream

    user, acc = active_user
    payload = [{"server_msg_id": 4242, "peer_uid": "999", "is_me": False,
                "kind": "text", "text": "在吗", "created_at": 1700000000000}]

    class _Req:
        async def is_disconnected(self):
            return False

    async def run():
        resp = await api_messages_stream(acc.id, _Req(), "auto", user)
        assert resp.media_type == "text/event-stream"
        assert resp.headers.get("x-accel-buffering") == "no"
        it = resp.body_iterator
        chunks = [await asyncio.wait_for(it.__anext__(), 5)]

        realtime.broadcast(acc.id, payload)
        deadline = time.time() + 10
        while time.time() < deadline:
            chunk = await asyncio.wait_for(it.__anext__(), 5)
            chunks.append(chunk)
            if "event: messages" in chunk:
                break
        await it.aclose()
        return "".join(chunks)

    body = asyncio.run(run())

    assert ": connected" in body, f"连上了但什么都没发: {body!r}"
    assert "event: messages" in body, \
        f"SSE 没把 broadcast 的消息发出来（用户只能手动刷新）: {body!r}"
    data = [ln for ln in body.split("\n") if ln.startswith("data: ")][0]
    got = json.loads(data[len("data: "):])
    assert got[0]["text"] == "在吗"
    assert got[0]["server_msg_id"] == 4242


def test_sse_unsubscribes_when_generator_closes(active_user):
    """浏览器一走就要注销，否则没人看还在给抖音打请求。"""
    import asyncio

    from app.routers.api import api_messages_stream

    user, acc = active_user

    class _Req:
        async def is_disconnected(self):
            return False

    async def run():
        resp = await api_messages_stream(acc.id, _Req(), "auto", user)
        it = resp.body_iterator
        await asyncio.wait_for(it.__anext__(), 5)
        watching = realtime.is_watching(acc.id)
        await it.aclose()
        return watching

    assert asyncio.run(run()) is True, "连上时没登记订阅"
    deadline = time.time() + 5
    while time.time() < deadline and realtime.is_watching(acc.id):
        time.sleep(0.1)
    assert not realtime.is_watching(acc.id), "断开后轮询没停"


def test_sse_honors_requested_interval(active_user, login):
    """聊天页选的刷新秒数要真的传到服务端。"""
    user, acc = active_user
    c = login(user)

    seen = {}

    def watch():
        deadline = time.time() + 8
        while time.time() < deadline:
            if realtime.watcher_count(acc.id) > 0:
                seen["iv"] = realtime.effective_interval(acc.id)
                realtime.broadcast(acc.id, [{"server_msg_id": 1, "peer_uid": "9",
                                             "is_me": False, "kind": "text",
                                             "text": "x", "created_at": 1}])
                return
            time.sleep(0.02)
    threading.Thread(target=watch, daemon=True).start()

    _collect_stream(c, f"/api/messages/{acc.id}/stream?interval=30",
                    "event: messages")
    assert seen.get("iv") == 30


def test_sse_rejects_bogus_interval(active_user, login):
    """刷新秒数来自 URL 参数，乱传不能让轮询乱掉。"""
    user, acc = active_user
    c = login(user)
    seen = {}

    def watch():
        deadline = time.time() + 8
        while time.time() < deadline:
            if realtime.watcher_count(acc.id) > 0:
                seen["iv"] = realtime.effective_interval(acc.id)
                realtime.broadcast(acc.id, [{"server_msg_id": 1, "peer_uid": "9",
                                             "is_me": False, "kind": "text",
                                             "text": "x", "created_at": 1}])
                return
            time.sleep(0.02)
    threading.Thread(target=watch, daemon=True).start()

    _collect_stream(c, f"/api/messages/{acc.id}/stream?interval=turbo%27--",
                    "event: messages")
    assert seen.get("iv") is None, "非法值应回落到自适应"
