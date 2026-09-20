"""凭据加固回归：口令哈希用随机盐 PBKDF2、不再预置弱口令账号、数据源密钥不入源码。"""
from __future__ import annotations

import os

import pytest

from sqlpa.business import storage


@pytest.fixture
def tmp_app_db(monkeypatch, tmpdir_clean):
    db = tmpdir_clean / "app.db"
    monkeypatch.setattr(storage, "_DB_PATH", db)
    return db


# ---------------------------------------------------------------- 口令哈希

def test_password_hash_uses_random_salt(tmp_app_db):
    """同一口令两次哈希必须不同（随机盐）——旧实现是固定盐 sha256，会直接撞库。"""
    h1, h2 = storage._hash_pwd("same-password"), storage._hash_pwd("same-password")
    assert h1 != h2
    assert h1.startswith("pbkdf2_sha256$")
    assert storage.verify_password("same-password", h1) is True
    assert storage.verify_password("same-password", h2) is True
    assert storage.verify_password("wrong", h1) is False


def test_legacy_hash_still_verifiable_and_upgraded(tmp_app_db):
    """旧格式哈希仍可登录，并在登录时透明升级为随机盐。"""
    legacy = storage._legacy_hash("old-password")
    assert storage.verify_user.__doc__          # 存在即说明是有意兼容
    with storage._conn() as c:
        c.execute("INSERT INTO users(username,password,role,created_at) VALUES(?,?,?,?)",
                  ("legacy", legacy, "analyst", "2026-01-01"))
    assert storage.verify_user("legacy", "old-password") == "analyst"
    with storage._conn() as c:
        row = c.execute("SELECT password FROM users WHERE username='legacy'").fetchone()
    assert row["password"].startswith("pbkdf2_sha256$"), "登录后应已升级哈希格式"


def test_create_and_verify_roundtrip(tmp_app_db):
    assert storage.create_user("alice", "s3cret", role="analyst") is True
    assert storage.create_user("alice", "other") is False      # 同名不覆盖
    assert storage.verify_user("alice", "s3cret") == "analyst"
    assert storage.verify_user("alice", "bad") is None
    assert storage.verify_user("nobody", "x") is None
    # 列表不返回口令哈希
    assert all("password" not in u for u in storage.list_users())


# ---------------------------------------------------------------- 默认账号

def test_no_default_weak_accounts(monkeypatch, tmp_app_db, capsys):
    """不再预置 admin/admin123 —— 未配置引导口令时不应创建任何账号。"""
    monkeypatch.delenv("SQLPA_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    storage.ensure_default_users()
    assert storage.list_users() == []
    assert storage.verify_user("admin", "admin123") is None
    assert "未创建任何账号" in capsys.readouterr().out


def test_bootstrap_admin_only_with_explicit_password(monkeypatch, tmp_app_db):
    monkeypatch.setenv("SQLPA_BOOTSTRAP_ADMIN_PASSWORD", "a-strong-one")
    monkeypatch.setenv("SQLPA_BOOTSTRAP_ADMIN_USER", "root")
    storage.ensure_default_users()
    assert storage.verify_user("root", "a-strong-one") == "admin"
    with storage._conn() as c:
        row = c.execute("SELECT password FROM users WHERE username='root'").fetchone()
    assert row["password"].startswith("pbkdf2_sha256$")


# ---------------------------------------------------------------- 数据源密钥

def test_datasource_secret_not_hardcoded(monkeypatch, tmpdir_clean):
    """加密密钥必须来自 env 或本机生成文件，源码里不存在可用密钥。"""
    from sqlpa.business import datasources as ds

    monkeypatch.delenv("SQLPA_DS_SECRET", raising=False)
    kf = tmpdir_clean / ".k"
    monkeypatch.setattr(ds, "_KEY_FILE", kf)
    monkeypatch.setattr(ds, "_SECRET", ds._load_secret())
    assert kf.exists(), "首次使用应生成随机密钥文件而非用硬编码密钥"
    assert ds._SECRET != b"sqlpa_datasource_secret_2026"
    assert len(ds._SECRET) == 32
    enc = ds.encrypt_password("p@ss")
    assert enc.startswith("enc:") and "p@ss" not in enc
    assert ds.decrypt_password(enc) == "p@ss"


def test_datasource_secret_from_env(monkeypatch, tmpdir_clean):
    from sqlpa.business import datasources as ds

    monkeypatch.setenv("SQLPA_DS_SECRET", "env-provided-key")
    kf = tmpdir_clean / ".k2"
    monkeypatch.setattr(ds, "_KEY_FILE", kf)
    assert ds._load_secret() == b"env-provided-key"
    assert not kf.exists(), "有 env 时不应再生成密钥文件"


def test_legacy_datasource_password_still_decryptable(monkeypatch):
    """旧硬编码密钥加密的历史数据仍可解密（否则升级即"数据源全挂"），但会提示重存。"""
    from sqlpa.business import datasources as ds

    legacy_blob = "enc:" + __import__("base64").b64encode(
        ds._xor_crypt("old-pw".encode(), ds._LEGACY_KEY)).decode()
    monkeypatch.setattr(ds, "_SECRET", b"a-brand-new-key")
    assert ds.decrypt_password(legacy_blob) == "old-pw"
