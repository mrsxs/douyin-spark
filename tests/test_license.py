"""部署 License 验签与机器绑定。

背景：机器绑定原本由客户可控的 LICENSE_STRICT 环境变量开关，
客户删掉它就退回宽松模式，绑定形同虚设。payload 受 RSA 签名保护，
「是否绑定」应当只由签发方决定。

测试用临时密钥对，不触碰真实的 license_private_key.pem。
"""
import base64
import json
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app import license as lic


@pytest.fixture(scope="module")
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub_pem


@pytest.fixture(autouse=True)
def _use_test_key(keypair, monkeypatch):
    _, pub_pem = keypair
    monkeypatch.setattr(lic, "_PUBLIC_KEY_PEM", pub_pem)


def issue(priv, *, days=365, machine=None, tier="pro", note="test"):
    payload = {
        "issued_at": datetime.utcnow().isoformat(),
        "expires_at": (datetime.utcnow() + timedelta(days=days)).isoformat(),
        "tier": tier,
        "machine": machine,
        "note": note,
    }
    raw = json.dumps(payload).encode()
    sig = priv.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    return (base64.b64encode(raw).decode() + "."
            + base64.b64encode(sig).decode())


@pytest.fixture
def fake_machine(tmp_path, monkeypatch):
    """把三个机器指纹来源都钉死，避免测试读到真实 MAC/hostname。

    必须 patch 底层三个函数而不是 _machine_id —— 校验走的是
    _machine_candidates()，只 patch _machine_id 会让「显示相同却被拒」。
    """
    def _set(install=None, legacy="legacy-code-xxxx", mac="mac-code-xxxxxx"):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setattr(lic, "_install_id", lambda: install)
        monkeypatch.setattr(lic, "_legacy_machine_id", lambda: legacy)
        monkeypatch.setattr(lic, "_mac_id", lambda: mac)
    return _set


def test_unbound_license_passes(keypair, monkeypatch):
    priv, _ = keypair
    monkeypatch.setenv("LICENSE_KEY", issue(priv, machine=None))
    data = lic.verify_license_or_exit()
    assert data["tier"] == "pro"


def test_bound_license_passes_on_matching_machine(keypair, fake_machine, monkeypatch):
    priv, _ = keypair
    fake_machine(install="aaaabbbbccccdddd")
    monkeypatch.setenv("LICENSE_KEY", issue(priv, machine="aaaabbbbccccdddd"))
    assert lic.verify_license_or_exit()["tier"] == "pro"


def test_bound_license_rejected_on_other_machine(keypair, fake_machine, monkeypatch):
    """核心回归：绑定的 License 换机器必须失败。"""
    priv, _ = keypair
    fake_machine(install="999999999999ffff")
    monkeypatch.setenv("LICENSE_KEY", issue(priv, machine="aaaabbbbccccdddd"))
    with pytest.raises(SystemExit):
        lic.verify_license_or_exit()


@pytest.mark.parametrize("strict_env", [None, "0", "", "false"])
def test_binding_not_bypassable_by_removing_strict_env(
        keypair, fake_machine, monkeypatch, strict_env):
    """删除/关闭 LICENSE_STRICT 不能解除机器绑定。"""
    priv, _ = keypair
    fake_machine(install="999999999999ffff")
    monkeypatch.setenv("LICENSE_KEY", issue(priv, machine="aaaabbbbccccdddd"))
    if strict_env is None:
        monkeypatch.delenv("LICENSE_STRICT", raising=False)
    else:
        monkeypatch.setenv("LICENSE_STRICT", strict_env)
    with pytest.raises(SystemExit):
        lic.verify_license_or_exit()


def test_reported_machine_code_is_actually_accepted(keypair, fake_machine, monkeypatch):
    """报错里提示的「当前机器码」必须是真正会被接受的那个。

    否则客户拿这个码去申请签发，回来仍然启动不了。
    """
    priv, _ = keypair
    fake_machine(install="aaaabbbbccccdddd")
    monkeypatch.setenv("LICENSE_KEY", issue(priv, machine="somebodyelses1"))
    with pytest.raises(SystemExit):
        lic.verify_license_or_exit()

    # 拿 _machine_id() 报出来的码重新签发 → 必须能过
    monkeypatch.setenv("LICENSE_KEY", issue(priv, machine=lic._machine_id()))
    assert lic.verify_license_or_exit()["tier"] == "pro"


def test_expired_license_rejected(keypair, monkeypatch):
    priv, _ = keypair
    monkeypatch.setenv("LICENSE_KEY", issue(priv, days=-1))
    with pytest.raises(SystemExit):
        lic.verify_license_or_exit()


def test_tampered_payload_rejected(keypair, monkeypatch):
    """改 payload（比如把 machine 抹掉）会让签名失效。"""
    priv, _ = keypair
    key = issue(priv, machine="aaaabbbbccccdddd")
    payload_b64, sig_b64 = key.split(".", 1)
    tampered = json.loads(base64.b64decode(payload_b64))
    tampered["machine"] = None
    forged = (base64.b64encode(json.dumps(tampered).encode()).decode()
              + "." + sig_b64)
    monkeypatch.setenv("LICENSE_KEY", forged)
    with pytest.raises(SystemExit):
        lic.verify_license_or_exit()


