<script setup>
import { ref, reactive } from 'vue'
import AgentPanel from './components/AgentPanel.vue'
import ChartsBoard from './components/ChartsBoard.vue'
import DecisionCard from './components/DecisionCard.vue'

// ---- 提问输入 ----
const question = ref('2018年6月 GMV 为什么比上月跌？')
const running = ref(false)
const error = ref('')
// 会话级 Agent 记忆：页面上持有一个 session_id，跨轮复用后端的工作记忆
const sessionId = ref('session-' + Math.random().toString(36).slice(2, 10))

// ---- 流式事件收集 ----
const events = ref([])                 // 原始 SSE 事件（面板 + 时间线用）
const agents = ref([])                 // 归并后的 Agent 卡片（start/done 生命周期）
const final = ref(null)                // 最终聚合（结论/依据/决策/chart）
const streaming = ref(false)
// 口径待确认（HITL 前置）：LLM 推断的口径需要用户确认后才拉数归因
const confirmState = ref(null)         // { confirm_id, proposed, candidates }
const selectedMetric = ref('')

const SAMPLE = [
  '2018年6月 GMV 为什么比上月跌？',
  '那 state 的 SP 呢？',   // 追问：靠会话记忆续下钻
  '2018年6月 GMV 下滑主要出在哪个州？',
  '2018年5月 GMV 高光，但运费涨了，是订单少了还是客单价低了？',
]

function useSample(q) { question.value = q }

// 解析 SSE 事件：按 agent 聚合出卡片生命周期
function reduceAgents(evts) {
  const map = new Map()
  for (const e of evts) {
    if (e.type === 'agent_start') {
      map.set(e.name, { name: e.name, role: e.role, detail: e.detail || '', status: 'running', steps: [] })
    } else if (e.type === 'agent_step') {
      const a = map.get(e.name)
      if (a) a.steps.push({ stage: e.stage, detail: e.detail, at: e.at })
    } else if (e.type === 'agent_done') {
      const a = map.get(e.name)
      if (a) { a.status = 'done'; a.result = e.payload }
    } else if (e.type === 'error') {
      error.value = e.detail || '分析出错'
    }
  }
  return Array.from(map.values())
}

async function runAnalysis(confirmId) {
  if (!question.value.trim()) return
  running.value = true
  streaming.value = true
  error.value = ''
  events.value = []
  agents.value = []
  final.value = null
  confirmState.value = null

  try {
    // 后端 /api/analyze/stream 是 SSE（text/event-stream），用 fetch 读流逐行解析。
    const body = { question: question.value, role: 'analyst', session_id: sessionId.value }
    // 确认口径后重跑：携带**落库的 confirm_id**，后端按该记录的口径逐字执行
    if (confirmId) body.confirm_id = confirmId
    const resp = await fetch('/api/analyze/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!resp.ok || !resp.body) throw new Error('流式请求失败 HTTP ' + resp.status)

    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buf = ''
    // 记录最后一帧的最终载荷（done 事件里带 conclusion/decision/chart）
    let lastDone = null

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        if (!chunk.trim()) continue
        const line = chunk.trim()
        if (!line.startsWith('data: ')) continue
        try {
          const payload = JSON.parse(line.slice(6))
          if (payload.type === 'session_start') events.value = []
          events.value.push(payload)
          if (payload.type === 'done' && payload.payload) lastDone = payload.payload
        } catch { /* 忽略无法解析的事件帧 */ }
      }
      agents.value = reduceAgents(events.value)
    }
    final.value = lastDone
    // 口径待确认：回显 proposed 口径 + 候选，供用户确认/改选后重跑
    if (lastDone && lastDone.need_confirm) {
      confirmState.value = { confirm_id: lastDone.confirm_id, proposed: lastDone.proposed, candidates: lastDone.candidates || [] }
      selectedMetric.value = lastDone.proposed?.metric || ''
    }
  } catch (e) {
    error.value = e.message || String(e)
  } finally {
    streaming.value = false
    running.value = false
  }
}

// 确认口径：携带落库的 confirm_id 重新发起（改选走 override 换一张确认记录）
async function confirmAndRun() {
  if (!selectedMetric.value || running.value) return
  const cur = confirmState.value
  // 用户改了指标 → 先按新指标换一张确认记录（原记录置 dismissed 留痕）
  if (cur && selectedMetric.value !== cur.proposed?.metric) {
    const url = '/api/confirmations/' + cur.confirm_id +
                '/override?metric=' + encodeURIComponent(selectedMetric.value)
    const r = await fetch(url, { method: 'POST' })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) { error.value = j.detail || '改选口径失败'; return }
    runAnalysis(j.confirm_id)
    return
  }
  runAnalysis(cur && cur.confirm_id)
}
</script>

