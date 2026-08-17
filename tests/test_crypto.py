"""字段级加密不能静默降级成明文。

背景：crypto.py 在拿不到 SAAS_CRYPT_KEY 时直接 `return plain`，
注释写着「会有日志告警」但代码里根本没有告警 —— SMTP 密码等敏感字段
就这么明文进了库，而且没有任何人会知道。
"""
import pytest

from app import crypto


@pytest.fixture(autouse=True)
def _reset_warn_state():
    crypto._WARNED = False
    yield
    crypto._WARNED = False


# ── 正常加密路径 ─────────────────────────────────────────────────

def test_roundtrip_with_key(monkeypatch):
    monkeypatch.setenv("SAAS_CRYPT_KEY", "unit-test-key")
    enc = crypto.encrypt("hunter2")
    assert enc.startswith(crypto._PREFIX)
    assert "hunter2" not in enc
    assert crypto.decrypt(enc) == "hunter2"


def test_encrypt_is_idempotent(monkeypatch):
    monkeypatch.setenv("SAAS_CRYPT_KEY", "unit-test-key")
    once = crypto.encrypt("s3cret")
    assert crypto.encrypt(once) == once


def test_empty_values_pass_through(monkeypatch):
    monkeypatch.setenv("SAAS_CRYPT_KEY", "unit-test-key")
    assert crypto.encrypt("") == ""
    assert crypto.encrypt(None) == ""
    assert crypto.decrypt("") == ""


def test_plaintext_legacy_value_reads_back(monkeypatch):
    """升级前存的明文没有前缀，应原样返回而不是当密文解。"""
    monkeypatch.setenv("SAAS_CRYPT_KEY", "unit-test-key")
    assert crypto.decrypt("legacy-plain") == "legacy-plain"


# ── 无密钥时必须告警 ─────────────────────────────────────────────

def test_missing_key_warns_loudly(monkeypatch, capsys):
    """核心回归：降级存明文时必须有醒目告警，不能悄悄发生。"""
    monkeypatch.delenv("SAAS_CRYPT_KEY", raising=False)
    crypto.encrypt("smtp-password")
    err = capsys.readouterr().err + capsys.readouterr().out
    assert "明文" in err or "SAAS_CRYPT_KEY" in err, "静默存了明文，无任何告警"


def test_warning_not_spammed(monkeypatch, capsys):
    """告警只提示一次，不能每次加密都刷屏淹没其它日志。"""
    monkeypatch.delenv("SAAS_CRYPT_KEY", raising=False)
    for _ in range(5):
        crypto.encrypt("x")
    out = capsys.readouterr()
    combined = out.err + out.out
    assert combined.count("SAAS_CRYPT_KEY") <= 1


def test_strict_mode_refuses_plaintext(monkeypatch):
    """生产可开严格模式：宁可报错也不把敏感字段明文落库。"""
    monkeypatch.delenv("SAAS_CRYPT_KEY", raising=False)
    monkeypatch.setenv("CRYPT_STRICT", "1")
    with pytest.raises(RuntimeError, match="SAAS_CRYPT_KEY"):
        crypto.encrypt("smtp-password")


def test_strict_mode_off_by_default(monkeypatch):
    """默认不开严格模式，避免升级瞬间打挂存量部署。"""
    monkeypatch.delenv("SAAS_CRYPT_KEY", raising=False)
    monkeypatch.delenv("CRYPT_STRICT", raising=False)
    assert crypto.encrypt("x") == "x"


def test_decrypt_without_key_returns_empty(monkeypatch):
    """有前缀却没 key → 返回空串，绝不能把密文当明文用出去。"""
    monkeypatch.setenv("SAAS_CRYPT_KEY", "unit-test-key")
    enc = crypto.encrypt("topsecret")
    monkeypatch.delenv("SAAS_CRYPT_KEY", raising=False)
    assert crypto.decrypt(enc) == ""
