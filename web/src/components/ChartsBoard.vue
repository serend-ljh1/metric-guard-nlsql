<script setup>
// 炫的可视化面板：归因瀑布图 + 主因占比 + 因子占比 + 下钻链。
// 消费后端 /api/analyze/* 的 final.chart 结构：{waterfall, share, tree, factors, summary}
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import * as echarts from 'echarts'

const props = defineProps({
  chart: { type: Object, default: null },
  metric_name: { type: String, default: '' },
})

const els = {
  waterfall: ref(null),
  share: ref(null),
  factors: ref(null),
  tree: ref(null),
}
let charts = {}

function disposeAll() {
  for (const k in charts) { charts[k]?.dispose() }
  charts = {}
}

function render() {
  const c = props.chart
  disposeAll()
  if (!c) return
  const theme = { text: '#c9d2ff', muted: '#8b96c9', brand: '#7c6bff', accent: '#22d3ee',
                  ok: '#34d399', danger: '#f87171', line: '#262f52' }
  const base = (label, sub) => ({
    backgroundColor: 'transparent', textStyle: { color: theme.text },
    title: { text: label, subtext: sub, left: 8, top: 6,
             textStyle: { color: theme.text, fontSize: 14 }, subtextStyle: { color: theme.muted, fontSize: 12 } },
    tooltip: { trigger: 'axis', backgroundColor: '#161e3d', borderColor: theme.line,
               textStyle: { color: theme.text } },
  })

  // ---- 归因瀑布 ----
  let wEl = els.waterfall.value
  if (wEl && c.waterfall && c.waterfall.labels) {
    const w = c.waterfall
    const cats = w.labels || []
    const baseVals = cats.map((_, i) => {
      let acc = 0
      for (let j = 1; j < i; j++) acc += w.deltas[j] || 0
      return acc
    })
    const heights = (w.deltas || []).map(d => Math.abs(d || 0))
    const series = [
      { name: '基线', type: 'bar', stack: 'w', itemStyle: { color: 'transparent' }, data: baseVals },
      { name: '增量', type: 'bar', stack: 'w',
        itemStyle: { color: (p) => p.dataIndex === 0 ? '#7c6bff' : (w.deltas[p.dataIndex] >= 0 ? 'transparent' : 'transparent') },
        data: heights, label: { show: true, position: 'top', color: theme.muted, fontSize: 10 } },
    ]
    charts.waterfall = echarts.init(wEl)
    charts.waterfall.setOption({
      ...base('归因瀑布', `${props.metric_name} 从基期到当期的变化`),
      grid: { left: 50, right: 16, top: 44, bottom: 8, containLabel: true },
      xAxis: { type: 'category', data: cats, axisLabel: { color: theme.muted, rotate: 24 } },
      yAxis: { type: 'value', axisLabel: { color: theme.muted } },
      series,
    })
  }

  // ---- 主因占比（横向条形：用 delta 绝对值显示各维度贡献） ----
  let sEl = els.share.value
  if (sEl && c.share && c.share.dims) {
    const dims = c.share.dims || []
    const vals = c.share.values || []
    charts.share = echarts.init(sEl)
    charts.share.setOption({
      ...base('主因贡献', '各维度对波动的贡献（绝对值）'),
      grid: { left: 130, right: 30, top: 44, bottom: 20, containLabel: true },
      xAxis: { type: 'value', axisLabel: { color: theme.muted } },
      yAxis: { type: 'category', data: dims, axisLabel: { color: theme.text, width: 110, overflow: 'truncate' } },
      series: [{
        type: 'bar', barWidth: 18, data: vals,
        itemStyle: { color: (p) => (vals[p.dataIndex] >= 0 ? theme.brand : theme.danger),
                     borderRadius: 6 },
        label: { show: true, position: 'right', color: theme.muted, fontSize: 10,
                 formatter: ({ value }) => (value >= 0 ? '+' : '') + value.toFixed(0) },
      }],
    })
  }

  // ---- 因子占比（环形，factorize） ----
  let fEl = els.factors.value
  if (fEl && c.factors && c.factors.labels && c.factors.labels.length) {
    const labels = c.factors.labels || []
    const shares = c.factors.shares || []
    charts.factors = echarts.init(fEl)
    charts.factors.setOption({
      ...base('因子占比', c.summary?.metric_name ? `${c.summary.metric_name} 拆解` : ''),
      tooltip: { trigger: 'item',
                 formatter: '{b}: {c}%' },
      legend: { bottom: 0, textStyle: { color: theme.muted }, itemWidth: 12, itemHeight: 12 },
      series: [{
        type: 'pie', radius: ['45%', '72%'], center: ['50%', '46%'],
        data: labels.map((l, i) => ({ name: l, value: shares[i] || 0 })),
        label: { color: theme.text, formatter: '{b} {d}%' },
        itemStyle: { borderRadius: 6, borderColor: '#0b1020', borderWidth: 2 },
      }],
    })
  }

  // ---- 下钻树（drill 定位） ----
  let tEl = els.tree.value
  if (tEl && c.tree && c.tree.length) {
    charts.tree = echarts.init(tEl)
    charts.tree.setOption({
      ...base('下钻定位链', '沿路径逐层拆到主因'),
      tooltip: { trigger: 'item' },
      series: [{
        type: 'tree', data: c.tree, orient: 'LR', top: 30, bottom: 30, left: 20, right: 20,
        symbol: 'circle', symbolSize: 9,
        lineStyle: { color: theme.line, width: 1.5 },
        label: { color: theme.text, fontSize: 11, position: 'right' },
        leaves: { label: { position: 'right', color: theme.accent } },
        emphasis: { focus: 'descendant' },
      }],
    })
  }
}

function resizeAll() { for (const k in charts) charts[k]?.resize() }

onMounted(() => { render(); window.addEventListener('resize', resizeAll) })
onBeforeUnmount(() => { disposeAll(); window.removeEventListener('resize', resizeAll) })
watch(() => props.chart, () => nextTick(render))
</script>

<template>
  <section v-if="chart" class="board">
    <div class="cell" ref="els.waterfall.value"></div>
    <div class="cell" ref="els.share.value"></div>
    <div class="cell" ref="els.factors.value"></div>
    <div class="cell" ref="els.tree.value"></div>
  </section>
</template>

<style scoped>
.board { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.cell { height: 300px; background: var(--surface); border: 1px solid var(--border);
        border-radius: 16px; padding: 6px; }
@media (max-width: 820px) { .board { grid-template-columns: 1fr; } }
</style>