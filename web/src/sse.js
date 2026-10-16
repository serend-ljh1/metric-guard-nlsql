// SSE 事件解析与归并（从 App.vue 抽出，便于单测 + 复用）。
// 纯函数：只依赖输入事件数组，不触碰任何 Vue 响应式状态，因此可直接用 node:test 断言。

/**
 * 按 agent 名归并出"卡片生命周期"：start 建卡 → step 追加步骤 → decision 记决策 → done 收尾。
 * @param {Array<object>} evts 原始 SSE 事件（{type,name,role,detail,stage,at,payload}）
 * @returns {Array<{name,role,detail,status,steps,decisions,result}>}
 */
export function reduceAgents(evts) {
  const map = new Map()
  for (const e of evts || []) {
    if (e.type === 'agent_start') {
      map.set(e.name, {
        name: e.name, role: e.role, detail: e.detail || '',
        status: 'running', steps: [], decisions: [],
      })
    } else if (e.type === 'agent_step') {
      const a = map.get(e.name)
      if (a) a.steps.push({ stage: e.stage, detail: e.detail, at: e.at })
    } else if (e.type === 'agent_decision') {
      // 决策轨迹（L4）：候选/选中/被拒+原因/门控，供重放时展示"为什么这么选"。
      const a = map.get(e.name)
      if (a) {
        const p = e.payload || {}
        a.decisions.push({
          stage: e.stage, detail: e.detail, at: e.at,
          candidates: p.candidates || [], chosen: p.chosen || null,
          rejected: p.rejected || [], gate: p.gate || null,
        })
      }
    } else if (e.type === 'agent_done') {
      const a = map.get(e.name)
      if (a) { a.status = 'done'; a.result = e.payload }
    }
  }
  return Array.from(map.values())
}

/**
 * 从事件流里取最后一条 error 事件的 detail（没有则返回空串）。
 * @param {Array<object>} evts
 * @returns {string}
 */
export function lastError(evts) {
  let err = ''
  for (const e of evts || []) {
    if (e.type === 'error') err = e.detail || '分析出错'
  }
  return err
}
