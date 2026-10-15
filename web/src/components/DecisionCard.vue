<template>
  <section v-if="decision" class="card decision"
           :class="{ alert: decision.alert_level === 'alert' }">
    <div class="head">
      <span class="badge" :class="badgeClass">{{ badgeText }}</span>
      <h2>{{ decision.alert_level === 'query' ? '归因定位' : '决策收口' }}</h2>
    </div>
    <p class="reason">{{ decision.reason }}</p>

    <!-- 统计门控（显著性检验）：阈值过了但没过检验 → 降级观察项。
         此前这些数字只在后端存在，前端看不见，用户无法复核"为什么没告警"。 -->
    <div v-if="sig && sig.tested" class="sig">
      <div class="sig-head">
        <b>统计门控</b>
        <span class="verdict" :class="{ pass: sig.is_significant }">
          {{ sig.is_significant ? '通过显著性检验' : '未通过（判定为日间波动噪声）' }}
        </span>
      </div>
      <div class="sig-metrics">
        <div><b>z 值</b><span class="mono">{{ sig.z }}</span></div>
        <div><b>p 值</b><span class="mono">{{ sig.p_value }}</span></div>
        <div><b>显著性水平 α</b><span class="mono">{{ sig.alpha }}</span></div>
      </div>
    </div>
    <p v-else-if="sig === null && decision.alert_level !== 'query'" class="sig-na">
      本次未做显著性检验（无可比较的逐日样本）
    </p>

    <div class="meta" v-if="decision.alert_level !== 'query'">
      <div><b>推送对象</b><span>{{ decision.owner || '未指定' }}</span></div>
      <div><b>通道</b><span>{{ decision.channel === 'hitl' ? 'HITL 工单' : '—' }}</span></div>
      <div v-if="decision.hitl_id"><b>工单号</b><span class="mono">{{ decision.hitl_id }}</span></div>
    </div>
  </section>
</template>

<script setup>
// 决策闭环卡片：是否告警、推给谁、HITL 工单号 + **统计门控**（显著性检验）明细。
import { computed, defineProps } from 'vue'

const props = defineProps({
  decision: { type: Object, default: null },
})

const sig = computed(() => (props.decision ? props.decision.significance : null))

// 三态之外还有"观察项"（阈值过了但证据不足）与"数据不足"，都要有自己的视觉语义，
// 否则一律显示成"无需告警"会把"没敢报"伪装成"没问题"。
const badgeText = computed(() => {
  const lv = props.decision && props.decision.alert_level
  if (lv === 'query') return '◎ 定向查询'
  if (lv === 'alert') return '● 已告警推送'
  if (lv === 'watch') return '◐ 观察项（证据不足）'
  if (lv === 'inconclusive') return '? 数据不足'
  return '○ 无需告警'
})

const badgeClass = computed(() => {
  const lv = props.decision && props.decision.alert_level
  if (lv === 'query') return 'query'
  if (lv === 'alert') return 'on'
  if (lv === 'watch') return 'watch'
  if (lv === 'inconclusive') return 'na'
  return 'ok'
})
</script>

<style scoped>
.decision { border-left: 4px solid var(--ok); }
.decision.alert { border-left-color: var(--danger); }
.head { display: flex; align-items: center; gap: 12px; }
.head h2 { margin: 0; font-size: 18px; color: var(--brand-2); }
.badge { font-size: 13px; padding: 5px 12px; border-radius: 999px; font-weight: 600; }
.badge.on { background: #f8717122; color: var(--danger); }
.badge.ok { background: #34d39922; color: var(--ok); }
.badge.query { background: #60a5fa22; color: var(--accent); }
.badge.watch { background: #fbbf2422; color: #fbbf24; }
.badge.na { background: #94a3b822; color: var(--text-muted); }
.reason { margin: 14px 0 0; color: var(--text-muted); font-size: 14px; line-height: 1.6; }

.sig { margin-top: 14px; padding: 12px 14px; border-radius: 10px;
       background: var(--surface-muted); border: 1px solid var(--border); }
.sig-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.sig-head b { font-size: 13px; color: var(--text); }
.verdict { font-size: 12.5px; padding: 3px 10px; border-radius: 999px;
           background: #fbbf2422; color: #fbbf24; }
.verdict.pass { background: #34d39922; color: var(--ok); }
.sig-metrics { margin-top: 10px; display: grid;
               grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; }
.sig-metrics div { display: flex; flex-direction: column; gap: 3px; }
.sig-metrics b { color: var(--text-muted); font-size: 12px; font-weight: 500; }
.sig-metrics span { font-size: 14px; }
.sig-na { margin: 12px 0 0; color: var(--text-muted); font-size: 12.5px; }

.meta { margin-top: 16px; display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 12px; }
.meta div { display: flex; flex-direction: column; gap: 4px; }
.meta b { color: var(--text-muted); font-size: 12px; font-weight: 500; }
.meta span { font-size: 14px; }
.mono { font-family: var(--mono); color: var(--accent); }
</style>