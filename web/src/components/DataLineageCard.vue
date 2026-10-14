<script setup>
// 数据溯源卡片：把本次分析**真实用到的东西**摊开给用户看 ——
// 用到的表、口径公式、口径负责人/版本、过滤条件、以及**实际执行的 SQL**。
// 目的：让"结论从哪来"可复核，而不是只给一个结论（否则像个黑箱）。
import { computed, defineProps } from 'vue'

const props = defineProps({
  compile: { type: Object, default: () => ({}) },
  sql: { type: String, default: '' },
  rowCount: { type: Number, default: 0 },
})

const filterText = computed(() => {
  const f = props.compile && props.compile.filters
  if (!f || !Object.keys(f).length) return '—'
  return Object.entries(f).map(([k, v]) => `${k}=${v}`).join('，')
})
</script>

<template>
  <section class="card lineage">
    <div class="head">
      <span class="badge">数据溯源</span>
      <h2>用了哪些表 · 口径公式 · 实际 SQL</h2>
    </div>
    <p class="lead">以下是本次分析**真实执行**的内容，可逐项复核。</p>

    <div class="grid">
      <div class="item">
        <b>数据表</b>
        <div class="chips">
          <span v-for="t in compile.sources || []" :key="t" class="chip">{{ t }}</span>
          <span v-if="!(compile.sources || []).length" class="muted">—</span>
        </div>
      </div>
      <div class="item">
        <b>口径公式</b>
        <span class="mono">{{ compile.metric_expr || '—' }}</span>
      </div>
      <div class="item">
        <b>口径负责人 / 版本</b>
        <span>{{ compile.owner || '—' }}<template v-if="compile.version"> · {{ compile.version }}</template></span>
      </div>
      <div class="item">
        <b>分组维度</b>
        <span>{{ (compile.dims || []).join(', ') || '—' }}</span>
      </div>
      <div class="item">
        <b>过滤条件</b>
        <span class="mono">{{ filterText }}</span>
      </div>
      <div class="item">
        <b>结果行数</b>
        <span>{{ rowCount }}</span>
      </div>
    </div>

    <div class="sql-head">
      <b>实际执行的 SQL</b>
      <span class="hint">口径由语义层确定性编译，LLM 不参与生成</span>
    </div>
    <pre class="sql"><code>{{ sql || '（无）' }}</code></pre>
  </section>
</template>

<style scoped>
.lineage { border-left: 4px solid var(--accent); }
.head { display: flex; align-items: center; gap: 12px; }
.head h2 { margin: 0; font-size: 18px; color: var(--brand-2); }
.badge { font-size: 12px; padding: 4px 10px; border-radius: 999px; font-weight: 600;
         background: #22d3ee22; color: var(--accent); }
.lead { margin: 12px 0 0; color: var(--text-muted); font-size: 13px; }
.grid { margin-top: 16px; display: grid;
        grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; }
.item { display: flex; flex-direction: column; gap: 6px; }
.item b { color: var(--text-muted); font-size: 12px; font-weight: 500; }
.item span { font-size: 14px; word-break: break-all; }
.mono { font-family: var(--mono); color: var(--accent); font-size: 13px; }
.muted { color: var(--text-muted); }
.chips { display: flex; gap: 6px; flex-wrap: wrap; }
.chip { font-size: 13px; padding: 3px 10px; border-radius: 8px;
        background: var(--surface-muted); border: 1px solid var(--border); font-family: var(--mono); }
.sql-head { margin-top: 18px; display: flex; align-items: baseline; gap: 10px; }
.sql-head b { font-size: 14px; }
.sql-head .hint { color: var(--text-muted); font-size: 12px; }
.sql { margin: 10px 0 0; padding: 14px; border-radius: 10px; background: #0b1020;
       color: #d7dcf0; font-family: var(--mono); font-size: 12.5px; line-height: 1.6;
       white-space: pre-wrap; word-break: break-word; overflow-x: auto; }
</style>
