<script setup>
// 我的工作台：查询历史 / 收藏 / 反馈。
// 三张表后端早就有，却一直没有入口 —— "看过什么、存了什么、觉得哪里答错了"在系统里
// 根本留不下来。这里把它们接上，并让收藏支持"一键复跑"（回填问题到分析台）。
import { ref, onMounted } from 'vue'
import { authHeaders } from '../auth'

const emit = defineEmits(['reuse'])

const history = ref([])
const favorites = ref([])
const feedback = ref([])
const histTotal = ref(0)
const busy = ref(false)
const msg = ref('')
const favTitle = ref('')
const comment = ref('')

async function jget(url) {
  const r = await fetch(url, { headers: authHeaders() })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status))
  return j
}

async function loadAll() {
  busy.value = true
  msg.value = ''
  try {
    const [h, f, fb] = await Promise.all([
      jget('/api/history?limit=50'),
      jget('/api/favorites'),
      jget('/api/feedback?limit=50'),
    ])
    history.value = h.items || []
    histTotal.value = h.total || 0
    favorites.value = f || []
    feedback.value = fb.items || []
  } catch (e) {
    msg.value = e.message || String(e)
  } finally {
    busy.value = false
  }
}

async function addFav(row) {
  const title = favTitle.value.trim() || row.user_input || '未命名收藏'
  const r = await fetch('/api/favorites', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ title, user_input: row.user_input || '', generated_sql: row.generated_sql || '' }),
  })
  const j = await r.json().catch(() => ({}))
  msg.value = r.ok ? '已收藏：' + title : (j.detail || '收藏失败')
  if (r.ok) { favTitle.value = ''; await loadAll() }
}

async function delFav(f) {
  const r = await fetch('/api/favorites/' + f.id, { method: 'DELETE', headers: authHeaders() })
  msg.value = r.ok ? '已删除收藏' : '删除失败'
  if (r.ok) await loadAll()
}

async function rate(row, rating) {
  const r = await fetch('/api/feedback', {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      query_id: row.query_id || '', user_input: row.user_input || '',
      generated_sql: row.generated_sql || '', rating, comment: comment.value,
    }),
  })
  const j = await r.json().catch(() => ({}))
  msg.value = r.ok ? (rating === 1 ? '已点赞' : '已记录问题') : (j.detail || '反馈失败')
  if (r.ok) { comment.value = ''; await loadAll() }
}

function reuse(f) { emit('reuse', f.user_input || f.title) }

onMounted(loadAll)
</script>

<template>
  <div class="ws">
    <div class="bar">
      <span class="hint">当前身份的历史 / 收藏 / 反馈（按调用者隔离）。</span>
      <button class="ghost" :disabled="busy" @click="loadAll()">{{ busy ? '加载中…' : '刷新' }}</button>
    </div>
    <p v-if="msg" class="msg">{{ msg }}</p>

    <section class="card">
      <h2>查询历史<span class="n">（共 {{ histTotal }} 条）</span></h2>
      <div class="fb-row">
        <input v-model="favTitle" class="q small" placeholder="收藏标题（留空则用问题原文）" />
        <input v-model="comment" class="q small" placeholder="反馈备注（点👍/👎时一并提交）" />
      </div>
      <p v-if="!history.length" class="empty">暂无查询历史。</p>
      <div v-for="h in history" :key="h.query_id || h.id" class="row">
        <div class="main">
          <span class="q-text">{{ h.user_input }}</span>
          <span class="meta">
            {{ h.created_at }} · 指标 {{ h.matched_metric || '—' }} ·
            {{ h.is_success ? '成功' : '未命中' }} · {{ h.result_rows }} 行
          </span>
        </div>
        <div class="ops">
          <button class="ghost tiny" @click="addFav(h)">收藏</button>
          <button class="ghost tiny" title="点赞" @click="rate(h, 1)">👍</button>
          <button class="ghost tiny" title="点踩" @click="rate(h, -1)">👎</button>
        </div>
      </div>
    </section>

    <section class="card">
      <h2>我的收藏<span class="n">（{{ favorites.length }} 条）</span></h2>
      <p v-if="!favorites.length" class="empty">暂无收藏。可在上方历史里点「收藏」。</p>
      <div v-for="f in favorites" :key="f.id" class="row">
        <div class="main">
          <span class="q-text">{{ f.title }}</span>
          <span class="meta">{{ f.created_at }} · {{ f.user_input }}</span>
        </div>
        <div class="ops">
          <button class="ghost tiny" @click="reuse(f)">复跑</button>
          <button class="ghost tiny" @click="delFav(f)">删除</button>
        </div>
      </div>
    </section>

    <section class="card">
      <h2>我的反馈<span class="n">（{{ feedback.length }} 条）</span></h2>
      <p v-if="!feedback.length" class="empty">暂无反馈。点踩的坏例可用于补充评测集。</p>
      <div v-for="fb in feedback" :key="fb.id" class="row">
        <div class="main">
          <span class="q-text">{{ fb.rating === 1 ? '👍' : '👎' }} {{ fb.user_input }}</span>
          <span class="meta">{{ fb.created_at }}{{ fb.comment ? ' · ' + fb.comment : '' }}</span>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.ws { display: flex; flex-direction: column; gap: 16px; margin-top: 4px; }
.bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.bar .hint { color: var(--text-muted); font-size: 13px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 20px; }
.card h2 { margin: 0 0 14px; font-size: 17px; color: var(--brand-2); }
.card h2 .n { color: var(--text-muted); font-size: 13px; font-weight: 500; }
.msg { margin: 0; color: var(--accent); font-size: 13px; }
.empty { color: var(--text-muted); font-size: 13.5px; margin: 6px 0 0; }
.fb-row { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.q.small { padding: 9px 12px; font-size: 13px; border-radius: 10px; }
.row { display: flex; align-items: center; justify-content: space-between; gap: 12px;
       padding: 11px 0; border-top: 1px solid var(--border); }
.row .main { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.q-text { font-size: 14px; color: var(--text); word-break: break-word; }
.meta { color: var(--text-muted); font-size: 12px; }
.ops { display: flex; gap: 8px; flex: none; }
.ghost { border: 1px solid var(--border); background: transparent; color: var(--text);
         border-radius: 10px; padding: 8px 14px; font-size: 13px; font-weight: 600; transition: .15s; }
.ghost:hover:not(:disabled) { border-color: var(--brand); color: var(--brand-2); }
.ghost:disabled { opacity: .6; cursor: not-allowed; }
.ghost.tiny { padding: 5px 11px; font-size: 12.5px; border-radius: 8px; }
</style>
