<template>
  <div>
    <div class="hero">
      <div>
        <h1>总览</h1>
        <p>当前到货日三腿行数、预检、对账批次与结案状态。硬闸：未通过有效预检不能封印；结案后当日预报/实收/回执不可再改。</p>
      </div>
      <button @click="load">刷新</button>
    </div>
    <div class="grid" v-if="d">
      <div class="stat"><div class="k">到货日</div><div class="v">{{ d.dock_day }}</div></div>
      <div class="stat"><div class="k">预报 / 实收 / 回执</div><div class="v">{{ d.expect_count }} / {{ d.receive_count }} / {{ d.confirm_count }}</div></div>
      <div class="stat"><div class="k">未决差额</div><div class="v">{{ d.pending_variance }}</div></div>
      <div class="stat">
        <div class="k">结案</div>
        <div class="v"><span class="tag" :class="d.sealed ? 'seal' : 'bad'">{{ d.sealed ? '已结案' : '未结案' }}</span></div>
      </div>
    </div>
    <div class="panel" v-if="d && d.latest_precheck">
      <h2>最近结案预检</h2>
      <p>
        {{ d.latest_precheck.precheck_no }}（#{{ d.latest_precheck.id }}）·
        <span class="tag" :class="d.latest_precheck.passed ? 'ok' : 'bad'">{{ d.latest_precheck.passed ? '通过' : '未通过' }}</span>
        <span class="tag" :class="d.latest_precheck.status === 'valid' ? 'ok' : 'bad'">
          {{ d.latest_precheck.status === 'valid' ? '有效' : '已失效' }}
        </span>
      </p>
      <p v-if="d.latest_precheck.status !== 'valid'" class="msg">失效原因：{{ d.latest_precheck.invalidate_reason }}</p>
      <p class="mono">三腿指纹 {{ d.latest_precheck.legs_fingerprint.slice(0, 20) }}…</p>
    </div>
    <div class="panel" v-if="d && d.latest_batch">
      <h2>最近对账批次</h2>
      <p>{{ d.latest_batch.batch_no }} · 对齐 {{ d.latest_batch.matched_count }} · 差额 {{ d.latest_batch.variance_count }} · {{ d.latest_batch.status }}</p>
      <p v-if="d.latest_batch.status === 'sealed'">
        封印快照绑定预检：
        <strong v-if="d.latest_batch.precheck_id">#{{ d.latest_batch.precheck_id }}</strong>
        <span v-else class="tag bad">无绑定（异常）</span>
      </p>
    </div>
    <p class="msg">{{ err }}</p>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const d = ref(null)
const err = ref('')
async function load() {
  err.value = ''
  try { d.value = await api('/dashboard') } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
<style scoped>
.mono { font-family: ui-monospace, monospace; font-size: 0.82rem; color: var(--muted); word-break: break-all; }
</style>
