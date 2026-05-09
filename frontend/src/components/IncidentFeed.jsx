import { useState, useCallback } from 'react'
import { useIncidentHistory } from '../hooks/useIncidentHistory'
import { useAgent } from '../context/AgentContext'
import StatusBadge from './StatusBadge'
import IncidentDetail from './IncidentDetail'
import { formatDuration, formatRelativeTime, priorityColor, formatPriority } from '../utils/formatters'

function exportCsv(items) {
  const cols = ['incident_number','short_description','sop_title','outcome','priority','duration_ms','started_at']
  const header = cols.join(',')
  const rows = items.map(r => cols.map(c => JSON.stringify(r[c] ?? '')).join(','))
  const blob = new Blob([header + '\n' + rows.join('\n')], { type: 'text/csv' })
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'incidents.csv'; a.click()
}

const inputStyle = {
  background: '#fff', border: '1px solid var(--border-subtle)',
  borderRadius: 2, padding: '7px 12px', fontSize: 13,
  color: 'var(--text-primary)', outline: 'none',
}

export default function IncidentFeed() {
  const { incidentFilters, setIncidentFilters } = useAgent()
  const { total, items, loading, refresh } = useIncidentHistory(incidentFilters)
  const [selected, setSelected] = useState(null)

  const setFilter = useCallback((key, val) => {
    setIncidentFilters(f => ({ ...f, [key]: val, page: key === 'page' ? val : 1 }))
  }, [setIncidentFilters])

  const totalPages = Math.max(1, Math.ceil(total / incidentFilters.perPage))

  return (
    <div style={{ padding: '28px 40px', display: 'flex', flexDirection: 'column', gap: 20 }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: 'var(--ss-navy)' }}>Incident Feed</h1>
          <p style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 2 }}>
            {total} incident{total !== 1 ? 's' : ''} processed — click any row to view step-by-step execution trace
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          {/* Search */}
          <div style={{ position: 'relative' }}>
            <span className="material-symbols-rounded" style={{ position: 'absolute', left: 9, top: '50%', transform: 'translateY(-50%)', fontSize: 15, color: 'var(--text-muted)' }}>search</span>
            <input value={incidentFilters.search} onChange={e => setFilter('search', e.target.value)}
              placeholder="Search incidents…"
              style={{ ...inputStyle, paddingLeft: 30, width: 200 }} />
          </div>
          <select value={incidentFilters.outcome} onChange={e => setFilter('outcome', e.target.value)} style={inputStyle}>
            <option value="">All Outcomes</option>
            <option value="resolved">Resolved</option>
            <option value="escalated">Escalated</option>
            <option value="failed">Failed</option>
            <option value="partial">Partial</option>
          </select>
          <select value={incidentFilters.priority} onChange={e => setFilter('priority', e.target.value)} style={inputStyle}>
            <option value="">All Priorities</option>
            {[1,2,3,4,5].map(p => <option key={p} value={p}>{formatPriority(p)}</option>)}
          </select>
          <button onClick={() => exportCsv(items)} style={{
            ...inputStyle, display: 'flex', alignItems: 'center', gap: 5,
            cursor: 'pointer', color: 'var(--text-secondary)',
          }}>
            <span className="material-symbols-rounded" style={{ fontSize: 15 }}>download</span>
            Export CSV
          </button>
          <button onClick={refresh} style={{ ...inputStyle, cursor: 'pointer', color: 'var(--text-secondary)' }}>
            <span className="material-symbols-rounded" style={{ fontSize: 15 }}>refresh</span>
          </button>
        </div>
      </div>

      {/* Table */}
      <div style={{
        background: '#fff', border: '1px solid var(--border-subtle)',
        borderRadius: 'var(--radius-md)', boxShadow: 'var(--shadow-card)', overflow: 'hidden',
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ background: 'var(--bg-1)', borderBottom: '2px solid var(--border-subtle)' }}>
              {['Priority','Incident #','Description','SOP','Outcome','Duration','Time'].map(h => (
                <th key={h} style={{
                  padding: '10px 14px', textAlign: 'left',
                  fontSize: 11, fontWeight: 600,
                  color: 'var(--text-muted)', textTransform: 'uppercase',
                  letterSpacing: '0.05em', whiteSpace: 'nowrap',
                }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={7} style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>Loading…</td></tr>
            ) : items.length === 0 ? (
              <tr><td colSpan={7} style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>
                No incidents yet. Fire some webhooks to see data here.
              </td></tr>
            ) : items.map((r, i) => (
              <tr key={r.incident_number}
                onClick={() => setSelected(r.incident_number)}
                style={{ borderBottom: '1px solid var(--border-subtle)', cursor: 'pointer', transition: 'background 0.1s' }}
                onMouseEnter={e => e.currentTarget.style.background = 'rgba(0,26,255,0.03)'}
                onMouseLeave={e => e.currentTarget.style.background = ''}
              >
                <td style={{ padding: '11px 14px' }}>
                  <span style={{ fontSize: 11, fontWeight: 700, color: priorityColor(r.priority), background: `${priorityColor(r.priority)}18`, padding: '2px 7px', borderRadius: 2 }}>
                    {formatPriority(r.priority).split(' ')[0]}
                  </span>
                </td>
                <td style={{ padding: '11px 14px', fontWeight: 600, color: 'var(--ss-blue)', whiteSpace: 'nowrap' }}>{r.incident_number}</td>
                <td style={{ padding: '11px 14px', maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--text-secondary)' }}>{r.short_description}</td>
                <td style={{ padding: '11px 14px', maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--text-muted)', fontSize: 12 }}>{r.sop_title || '—'}</td>
                <td style={{ padding: '11px 14px' }}><StatusBadge outcome={r.outcome} /></td>
                <td style={{ padding: '11px 14px', color: 'var(--text-muted)', whiteSpace: 'nowrap' }}>{formatDuration(r.duration_ms)}</td>
                <td style={{ padding: '11px 14px', color: 'var(--text-muted)', whiteSpace: 'nowrap', fontSize: 12 }}>{formatRelativeTime(r.started_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, justifyContent: 'center' }}>
        <button onClick={() => setFilter('page', incidentFilters.page - 1)}
          disabled={incidentFilters.page <= 1}
          style={{ padding: '6px 14px', borderRadius: 2, border: '1px solid var(--border-subtle)', background: '#fff', fontSize: 13, color: incidentFilters.page <= 1 ? 'var(--text-muted)' : 'var(--ss-navy)', cursor: incidentFilters.page <= 1 ? 'default' : 'pointer' }}>
          Previous
        </button>
        <span style={{ fontSize: 13, color: 'var(--text-secondary)', padding: '0 8px' }}>
          Page {incidentFilters.page} of {totalPages}
        </span>
        <button onClick={() => setFilter('page', incidentFilters.page + 1)}
          disabled={incidentFilters.page >= totalPages}
          style={{ padding: '6px 14px', borderRadius: 2, border: '1px solid var(--border-subtle)', background: '#fff', fontSize: 13, color: incidentFilters.page >= totalPages ? 'var(--text-muted)' : 'var(--ss-navy)', cursor: incidentFilters.page >= totalPages ? 'default' : 'pointer' }}>
          Next
        </button>
      </div>

      {selected && <IncidentDetail incidentNumber={selected} onClose={() => setSelected(null)} />}
    </div>
  )
}
