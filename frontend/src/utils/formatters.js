export function formatDuration(ms) {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`
  const m = Math.floor(ms / 60000)
  const s = Math.round((ms % 60000) / 1000)
  return `${m} m ${s} s`
}

export function formatUptime(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—'
  const s = Math.max(0, Math.floor(seconds))
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d} d ${h} h`
  if (h > 0) return `${h} h ${m} m`
  if (m > 0) return `${m} m`
  return `${s} s`
}

const PRIORITY_NAMES = { 1: 'Critical', 2: 'High', 3: 'Moderate', 4: 'Low', 5: 'Planning' }

export function priorityNumber(n) {
  const p = Number(n)
  return Number.isInteger(p) && p >= 1 && p <= 5 ? p : null
}

export function formatPriority(n) {
  const p = priorityNumber(n)
  return p ? `P${p} ${PRIORITY_NAMES[p]}` : n ? `P${n}` : '—'
}

export function formatRelativeTime(ts) {
  if (!ts) return ''
  const t = new Date(ts).getTime()
  if (Number.isNaN(t)) return ''
  const diff = (Date.now() - t) / 1000
  if (diff < 5) return 'just now'
  if (diff < 60) return `${Math.round(diff)} s ago`
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`
  return new Date(ts).toLocaleDateString()
}

export function formatTimestamp(ts) {
  if (!ts) return '—'
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString()
}

export function formatClock(ms) {
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

// Terminal outcomes of a run. `failed` and `partial` only appear in history recorded
// before outcomes became explicit; both meant a person had to pick the incident up.
const OUTCOME_LABELS = {
  resolved: 'Resolved',
  escalated: 'Escalated',
  needs_human: 'Needs human',
  aborted: 'Stopped',
  failed: 'Needs human',
  partial: 'Needs human',
}

const OUTCOME_TONES = {
  resolved: 'resolved',
  escalated: 'escalated',
  needs_human: 'failed',
  aborted: 'failed',
  failed: 'failed',
  partial: 'failed',
}

export function outcomeTone(s) {
  return OUTCOME_TONES[(s || '').toLowerCase()] || 'neutral'
}

export function outcomeLabel(s) {
  const key = (s || '').toLowerCase()
  return OUTCOME_LABELS[key] || (s ? String(s) : 'Unknown')
}

const STEP_LABELS = {
  SPLUNK_SEARCH: 'Splunk search',
  MQ_CHECK: 'MQ check',
  FILE_CHECK: 'File check',
  AUTOSYS_STATUS: 'Autosys status',
  DYNATRACE_VM_CHECK: 'Dynatrace VM check',
  DYNATRACE_METRICS: 'Dynatrace metrics',
  WEB_UI_CHECK: 'Web UI check',
  MAINFRAME_CHECK: 'Mainframe check',
  DECISION: 'Decision',
  NOTE: 'Note',
}

function sentenceCase(s) {
  const t = String(s).replace(/[_-]+/g, ' ').trim().toLowerCase()
  return t ? t[0].toUpperCase() + t.slice(1) : ''
}

export function stepLabel(type) {
  const key = (type || '').toUpperCase()
  return STEP_LABELS[key] || sentenceCase(type || 'Step')
}

const STEP_STATUS_LABELS = { success: 'Passed', fail: 'Failed', skip: 'Skipped', escalated: 'Escalated', error: 'Could not run' }
export function stepStatusLabel(status) {
  const key = (status || '').toLowerCase()
  return STEP_STATUS_LABELS[key] || sentenceCase(status || 'unknown')
}
export function stepStatusTone(status) {
  const key = (status || '').toLowerCase()
  return STEP_STATUS_LABELS[key] ? key : 'skip'
}

const REASON_LABELS = {
  no_sop: 'No SOP matched',
  no_sop_ai: 'No SOP matched (AI)',
  low_confidence: 'Low SOP confidence',
  low_confidence_ai: 'Low SOP confidence (AI)',
  sop_execution: 'SOP step failed',
}

/** For metric label keys such as `no_sop`. */
export function reasonLabel(key) {
  return REASON_LABELS[key] || sentenceCase(key || 'unknown')
}

/** For event payloads, where `reason` may be a key or already free text; free text is left as written. */
export function escalationReasonText(reason) {
  return REASON_LABELS[reason] || reason || 'no reason given'
}

/** CSV field with RFC 4180 quoting; leading = + - @ are neutralised to stop spreadsheet formula injection. */
export function csvField(value) {
  let s = value === null || value === undefined ? '' : String(value)
  if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`
  return `"${s.replace(/"/g, '""')}"`
}
