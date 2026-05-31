"""
QR 扫码 / 短信登录流程（Playwright）
流程：
  POST /api/login/qr/start  {account_id}  → 启动 headless chromium，后端返回二维码
  GET  /api/login/qr/status?account_id=X  → 轮询状态
  POST /api/login/sms/send-code            → 纯 HTTP 短信
  POST /api/login/sms/verify               → 提交验证码
"""
import json
import os
import threading
import traceback
import time
from datetime import datetime
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db, SessionLocal
from ..models import DouyinAccount, AuditLog
from ..deps import require_active
from ..storage import AccountCtx, set_account_ctx
import douyin_im as dy

router = APIRouter(prefix="/api/login")

# 每个 (user_id, account_id) 的登录任务状态
LOGIN_STATE: dict[tuple[int, int], dict] = {}
_lock = threading.Lock()


def _key(user_id, account_id):
    return (int(user_id), int(account_id))


@router.post("/qr/start")
def qr_start(
    payload: dict = Body(...),
    user = Depends(require_active),
    db: Session = Depends(get_db),
):
    aid = int(payload.get("account_id") or 0)
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == aid, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)

    k = _key(user.id, acc.id)
    with _lock:
        existing = LOGIN_STATE.get(k)
        if existing and existing.get("status") in ("waiting_qr", "waiting_scan"):
            pass
        else:
            LOGIN_STATE[k] = {"status": "waiting_qr"}
            threading.Thread(target=_run_qr_login,
                             args=(user.id, acc.id), daemon=True).start()

    # 等拿到二维码（最多 25s）
    deadline = time.time() + 25
    while time.time() < deadline:
        s = LOGIN_STATE.get(k, {})
        if s.get("qr_b64"):
            return {"ok": True, "qr_b64": s["qr_b64"]}
        if s.get("status") == "failed":
            return {"ok": False, "error": s.get("error") or "启动失败"}
        time.sleep(0.3)
    return {"ok": False, "error": "获取二维码超时"}


@router.get("/qr/status")
def qr_status(
    account_id: int,
    user = Depends(require_active),
    db: Session = Depends(get_db),
):
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == account_id, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)
    k = _key(user.id, acc.id)
    s = LOGIN_STATE.get(k, {})
    return {"status": s.get("status", "idle"), "error": s.get("error"),
            "masked_phone": s.get("masked_phone", "")}


@router.post("/qr/submit-code")
def qr_submit_code(
    payload: dict = Body(...),
    user = Depends(require_active),
    db: Session = Depends(get_db),
):
    """扫码二次验证：前端提交短信验证码，交给后台登录线程的 code_provider。"""
    aid = int(payload.get("account_id") or 0)
    code = (payload.get("code") or "").strip()
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == aid, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)
    if not code:
        return {"ok": False, "error": "验证码不能为空"}
    k = _key(user.id, acc.id)
    st = LOGIN_STATE.get(k)
    if not st or st.get("status") != "waiting_sms_code":
        return {"ok": False, "error": "当前不在等待验证码状态（可能已超时，请重新扫码）"}
    st["sms_code"] = code
    st["status"] = "verifying"
    return {"ok": True}


def _run_qr_login(user_id: int, account_id: int):
    """后台线程：调 douyin_im.qr_login（headless Chromium）

    流程：
      1. 拿到二维码 → status=waiting_scan
      2. 拿到 sessionid → status=success + DB 标 active（前端此时就能跳转）
      3. 继续跑 init_req / 私钥抓取 → status=post_login_finalizing → finalized
    """
    k = _key(user_id, account_id)
    st = LOGIN_STATE.setdefault(k, {})
    set_account_ctx(AccountCtx(user_id, account_id))
    try:
        def qr_sink(b64, url):
            st["qr_b64"] = b64
            st["qr_url"] = url
            st["status"] = "waiting_scan"

        def on_logged_in(_cookies):
            # sessionid 已拿到 + cookies 已预落盘；立即让前端跳转
            st["status"] = "success"
            try:
                with SessionLocal() as db:
                    acc = db.get(DouyinAccount, account_id)
                    if acc:
                        acc.status = "active"
                        acc.cookies_exist = True
                        acc.last_login_at = datetime.utcnow()
                        db.add(AuditLog(actor_user_id=user_id, actor_kind="user",
                                        action="login_qr", target_type="account",
                                        target_id=str(account_id)))
                        db.commit()
            except Exception as _e:
                traceback.print_exc()
            # 标记进入尾段（仅用于诊断/状态展示）
            st["status"] = "post_login_finalizing"
            # 前端依然把 post_login_finalizing 视作成功已发生 → 保留跳转
            # 为兼容前端 "=== 'success'" 判断，同时保留 success 标志
            st["finalizing"] = True
            st["status"] = "success"  # 前端轮询拿到 success 即跳转

        def verify_sink(masked_phone):
            # 扫码后需二次验证：让前端弹出短信验证码输入
            st["status"] = "waiting_sms_code"
            st["masked_phone"] = masked_phone or ""
            st.pop("sms_code", None)

        def code_provider():
            # 阻塞等前端提交验证码，最多 180s
            waited = 0.0
            while waited < 180:
                code = st.get("sms_code")
                if code:
                    st.pop("sms_code", None)
                    st["status"] = "verifying"
                    return code
                time.sleep(1); waited += 1
            return None

        dy.qr_login(headless=True, qr_sink=qr_sink, on_logged_in=on_logged_in,
                    verify_sink=verify_sink, code_provider=code_provider)
        # qr_login 全部跑完（init_req/私钥/昵称抓取都完成）
        st["finalizing"] = False
        st["status"] = "success"  # 幂等
    except Exception as e:
        # 如果回调已标 success，这里降级为警告（尾段失败不影响登录本身可用）
        if st.get("status") == "success":
            st["finalize_error"] = str(e)
            print(f"  [warn] 登录成功但尾段补齐失败: {e}")
        else:
            st["status"] = "failed"
            st["error"]  = str(e)
        traceback.print_exc()


