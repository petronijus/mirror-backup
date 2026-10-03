.pragma library

// Pure formatting for the Mirror Backup bar widget. The data is the snapshot
// `mirror-backup status --json` prints (backup_monitor/services/snapshot.py);
// nothing here reads files or runs processes, so tests/test_omarchy_model.mjs
// can exercise it under node.

var STATE_LABELS = {
  idle: "Idle",
  queued: "Queued",
  scanning: "Scanning",
  running: "Syncing",
  paused: "Paused",
  deferred: "Postponed",
  error: "Error",
  unavailable: "Unavailable"
}

// Same order as snapshot.summary_state: what the bar icon shows.
var SUMMARY_ORDER = ["error", "running", "scanning", "paused", "queued", "deferred", "unavailable"]

function jobState(job) {
  return job && job.status ? String(job.status.state || "idle") : "idle"
}

function isActive(state) {
  return state === "running" || state === "scanning" || state === "paused"
}

function isBusy(state) {
  return isActive(state) || state === "queued"
}

function summaryState(jobs) {
  var states = (jobs || []).map(jobState)
  for (var i = 0; i < SUMMARY_ORDER.length; i++) {
    if (states.indexOf(SUMMARY_ORDER[i]) !== -1) return SUMMARY_ORDER[i]
  }
  return "idle"
}

function lastRunOk(job) {
  return !!(job && job.last_run && job.last_run.exit_code === 0)
}

function stateLabel(job) {
  var state = jobState(job)
  if (state === "idle") return lastRunOk(job) ? "Done" : (job && job.last_run ? "Failed" : "Never ran")
  var label = STATE_LABELS[state] || state
  var streak = job && job.status ? Number(job.status.consecutive_failures || 0) : 0
  if (state === "error" && streak >= 2) label += " " + streak + "×"
  return label
}

// Which actions a job offers in its state.
function actions(job) {
  var state = jobState(job)
  if (state === "unavailable") return []
  if (state === "running" || state === "scanning") return ["pause", "stop"]
  if (state === "paused") return ["resume", "stop"]
  if (state === "queued") return ["stop"]
  return ["start"]
}

function parseTime(iso) {
  if (!iso) return NaN
  var t = new Date(iso).getTime()
  return isFinite(t) ? t : NaN
}

function countdown(iso, nowMs) {
  var t = parseTime(iso)
  if (isNaN(t)) return ""
  var sec = Math.floor((t - nowMs) / 1000)
  if (sec <= 0) return "now"
  var d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60)
  if (d > 0) return "in " + d + "d " + h + "h"
  if (h > 0) return "in " + h + "h " + m + "m"
  if (m > 0) return "in " + m + "m"
  return "in " + sec + "s"
}

var WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

function pad2(n) { return (n < 10 ? "0" : "") + n }

function relativePast(iso, nowMs) {
  var t = parseTime(iso)
  if (isNaN(t)) return ""
  var sec = Math.floor((nowMs - t) / 1000)
  var then = new Date(t)
  var clock = pad2(then.getHours()) + ":" + pad2(then.getMinutes())
  if (sec < 0) return clock
  if (sec < 60) return "just now"
  var now = new Date(nowMs)
  var startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  var days = Math.floor((startOfToday - new Date(then.getFullYear(), then.getMonth(), then.getDate()).getTime()) / 86400000)
  if (days <= 0) return sec < 3600 ? Math.floor(sec / 60) + "m ago" : Math.floor(sec / 3600) + "h ago"
  if (days === 1) return "yesterday " + clock
  if (days < 7) return WEEKDAYS[then.getDay()] + " " + clock
  return MONTHS[then.getMonth()] + " " + then.getDate()
}

function elapsed(iso, nowMs) {
  var t = parseTime(iso)
  if (isNaN(t)) return ""
  var sec = Math.max(0, Math.floor((nowMs - t) / 1000))
  var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60
  return h > 0 ? h + ":" + pad2(m) + ":" + pad2(s) : m + ":" + pad2(s)
}

function shortenPath(p) {
  if (!p) return ""
  var parts = String(p).replace(/\/$/, "").split("/")
  return parts.length <= 2 ? String(p) : "…/" + parts.slice(-2).join("/")
}

