"""
run_business.py
===============
**业务产品模式入口**（当前仓库唯一的运行模式）。

流程：业务人员中文提问 → MetricMatcher(LLM/关键词意图识别) → 编译器按配置确定性生成 SQL
      → 只读沙箱执行 → 业务口径说明 → 审计留痕。

关键设计（防篡改）：
  - 指标公式**只来自 business_config.yaml**，编译器用纯代码拼装 SQL；
  - 因此**没有任何"让 LLM 改写公式/自愈重试"的路径**，执行出错即如实报告；
  - 口径外问题明确拒绝并给出可操作原因，不生成未认证 SQL。

用法：
  # 真实业务查询（使用 .env 的 LLM Key；数据默认 data/olist/olist.db）
  python run_business.py --question "2018年8月各个品类的GMV"
  python run_business.py --mock --question "每州的客单价"     # 离线用关键词匹配
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "tools"))

from sqlpa.business.metric_config import load_config  # noqa: E402
from sqlpa.sandbox.sql_executor import SqlSandbox, ExecConfig  # noqa: E402

REAL_DB = ROOT / "data" / "olist" / "olist.db"

DEFAULT_QS = [
    "各个品类的GMV",
    "各州的客单价",
    "各品类的取消率",
    "各州的订单数",
]


def pick_db() -> Path:
    if not REAL_DB.exists():
        raise FileNotFoundError(
            f"缺少真实 Olist 数据 {REAL_DB}，请先 python tools/build_olist_db.py")
    print(f"  [库] 使用真实 Olist 数据: {REAL_DB}")
    return REAL_DB


def run_question(question: str, cfg, sb, db_path, llm, role: str = "analyst",
                 history=None) -> None:
    from sqlpa.business.service import answer
    print("\n" + "=" * 72)
    print(f"用户问题: {question}  (角色: {role})")
    a = answer(question, cfg, sb, db_path, llm, role=role, history=history)
    if a.get("used_context"):
        print(f"  [多轮] 已结合上下文改写为: {a.get('rewritten_question')}")
    if not a["ok"]:
        tag = "口径内拦截" if a.get("matched") else "口径外拒绝"
        print(f"  [{tag}] {a['reject']}")
        return
    m = cfg.metrics[a["metric"]]
    print("  【口径已认证】业务口径说明")
    print(f"    - {m.name} = {m.desc}")
    print(f"    - 公式来自配置(硬约束,已校验): {a['metric_expr']}")
    print(f"    - 来源: {a['source']}   维度: {a['dims']}")
    print("  【SQL】"); print(" " + a["sql"])
    print(f"  结果 ok={a['ok']} rows={len(a['rows'])}")
    for r in a["rows"][:8]:
        print("     ", r)


def main() -> int:
    ap = argparse.ArgumentParser(description="业务产品模式")
    ap.add_argument("--question", default=None, help="单个业务问题")
    ap.add_argument("--mock", action="store_true", help="离线用关键词匹配(不调LLM)")
    ap.add_argument("--db", default=None, help="业务库路径(默认自动探测)")
    ap.add_argument("--role", default="analyst", choices=["analyst", "admin"],
                    help="角色：analyst(受限) / admin(全放行)")
    args = ap.parse_args()

    cfg = load_config()
    sb_path = Path(args.db) if args.db else pick_db()
    sb = SqlSandbox(sb_path, ExecConfig.from_settings(max_rows=2000))

    if args.mock:
        llm = None
        print("  [LLM] 离线模式(Mock/关键词匹配)")
    else:
        from sqlpa.llm.openai_compat import OpenAICompatLLM
        llm = OpenAICompatLLM()
        print("  [LLM] 真实 LLM 做意图识别")

    questions = [args.question] if args.question else DEFAULT_QS
    for q in questions:
        run_question(q, cfg, sb, sb_path, llm, role=args.role)

    print("\n审计已记录（权威存储 SQLite: data/app.db；JSONL 镜像: data/business_audit.jsonl）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
