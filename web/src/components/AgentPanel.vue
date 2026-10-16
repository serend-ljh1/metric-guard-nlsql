<script setup>
// Agent 执行面板：每个 Agent 一张卡片，逐个出现、显示状态与步骤流 + 决策轨迹。
import { defineProps } from 'vue'

const props = defineProps({
  agents: { type: Array, default: () => [] },
  streaming: { type: Boolean, default: false },
})

/** 把"选中"对象压成一行可读文本（供决策轨迹展示）。 */
function fmtChosen(c) {
  if (!c || typeof c !== 'object') return ''
  const parts = []
  for (const [k, v] of Object.entries(c)) {
    if (v === null || v === undefined || v === '') continue
    let s
    if (Array.isArray(v)) s = v.map((x) => (typeof x === 'object' ? JSON.stringify(x) : String(x))).join(', ')
    else if (typeof v === 'object') s = JSON.stringify(v)
    else s = String(v)
    if (s.length > 60) s = s.slice(0, 57) + '…'
    parts.push(k + '=' + s)
  }
  return parts.join(' · ')
}

/** 门控判据的可读文本（z / p / α / 结论）。 */
function fmtGate(g) {
  if (!g || typeof g !== 'object') return ''
  const parts = []
  if (g.threshold_pct !== undefined && g.threshold_pct !== null) parts.push('α=' + g.threshold_pct)
  if (g.z !== undefined && g.z !== null) parts.push('z=' + g.z)
  if (g.p_value !== undefined && g.p_value !== null) parts.push('p=' + g.p_value)
  if (g.tested === false) parts.push('未做显著性检验')
  if (g.conclusion) parts.push('→ ' + g.conclusion)
  return parts.join(' · ')
}
</script>

<template>
  <section class="panel">
    <div class="panel-head">
      <h2>Agent 执行流水</h2>
      <span class="hint">{{ streaming ? '协作编排进行中…' : (props.agents.length ? `已完成 ${props.agents.length} 个环节` : '等待分析') }}</span>
    </div>

    <div v-if="!props.agents.length" class="empty">
      <span class="radar"></span> 多 Agent 将在这里逐一步骤展开
    </div>

    <div class="flow">
      <div v-for="(a, i) in props.agents" :key="a.name" class="agent card"
           :class="{ running: a.status === 'running' }">
        <div class="agent-row">
          <span class="idx">{{ String(i + 1).padStart(2, '0') }}</span>
          <div class="ident">
            <strong>{{ a.name }}</strong>
            <span class="role">{{ a.role }}</span>
          </div>
          <span class="status" :class="a.status">{{ a.status }}</span>
        </div>
        <p v-if="a.detail" class="detail">{{ a.detail }}</p>
        <ul v-if="a.steps.length" class="steps">
          <li v-for="(s, j) in a.steps" :key="j">
            <span class="st">{{ s.stage }}</span>{{ s.detail }}
          </li>
        </ul>

        <!-- 决策轨迹（L4）：候选 / 选中 / 被拒+原因 / 门控判据 —— 回答"为什么这么选" -->
        <div v-if="a.decisions && a.decisions.length" class="decisions">
          <div v-for="(d, k) in a.decisions" :key="k" class="dec">
            <div class="dec-head">
              <span class="dst">{{ d.stage }}</span>
              <span class="dtxt">{{ d.detail }}</span>
              <span v-if="d.candidates.length" class="dcand">候选 {{ d.candidates.length }}</span>
            </div>
            <p v-if="d.chosen" class="dchosen">✓ 选中：{{ fmtChosen(d.chosen) }}</p>
            <ul v-if="d.rejected.length" class="drej">
              <li v-for="(r, m) in d.rejected" :key="m">
                <span class="x">✗</span>{{ r.option }}<span v-if="r.reason"> · {{ r.reason }}</span>
              </li>
            </ul>
            <p v-if="d.gate" class="dgate">门控：{{ fmtGate(d.gate) }}</p>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.panel { margin-bottom: 12px; }
.panel-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 12px; }
.panel-head h2 { margin: 0; font-size: 18px; color: var(--brand-2); }
.hint { color: var(--text-muted); font-size: 13px; }

.empty { padding: 34px; text-align: center; color: var(--text-muted); font-size: 14px;
         border: 1px dashed var(--border); border-radius: 16px; }
.radar { display: inline-block; width: 12px; height: 12px; border-radius: 50%;
         background: var(--brand); margin-right: 8px; box-shadow: 0 0 0 0 #7c6bffaa;
         animation: pulse 1.6s infinite; vertical-align: middle; }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 #7c6bffaa; } 70% { box-shadow: 0 0 0 12px transparent; } 100% { box-shadow: 0 0 0 0 transparent; } }

.flow { display: flex; flex-direction: column; gap: 10px; }
.agent { background: var(--surface-muted); border: 1px solid var(--border); padding: 14px 16px;
         border-radius: 12px; transition: border-color .3s; }
.agent.running { border-color: var(--brand); box-shadow: 0 0 20px #7c6bff22; }
.agent-row { display: flex; align-items: center; gap: 12px; }
.idx { font-family: var(--mono); color: var(--text-muted); font-size: 13px; }
.ident { display: flex; flex-direction: column; flex: 1; }
.ident strong { font-size: 15px; }
.role { color: var(--text-muted); font-size: 12px; margin-top: 2px; }
.status { font-size: 12px; padding: 3px 10px; border-radius: 999px; text-transform: capitalize; }
.status.done { background: #34d39922; color: var(--ok); }
.status.running { background: #fbbf2422; color: var(--warn); }
.detail { margin: 10px 0 0; font-size: 13px; color: var(--text-muted); }
.steps { margin: 10px 0 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.steps li { font-size: 13px; color: var(--text); padding-left: 6px; border-left: 2px solid var(--border); }
.st { margin-right: 8px; font-family: var(--mono); font-size: 11px; color: var(--accent); }

/* 决策轨迹（L4） */
.decisions { margin: 10px 0 0; display: flex; flex-direction: column; gap: 8px;
             padding: 8px 10px; border-radius: 10px; background: var(--surface);
             border: 1px dashed var(--border); }
.dec { display: flex; flex-direction: column; gap: 4px; }
.dec-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.dst { font-family: var(--mono); font-size: 11px; color: var(--brand-2); }
.dtxt { font-size: 13px; color: var(--text); }
.dcand { font-size: 11px; color: var(--text-muted); padding: 1px 6px;
         border: 1px solid var(--border); border-radius: 999px; }
.dchosen { margin: 0; font-size: 12px; color: var(--ok); font-family: var(--mono); }
.drej { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 3px; }
.drej li { font-size: 12px; color: var(--text-muted); }
.drej .x { color: var(--warn); margin-right: 6px; font-family: var(--mono); }
.dgate { margin: 0; font-size: 12px; color: var(--text-muted); font-family: var(--mono); }
</style>