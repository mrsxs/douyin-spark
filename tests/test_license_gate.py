"""License 启动闸门 + 运行时断言。

背景：验签原本只在未编译的 run.py 里做，于是有两条白嫖路径 ——
  docker run -e SKIP_LICENSE_CHECK=1 <image>
  docker run <image> uvicorn app.main:app     # 覆盖 CMD，绕开 run.py
而镜像在 Docker Hub 是公开的（见 obsidian-vault/00-总览.md）。

现在：
  - license_gate() 是统一入口，run.py 和 app/main.py 的 lifespan 都走它
  - SKIP_LICENSE_CHECK 只在源码态有效；镜像构建时把 _ALLOW_SKIP_LICENSE
    改成 False 再 cythonize 进 .so，客户改不了
  - assert_licensed() 挂在每请求必经的 csrf_mw（已编译）里，
    即使有人改了未编译的 run.py / app/main.py 也拿不到服务
"""
import base64
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app import license as lic

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub_pem


@pytest.fixture(autouse=True)
def _reset(keypair, monkeypatch):
    """每个用例都从「未验证」状态开始，并用测试公钥。"""
    _, pub_pem = keypair
    monkeypatch.setattr(lic, "_PUBLIC_KEY_PEM", pub_pem)
    monkeypatch.setattr(lic, "_LICENSE_OK", False)


def _issue(priv, days=365, machine=None):
    payload = {
        "issued_at": datetime.utcnow().isoformat(),
        "expires_at": (datetime.utcnow() + timedelta(days=days)).isoformat(),
        "tier": "pro", "machine": machine, "note": "t",
    }
    raw = json.dumps(payload).encode()
    sig = priv.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(raw).decode() + "." + base64.b64encode(sig).decode()


# ── 开发态：允许跳过 ──────────────────────────────────────────────

def test_dev_mode_can_skip(monkeypatch):
    monkeypatch.setattr(lic, "_ALLOW_SKIP_LICENSE", True)
    monkeypatch.setenv("SKIP_LICENSE_CHECK", "1")
    assert lic.license_gate() is None
    lic.assert_licensed()          # 不抛


def test_dev_mode_without_skip_still_verifies(keypair, monkeypatch):
    priv, _ = keypair
    monkeypatch.setattr(lic, "_ALLOW_SKIP_LICENSE", True)
    monkeypatch.delenv("SKIP_LICENSE_CHECK", raising=False)
    monkeypatch.setenv("LICENSE_KEY", _issue(priv))
    assert lic.license_gate()["tier"] == "pro"


# ── 镜像态：SKIP 必须失效 ────────────────────────────────────────

@pytest.mark.parametrize("skip_val", ["1", "true", "True", "yes"])
def test_release_build_ignores_skip_env(monkeypatch, skip_val):
    """核心回归：正式镜像里 SKIP_LICENSE_CHECK 不能白嫖。"""
    monkeypatch.setattr(lic, "_ALLOW_SKIP_LICENSE", False)
    monkeypatch.setenv("SKIP_LICENSE_CHECK", skip_val)
    monkeypatch.delenv("LICENSE_KEY", raising=False)
    with pytest.raises(SystemExit):
        lic.license_gate()


def test_release_build_with_valid_license_passes(keypair, monkeypatch):
    priv, _ = keypair
    monkeypatch.setattr(lic, "_ALLOW_SKIP_LICENSE", False)
    monkeypatch.setenv("SKIP_LICENSE_CHECK", "1")
    monkeypatch.setenv("LICENSE_KEY", _issue(priv))
    assert lic.license_gate()["tier"] == "pro"
    lic.assert_licensed()


# ── 运行时断言：改掉明文入口也没用 ──────────────────────────────

def test_assert_licensed_blocks_before_gate():
    with pytest.raises(RuntimeError, match="License"):
        lic.assert_licensed()


def test_assert_licensed_allows_after_gate(keypair, monkeypatch):
    priv, _ = keypair
    monkeypatch.setattr(lic, "_ALLOW_SKIP_LICENSE", False)
    monkeypatch.setenv("LICENSE_KEY", _issue(priv))
    lic.license_gate()
    lic.assert_licensed()


# ── 构建期替换的防呆 ─────────────────────────────────────────────

def test_build_flag_marker_exists_in_source():
    """Dockerfile 靠 sed 把这行改成 False。

    sed 匹配不到时不会报错，会静默留下 True —— 保护直接失效且无人察觉。
    所以这里锁住标记行的确切形状；改它必须同步改 Dockerfile。
    """
    src = (ROOT / "app" / "license.py").read_text()
    assert re.search(r"^_ALLOW_SKIP_LICENSE = True\s*#", src, re.M), (
        "app/license.py 里的 _ALLOW_SKIP_LICENSE 标记行变了，"
        "Dockerfile 的 sed 会静默失效"
    )


def test_dockerfile_flips_flag_and_asserts():
    """Dockerfile 必须替换该标记，且替换后要自我校验。"""
    df = (ROOT / "Dockerfile").read_text()
    assert "_ALLOW_SKIP_LICENSE" in df, "Dockerfile 没有关闭 SKIP_LICENSE_CHECK"
    assert "_ALLOW_SKIP_LICENSE = False" in df, "Dockerfile 未断言替换结果"
