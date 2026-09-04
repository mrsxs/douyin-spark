"""
应用配置 — 从 .env 读取。
"""
import os
import secrets
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── 管理员（启动时 upsert 到 users 表） ──
    admin_username: str = "admin"
    admin_password_hash: str = ""   # bcrypt 哈希；见 `python -m app.cli hash-password`

    # ── 注册开关 ──
    # 默认关闭。拆掉授权码后，注册即拿到 DEFAULT_MAX_ACCOUNTS 个抖音号槽位
    # 和完整 API 权限 —— 面板一旦暴露到公网，陌生人注册完就能用你的服务器
    # 和 IP 去打抖音接口，风控算在你头上。自部署想开放注册再显式打开。
    allow_register: bool = False

    # ── Session ──
    secret_key: str = ""
    session_max_age: int = 30 * 24 * 3600
    cookie_secure: bool = False

    # ── 数据 ──
    data_dir: str = "./data"
    db_url: str = ""
    site_name: str = "续火花"

    # ── SMTP（可选，用于站内通知同时发邮件）──
    smtp_host:     str = ""
    smtp_port:     int = 587
    smtp_user:     str = ""
    smtp_password: str = ""
    smtp_from:     str = ""     # From 地址；为空则用 smtp_user
    smtp_tls:      bool = True  # STARTTLS (587)；465 时需自行改成 SSL

    # ── 保留现有配置（douyin_im 签名 / ddocr）──
    # 自建 ddddocr 滑块识别服务（仅短信登录用）。留空则短信登录的滑块验证不可用。
    # 在 .env 配 DOUYIN_CAPTCHA_URL，不要把真实地址写进代码默认值。
    douyin_captcha_url: str = ""
    douyin_captcha_endpoint: str = "/slide_match"
    douyin_captcha_key: str = ""
    douyin_node_bin: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


settings = Settings()

# 确保数据目录存在（放前面以便 secret_key 持久化文件落在这里）
Path(settings.data_dir).mkdir(parents=True, exist_ok=True)

# 补齐默认
if not settings.secret_key:
    # 未在 .env 配置 → 尝试从 data/.secret_key 读；没有则生成并落盘
    # （持久化比临时生成更安全：重启不会让所有用户被踢下线）
    _key_file = Path(settings.data_dir) / ".secret_key"
    if _key_file.exists():
        try:
            settings.secret_key = _key_file.read_text().strip()
        except Exception:
            pass
    if not settings.secret_key:
        settings.secret_key = secrets.token_hex(32)
        try:
            _key_file.write_text(settings.secret_key)
            _key_file.chmod(0o600)
        except Exception as _e:
            print(f"[config] 警告：SECRET_KEY 落盘失败: {_e}")
    print(f"[config] 警告：SECRET_KEY 未在 .env 配置，已使用 {_key_file}；生产环境请显式设置 SECRET_KEY")

if not settings.db_url:
    settings.db_url = f"sqlite:///{settings.data_dir}/app.db"

# 让 douyin_im.py 找到数据根（它从 env 读 DOUYIN_DATA_ROOT）
os.environ.setdefault("DOUYIN_DATA_ROOT", settings.data_dir)
# 敏感文件加密密钥（cookies / private_key 用，Fernet 格式由 douyin_im 处理）
os.environ["SAAS_CRYPT_KEY"] = settings.secret_key

# 让旧的 captcha 配置可用（douyin_im.py _config() 读 ~/.douyin_config.json）
# 写一份基于 .env 的配置到 ~/.douyin_config.json（如果用户改 .env，这里也跟新）
_CFG_FILE = os.path.expanduser("~/.douyin_config.json")
try:
    import json
    import shutil
    cfg = {
        "captcha_solver_url":      settings.douyin_captcha_url,
        "captcha_solver_endpoint": settings.douyin_captcha_endpoint,
        "captcha_solver_api_key":  settings.douyin_captcha_key,
        "node_bin":                settings.douyin_node_bin or (shutil.which("node") or "/usr/local/bin/node"),
        "default_mobile":          "",
    }
    existing = {}
    if os.path.exists(_CFG_FILE):
        try: existing = json.load(open(_CFG_FILE))
        except Exception: pass
    # 只在空/不同时覆盖
    if existing != cfg:
        json.dump(cfg, open(_CFG_FILE, "w"), ensure_ascii=False, indent=2)
except Exception:
    pass
