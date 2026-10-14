<script setup>
// 治理 / HITL 看板：把此前"只有后端端点、前端没入口"的治理能力暴露出来。
// 覆盖：口径冲突（/api/governance/conflicts）、人工复核队列（/api/hitl）、
//       待确认口径（/api/confirmations）、订阅规则（/api/subscriptions）。
import { ref, onMounted } from 'vue'

const loading = ref(false)
const error = ref('')
const conflicts = ref({ total: 0, by_type: {}, conflicts: [] })
const hitl = ref([])
const confirmations = ref([])
const subscriptions = ref([])

async function getJson(url) {
  const r = await fetch(url)
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(j.detail || ('请求失败 HTTP ' + r.status))
  return j
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    // 并行拉取四个治理面；任一端点失败不阻塞其余（各自 try）
    const [c, h, cf, s] = await Promise.allSettled([
      getJson('/api/governance/conflicts'),
      getJson('/api/hitl?status=pending'),
      getJson('/api/confirmations'),
      getJson('/api/subscriptions'),
    ])
    if (c.status === 'fulfilled') conflicts.value = c.value
    if (h.status === 'fulfilled') hitl.value = h.value || []
    if (cf.status === 'fulfilled') confirmations.value = cf.value || []
    if (s.status === 'fulfilled') subscriptions.value = s.value || []
    const firstReject = [c, h, cf, s].find((x) => x.status === 'rejected')
    if (firstReject) error.value = '部分治理数据获取失败：' + firstReject.reason.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="gov card">
    <div class="gov-head">
      <h2>治理 / 人工复核看板</h2>
      <button class="ghost" :disabled="loading" @click="load()">
        {{ loading ? '刷新中…' : '↻ 刷新' }}
      </button>
    </div>
    <p v-if="error" class="err">⚠ {{ error }}</p>

    <div class="grid">
      <!-- 口径冲突：四类（同名不同式/同式不同名/版本漂移/近义不同式） -->
      <div class="box">
        <h3>口径冲突（{{ conflicts.total }}）</h3>
        <div class="chips">
          <span v-for="(n, t) in conflicts.by_type" :key="t" class="pill">{{ t }} · {{ n }}</span>
          <span v-if="!conflicts.total" class="empty">未发现口径冲突</span>
        </div>
        <ul class="list">
          <li v-for="(c, i) in conflicts.conflicts" :key="i">
            <b>{{ c.metric || c.name }}</b> <span class="ty">{{ c.type }}</span>
            <div class="muted">{{ c.detail || c.reason || '' }}</div>
          </li>
        </ul>
      </div>

      <!-- 人工复核队列（HITL） -->
      <div class="box">
        <h3>人工复核队列（{{ hitl.length }}）</h3>
        <div v-if="!hitl.length" class="empty">暂无待处理工单</div>
        <ul class="list">
          <li v-for="h in hitl" :key="h.record_id || h.id">
            <b>{{ h.metric || h.metric_key }}</b>
            <span class="ty">{{ h.status }}</span>
            <div class="muted">{{ h.time_spec || h.window || '' }} · {{ h.owner || '未指派' }}</div>
          </li>
        </ul>
      </div>

      <!-- 待确认口径（HITL 前置门） -->
      <div class="box">
        <h3>待确认口径（{{ confirmations.length }}）</h3>
        <div v-if="!confirmations.length" class="empty">暂无待确认口径</div>
        <ul class="list">
          <li v-for="c in confirmations" :key="c.confirm_id || c.id">
            <b>{{ c.metric_name || c.metric }}</b>
            <span class="ty">{{ c.status }}</span>
            <div class="muted">{{ c.time_spec || '' }}</div>
          </li>
        </ul>
      </div>

      <!-- 订阅告警规则 -->
      <div class="box">
        <h3>订阅告警规则（{{ subscriptions.length }}）</h3>
        <div v-if="!subscriptions.length" class="empty">暂无订阅</div>
        <ul class="list">
          <li v-for="s in subscriptions" :key="s.id || s.sub_id">
            <b>{{ s.metric }}</b>
            <span class="ty">阈值 {{ ((s.threshold_pct || 0) * 100).toFixed(0) }}%</span>
            <div class="muted">{{ s.owner || '未指派' }} · {{ s.channel }}</div>
          </li>
        </ul>
      </div>
    </div>
  </section>
</template>

<style scoped>
.gov { margin-top: 16px; }
.gov-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.gov h2 { margin: 0; font-size: 18px; color: var(--brand-2); }
.grid { margin-top: 16px; display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }
@media (max-width: 760px) { .grid { grid-template-columns: 1fr; } }
.box { background: var(--surface-muted); border: 1px solid var(--border); border-radius: 12px;
       padding: 14px; }
.box h3 { margin: 0 0 10px; font-size: 15px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.pill { font-size: 12px; padding: 3px 8px; border-radius: 999px; background: #7c6bff22;
        color: var(--brand-2); }
.list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px;
        max-height: 220px; overflow-y: auto; }
.list li { font-size: 13px; }
.ty { font-size: 11px; padding: 2px 7px; border-radius: 6px; background: #22d3ee22;
      color: var(--brand-2); margin-left: 6px; }
.muted { color: var(--text-muted); font-size: 12px; margin-top: 2px; }
.empty { color: var(--text-muted); font-size: 13px; }
.err { color: var(--danger); font-size: 13px; margin: 8px 0 0; }
.ghost { border: 1px solid var(--border); background: transparent; color: var(--text);
         border-radius: 10px; padding: 8px 14px; font-size: 13px; font-weight: 600; }
.ghost:hover:not(:disabled) { border-color: var(--brand); color: var(--brand-2); }
</style>
