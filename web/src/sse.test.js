// reduceAgents / lastError 的单元测试（node:test，零额外依赖）。
// 运行：npm test （即 node --test src/）
import test from 'node:test'
import assert from 'node:assert/strict'
import { reduceAgents, lastError } from './sse.js'

test('reduceAgents：start/step/done 归并出卡片生命周期', () => {
  const evts = [
    { type: 'session_start', question: 'q' },
    { type: 'agent_start', name: 'RouterAgent', role: '意图分流', detail: '判断意图' },
    { type: 'agent_step', name: 'RouterAgent', stage: 'match', detail: '命中 gmv', at: '10:00:00' },
    { type: 'agent_done', name: 'RouterAgent', payload: { route: 'analyze' } },
  ]
  const agents = reduceAgents(evts)
  assert.equal(agents.length, 1)
  assert.equal(agents[0].name, 'RouterAgent')
  assert.equal(agents[0].status, 'done')
  assert.equal(agents[0].steps.length, 1)
  assert.equal(agents[0].steps[0].stage, 'match')
  assert.deepEqual(agents[0].result, { route: 'analyze' })
})

test('reduceAgents：step/done 先于 start 到达时不抛错（事件缺失防御）', () => {
  const agents = reduceAgents([
    { type: 'agent_step', name: 'X', stage: 's', detail: 'd' },
    { type: 'agent_done', name: 'X', payload: { ok: true } },
  ])
  assert.deepEqual(agents, [])
})

test('reduceAgents：多个 Agent 保持插入顺序', () => {
  const evts = [
    { type: 'agent_start', name: 'A', role: 'r' },
    { type: 'agent_start', name: 'B', role: 'r' },
    { type: 'agent_done', name: 'A', payload: {} },
    { type: 'agent_done', name: 'B', payload: {} },
  ]
  const names = reduceAgents(evts).map((a) => a.name)
  assert.deepEqual(names, ['A', 'B'])
})

test('reduceAgents：agent_decision 归并入卡片的 decisions（候选/选中/被拒/门控）', () => {
  const evts = [
    { type: 'agent_start', name: 'DecisionAgent', role: '决策收口' },
    {
      type: 'agent_decision', name: 'DecisionAgent', stage: 'gate', detail: '决策门控 → alert',
      payload: {
        candidates: [{ level: 'alert' }, { level: 'watch' }],
        chosen: { level: 'alert' },
        rejected: [{ option: 'watch', reason: '已通过检验' }],
        gate: { z: 2.1, p_value: 0.03, conclusion: 'alert' },
      },
    },
    { type: 'agent_done', name: 'DecisionAgent', payload: { alert: true } },
  ]
  const agents = reduceAgents(evts)
  assert.equal(agents.length, 1)
  assert.equal(agents[0].decisions.length, 1)
  const d = agents[0].decisions[0]
  assert.equal(d.stage, 'gate')
  assert.equal(d.candidates.length, 2)
  assert.equal(d.chosen.level, 'alert')
  assert.equal(d.rejected[0].reason, '已通过检验')
  assert.equal(d.gate.conclusion, 'alert')
})

test('reduceAgents：agent_decision 先于 start 到达时不抛错', () => {
  assert.deepEqual(reduceAgents([{ type: 'agent_decision', name: 'X', payload: {} }]), [])
})

test('lastError：取最后一条 error 的 detail；无 error 返回空串', () => {
  assert.equal(lastError([]), '')
  assert.equal(lastError([{ type: 'agent_start', name: 'A' }]), '')
  const errs = [
    { type: 'error', detail: '第一处错' },
    { type: 'error', detail: '最后的错' },
  ]
  assert.equal(lastError(errs), '最后的错')
  // 无 detail 时回落通用文案
  assert.equal(lastError([{ type: 'error' }]), '分析出错')
})
