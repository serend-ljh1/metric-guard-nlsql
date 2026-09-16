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

# 加密密钥：优先取环境变量，否则用固定派生密钥（原型用）
_SECRET = os.environ.get("SQLPA_DS_SECRET", "sqlpa_datasource_secret_2026").encode()


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
    if not enc or not enc.startswith("enc:"):
        return enc  # 兼容旧的明文密码
    raw = base64.b64decode(enc[4:])
    return _xor_crypt(raw, _SECRET).decode("utf-8")


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