def test_missing_and_malformed_key_rejected(monkeypatch):
    monkeypatch.delenv("LICENSE_KEY", raising=False)
    with pytest.raises(SystemExit):
        lic.verify_license_or_exit()

    monkeypatch.setenv("LICENSE_KEY", "not-a-valid-license")
    with pytest.raises(SystemExit):
        lic.verify_license_or_exit()


# ── 机器码稳定性：绑定不能因为容器重建而自己失效 ────────────────────

def test_install_id_is_stable_and_persisted(tmp_path, monkeypatch):
    # settings.data_dir 是权威位置：pydantic-settings 里环境变量优先级高于
    # .env，所以生产上它和 DATA_DIR 不可能不一致；这里两个一起设，
    # 模拟真实部署（只改 os.environ 的话 settings 已经冻结，改不动）。
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))
    first = lic._install_id()
    assert first and len(first) == 16
    assert (tmp_path / ".install_id").exists()
    assert lic._install_id() == first          # 重复调用稳定
    assert lic._install_id() == first          # 进程重启后仍一致


def test_legacy_bound_license_survives_hostname_change(
        keypair, tmp_path, monkeypatch):
    """存量客户绑的是 MAC+hostname；容器重建 hostname 变了不能把人锁在门外。"""
    priv, _ = keypair
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(lic, "_legacy_machine_id", lambda: "old-host-code")
    monkeypatch.setattr(lic, "_mac_id", lambda: "stable-mac-code")

    # 签发时绑的是当时的 MAC+hostname
    monkeypatch.setenv("LICENSE_KEY", issue(priv, machine="old-host-code"))
    assert lic.verify_license_or_exit()["tier"] == "pro"

    # 容器重建：hostname 变了，legacy 码跟着变，但 MAC 没变 → 仍应放行
    monkeypatch.setattr(lic, "_legacy_machine_id", lambda: "new-host-code")
    monkeypatch.setenv("LICENSE_KEY", issue(priv, machine="stable-mac-code"))
    assert lic.verify_license_or_exit()["tier"] == "pro"


def test_install_id_bound_license_passes(keypair, tmp_path, monkeypatch):
    priv, _ = keypair
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    iid = lic._install_id()
    monkeypatch.setenv("LICENSE_KEY", issue(priv, machine=iid))
    assert lic.verify_license_or_exit()["tier"] == "pro"


def test_unrelated_machine_still_rejected(keypair, tmp_path, monkeypatch):
    """放宽候选不等于放弃绑定：不相干的机器码依然必须拒。"""
    priv, _ = keypair
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LICENSE_KEY", issue(priv, machine="somebodyelses1"))
    with pytest.raises(SystemExit):
        lic.verify_license_or_exit()


# ── 机器码落点：必须跟着 settings.data_dir 走 ─────────────

def test_机器码写进配置的数据目录(tmp_path, monkeypatch):
    """只在 .env 里配 DATA_DIR 的部署，环境变量里没有它。

    读 os.environ 的话机器码会落到 CWD 下的 ./data —— 不在客户备份的目录里，
    换个工作目录重启机器码就变了，绑定的 License 直接起不来。
    """
    from app import license as lic

    real = tmp_path / "srv-data"
    real.mkdir()
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setattr("app.config.settings.data_dir", str(real))
    monkeypatch.chdir(tmp_path)

    mid = lic._install_id()
    assert mid
    assert (real / ".install_id").read_text().strip() == mid
    assert not (tmp_path / "data" / ".install_id").exists()


def test_读得到老位置的机器码(tmp_path, monkeypatch):
    """存量安装的 .install_id 在旧位置，升级后机器码不能变 ——
    变了等于所有绑定 License 集体失效。"""
    from app import license as lic

    old = tmp_path / "data"
    old.mkdir()
    (old / ".install_id").write_text("legacy0123456789")
    new = tmp_path / "srv-data"
    new.mkdir()

    monkeypatch.setenv("DATA_DIR", str(old))
    monkeypatch.setattr("app.config.settings.data_dir", str(new))
    assert lic._install_id() == "legacy0123456789"
    assert not (new / ".install_id").exists()      # 不该重新生成


def test_机器码稳定不变(tmp_path, monkeypatch):
    from app import license as lic
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))
    assert lic._install_id() == lic._install_id()


def test_目录不可写时不崩溃(tmp_path, monkeypatch):
    """取机器码失败要能退回 legacy 算法，不能让整个启动挂掉。"""
    from app import license as lic
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setattr("app.config.settings.data_dir", "/proc/nonexistent/nope")
    assert lic._install_id() is None
    assert lic._machine_id()                       # 退回 MAC+hostname
