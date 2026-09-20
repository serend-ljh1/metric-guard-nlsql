<script setup>
// 决策闭环卡片：是否告警、推给谁、HITL 工单号。
import { defineProps } from 'vue'

defineProps({
  decision: { type: Object, default: null },
})
</script>

<template>
  <section v-if="decision" class="card decision" :class="{ alert: decision.alert }">
    <div class="head">
      <span class="badge" :class="decision.alert ? 'on' : 'ok'">
        {{ decision.alert ? '● 已告警推送' : '○ 无需告警' }}
      </span>
      <h2>决策收口</h2>
    </div>
    <p class="reason">{{ decision.reason }}</p>
    <div class="meta">
      <div><b>推送对象</b><span>{{ decision.owner || '未指定' }}</span></div>
      <div><b>通道</b><span>{{ decision.channel === 'hitl' ? 'HITL 工单' : '—' }}</span></div>
      <div v-if="decision.hitl_id"><b>工单号</b><span class="mono">{{ decision.hitl_id }}</span></div>
    </div>
  </section>
</template>

<style scoped>
.decision { border-left: 4px solid var(--ok); }
.decision.alert { border-left-color: var(--danger); }
.head { display: flex; align-items: center; gap: 12px; }
.head h2 { margin: 0; font-size: 18px; color: var(--brand-2); }
.badge { font-size: 13px; padding: 5px 12px; border-radius: 999px; font-weight: 600; }
.badge.on { background: #f8717122; color: var(--danger); }
.badge.ok { background: #34d39922; color: var(--ok); }
.reason { margin: 14px 0 0; color: var(--text-muted); font-size: 14px; line-height: 1.6; }
.meta { margin-top: 16px; display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 12px; }
.meta div { display: flex; flex-direction: column; gap: 4px; }
.meta b { color: var(--text-muted); font-size: 12px; font-weight: 500; }
.meta span { font-size: 14px; }
.mono { font-family: var(--mono); color: var(--accent); }
</style>