// Tests for omarchy-plugin/Model.js — the bar widget's formatting — under node.
//
//   node tests/test_omarchy_model.mjs
//
// Model.js is a QML JavaScript library (`.pragma library`); the pragma line
// is dropped and the rest evaluated as a plain script.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'
import vm from 'node:vm'

const source = readFileSync(fileURLToPath(new URL('../omarchy-plugin/Model.js', import.meta.url)), 'utf8')
const M = {}
vm.runInNewContext(source.replace(/^\.pragma library\s*$/m, ''), M)

let passed = 0
function test(name, fn) {
  try { fn(); passed++; console.log(`  ✓ ${name}`) }
  catch (e) { console.log(`  ✗ ${name}\n    ${e.message}`); process.exitCode = 1 }
}

const now = new Date('2026-09-28T18:00:00+02:00').getTime()
const job = (state, extra = {}) => ({
  id: 'backup-x', name: 'Music', service: 'backup-x.service',
  source: '/mnt/SECONDARY/music/', destination: '/mnt/DATA-SLOW/Music/',
  schedule: 'Mon *-*-* 11:00:00', next_run: '', last_run: null,
  status: { state, progress: 0, consecutive_failures: 0 }, ...extra,
})

test('summary follows the snapshot order', () => {
  assert.equal(M.summaryState([job('idle'), job('running'), job('unavailable')]), 'running')
  assert.equal(M.summaryState([job('running'), job('error')]), 'error')
  assert.equal(M.summaryState([]), 'idle')
})

test('state labels', () => {
  assert.equal(M.stateLabel(job('idle')), 'Never ran')
  assert.equal(M.stateLabel(job('idle', { last_run: { exit_code: 0 } })), 'Done')
  assert.equal(M.stateLabel(job('idle', { last_run: { exit_code: 23 } })), 'Failed')
  assert.equal(M.stateLabel(job('error', { status: { state: 'error', consecutive_failures: 3 } })), 'Error 3×')
  assert.equal(M.stateLabel(job('unavailable')), 'Unavailable')
})

test('actions per state', () => {
  assert.deepEqual(Array.from(M.actions(job('idle'))), ['start'])
  assert.deepEqual(Array.from(M.actions(job('running'))), ['pause', 'stop'])
  assert.deepEqual(Array.from(M.actions(job('paused'))), ['resume', 'stop'])
  assert.deepEqual(Array.from(M.actions(job('queued'))), ['stop'])
  assert.deepEqual(Array.from(M.actions(job('unavailable'))), [])
})

test('countdown', () => {
  assert.equal(M.countdown('2026-09-30T11:00:00+02:00', now), 'in 1d 17h')
  assert.equal(M.countdown('2026-09-28T18:05:30+02:00', now), 'in 5m')
  assert.equal(M.countdown('2026-09-28T17:00:00+02:00', now), 'now')
  assert.equal(M.countdown('', now), '')
})

test('relative past', () => {
  assert.equal(M.relativePast('2026-09-28T17:59:30+02:00', now), 'just now')
  assert.equal(M.relativePast('2026-09-28T15:30:00+02:00', now), '2h ago')
  assert.equal(M.relativePast('2026-09-27T22:06:31+02:00', now), 'yesterday 22:06')
  assert.equal(M.relativePast('2026-09-01T10:00:00+02:00', now), 'Sep 1')
})

test('route shortens mount paths', () => {
  assert.equal(M.route(job('idle')), 'SECONDARY/music → DATA-SLOW/Music')
})

test('activity line', () => {
  const j = job('running', { status: { state: 'running', progress: 42.4, speed: '12.3MB/s', eta: '0:03:12', files_transferred: 120, files_total: 400 } })
  assert.equal(M.activityLine(j, now), '42%  ·  12.3MB/s  ·  ETA 0:03:12  ·  120 / 400 files')
  const s = job('scanning', { status: { state: 'scanning', scan_read: '1.2 GB', started: '2026-09-28T17:58:00+02:00' } })
  assert.equal(M.activityLine(s, now), 'Building file list…  ·  1.2 GB read  ·  2:00')
  const z = job('scanning', { status: { state: 'scanning', scan_read: '0 B', started: '2026-09-28T17:59:56+02:00' } })
  assert.equal(M.activityLine(z, now), 'Building file list…  ·  0:04')
})

test('activity line through the phases', () => {
  const l = job('scanning', { status: { state: 'scanning', phase: 'listing', phase_label: 'Reading the source',
    progress_text: '~75%', phase_count: '3,000 / ~4,000 files', eta: '0:01:10', scan_read: '1.2 GB',
    started: '2026-09-28T17:58:00+02:00' } })
  assert.equal(M.activityLine(l, now), 'Reading the source  ·  ~75%  ·  ETA 0:01:10  ·  3,000 / ~4,000 files  ·  2:00')
  const c = job('running', { status: { state: 'running', phase: 'checking', phase_label: '', progress: 50,
    progress_text: '50%', speed: '1.23MB/s', phase_count: '2,051 / 4,100 checked  ·  1 copied',
    files_transferred: 1, files_total: 4100 } })
  assert.equal(M.activityLine(c, now), '50%  ·  1.23MB/s  ·  2,051 / 4,100 checked  ·  1 copied')
  const p = job('running', { status: { state: 'running', phase: 'pruning', phase_label: 'Removing expired archives',
    progress_text: '', phase_count: '' } })
  assert.equal(M.activityLine(p, now), 'Removing expired archives')
})

test('schedule line names the host of the last run', () => {
  const j = job('idle', {
    next_run: '2026-09-30T11:00:00+02:00',
    last_run: { started: '2026-09-28T11:19:51+02:00', exit_code: 0, host: 'petronijus-PC' },
  })
  assert.equal(M.scheduleLine(j, now), 'Next in 1d 17h  ·  last 6h ago on petronijus-PC')
  assert.equal(M.scheduleLine(job('deferred', { status: { state: 'deferred', deferred_reason: 'interrupted' } }), now),
    'Interrupted — restarts at next start')
  assert.equal(M.scheduleLine(job('idle', { schedule: '' }), now), 'Manual only')
})

test('hero line', () => {
  assert.equal(M.heroMeta([]), 'No backup jobs')
  assert.equal(M.heroMeta([job('idle')]), 'All mirrors up to date')
  assert.equal(M.heroMeta([job('running', { status: { state: 'running', progress: 42 } })]), 'Music syncing 42%')
  assert.equal(M.heroMeta([job('scanning', { status: { state: 'scanning', progress: 75, progress_text: '~75%' } })]),
    'Music scanning ~75%')
  assert.equal(M.heroMeta([job('scanning', { status: { state: 'scanning', progress: 0 } })]), 'Music scanning')
  assert.equal(M.heroMeta([job('error'), job('idle')]), '1 backup failed')
  assert.equal(M.heroMeta([job('unavailable'), job('unavailable')]), 'Destinations not mounted')
})

console.log(`\n${passed} passed`)
