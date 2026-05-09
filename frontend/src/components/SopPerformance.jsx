import { useState } from 'react'
import { useAgent } from '../context/AgentContext'
import { formatDuration } from '../utils/formatters'

export default function SopPerformance() {
  const { sops: { items, loading } } = useAgent()
  const [sortKey, setSortKey] = useState('total_runs')
  const [sortAsc, setSortAsc] = useState(false)

  const sorted = [...items].sort((a, b) => {
    const diff = a[sortKey] < b[sortKey] ? -1 : a[sortKey] > b[sortKey] ? 1 : 0
    return sortAsc ? diff : -diff
  })

  const toggleSort = key => {
    if (sortKey === key) setSortAsc(a => !a)
    else { setSortKey(key); setSortAsc(false) }
  }

  const Th = ({ col, label }) => (
    <th onClick={() => toggleSort(col)} style={{
      padding: '10px 14px', textAlign: 'left', cursor: 'pointer',
      fontSize: 11, fontWeight: 600,
      color: sortKey === col ? 'var(--ss-blue)' : 'var(--text-muted)',
      textTransform: 'uppercase', letterSpacing: '0.05em',
      whiteSpace: 'nowrap', userSelect: 'none',
    }}>
      {label}{sortKey === col ? (sortAsc ? ' ↑' : ' ↓') : ''}
    </th>
  )

  return (
    <div style={{ padding: '28px 40px', display: 'flex', flexDirection: 'column', gap: 20 }}>

      {/* Header */}
      <div>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: 'var(--ss-navy)' }}>SOP Performance</h1>
        <p style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 2 }}>
          Resolution statistics per Standard Operating Procedure — {items.length} SOP{items.length !== 1 ? 's' : ''} used
        </p>
      </div>

      {/* Summary strip — mirrors State Street's "In focus" section style */}
      {items.length > 0 && (() => {
        const totalRuns = items.reduce((s, i) => s + i.total_runs, 0)
        const totalResolved = items.reduce((s, i) => s + i.resolved_count, 0)
        const overallRate = totalRuns ? Math.round(totalResolved / totalRuns * 100) : 0
        return (
          <div style={{
            background: 'var(--ss-light-blue)', border: '1px solid var(--ss-mid-blue)',
            borderRadius: 'var(--radius-md)', padding: '16px 24px',
            display: 'flex', gap: 32, flexWrap: 'wrap',
          }}>
            {[['Total SOP Executions', totalRuns], ['Auto-Resolved', totalResolved], ['Overall Success Rate', `${overallRate}%`], ['SOPs in Use', items.length]].map(([l, v]) => (
              <div key={l}>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>{l}</div>
                <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--ss-navy)', marginTop: 2 }}>{v}</div>
              </div>
            ))}
          </div>
        )
      })()}

      {/* Table */}
      <div style={{
        background: '#fff', border: '1px solid var(--border-subtle)',
        borderRadius: 'var(--radius-md)', boxShadow: 'var(--shadow-card)', overflow: 'hidden',
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ background: 'var(--bg-1)', borderBottom: '2px solid var(--border-subtle)' }}>
              <Th col="sop_id" label="SOP ID" />
              <th style={{ padding: '10px 14px', textAlign: 'left', fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Title</th>
              <Th col="total_runs"     label="Runs" />
              <Th col="resolved_count" label="Resolved" />
              <Th col="escalated_count" label="Escalated" />
              <Th col="success_rate_pct" label="Success Rate" />
              <Th col="avg_duration_ms" label="Avg Duration" />
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={7} style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>Loading…</td></tr>
            ) : sorted.length === 0 ? (
              <tr><td colSpan={7} style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>
                No SOP execution data yet. Process incidents to see performance metrics.
              </td></tr>
            ) : sorted.map((r, i) => (
              <tr key={r.sop_id} style={{ borderBottom: '1px solid var(--border-subtle)', background: i % 2 === 0 ? '#fff' : 'var(--bg-1)' }}>
                <td style={{ padding: '12px 14px', fontFamily: 'monospace', fontSize: 12, color: 'var(--ss-blue)', fontWeight: 600, whiteSpace: 'nowrap' }}>{r.sop_id}</td>
                <td style={{ padding: '12px 14px', color: 'var(--text-secondary)', maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.sop_title}</td>
                <td style={{ padding: '12px 14px', fontWeight: 600, color: 'var(--ss-navy)', textAlign: 'center' }}>{r.total_runs}</td>
                <td style={{ padding: '12px 14px', fontWeight: 600, color: '#00875A', textAlign: 'center' }}>{r.resolved_count}</td>
                <td style={{ padding: '12px 14px', fontWeight: 600, color: '#C35109', textAlign: 'center' }}>{r.escalated_count}</td>
                <td style={{ padding: '12px 14px', minWidth: 160 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <div style={{ flex: 1, height: 5, background: 'var(--bg-3)', borderRadius: 2, overflow: 'hidden' }}>
                      <div style={{
                        height: '100%', borderRadius: 2,
                        width: `${r.success_rate_pct}%`,
                        background: r.success_rate_pct >= 80 ? '#00875A' : r.success_rate_pct >= 50 ? '#E6A817' : '#D32F2F',
                        transition: 'width 0.6s ease',
                      }} />
                    </div>
                    <span style={{ fontSize: 12, fontWeight: 700, color: 'var(--ss-navy)', width: 36, textAlign: 'right', flexShrink: 0 }}>{r.success_rate_pct}%</span>
                  </div>
                </td>
                <td style={{ padding: '12px 14px', color: 'var(--text-muted)' }}>{formatDuration(r.avg_duration_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