def _run_sms_login(user_id: int, account_id: int, mobile: str):
    """后台线程：Playwright 驱动短信登录（避开纯 HTTP 的 msToken/滑块问题）。
    流程同扫码：发码 → 前端填码（/sms/verify 写入 sms_code）→ 自动登录 → 抓 init_req/私钥。"""
    k = _key(user_id, account_id)
    st = LOGIN_STATE.setdefault(k, {})
    st.clear()
    st["status"] = "sms_starting"
    set_account_ctx(AccountCtx(user_id, account_id))

    def send_sink():
        st["status"] = "waiting_sms_code"
        st["masked_phone"] = (mobile[:3] + "****" + mobile[-2:]) if len(mobile) >= 5 else mobile
        st.pop("sms_code", None)

    def code_provider():
        waited = 0.0
        while waited < 180:
            code = st.get("sms_code")
            if code:
                st.pop("sms_code", None)
                st["status"] = "verifying"
                return code
            time.sleep(1); waited += 1
        return None

    def on_logged_in(_cookies):
        st["status"] = "success"
        try:
            with SessionLocal() as db:
                acc = db.get(DouyinAccount, account_id)
                if acc:
                    acc.status = "active"
                    acc.cookies_exist = True
                    acc.last_login_at = datetime.utcnow()
                    db.add(AuditLog(actor_user_id=user_id, actor_kind="user",
                                    action="login_sms", target_type="account",
                                    target_id=str(account_id)))
                    db.commit()
        except Exception:
            traceback.print_exc()
        st["status"] = "success"

    try:
        dy.sms_login_browser(mobile, headless=True, send_sink=send_sink,
                             code_provider=code_provider, on_logged_in=on_logged_in)
        st["status"] = "success"
    except Exception as e:
        if st.get("status") == "success":
            st["finalize_error"] = str(e)
            print(f"  [warn] 短信登录成功但尾段补齐失败: {e}")
        else:
            st["status"] = "failed"
            st["error"] = str(e)
        traceback.print_exc()


@router.post("/sms/send-code")
def sms_send(
    payload: dict = Body(...),
    user = Depends(require_active),
    db: Session = Depends(get_db),
):
    """启动 Playwright 短信登录：开浏览器发码。前端随后轮询 /qr/status，
    status=waiting_sms_code 时显示验证码输入，提交走 /sms/verify。"""
    aid = int(payload.get("account_id") or 0)
    mobile = (payload.get("mobile") or "").strip()
    if not mobile:
        return {"ok": False, "error": "手机号不能为空"}
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == aid, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)
    k = _key(user.id, acc.id)
    existing = LOGIN_STATE.get(k)
    if not (existing and existing.get("status") in ("sms_starting", "waiting_sms_code", "verifying")):
        threading.Thread(target=_run_sms_login, args=(user.id, acc.id, mobile), daemon=True).start()
    return {"ok": True}


@router.post("/sms/verify")
def sms_verify(
    payload: dict = Body(...),
    user = Depends(require_active),
    db: Session = Depends(get_db),
):
    """提交短信验证码：交给后台登录线程的 code_provider 完成登录。"""
    aid = int(payload.get("account_id") or 0)
    code = (payload.get("code") or "").strip()
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == aid, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)
    if not code:
        return {"ok": False, "error": "验证码不能为空"}
    k = _key(user.id, acc.id)
    st = LOGIN_STATE.get(k)
    if not st or st.get("status") != "waiting_sms_code":
        return {"ok": False, "error": "当前不在等待验证码状态（请先发送验证码）"}
    st["sms_code"] = code
    st["status"] = "verifying"
    return {"ok": True}
