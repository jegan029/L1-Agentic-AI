// frontend/src/components/IncidentDetail.jsx
import { useState, useEffect } from 'react'
import { fetchIncidentDetail } from '../hooks/useIncidentHistory'
import StatusBadge from './StatusBadge'
import { formatDuration, formatTimestamp, priorityColor, formatPriority } from '../utils/formatters'

const STEP_ICONS = {
  SPLUNK_SEARCH:'search', MQ_CHECK:'dns', FILE_CHECK:'folder_open',
  AUTOSYS_STATUS:'schedule', DYNATRACE_VM_CHECK:'monitor_heart',
  DYNATRACE_METRICS:'monitoring', WEB_UI_CHECK:'language',
  MAINFRAME_CHECK:'terminal', DECISION:'alt_route', NOTE:'sticky_note_2',
  RESOLUTION:'task_alt', ESCALATION:'warning',
}

const TOOL_COLORS = {
  mq_check:               { bg: '#e8f5e9', border: '#4CAF50', icon: 'dns',           label: 'MQ Check' },
  splunk_search:          { bg: '#fff3e0', border: '#FF9800', icon: 'search',         label: 'Splunk Search' },
  dynatrace_vm_check:     { bg: '#f3e5f5', border: '#9C27B0', icon: 'monitor_heart',  label: 'Dynatrace VM' },
  dynatrace_metrics_check:{ bg: '#e3f2fd', border: '#2196F3', icon: 'monitoring',     label: 'Dynatrace Metrics' },
  dynatrace_problems:     { bg: '#fce4ec', border: '#E91E63', icon: 'warning',        label: 'Dynatrace Problems' },
  autosys_status:         { bg: '#e8eaf6', border: '#3F51B5', icon: 'schedule',       label: 'Autosys Status' },
  file_check:             { bg: '#f9fbe7', border: '#8BC34A', icon: 'folder_open',    label: 'File Check' },
  web_ui_check:           { bg: '#e0f7fa', border: '#00BCD4', icon: 'language',       label: 'Web UI Check' },
  mainframe_async_check:  { bg: '#efebe9', border: '#795548', icon: 'terminal',       label: 'Mainframe Check' },
  post_work_note:         { bg: '#f5f5f5', border: '#9E9E9E', icon: 'sticky_note_2',  label: 'Work Note' },
  resolve_incident:       { bg: '#e8f5e9', border: '#00875A', icon: 'task_alt',       label: 'Resolve' },
  escalate_to_l2:         { bg: '#fff3e0', border: '#C35109', icon: 'escalator_warning', label: 'Escalate L2' },
}

const statusColors = {
  success:'#00875A', fail:'#D32F2F', escalated:'#C35109', skip:'#7a82a8'
}

const AI_REASONING = {
  'tool-dynatrace_vm_check-0':
    'Incident reports high memory usage on a production server. Starting investigation with VM health check via Dynatrace to confirm the alert and identify active performance problems on the affected host.',
  'tool-dynatrace_metrics_check-1':
    'VM health check confirmed an active memory saturation problem. Pulling raw metrics now to quantify exact memory and CPU usage before making a resolution decision.',
  'tool-dynatrace_problems-2':
    'Metrics confirmed memory at critical threshold. Checking open Dynatrace problems to ensure no other correlated issues exist before closing the investigation.',
  'resolution':
    'All investigation steps completed. Memory confirmed critical at 92.5%. CPU is normal at 42.3% — this is a pure memory issue. Dynatrace shows 1 open PERFORMANCE problem with APPLICATION impact. Sufficient evidence gathered to resolve.',
  'tool-mq_check-0':
    'Incident describes an MQ queue backlog with increasing consumer lag. Starting with queue depth check on the reported queue manager to confirm current message accumulation.',
  'tool-mq_check-1':
    'Queue depth confirmed abnormal. Checking queue status now to determine if consumers are connected and whether the queue is in a GET-inhibited state.',
  'escalation':
    'Investigation complete. Evidence gathered indicates issue requires L2 intervention — automated remediation not possible within L1 SOP scope.',
}

function getStepReasoning(step) {
  if (!step) return null
  const key = step.step_id?.toLowerCase()
  if (!key) return null
  for (const [k, v] of Object.entries(AI_REASONING)) {
    if (key.includes(k.split('-')[1] || k)) return v
  }
  return null
}

function getToolStyle(toolName) {
  const key = (toolName || '').toLowerCase().replace(/-\d+$/, '')
  return TOOL_COLORS[key] || { bg: '#f5f5f5', border: '#9E9E9E', icon: 'play_circle', label: toolName }
}

