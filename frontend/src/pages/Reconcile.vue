<template>
  <div>
    <div class="hero">
      <div>
        <h1>三联对账</h1>
        <p>按供应商+货号+到货日对齐三数量；不一致进差额。未决差额清零并通过<router-link to="/precheck">结案预检</router-link>后才能封印，封印必须携带有效 precheck_id。</p>
      </div>
    </div>
    <div class="panel">
      <div class="row">
        <input v-model="day" placeholder="到货日" />
        <button @click="run">跑三联对账</button>
        <button class="secondary" @click="loadPrecheck">刷新预检</button>
        <button class="warn" :disabled="!canSeal" @click="seal">结案封印</button>
        <button class="secondary" @click="load">刷新</button>
      </div>
      <p class="msg">{{ err }}</p>
      <p class="okmsg">{{ ok }}</p>
      <p v-if="batch">批次 {{ batch.batch_no }} · 对齐 {{ batch.matched_count }} · 差额 {{ batch.variance_count }} · {{ batch.status }}</p>
      <div v-if="batch && batch.status === 'sealed'" class="snap">
        <strong>封印快照：</strong>
        绑定预检 #{{ batch.precheck_id }}
        <template v-if="snapshot">（{{ snapshot.precheck_no }} · 指纹 {{ snapshot.legs_fingerprint.slice(0, 12) }}…）</template>
      </div>
    </div>

    <div class="panel">
      <h2>预检闸门状态</h2>
      <p v-if="!precheck" class="msg">该到货日尚无预检批——硬闸下不能结案，请先到「结案预检」跑预检。</p>
      <div v-else class="grid">
        <div class="stat"><div class="k">预检号</div><div class="v">{{ precheck.precheck_no }}</div></div>
        <div class="stat"><div class="k">precheck_id</div><div class="v">#{{ precheck.id }}</div></div>
        <div class="stat"><div class="k">结论</div><div class="v">
          <span class="tag" :class="precheck.passed ? 'ok' : 'bad'">{{ precheck.passed ? '通过' : '未通过' }}</span>
        </div></div>
        <div class="stat"><div class="k">状态</div><div class="v">
          <span class="tag" :class="precheck.status === 'valid' ? 'ok' : 'bad'">
            {{ precheck.status === 'valid' ? '有效' : '已失效' }}
          </span>
        </div></div>
      </div>
      <p v-if="precheck && precheck.status !== 'valid'" class="msg">
        预检已失效（{{ precheck.invalidate_reason }}），需重新预检；失效 id 不能用于结案。
      </p>
      <p v-if="precheck && precheck.passed && precheck.status === 'valid' && batch && batch.status !== 'sealed'" class="okmsg">
        预检有效，结案将携带 precheck_id={{ precheck.id }}；封印前服务端会再次校验指纹，防止预检后偷装三腿。
      </p>
    </div>

    <div class="panel">
      <h2>对齐行</h2>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>预报</th><th>实收</th><th>回执</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="r in matches" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td><td>{{ r.expect_qty }}</td><td>{{ r.receive_qty }}</td><td>{{ r.confirm_qty }}</td>
            <td><span class="tag" :class="r.aligned ? 'ok' : 'bad'">{{ r.aligned ? '对齐' : '差额' }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h2>差额处置</h2>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>三数量</th><th>命运</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in variances" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td>
            <td>{{ r.expect_qty }} / {{ r.receive_qty }} / {{ r.confirm_qty }}</td>
            <td>{{ r.fate }}</td>
            <td>
              <button class="secondary" @click="fate(r.id, 'accept_short')">认短</button>
              <button class="secondary" @click="fate(r.id, 'accept_over')">认溢</button>
              <button class="secondary" @click="fate(r.id, 'quarantine')">隔离</button>
            </td>
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
const batch = ref(null)
const precheck = ref(null)
const matches = ref([])
const variances = ref([])
const err = ref('')
const ok = ref('')
const snapshot = computed(() => {
  if (!batch.value?.precheck_summary) return null
  try { return JSON.parse(batch.value.precheck_summary) } catch { return null }
})
const canSeal = computed(() =>
  !!precheck.value && precheck.value.passed && precheck.value.status === 'valid'
  && !!batch.value && batch.value.status !== 'sealed'
)
async function load() {
  err.value = ''
  const batches = await api(`/reconcile/batches?dock_day=${encodeURIComponent(day.value)}`)
  batch.value = batches[0] || null
  if (!batch.value) { matches.value = []; variances.value = []; return }
  matches.value = await api(`/reconcile/matches?batch_id=${batch.value.id}`)
  variances.value = await api(`/reconcile/variances?batch_id=${batch.value.id}`)
}
async function loadPrecheck() {
  const list = await api(`/precheck/batches?dock_day=${encodeURIComponent(day.value)}`)
  precheck.value = list[0] || null
}
async function run() {
  err.value = ''; ok.value = ''
  try {
    batch.value = await api('/reconcile/run', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = '对账完成（旧预检已随对账批重建失效，请重新预检）'
    await loadPrecheck()
    await load()
  } catch (e) { err.value = e.message }
}
async function seal() {
  err.value = ''; ok.value = ''
  if (!precheck.value || !precheck.value.passed || precheck.value.status !== 'valid') {
    err.value = '硬闸：没有有效且通过的预检批，不能结案'
    return
  }
  try {
    batch.value = await api('/reconcile/seal', {
      method: 'POST',
      body: JSON.stringify({ dock_day: day.value, precheck_id: precheck.value.id }),
    })
    ok.value = '已结案，预检已绑定进封印快照'
    await loadPrecheck()
    await load()
  } catch (e) { err.value = e.message }
}
async function fate(id, f) {
  err.value = ''; ok.value = ''
  try {
    await api(`/reconcile/variances/${id}/fate`, { method: 'POST', body: JSON.stringify({ fate: f }) })
    ok.value = '已更新命运（旧预检已失效，请重新预检）'
    await loadPrecheck()
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(async () => {
  await loadPrecheck()
  await load()
})
</script>
<style scoped>
.snap { margin-top: 10px; font-size: 0.9rem; color: var(--accent); }
</style>
