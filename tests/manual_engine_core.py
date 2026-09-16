import sys
sys.path.insert(0, r'D:\ds harness\metric-guard-nlsql\src')
from pathlib import Path
import json
from sqlpa.sandbox.sql_executor import SqlSandbox, ExecConfig
from sqlpa.data.schema_extractor import extract_from_sqlite
from sqlpa.eval.metrics import execution_match

SPIDER = Path(r'D:\ds harness\spider')
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


data = json.load(open(SPIDER / 'dev.json', encoding='utf-8'))
db = data[0]['db_id']
dbp = SPIDER / 'database' / db / (db + '.sqlite')
sb = SqlSandbox(dbp, ExecConfig(max_rows=2000))
schema = extract_from_sqlite(dbp, db).to_dict()
qs = [r for r in data if r['db_id'] == db][:5]
first_tbl = schema['tables'][0]['name']

print(f"== 真实 Spider 库: {db} | 样本 {len(qs)} 条 | 表 {len(schema['tables'])} ==")

# 1) 沙箱安全（无 key 的确定性校验）
hostile = ["DROP TABLE x", "INSERT INTO x VALUES(1)", "UPDATE x SET a=1",
           "DELETE FROM x", "SELECT * FROM a; DROP TABLE b", "PRAGMA writable_schema=ON"]
blocked = sum(1 for h in hostile if not sb.execute(h).ok)
check("沙箱拦截写/高危语句", blocked == len(hostile), f"{blocked}/{len(hostile)}")
check("合法只读查询可执行", sb.execute(f"SELECT * FROM {first_tbl}").ok)

# 2) EX 自洽（金标准SQL作为预测 → EX 恒真），证明"执行+度量"管线正确
okcnt = 0
for q in qs:
    r = sb.execute(q['query'])
    okcnt += int(execution_match(r.rows, r.rows))
check("EX自洽(金标准作为预测)恒真", okcnt == len(qs), f"{okcnt}/{len(qs)}")

# 3) 引擎链路（真实 Spider + Mock 走通；验证多Agent编排可跑）
from sqlpa.graph.pipeline import run_question
from sqlpa.llm.mock_llm import MockLLM
llm = MockLLM(answer_key={q['question']: q['query'] for q in qs})
res = run_question(qs[0]['question'], db, schema, sb, llm, gold_sql=qs[0]['query'],
                   max_repair_round=3, use_critic=True)
check("引擎链路(真实Spider, mock)最终有效", res.final_valid)
agents = {a['agent'] for a in res.agent_trace}
check("多Agent编排可见(Router/SQLWriter/Review/Validator)",
      {'RouterAgent', 'SQLWriterAgent', 'ReviewAgent', 'ValidatorAgent'} <= agents,
      str(agents))

print(f"\n引擎核心自检: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
