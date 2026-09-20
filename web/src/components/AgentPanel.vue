<script setup>
// Agent 执行面板：每个 Agent 一张卡片，逐个出现、显示状态与步骤流。
import { defineProps } from 'vue'

const props = defineProps({
  agents: { type: Array, default: () => [] },
  streaming: { type: Boolean, default: false },
})
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
</style>