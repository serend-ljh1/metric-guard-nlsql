"""P1-3：长会话记忆的压缩（写入前压到字节上限，确定性、不调 LLM）。

要点：
  - 未超限 → **原样写回**（不做无谓改写，避免版本抖动）；
  - 超限 → 按"先丢最不关键"顺序压缩：历史列表 → 辅助字段 → 只留 metric/time_spec；
  - 上限由 `SQLPA_SESSION_MEMORY_MAX_BYTES` 控制；压缩后仍是合法 JSON，且能 session_load 读回。
"""
from __future__ import annotations

import json

from sqlpa.business import storage


def test_small_payload_is_untouched():
    data = {"metric": "gmv", "time_spec": "2018-06", "dims": ["customer_state"]}
    out = storage.compact_session_payload(data, max_bytes=10_000)
    assert out == data


def test_oversized_history_is_trimmed_to_last_entry():
    big = {"metric": "gmv", "time_spec": "2018-06",
           "drill_history": [{"dim": "customer_state", "value": "x" * 300, "i": i}
                             for i in range(50)]}
    cap = 900
    out = storage.compact_session_payload(big, max_bytes=cap)
    assert len(out["drill_history"]) == 1
    assert out["drill_history"][0]["i"] == 49      # 保留最近一条
    assert out["metric"] == "gmv"
    assert storage._json_size(out) <= cap


def test_extreme_payload_falls_back_to_minimal_set():
    huge = {"metric": "gmv", "time_spec": "2018-06", "dims": ["x" * 5000],
            "att_change_pct": -0.3, "last_drill": {"path_desc": "y" * 5000}}
    out = storage.compact_session_payload(huge, max_bytes=120)
    assert set(out) <= {"metric", "time_spec"}     # 只保底留续钻最小集
    assert out.get("metric") == "gmv"
    assert storage._json_size(out) <= 120


def test_degenerate_cap_yields_empty_memory():
    out = storage.compact_session_payload({"metric": "g" * 500}, max_bytes=20)
    assert out == {}                               # 连最小集都超限 → 空记忆（好过写超限行）


def test_session_save_compacts_and_roundtrips(tmpdir_clean, monkeypatch):
    monkeypatch.setenv("SQLPA_SESSION_MEMORY_MAX_BYTES", "600")
    key = "user:u::s1"
    storage.session_save(key, {"metric": "gmv", "time_spec": "2018-06",
                               "dims": ["a" * 900], "att_change_pct": 0.1})
    got = storage.session_load(key)
    assert got is not None and got.get("metric") == "gmv"
    assert storage._json_size(got) <= 600
    # 库里真正落的是**压缩后**的 JSON，而不是原始超限载荷
    with storage._conn() as c:
        raw = c.execute("SELECT data_json FROM session_memory WHERE key=?", (key,)).fetchone()[0]
    assert storage._json_size(json.loads(raw)) <= 600
