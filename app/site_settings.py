"""站点级配置：对外域名。

存在 AppSetting KV 表里（和 SMTP 同一套机制）。

- site_url  对外访问地址，分享卡二维码指向它
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from .models import AppSetting

SETTING_KEY = "site"

DEFAULTS = {
    "site_url": "",
}


def _validate_url(raw: str, field: str) -> str:
    """只放行 http(s)。

    后台配置会直接进模板的 href —— 放任 javascript: 等于开了个 XSS 入口。
    """
    v = (raw or "").strip().rstrip("/")
    if not v:
        return ""
    parsed = urlparse(v)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{field} 必须是 http:// 或 https:// 开头的完整地址")
    return v


def load(db: Session) -> dict:
    row = db.get(AppSetting, SETTING_KEY)
    cfg = dict(DEFAULTS)
    if row and row.value:
        try:
            cfg.update(json.loads(row.value))
        except Exception:
            pass
    return cfg


def save(db: Session, cfg: dict, admin_id: int | None = None) -> dict:
    """写入配置。URL 非法时抛 ValueError。不 commit，由调用方决定事务边界。"""
    merged = load(db)
    if "site_url" in cfg:
        merged["site_url"] = _validate_url(cfg["site_url"], "站点域名")

    row = db.get(AppSetting, SETTING_KEY)
    if not row:
        row = AppSetting(key=SETTING_KEY, value=json.dumps(merged, ensure_ascii=False),
                         updated_by=admin_id)
        db.add(row)
    else:
        row.value = json.dumps(merged, ensure_ascii=False)
        row.updated_by = admin_id
    _QR_CACHE.pop("url", None)      # 域名变了，二维码要重生成
    return merged


# 二维码是纯函数结果，同一域名不必重复生成
_QR_CACHE: dict = {}


def qr_data_url(site_url: str) -> str:
    """把站点域名渲染成 data:image/svg+xml 二维码，直接嵌进分享卡。

    服务端生成而不是引 CDN 的 JS 库：分享卡要用 html2canvas 截图，
    外链图片会带跨域污染 canvas，导致「保存图片」直接失败。
    用 SVG 而不是 PNG：qrcode 生成 PNG 需要 Pillow，SVG 后端零额外依赖。
    """
    url = (site_url or "").strip()
    if not url:
        return ""
    if _QR_CACHE.get("url") == url:
        return _QR_CACHE.get("data", "")
    try:
        import base64
        import io

        import qrcode
        import qrcode.image.svg

        qr = qrcode.QRCode(box_size=10, border=1,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        buf = io.BytesIO()
        qr.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(buf)
        data = ("data:image/svg+xml;base64,"
                + base64.b64encode(buf.getvalue()).decode())
        _QR_CACHE.update(url=url, data=data)
        return data
    except Exception as e:
        print(f"[site] 生成二维码失败: {e}")
        return ""
