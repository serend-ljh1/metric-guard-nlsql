# -*- coding: utf-8 -*-
"""verify_real_llm - 真实 LLM 联调冒烟（tools/ 下，非 CI，不打印 Key）。

跑法：AREA=0 时用 api._llm()（读 .env 的真实 OpenAI 兼容端点）；带 --dry 时用 MockLLM。
只跑一个「本月 GMV 为什么跌？」验证诊断链在真实模型下：
  - 指标识别 / decide_drill 是否真实调用 LLM 且返回合理动作
  - ConclusionAgent 产出的人话结论质量
  - DecisionAgent 是否触发告警 + 写 HITL
在线程结束后立即删除临时 HITL 文件，避免污染 data/app.db。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

# 先加载 .env（Key 只在 .env，落盘，不打印），再判定有无真实模型
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:  # noqa: BLE001
    pass

from sqlpa.analysis.orchestrator import run_analysis           # noqa: E402
from sqlpa.business.metric_config import load_config           # noqa: E402
from sqlpa.sandbox.sql_executor import ExecConfig, SqlSandbox  # noqa: E402

DB = os.path.join(ROOT, "data", "olist", "olist.db")
# 归因拆解维度：问题自身不带维度词，这里显式指定，让演示真正走出"按州/品类定位主因"。
DIMS = ["state", "category"]


class _MockLLM:
    """dry 模式用的假 actor，便于对照「真实 vs 假」输出差异。"""

    def complete(self, prompt):
        return "9 月 GMV 下滑主因是 SP 州订单收缩，建议核查供应与促销。"


def _real_llm():
    # 复用 api._llm 的判定逻辑：有 Key 才出真实模型
    if not (os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("OPENAI_API_KEY")):
        return None
    from sqlpa.llm.openai_compat import OpenAICompatLLM
    return OpenAICompatLLM()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="用 MockLLM 冒烟（不花钱）")
    ap.add_argument("--question", default="2018-06 GMV 为什么比上月跌？")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print(f"[!!] 缺少真实 Olist 数据 {DB}，请先 python tools/build_olist_db.py")
        return 1

    llm = _MockLLM() if args.dry else _real_llm()
    mode = "MOCK(假模型)" if args.dry else "REAL(真实LLM)"
    if llm is None and not args.dry:
        print("[跳过] 未检测到 {LLM,DEEPSEEK,OPENAI}_API_KEY，[--dry] 冒烟后请配 .env 再跑真实联调")
        llm = _MockLLM()
        mode = "MOCK(未配Key，回落)"

    cfg = load_config()
    sb = SqlSandbox(DB, ExecConfig.from_settings(max_rows=2000))
    events = []
    hitl_fd, hitl = tempfile.mkstemp(suffix=".db")
    os.close(hitl_fd)
    try:
        res = run_analysis(args.question, cfg, sb, DB, llm,
                           role="analyst", username="verify",
                           alert_threshold_pct=0.05,
                           dims_override=DIMS,
                           hitl_path=hitl, emit=events.append)
        print(f"\n===== 完整诊断链（{mode}）=====")
        print("ok / route / metric:", res.get("ok"), "/", res.get("route"), "/", res.get("metric"))
        print("波动:", (res.get("attribution") or {}).get("change_pct"))
        dec = res.get("decision") or {}
        print("决策: alert=", dec.get("alert"), "channel=", dec.get("channel"),
              "owner=", dec.get("owner"))
        print("结论:", res.get("conclusion"))
        # 告警时校验 HITL 写了工单
        if dec.get("alert"):
            try:
                con = sqlite3.connect(hitl)
                tabs = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
                hitl_t = next((t for t in tabs if "hitl" in t.lower()), None)
                if hitl_t:
                    n = con.execute(f"SELECT COUNT(*) FROM {hitl_t}").fetchone()[0]
                    print(f"HITL 工单已写入: {hitl_t} {n} 条")
                con.close()
            except Exception as e:
                print("HITL 读取注意:", e)
        print("Agent 事件顺序:")
        for e in events:
            if e.get("type") in ("session_start", "agent_start", "done"):
                print("  ", e.get("name") or e.get("type"),
                      "/", (e.get("detail") or "")[:40])
        return 0
    finally:
        try:
            os.unlink(hitl)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())