// "/mnt/FUN/" → "FUN", "/mnt/DATA-SLOW/BACKUP/FUN/" → "DATA-SLOW/BACKUP/FUN"
function shortMount(p) {
  var s = String(p || "").replace(/\/+$/, "")
  var m = s.match(/^\/(?:mnt|media\/[^/]+)\/(.+)$/)
  return m ? m[1] : s
}

function route(job) {
  return shortMount(job.source) + " → " + shortMount(job.destination)
}

// phase_label, phase_count and progress_text are worded by mirror-backup
// (models/job.py); a status written by a backup-sync without phases has none.
function activityLine(job, nowMs) {
  var st = job.status || {}
  var state = jobState(job)
  var parts = []
  if (st.phase_label) parts.push(st.phase_label)
  else if (state === "scanning") parts.push("Building file list…")
  var pct = st.progress_text !== undefined ? st.progress_text
    : st.progress > 0 ? Math.round(st.progress) + "%" : ""
  if (pct) parts.push(pct)
  if (st.speed) parts.push(st.speed)
  if (st.eta && st.eta !== "0:00:00") parts.push("ETA " + st.eta)
  if (st.phase_count) parts.push(st.phase_count)
  else if (st.files_total > 0) parts.push(st.files_transferred + " / " + st.files_total + " files")
  if (state === "scanning") {
    // Nothing read yet (metadata from the cache, or over NFS) says nothing.
    if (!st.phase && st.scan_read && !/^0 B$/.test(st.scan_read)) parts.push(st.scan_read + " read")
    var el = elapsed(st.started, nowMs)
    if (el) parts.push(el)
  }
  return parts.join("  ·  ")
}

function lastRunText(job, nowMs) {
  var last = job && job.last_run
  if (!last) return ""
  var text = "last " + relativePast(last.started, nowMs)
  if (last.host) text += " on " + last.host
  if (last.exit_code !== 0) text += " (failed)"
  return text
}

function scheduleLine(job, nowMs) {
  var st = job.status || {}
  var state = jobState(job)
  var parts = []
  if (state === "deferred") {
    parts.push(st.deferred_reason === "suspend" ? "Restarts after wake"
      : st.deferred_reason === "interrupted" ? "Interrupted — restarts at next start"
      : "Restarts at next start")
  } else if (!isBusy(state) && job.next_run) {
    var cd = countdown(job.next_run, nowMs)
    if (cd) parts.push("Next " + cd)
  } else if (!isBusy(state) && !job.schedule) {
    parts.push("Manual only")
  }
  var last = lastRunText(job, nowMs)
  if (last && !isBusy(state)) parts.push(last)
  return parts.join("  ·  ")
}

function heroMeta(jobs) {
  jobs = jobs || []
  if (jobs.length === 0) return "No backup jobs"
  var active = jobs.filter(function(j) { return isActive(jobState(j)) })
  if (active.length === 1) {
    var st = active[0].status || {}
    var pct = st.progress_text !== undefined ? st.progress_text
      : st.progress > 0 ? Math.round(st.progress) + "%" : ""
    return active[0].name + (jobState(active[0]) === "paused" ? " paused"
      : (jobState(active[0]) === "scanning" ? " scanning" : " syncing") + (pct ? " " + pct : ""))
  }
  if (active.length > 1) return active.length + " backups running"
  var failed = jobs.filter(function(j) { return jobState(j) === "error" }).length
  if (failed) return failed + (failed === 1 ? " backup failed" : " backups failed")
  var queued = jobs.filter(function(j) { return jobState(j) === "queued" }).length
  if (queued) return queued + " queued"
  var away = jobs.filter(function(j) { return jobState(j) === "unavailable" }).length
  if (away === jobs.length) return "Destinations not mounted"
  if (jobs.some(function(j) { return jobState(j) === "deferred" })) return "Postponed runs pending"
  return "All mirrors up to date"
}

function tooltip(jobs, nowMs) {
  jobs = jobs || []
  var lines = ["Mirror Backup — " + heroMeta(jobs)]
  var newest = null
  jobs.forEach(function(j) {
    if (j.last_run && (!newest || parseTime(j.last_run.started) > parseTime(newest.last_run.started))) newest = j
  })
  if (newest && !jobs.some(function(j) { return isActive(jobState(j)) })) {
    lines.push(newest.name + " " + lastRunText(newest, nowMs))
  }
  return lines.join("\n")
}