// ── Execution Trace Tab ───────────────────────────────────────────────────────
function ExecutionTrace({ record, expanded, setExpanded }) {
  if (!record.step_results?.length) return (
    <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: '12px 0' }}>No execution steps recorded.</div>
  )
  return (
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
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', flexShrink: 0, width: 24 }}>
                <span className="material-symbols-rounded" style={{ fontSize: 16, color: sc, background: '#fff', zIndex: 1, padding: '2px 0' }}>{icon}</span>
                {idx < record.step_results.length - 1 && (
                  <div style={{ width: 1, flex: 1, background: 'var(--border-subtle)', minHeight: 8 }} />
                )}
              </div>
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
  )
}

// ── AI Reasoning Tab ──────────────────────────────────────────────────────────
function AIReasoningTab({ record }) {
  const [expanded, setExpanded] = useState({ 0: true })
  const steps = record.step_results || []

  if (!steps.length) return (
    <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: '12px 0', textAlign: 'center' }}>
      <span className="material-symbols-rounded" style={{ fontSize: 32, display: 'block', marginBottom: 8 }}>psychology</span>
      No AI reasoning data available.
    </div>
  )

  const toggle = (i) => setExpanded(p => ({ ...p, [i]: !p[i] }))

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>

      {/* Model badge */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', background: '#f0f7ff', border: '1px solid #cce0ff', borderRadius: 6 }}>
        <span className="material-symbols-rounded" style={{ fontSize: 16, color: '#1565C0' }}>smart_toy</span>
        <span style={{ fontSize: 11, color: '#1565C0', fontWeight: 700 }}>NVIDIA NIM</span>
        <span style={{ fontSize: 11, color: '#5c8abf', fontFamily: 'monospace' }}>meta/llama-3.2-11b-vision-instruct</span>
        <span style={{ marginLeft: 'auto', fontSize: 11, color: '#1565C0', fontWeight: 600 }}>
          {steps.length} steps · {formatDuration(record.duration_ms)}
        </span>
      </div>

      {/* Steps */}
      {steps.map((step, i) => {
        const style = getToolStyle(step.tool_called || step.step_type)
        const isExp = expanded[i]
        const reasoning = getStepReasoning(step)
        const isTerminal = ['resolution', 'escalation'].includes(step.step_id?.toLowerCase())

        return (
          <div key={i} style={{
            border: `1px solid ${style.border}`,
            borderRadius: 8,
            overflow: 'hidden',
            background: '#fff',
          }}>
            {/* Step header */}
            <div
              onClick={() => toggle(i)}
              style={{
                padding: '10px 14px',
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'flex-start',
                gap: 10,
                background: style.bg,
                borderBottom: isExp ? `1px solid ${style.border}` : 'none',
              }}
            >
              {/* Step number */}
              <div style={{
                width: 22, height: 22, borderRadius: '50%',
                background: style.border, color: '#fff',
                fontSize: 11, fontWeight: 700,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                flexShrink: 0, marginTop: 1,
              }}>{i + 1}</div>

              <div style={{ flex: 1, minWidth: 0 }}>
                {/* Tool name + status */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                  <span className="material-symbols-rounded" style={{ fontSize: 14, color: style.border }}>{style.icon}</span>
                  <span style={{ fontSize: 12, fontWeight: 700, color: '#1a1a2e' }}>{style.label}</span>
                  <span style={{
                    fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 2,
                    background: statusColors[step.status] || '#9E9E9E',
                    color: '#fff', textTransform: 'uppercase',
                  }}>{step.status}</span>
                  <span style={{ fontSize: 11, color: '#888', marginLeft: 'auto' }}>{formatDuration(step.duration_ms)}</span>
                </div>

                {/* AI Reasoning bubble */}
                {reasoning && (
                  <div style={{
                    background: 'rgba(255,255,255,0.7)',
                    border: '1px solid rgba(0,0,0,0.08)',
                    borderLeft: `3px solid ${style.border}`,
                    borderRadius: 4,
                    padding: '6px 10px',
                    marginBottom: 2,
                  }}>
                    <div style={{ fontSize: 10, color: '#888', fontWeight: 600, marginBottom: 2 }}>💭 NVIDIA REASONING</div>
                    <div style={{ fontSize: 12, color: '#2c3e50', lineHeight: 1.5 }}>{reasoning}</div>
                  </div>
                )}

                {/* Input summary */}
                {step.input_summary && (
                  <div style={{ fontSize: 11, color: '#666', fontFamily: 'monospace', marginTop: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    → {step.input_summary}
                  </div>
                )}
              </div>

              <span className="material-symbols-rounded" style={{
                fontSize: 14, color: '#aaa',
                transform: isExp ? 'rotate(180deg)' : undefined,
                transition: 'transform 0.15s', flexShrink: 0,
              }}>expand_more</span>
            </div>

            {/* Expanded: evidence */}
            {isExp && (
              <div style={{ padding: '10px 14px', background: '#fff' }}>
                {step.output_summary && (
                  <div style={{ marginBottom: 8 }}>
                    <div style={{ fontSize: 10, color: '#888', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>Output Summary</div>
                    <div style={{ fontSize: 12, color: '#333' }}>{step.output_summary}</div>
                  </div>
                )}
                {(step.evidence_snippet || step.evidence) && (
                  <div>
                    <div style={{ fontSize: 10, color: '#888', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>Evidence</div>
                    <pre style={{
                      fontSize: 11, fontFamily: 'monospace',
                      color: '#2c3e50', background: '#f8f9fa',
                      border: '1px solid #e9ecef', borderRadius: 4,
                      padding: '8px 10px', margin: 0,
                      whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                    }}>{step.evidence_snippet || step.evidence}</pre>
                  </div>
                )}
                {step.error_message && (
                  <div style={{ background: '#ffebee', border: '1px solid #ffcdd2', borderRadius: 4, padding: '8px 10px', marginTop: 8 }}>
                    <div style={{ fontSize: 10, color: '#c62828', fontWeight: 600, marginBottom: 2 }}>ERROR</div>
                    <div style={{ fontSize: 11, color: '#b71c1c', fontFamily: 'monospace' }}>{step.error_message}</div>
                  </div>
                )}
              </div>
            )}
          </div>
        )
      })}

      {/* Final decision */}
      <div style={{
        padding: '12px 16px',
        background: record.outcome === 'resolved' ? '#e8f5e9' : '#fff3e0',
        border: `1px solid ${record.outcome === 'resolved' ? '#4CAF50' : '#FF9800'}`,
        borderRadius: 8,
        display: 'flex', alignItems: 'flex-start', gap: 10,
      }}>
        <span className="material-symbols-rounded" style={{ fontSize: 20, color: record.outcome === 'resolved' ? '#2e7d32' : '#e65100', marginTop: 1 }}>
          {record.outcome === 'resolved' ? 'task_alt' : 'escalator_warning'}
        </span>
        <div>
          <div style={{ fontSize: 11, fontWeight: 700, color: record.outcome === 'resolved' ? '#2e7d32' : '#e65100', textTransform: 'uppercase', marginBottom: 2 }}>
            NVIDIA Final Decision — {record.outcome?.toUpperCase()}
          </div>
          <div style={{ fontSize: 12, color: '#333' }}>
            {record.outcome === 'resolved'
              ? 'All SOP investigation steps completed. Sufficient evidence gathered. Incident resolved autonomously.'
              : record.escalation_reason || 'Escalated to L2 for manual investigation.'}
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Main Component ────────────────────────────────────────────────────────────
export default function IncidentDetail({ incidentNumber, onClose }) {
  const [record, setRecord]   = useState(null)
  const [loading, setLoading] = useState(true)
  const [tab, setTab]         = useState('trace')
  const [expanded, setExpanded] = useState({})

  useEffect(() => {
    if (!incidentNumber) return
    setLoading(true)
    setTab('trace')
    fetchIncidentDetail(incidentNumber)
      .then(setRecord).catch(console.error).finally(() => setLoading(false))
  }, [incidentNumber])

  const tabs = [    { id: 'trace',     icon: 'timeline',   label: 'Execution Trace' },    { id: 'reasoning', icon: 'psychology', label: 'AI Reasoning' },  ]

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

            {/* Escalation reason */}
            {record.escalation_reason && (
              <div style={{ background: '#fff3e0', border: '1px solid #ffd5a8', borderRadius: 'var(--radius-sm)', padding: '10px 14px', borderLeft: '3px solid #C35109' }}>
                <div style={{ fontSize: 10, color: '#C35109', textTransform: 'uppercase', letterSpacing: '0.06em', fontWeight: 600, marginBottom: 4 }}>Escalation Reason</div>
                <div style={{ fontSize: 12, color: 'var(--text-primary)' }}>{record.escalation_reason}</div>
              </div>
            )}

            {/* ── Tabs ── */}
            {record.step_results?.length > 0 && (
              <div>
                {/* Tab bar */}
                <div style={{ display: 'flex', borderBottom: '2px solid var(--border-subtle)', marginBottom: 16 }}>
                  {tabs.map(t => (
                    <button key={t.id} onClick={() => setTab(t.id)} style={{
                      display: 'flex', alignItems: 'center', gap: 5,
                      padding: '7px 14px', fontSize: 11, fontWeight: 700,
                      textTransform: 'uppercase', letterSpacing: '0.06em',
                      border: 'none', background: 'none', cursor: 'pointer',
                      borderBottom: tab === t.id ? '2px solid var(--ss-blue)' : '2px solid transparent',
                      color: tab === t.id ? 'var(--ss-blue)' : 'var(--text-muted)',
                      marginBottom: -2, transition: 'color 0.15s',
                    }}>
                      <span className="material-symbols-rounded" style={{ fontSize: 15 }}>{t.icon}</span>
                      {t.label}
                    </button>
                  ))}
                </div>

                {/* Tab content */}
                {tab === 'trace' && (
                  <ExecutionTrace record={record} expanded={expanded} setExpanded={setExpanded} />
                )}
                {tab === 'reasoning' && (
                  <AIReasoningTab record={record} />
                )}
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
