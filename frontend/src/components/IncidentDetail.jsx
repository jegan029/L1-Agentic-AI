import { useState, useEffect } from 'react'
import { fetchIncidentDetail } from '../hooks/useIncidentHistory'
import StatusBadge from './StatusBadge'
import { formatDuration, formatTimestamp, priorityColor, formatPriority } from '../utils/formatters'

const STEP_ICONS = {
  SPLUNK_SEARCH:'search', MQ_CHECK:'dns', FILE_CHECK:'folder_open',
  AUTOSYS_STATUS:'schedule', DYNATRACE_VM_CHECK:'monitor_heart',
  DYNATRACE_METRICS:'monitoring', WEB_UI_CHECK:'language',
  MAINFRAME_CHECK:'terminal', DECISION:'alt_route', NOTE:'sticky_note_2',
}

const statusColors = { success:'#00875A', fail:'#D32F2F', escalated:'#C35109', skip:'#7a82a8' }

export default function IncidentDetail({ incidentNumber, onClose }) {
  const [record, setRecord] = useState(null)
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState({})

  useEffect(() => {
    if (!incidentNumber) return
    setLoading(true)
    fetchIncidentDetail(incidentNumber).then(setRecord).catch(console.error).finally(() => setLoading(false))
  }, [incidentNumber])

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 200,
      background: 'rgba(2,11,91,0.25)',
      display: 'flex', justifyContent: 'flex-end',
    }} onClick={onClose}>
      <div onClick={e => e.stopPropagation()} style={{
        width: 520, height: '100%', overflowY: 'auto',
        background: '#fff', borderLeft: '1px solid var(--border-subtle)',
        boxShadow: '-4px 0 24px rgba(2,11,91,0.1)',
        animation: 'slide-in-right 0.22s ease both',
        display: 'flex', flexDirection: 'column',
      }}>
        {/* Header */}
        <div style={{
          padding: '20px 24px', borderBottom: '1px solid var(--border-subtle)',
          display: 'flex', alignItems: 'flex-start', gap: 12, flexShrink: 0,
          background: 'var(--ss-navy)',
        }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.55)', letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: 4 }}>Incident Detail</div>
            <div style={{ fontSize: 17, fontWeight: 700, color: '#fff' }}>{incidentNumber}</div>
            {record && <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.7)', marginTop: 4 }}>{record.short_description}</div>}
          </div>
          <button onClick={onClose} style={{ color: 'rgba(255,255,255,0.65)', padding: 4, marginTop: -4 }}>
            <span className="material-symbols-rounded" style={{ fontSize: 20 }}>close</span>
          </button>
        </div>

        {loading ? (
          <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)' }}>Loading…</div>
        ) : record ? (
          <div style={{ padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 18 }}>

            {/* Meta chips */}
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              <StatusBadge outcome={record.outcome} />
              <span style={{ fontSize: 11, padding: '3px 8px', borderRadius: 2, background: `${priorityColor(record.priority)}15`, color: priorityColor(record.priority), fontWeight: 700 }}>
                {formatPriority(record.priority)}
              </span>
              <span style={{ fontSize: 11, padding: '3px 8px', borderRadius: 2, background: 'var(--bg-1)', color: 'var(--text-secondary)' }}>
                {record.category}
              </span>
              <span style={{ fontSize: 11, padding: '3px 8px', borderRadius: 2, background: 'var(--ss-light-blue)', color: 'var(--ss-blue)', fontWeight: 600 }}>
                {formatDuration(record.duration_ms)}
              </span>
            </div>

            {/* SOP */}
            {record.sop_id && (
              <div style={{ background: 'var(--bg-1)', borderRadius: 'var(--radius-md)', padding: '12px 16px', border: '1px solid var(--border-subtle)' }}>
                <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 4 }}>SOP Executed</div>
                <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--ss-navy)' }}>{record.sop_title}</div>
                <div style={{ fontSize: 11, color: 'var(--ss-blue)', fontFamily: 'monospace', marginTop: 2 }}>{record.sop_id}</div>
              </div>
            )}

            {/* Times */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
              {[['Started', record.started_at], ['Completed', record.completed_at]].map(([l, v]) => (
                <div key={l} style={{ background: 'var(--bg-1)', padding: '10px 14px', borderRadius: 'var(--radius-sm)', border: '1px solid var(--border-subtle)' }}>
                  <div style={{ fontSize: 10, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 2 }}>{l}</div>
                  <div style={{ fontSize: 12, color: 'var(--text-primary)' }}>{formatTimestamp(v)}</div>
                </div>
              ))}
            </div>

            {record.escalation_reason && (
              <div style={{ background: '#fff3e0', border: '1px solid #ffd5a8', borderRadius: 'var(--radius-sm)', padding: '10px 14px', borderLeft: '3px solid #C35109' }}>
                <div style={{ fontSize: 10, color: '#C35109', textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600, marginBottom: 4 }}>Escalation Reason</div>
                <div style={{ fontSize: 12, color: 'var(--text-primary)' }}>{record.escalation_reason}</div>
              </div>
            )}

            {/* Step timeline */}
            {record.step_results?.length > 0 && (
              <div>
                <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 12 }}>
                  Execution Trace — {record.step_results.length} Steps
                </div>
                <div style={{ display: 'flex', flexDirection: 'column' }}>
                  {record.step_results.map((step, idx) => {
                    const sc = statusColors[step.status] || 'var(--text-muted)'
                    const icon = STEP_ICONS[step.step_type?.toUpperCase()] || 'play_circle'
                    const isExp = expanded[step.step_id]
                    return (
                      <div key={idx} style={{ display: 'flex', gap: 10, position: 'relative' }}>
                        {/* Timeline track */}
                        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', flexShrink: 0, width: 24 }}>
                          <span className="material-symbols-rounded" style={{ fontSize: 16, color: sc, background: '#fff', zIndex: 1, padding: '2px 0' }}>{icon}</span>
                          {idx < record.step_results.length - 1 && (
                            <div style={{ width: 1, flex: 1, background: 'var(--border-subtle)', minHeight: 8 }} />
                          )}
                        </div>
                        {/* Step card */}
                        <div style={{ flex: 1, border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-sm)', marginBottom: 6, overflow: 'hidden' }}>
                          <div onClick={() => setExpanded(p => ({ ...p, [step.step_id]: !p[step.step_id] }))}
                            style={{ padding: '9px 12px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8, background: 'var(--bg-1)' }}>
                            <span style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-muted)', width: 52, flexShrink: 0, fontFamily: 'monospace' }}>{step.step_id}</span>
                            <span style={{ fontSize: 12, color: 'var(--text-secondary)', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{step.output_summary}</span>
                            <span style={{ fontSize: 10, color: sc, fontWeight: 700, flexShrink: 0, textTransform: 'uppercase' }}>{step.status}</span>
                            <span style={{ fontSize: 11, color: 'var(--text-muted)', flexShrink: 0 }}>{formatDuration(step.duration_ms)}</span>
                            <span className="material-symbols-rounded" style={{ fontSize: 14, color: 'var(--text-muted)', transform: isExp ? 'rotate(180deg)' : undefined, transition: 'transform 0.15s' }}>expand_more</span>
                          </div>
                          {isExp && step.evidence_snippet && (
                            <div style={{ padding: '10px 12px', fontSize: 11, fontFamily: 'monospace', color: 'var(--text-secondary)', borderTop: '1px solid var(--border-subtle)', background: '#fff', wordBreak: 'break-all', whiteSpace: 'pre-wrap' }}>
                              {step.evidence_snippet}
                            </div>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </div>
        ) : (
          <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)' }}>Not found</div>
        )}
      </div>
    </div>
  )
}