<template>
  <div class="shell">
    <header class="hero">
      <div class="brand">
        <span class="dot"></span>
        <h1>多 Agent 协作数据分析系统</h1>
      </div>
      <p class="sub">问一句「为什么」，系统自动归因下钻到底 → 诊断结论 + 依据 + 建议动作 → 决策闭环</p>
    </header>

    <section class="ask">
      <div class="input-row">
        <input v-model="question" class="q" maxlength="120"
               placeholder="例如：2018年6月 GMV 为什么跌？" @keyup.enter="runAnalysis" />
        <button class="run" :disabled="running" @click="runAnalysis">
          {{ running ? '分析中…' : '开始分析' }}
        </button>
      </div>
      <div class="samples">
        <button v-for="s in SAMPLE" :key="s" class="chip" @click="useSample(s)">{{ s }}</button>
      </div>
      <p v-if="error" class="err">⚠ {{ error }}</p>
    </section>

    <AgentPanel :agents="agents" :streaming="streaming" />

    <!-- 口径待确认（HITL 前置）：LLM 推断的口径需人工确认后才拉数归因 -->
    <div v-if="final && final.need_confirm" class="outcome card confirm-card">
      <h2>口径待确认</h2>
      <p class="conclusion">
        系统由 LLM 把问题解析为口径「<b>{{ confirmState?.proposed?.metric_name || final.metric_name }}</b>」
        （{{ confirmState?.proposed?.metric || final.metric }}）。
        确认口径后才会拉取并归因；也可在下拉中改选其他官方口径。
      </p>
      <div class="confirm-row">
        <select v-model="selectedMetric" class="q" :disabled="running">
          <option v-for="c in confirmState?.candidates || []" :key="c.key" :value="c.key">
            {{ c.name }}（{{ c.key }}）
          </option>
        </select>
        <button class="run" :disabled="running || !selectedMetric" @click="confirmAndRun">
          {{ running ? '确认并分析中…' : '确认口径，开始分析' }}
        </button>
      </div>
    </div>

    <div v-if="final && !final.need_confirm" class="results">
      <section class="outcome card">
        <h2>诊断结论</h2>
        <p class="conclusion">{{ final.conclusion }}</p>
        <div class="evidence">
          <div v-for="(ev, i) in final.evidence" :key="i" class="ev">
            <span class="tag">{{ ev.type }}</span>
            <span>{{ ev.detail }}</span>
          </div>
        </div>
        <div v-if="final.actions && final.actions.action" class="action-box">
          <b>建议动作</b> {{ final.actions.action }}
        </div>
      </section>

      <ChartsBoard :chart="final.chart" :metric_name="final.metric_name" />

      <DecisionCard :decision="final.decision" />
    </div>

    <footer class="foot">
      数据底座：业务语义层确定性取数（口径已认证）· 分析主角：六 Agent 协作编排 · 决策闭环写入 HITL
    </footer>
  </div>
</template>

<style scoped>
.shell { max-width: 1180px; margin: 0 auto; padding: 28px 24px 60px; }
.hero { margin-bottom: 20px; }
.brand { display: flex; align-items: center; gap: 12px; }
.dot { width: 14px; height: 14px; border-radius: 50%;
       background: linear-gradient(135deg, var(--brand-2), var(--accent));
       box-shadow: 0 0 18px var(--brand); }
.brand h1 { font-size: 26px; margin: 0; letter-spacing: .5px; }
.sub { color: var(--text-muted); margin: 8px 0 0; font-size: 14px; }

.ask { margin: 18px 0 22px; }
.input-row { display: flex; gap: 10px; }
.q { flex: 1; padding: 14px 16px; border-radius: 12px; border: 1px solid var(--border);
     background: var(--surface); color: var(--text); font-size: 15px; outline: none; }
.q:focus { border-color: var(--brand); box-shadow: 0 0 0 3px #7c6bff33; }
.run { padding: 0 28px; border: none; border-radius: 12px; font-size: 15px; font-weight: 600;
       color: #0b1020; background: linear-gradient(135deg, var(--brand-2), var(--accent));
       transition: transform .15s, opacity .15s; }
.run:hover:not(:disabled) { transform: translateY(-2px); }
.run:disabled { opacity: .6; cursor: not-allowed; }
.samples { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
.chip { border: 1px solid var(--border); background: var(--surface-muted); color: var(--text-muted);
        border-radius: 999px; padding: 6px 12px; font-size: 13px; transition: .15s; }
.chip:hover { color: var(--text); border-color: var(--brand); }
.err { color: var(--danger); font-size: 14px; margin: 10px 0 0; }

.card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px;
        padding: 20px; }
.results { margin-top: 12px; display: flex; flex-direction: column; gap: 16px; }
.outcome h2 { margin: 0 0 12px; font-size: 18px; color: var(--brand-2); }
.conclusion { font-size: 16px; line-height: 1.7; }
.evidence { margin-top: 14px; display: flex; flex-direction: column; gap: 8px; }
.ev { display: flex; gap: 10px; align-items: baseline; font-size: 14px; color: var(--text-muted); }
.tag { flex: none; font-size: 12px; padding: 3px 8px; border-radius: 6px;
       background: #7c6bff22; color: var(--brand-2); }
.action-box { margin-top: 16px; padding: 12px 14px; border-radius: 10px;
              background: #22d3ee11; border: 1px solid #22d3ee44; font-size: 14px; }

.confirm-card { border-color: #fbbf2444; background: linear-gradient(180deg, #fbbf2410, var(--surface)); }
.confirm-card h2 { color: #fbbf24; }
.confirm-row { margin-top: 14px; display: flex; gap: 10px; align-items: center; }
.confirm-row .q { flex: 1; padding: 12px 14px; }

.foot { margin-top: 30px; color: var(--text-muted); font-size: 13px; text-align: center; }
</style>
