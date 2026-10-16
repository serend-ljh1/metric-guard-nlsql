<script setup>
// 治理 / HITL 看板：把此前"只有后端端点、前端没入口"的治理能力暴露出来。
// 覆盖：口径冲突（/api/governance/conflicts）、人工复核队列（/api/hitl）、
//       SLA 超期升级（/api/hitl/escalations）、待确认口径（/api/confirmations）、
//       订阅规则（/api/subscriptions）。
// 所有请求都带 X-API-Token：否则在启用了鉴权的部署下，admin 动作（巡检/推进工单）
// 会被 403 挡掉，看板看起来"有入口但点不动"。
import { ref, onMounted } from 'vue'
import { authHeaders } from '../auth'

const loading = ref(false)
const error = ref('')
const conflicts = ref({ total: 0, by_type: {}, conflicts: [] })
const hitl = ref([])
const escalations = ref({ sla_hours: 0, escalated: [], soon_due: [] })
const confirmations = ref([])
const subscriptions = ref([])
// LLM 成本/延迟（近 7 天）：仅 admin 可见 —— 403 时不再当成"加载失败"，
// 而是明确显示"仅 admin 可见"，避免普通分析者看到一条吓人的错误。
const llm = ref(null)
const llmDenied = ref(false)

async function getJson(url) {
  const r = await fetch(url, { headers: authHeaders() })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(j.detail || ('请求失败 HTTP ' + r.status))
  return j
}

/** 手动触发一次 SLA 巡检（仅 admin）：超期升级 / 临期催办，便于演示与运维。 */
async function sweep() {
  loading.value = true
  error.value = ''
  try {
    const r = await fetch('/api/hitl/sweep', { method: 'POST', headers: authHeaders() })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(j.detail || ('SLA 巡检失败 HTTP ' + r.status))
    alert(`SLA 巡检完成：超时升级 ${j.escalated_count}、临期催办 ${j.reminded_count}`)
  } catch (e) {
    error.value = e.message || String(e)
  } finally {
    loading.value = false
    await load()
  }
}

async function loadLlm() {
  // 独立拉取：admin 独占端点，403 单独处理，不污染主 error 提示
  try {
    const r = await fetch('/api/llm-usage?days=7', { headers: authHeaders() })
    if (r.status === 403) { llmDenied.value = true; llm.value = null; return }
    const j = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status))
    llm.value = j
    llmDenied.value = false
  } catch {
    llmDenied.value = false
    llm.value = null
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    // 并行拉取五个治理面；任一端点失败不阻塞其余（各自 try）
    const [c, h, es, cf, s] = await Promise.allSettled([
      getJson('/api/governance/conflicts'),
      getJson('/api/hitl?status=pending'),
      getJson('/api/hitl/escalations'),
      getJson('/api/confirmations'),
      getJson('/api/subscriptions'),
    ])
    if (c.status === 'fulfilled') conflicts.value = c.value
    if (h.status === 'fulfilled') hitl.value = h.value || []
    if (es.status === 'fulfilled') escalations.value = es.value || escalations.value
    if (cf.status === 'fulfilled') confirmations.value = cf.value || []
    if (s.status === 'fulfilled') subscriptions.value = s.value || []
    const firstReject = [c, h, es, cf, s].find((x) => x.status === 'rejected')
    if (firstReject) error.value = '部分治理数据获取失败：' + firstReject.reason.message
    await loadLlm()
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
      <div class="gov-actions">
        <button class="ghost" :disabled="loading" @click="sweep()">SLA 巡检（admin）</button>
        <button class="ghost" :disabled="loading" @click="load()">
          {{ loading ? '刷新中…' : '↻ 刷新' }}
        </button>
      </div>
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
            <b>{{ h.matched_metric || h.metric || h.metric_key }}</b>
            <span class="ty">{{ h.status }}</span>
            <div class="muted">{{ h.time_spec || h.window || '' }} · {{ h.owner || '未指派' }}</div>
          </li>
        </ul>
      </div>

      <!-- SLA 超时升级：超期未闭环的工单（/api/hitl/escalations） -->
      <div class="box">
        <h3>SLA 超期升级（{{ escalations.escalated.length }}）</h3>
        <div class="muted">SLA = {{ escalations.sla_hours }}h · 临期待催办 {{ escalations.soon_due.length }}</div>
        <div v-if="!escalations.escalated.length" class="empty">暂无超期工单</div>
        <ul class="list">
          <li v-for="e in escalations.escalated" :key="e.record_id">
            <b>{{ e.matched_metric }}</b>
            <span class="ty warn">已升级</span>
            <div class="muted">{{ e.escalated_at || '' }} · {{ e.owner || '未指派' }}</div>
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

      <!-- LLM 成本 / 延迟（近 7 天，仅 admin）：把"一次分析花多少钱、多久"变成可读数字 -->
      <div class="box">
        <h3>LLM 成本 / 延迟（近 7 天）</h3>
        <div v-if="llmDenied" class="empty">当前身份无权查看（仅 admin）</div>
        <div v-else-if="!llm || !llm.calls" class="empty">暂无 LLM 调用记录</div>
        <template v-else>
          <div class="muted">
            调用 {{ llm.calls }} 次（失败 {{ llm.failed_calls }}）· token {{ llm.total_tokens }}
            （prompt {{ llm.prompt_tokens }} / completion {{ llm.completion_tokens }}）· 成本 {{ llm.cost }}
          </div>
          <div class="muted">
            单次延迟 p50 {{ llm.latency_ms.p50 }}ms · p95 {{ llm.latency_ms.p95 }}ms · max {{ llm.latency_ms.max }}ms
          </div>
          <ul class="list">
            <li v-for="(b, m) in llm.by_model" :key="m">
              <b>{{ m }}</b> <span class="ty">{{ b.calls }} 次</span>
              <div class="muted">{{ b.total_tokens }} token · 成本 {{ b.cost }} · p50 {{ b.p50_ms }}ms · p95 {{ b.p95_ms }}ms</div>
            </li>
          </ul>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.gov { margin-top: 16px; }
.gov-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.gov-actions { display: flex; gap: 8px; }
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
.ty.warn { background: #fbbf2422; color: #fbbf24; }
.muted { color: var(--text-muted); font-size: 12px; margin-top: 2px; }
.empty { color: var(--text-muted); font-size: 13px; }
.err { color: var(--danger); font-size: 13px; margin: 8px 0 0; }
.ghost { border: 1px solid var(--border); background: transparent; color: var(--text);
         border-radius: 10px; padding: 8px 14px; font-size: 13px; font-weight: 600; }
.ghost:hover:not(:disabled) { border-color: var(--brand); color: var(--brand-2); }
</style>
