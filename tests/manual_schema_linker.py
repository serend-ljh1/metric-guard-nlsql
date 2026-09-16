import sys
sys.path.insert(0, r'D:\ds harness\metric-guard-nlsql\src')
from pathlib import Path
from sqlpa.data.schema_extractor import extract_from_sqlite
from sqlpa.agents.schema_linker import link

# 真实 Spider 多表库（英文题 -> 应列级裁剪）
SPIDER = Path(r'D:\ds harness\spider')
def spider_case(db, q):
    p = SPIDER / 'database' / db / (db + '.sqlite')
    schema = extract_from_sqlite(p, db).to_dict()
    r = link(q, schema)
    print(f"[Spider:{db}] 题: {q[:50]}")
    print(f"   列: {r['orig_cols']} -> {r['kept_cols']} (reduced={r['reduced']})")
    return r

r1 = spider_case('student_transcripts_tracking',
                 "How many students are enrolled in courses offered by the Computer Science department?")
assert r1['reduced'], "英文题应列级裁剪(reduced=True)"
assert 'Students' in r1['kept_tables']

# Olist 中文业务题: 安全、不抛错,且应让相关列保留
schema_olist = extract_from_sqlite(r'D:\ds harness\metric-guard-nlsql\data\olist\olist.db', 'olist').to_dict()
r2 = link('各个品类的GMV', schema_olist)
print(f"\n[Olist] 列: {r2['orig_cols']} -> {r2['kept_cols']} (reduced={r2['reduced']})")
assert r2['orig_cols'] > 0 and r2['kept_tables']

# 无关问题 -> 也应安全(至少保留一列/表,不抛错)
r3 = link('今天天气怎么样', schema_olist)
print(f"[无关] 列: {r3['orig_cols']} -> {r3['kept_cols']}")
assert r3['kept_tables']

print("\n全部 Schema-Linker(列级)单测通过")
