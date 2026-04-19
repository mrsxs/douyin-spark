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
    return {"status": s.get("status", "idle"), "error": s.get("error")}


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

        dy.qr_login(headless=True, qr_sink=qr_sink, on_logged_in=on_logged_in)
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


@router.post("/sms/send-code")
def sms_send(
    payload: dict = Body(...),
    user = Depends(require_active),
    db: Session = Depends(get_db),
):
    aid = int(payload.get("account_id") or 0)
    mobile = (payload.get("mobile") or "").strip()
    if not mobile:
        return {"ok": False, "error": "手机号不能为空"}
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == aid, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)
    try:
        set_account_ctx(AccountCtx(user.id, acc.id))
        s = dy.requests.Session()
        s.headers.update(dy.HEADERS)
        dy._prime_session(s)
        k = _key(user.id, acc.id)
        LOGIN_STATE[k] = {"sms_session": s, "mobile": mobile}
        ticket = ""
        for attempt in range(3):
            resp = dy._call_send_sms(s, mobile, ticket)
            err = (resp.get("data") or {}).get("error_code", resp.get("error_code", -1))
            if err == 0:
                return {"ok": True}
            if err in (1104, 2046, 8) or "captcha" in str(resp).lower():
                ticket = dy._handle_douyin_captcha(s)
                continue
            return {"ok": False, "error": f"发送短信失败: {resp}"}
        return {"ok": False, "error": "重试多次仍失败"}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


@router.post("/sms/verify")
def sms_verify(
    payload: dict = Body(...),
    user = Depends(require_active),
    db: Session = Depends(get_db),
):
    aid = int(payload.get("account_id") or 0)
    mobile = (payload.get("mobile") or "").strip()
    code = (payload.get("code") or "").strip()
    acc = db.query(DouyinAccount).filter(
        DouyinAccount.id == aid, DouyinAccount.user_id == user.id).first()
    if not acc:
        raise HTTPException(404)
    k = _key(user.id, acc.id)
    st = LOGIN_STATE.get(k, {})
    s = st.get("sms_session")
    if not s:
        return {"ok": False, "error": "请先发送验证码"}
    try:
        set_account_ctx(AccountCtx(user.id, acc.id))
        lr = dy._call_sms_login(s, mobile, code)
        err = (lr.get("data") or {}).get("error_code", -1)
        if err != 0:
            return {"ok": False, "error": f"登录失败: {lr}"}
        cookies = {c.name: c.value for c in s.cookies}
        dy._secure_write(str(dy.COOKIE_FILE), cookies)
        # SMS 无法拿 private_key → 后台跑 headless QR 补齐
        threading.Thread(target=_run_qr_login, args=(user.id, acc.id), daemon=True).start()
        return {"ok": True, "note": "cookies 已保存，后台补齐 init_req/私钥中"}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": str(e)}
