"""
sqlpa.business.datasources
==========================
数据源注册中心：持久化「业务库连接规格」，支持 SQLite / MySQL / PostgreSQL。

- 默认内置一个 SQLite 数据源（Olist 真实数据或同结构样本库）。
- 外部数据源连接信息保存在 data/datasources.yaml；密码用简单可逆加密存储
  （非生产级，原型用；生产部署建议改用密钥管理服务）。
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml

_STORE = Path(__file__).resolve().parents[3] / "data" / "datasources.yaml"
_KEY_FILE = Path(__file__).resolve().parents[3] / "data" / ".datasource_key"


def _load_secret() -> bytes:
    """取加密密钥：环境变量 > 本机密钥文件（首次自动生成）> 拒绝启动。

    旧实现在源码里硬编码 `sqlpa_datasource_secret_2026` —— 等于没有加密：
    任何拿到仓库的人都能解开 datasources.yaml 里的库密码。
    现在密钥必须来自环境变量或**本机生成且 gitignore 的**密钥文件，源码里不再有可用密钥。
    """
    env = (os.environ.get("SQLPA_DS_SECRET") or "").strip()
    if env:
        return env.encode()
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()
    import secrets
    key = secrets.token_bytes(32)
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KEY_FILE.write_bytes(key)
    try:
        _KEY_FILE.chmod(0o600)
    except OSError:
        pass
    print(f"[安全提示] 已生成本机数据源密钥 {_KEY_FILE}（请勿提交；"
          f"生产环境请用 SQLPA_DS_SECRET 或 KMS 注入）")
    return key


_SECRET = _load_secret()
# 旧硬编码密钥：**仅用于解密历史数据**（见 decrypt_password），不再参与新数据加密
_LEGACY_KEY = b"sqlpa_datasource_secret_2026"


def _xor_crypt(data: bytes, key: bytes) -> bytes:
    """简单 XOR 流加密（用 SHA256 派生密钥流）。原型用，非密码学安全。"""
    k = hashlib.sha256(key).digest()
    out = bytearray(len(data))
    for i in range(len(data)):
        out[i] = data[i] ^ k[i % len(k)]
    return bytes(out)


def encrypt_password(plain: str) -> str:
    if not plain:
        return ""
    raw = _xor_crypt(plain.encode("utf-8"), _SECRET)
    return "enc:" + base64.b64encode(raw).decode()


def decrypt_password(enc: str) -> str:
    """解密连接密码。

    兼容性：历史数据的密码可能由**旧硬编码密钥**加密（源码里那把 `sqlpa_datasource_secret_2026`）。
    硬编码密钥已从加密路径移除（不再用于新数据），但这里保留**仅解密**的回退，
    避免升级后旧数据源直接不可用；命中回退时会提示重新保存（以新密钥重加密）。
    """
    if not enc or not enc.startswith("enc:"):
        return enc  # 兼容旧的明文密码
    raw = base64.b64decode(enc[4:])
    try:
        plain = _xor_crypt(raw, _SECRET).decode("utf-8")
        if plain.isprintable():
            return plain
    except UnicodeDecodeError:
        pass
    legacy = _xor_crypt(raw, _LEGACY_KEY).decode("utf-8", "replace")
    print("[安全提示] 该数据源密码由旧的硬编码密钥加密，请在数据源管理中重新保存以升级密钥。")
    return legacy


def _load() -> Dict:
    if not _STORE.exists():
        return {"datasources": []}
    return yaml.safe_load(open(_STORE, encoding="utf-8")) or {"datasources": []}


def _save(data: Dict) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    yaml.safe_dump(data, open(_STORE, "w", encoding="utf-8"),
                   allow_unicode=True, sort_keys=False)


def list_datasources(include_builtin: bool = True) -> List[Dict]:
    out = []
    if include_builtin:
        out.append({"id": "builtin_sqlite", "name": "Olist 业务库 (SQLite)",
                    "kind": "sqlite", "builtin": True})
    out.extend(_load().get("datasources", []))
    return out


def get_datasource(ds_id: str) -> Optional[Dict]:
    if ds_id == "builtin_sqlite":
        return {"id": "builtin_sqlite", "name": "Olist 业务库 (SQLite)",
                "kind": "sqlite", "builtin": True}
    for d in _load().get("datasources", []):
        if d.get("id") == ds_id:
            d = dict(d)
            if d.get("password"):
                d["password"] = decrypt_password(d["password"])
            return d
    return None


def add_datasource(spec: Dict) -> Dict:
    data = _load()
    ds_id = spec.get("id") or f"{spec['kind']}_{len(data['datasources']) + 1}"
    spec = dict(spec)
    spec["id"] = ds_id
    if spec.get("password"):
        spec["password"] = encrypt_password(spec["password"])
    data["datasources"] = [d for d in data["datasources"] if d.get("id") != ds_id]
    data["datasources"].append(spec)
    _save(data)
    return spec


def delete_datasource(ds_id: str) -> bool:
    data = _load()
    before = len(data["datasources"])
    data["datasources"] = [d for d in data["datasources"] if d.get("id") != ds_id]
    if len(data["datasources"]) == before:
        return False
    _save(data)
    return True


def to_sandbox_spec(ds: Dict, sqlite_path) -> Dict:
    """把数据源记录转成 make_sandbox 的规格。"""
    if ds.get("kind") == "sqlite":
        return {"kind": "sqlite", "path": str(sqlite_path)}
    return {k: v for k, v in ds.items() if k not in ("id", "name", "builtin")}


def display(ds: Dict) -> str:
    """界面展示用（密码掩码）。"""
    if ds.get("kind") == "sqlite":
        return f"SQLite · {ds.get('name', '本地文件')}"
    pwd = "***" if ds.get("password") else "无"
    return (f"{ds['kind'].upper()} · {ds.get('user', '')}@{ds.get('host', '')}:"
            f"{ds.get('port', '')}/{ds.get('database', '')} (密码 {pwd})")
