<template>
  <div>
    <div class="hero">
      <div>
        <h1>结案预检</h1>
        <p>封印前硬闸：物化缺腿扫描、未决差额、对账批绑定三项清单与三腿指纹。只有通过且未失效的预检批才能用于结案；预检后三腿再装载/重跑对账/改差额命运都会使旧预检失效。</p>
      </div>
    </div>
    <div class="panel">
      <div class="row">
        <input v-model="day" placeholder="到货日 YYYY-MM-DD" />
        <button @click="run">跑结案预检</button>
        <button class="secondary" @click="load">刷新</button>
      </div>
      <p class="msg">{{ err }}</p>
      <p class="okmsg">{{ ok }}</p>
    </div>

    <div class="panel" v-if="latest">
      <h2>最近预检批 {{ latest.precheck_no }}</h2>
      <div class="grid">
        <div class="stat"><div class="k">结论</div><div class="v">
          <span class="tag" :class="latest.passed ? 'ok' : 'bad'">{{ latest.passed ? '通过' : '未通过' }}</span>
        </div></div>
        <div class="stat"><div class="k">状态</div><div class="v">
          <span class="tag" :class="latest.status === 'valid' ? 'ok' : 'bad'">
            {{ latest.status === 'valid' ? '有效' : '已失效' }}
          </span>
        </div></div>
        <div class="stat"><div class="k">三腿行数（预/实/回）</div><div class="v">
          {{ s.expect_rows }} / {{ s.receive_rows }} / {{ s.confirm_rows }}
        </div></div>
        <div class="stat"><div class="k">三腿键并集</div><div class="v">{{ s.union_keys }}</div></div>
        <div class="stat"><div class="k">缺腿</div><div class="v">{{ s.missing_leg_count }}</div></div>
        <div class="stat"><div class="k">批绑定问题</div><div class="v">{{ s.batch_binding_issue_count }}</div></div>
        <div class="stat"><div class="k">未决差额</div><div class="v">{{ s.pending_variance_count }}</div></div>
        <div class="stat"><div class="k">预检批 ID</div><div class="v">#{{ latest.id }}</div></div>
      </div>
      <p class="mono">指纹 {{ latest.legs_fingerprint.slice(0, 16) }}…</p>
      <p v-if="latest.status !== 'valid'" class="msg">失效原因：{{ latest.invalidate_reason }}（{{ latest.invalidated_at }}）</p>
      <p v-if="latest.passed && latest.status === 'valid'" class="okmsg">
        预检有效，可携带 precheck_id={{ latest.id }} 前往「三联对账」结案。
      </p>
    </div>

    <div class="panel" v-if="findings.length">
      <h2>预检发现（扫描物化，共 {{ findings.length }} 条）</h2>
      <table>
        <thead><tr><th>类型</th><th>供应商</th><th>货号</th><th>明细</th></tr></thead>
        <tbody>
          <tr v-for="f in findings" :key="f.id">
            <td><span class="tag" :class="kindClass(f.kind)">{{ kindLabel(f.kind) }}</span></td>
            <td>{{ f.supplier_code || '—' }}</td>
            <td>{{ f.sku || '—' }}</td>
            <td class="mono">{{ f.detail }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="history.length">
      <h2>预检批历史</h2>
      <table>
        <thead><tr><th>预检号</th><th>到货日</th><th>结论</th><th>状态</th><th>失效原因</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="p in history" :key="p.id">
            <td>{{ p.precheck_no }}</td>
            <td>{{ p.dock_day }}</td>
            <td><span class="tag" :class="p.passed ? 'ok' : 'bad'">{{ p.passed ? '通过' : '未通过' }}</span></td>
            <td>{{ p.status === 'valid' ? '有效' : '已失效' }}</td>
            <td>{{ p.invalidate_reason || '—' }}</td>
            <td>{{ p.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
const day = ref('2026-11-18')
const history = ref([])
const findings = ref([])
const err = ref('')
const ok = ref('')
const latest = computed(() => history.value[0] || null)
const s = computed(() => latest.value?.scan_summary || {})
function kindLabel(k) {
  return { missing_leg: '缺腿', pending_variance: '未决差额', batch_binding: '批绑定' }[k] || k
}
function kindClass(k) {
  return k === 'missing_leg' ? 'bad' : (k === 'pending_variance' ? 'bad' : 'seal')
}
async function load() {
  err.value = ''
  try {
    history.value = await api(`/precheck/batches?dock_day=${encodeURIComponent(day.value)}`)
    if (latest.value) {
      const detail = await api(`/precheck/batches/${latest.value.id}`)
      findings.value = detail.findings || []
    } else {
      findings.value = []
    }
  } catch (e) { err.value = e.message }
}
async function run() {
  err.value = ''; ok.value = ''
  try {
    const pc = await api('/precheck/run', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = pc.passed ? '预检通过，可以结案' : '预检未通过，请按发现清单整改后重检'
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
<style scoped>
.mono { font-family: ui-monospace, monospace; font-size: 0.82rem; color: var(--muted); word-break: break-all; }
</style>
