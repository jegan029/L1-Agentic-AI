export function formatDuration(ms) {
  if (!ms && ms !== 0) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  const m = Math.floor(ms / 60000)
  const s = Math.round((ms % 60000) / 1000)
  return `${m}m ${s}s`
}

export function formatPriority(n) {
  const map = { 1: 'P1 Critical', 2: 'P2 High', 3: 'P3 Moderate', 4: 'P4 Low', 5: 'P5 Planning' }
  return map[n] || `P${n}`
}

export function priorityColor(n) {
  const map = { 1: '#ef4444', 2: '#f97316', 3: '#facc15', 4: '#22c55e', 5: '#8b9cc8' }
  return map[n] || '#8b9cc8'
}

export function formatRelativeTime(ts) {
  if (!ts) return ''
  const diff = (Date.now() - new Date(ts).getTime()) / 1000
  if (diff < 5)  return 'just now'
  if (diff < 60) return `${Math.round(diff)}s ago`
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`
  return new Date(ts).toLocaleDateString()
}

export function formatTimestamp(ts) {
  if (!ts) return '—'
  return new Date(ts).toLocaleString()
}

export function outcomeLabel(s) {
  return (s || '').toUpperCase()
}

export function outcomeColor(s) {
  switch ((s || '').toLowerCase()) {
    case 'resolved':  return { bg: '#e8f5ee', text: '#00875A', border: '#b2dfcb' }
    case 'escalated': return { bg: '#fff3e0', text: '#C35109', border: '#ffd5a8' }
    case 'partial':   return { bg: '#fff8e1', text: '#b45309', border: '#fde68a' }
    case 'failed':    return { bg: '#fdecea', text: '#D32F2F', border: '#f5b8b2' }
    default:          return { bg: '#eceef5', text: '#3a4470', border: '#dde0ec' }
  }
}

export function eventIcon(type) {
  switch (type) {
    case 'incident_received':  return 'inbox'
    case 'sop_matched':        return 'auto_awesome'
    case 'step_executing':     return 'play_circle'
    case 'step_done':          return 'check_circle'
    case 'incident_resolved':  return 'verified'
    case 'incident_escalated': return 'escalator_warning'
    default:                   return 'bolt'
  }
}

export function eventColor(type) {
  switch (type) {
    case 'incident_resolved':  return '#22c55e'
    case 'incident_escalated': return '#f97316'
    case 'step_done':          return '#22d3ee'
    case 'sop_matched':        return '#6366f1'
    default:                   return '#8b9cc8'
  }
}
