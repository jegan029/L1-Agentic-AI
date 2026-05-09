import { useAgent } from '../context/AgentContext'
import { eventIcon, eventColor, formatRelativeTime } from '../utils/formatters'

function eventMessage(evt) {
  switch (evt.type) {
    case 'incident_received':  return `Received ${evt.incident_number} — ${evt.short_description || ''}`
    case 'sop_matched':        return `${evt.incident_number} — SOP matched: ${evt.sop_title} (${(evt.confidence * 100).toFixed(0)}% confidence)`
    case 'step_executing':     return `${evt.incident_number} executing ${evt.step_id} [${evt.step_type}]`
    case 'step_done':          return `${evt.incident_number} step ${evt.step_id}: ${evt.status?.toUpperCase()} (${evt.duration_ms}ms)`
    case 'incident_resolved':  return `${evt.incident_number} RESOLVED via ${evt.sop_id} in ${evt.duration_ms}ms`
    case 'incident_escalated': return `${evt.incident_number} escalated to L2 — ${evt.reason}`
    default:                   return JSON.stringify(evt)
  }
}

const TYPE_COLORS = {
  incident_received:  'var(--ss-blue)',
  sop_matched:        'var(--ss-navy)',
  step_executing:     'var(--text-muted)',
  step_done:          '#3a4470',
  incident_resolved:  '#00875A',
  incident_escalated: '#C35109',
}

export default function ActivityFeed() {
  const { stream: { events, isConnected, clear } } = useAgent()

  return (
    <div style={{ padding: '28px 40px', display: 'flex', flexDirection: 'column', gap: 20 }}>

      {/* Page header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: 'var(--ss-navy)' }}>Activity Feed</h1>
          <p style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 2 }}>Real-time stream of L1 Engineer actions via Server-Sent Events</p>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          {/* Connection badge */}
          <div style={{
            display: 'flex', alignItems: 'center', gap: 6,
            padding: '6px 12px',
            background: isConnected ? '#e8f5ee' : '#fff3e0',
            border: `1px solid ${isConnected ? '#b2dfcb' : '#ffd5a8'}`,
            borderRadius: 2,
            fontSize: 12, fontWeight: 600,
            color: isConnected ? '#00875A' : '#C35109',
          }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: isConnected ? '#00875A' : '#C35109' }} />
            {isConnected ? 'Live' : 'Reconnecting…'}
          </div>
          {events.length > 0 && (
            <button onClick={clear} style={{
              padding: '6px 12px', fontSize: 12, borderRadius: 2,
              border: '1px solid var(--border-subtle)',
              color: 'var(--text-secondary)',
              background: '#fff',
            }}>
              Clear
            </button>
          )}
        </div>
      </div>

      {/* Feed */}
      <div style={{
        background: '#fff',
        border: '1px solid var(--border-subtle)',
        borderRadius: 'var(--radius-md)',
        boxShadow: 'var(--shadow-card)',
        overflow: 'hidden',
        minHeight: 400,
      }}>
        {/* Table header */}
        <div style={{
          display: 'grid', gridTemplateColumns: '140px 1fr 90px',
          padding: '10px 20px',
          background: 'var(--bg-1)',
          borderBottom: '1px solid var(--border-subtle)',
          fontSize: 11, fontWeight: 600, color: 'var(--text-muted)',
          textTransform: 'uppercase', letterSpacing: '0.05em',
        }}>
          <span>Event Type</span>
          <span>Details</span>
          <span>Time</span>
        </div>

        {events.length === 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '60px 20px', gap: 10 }}>
            <span className="material-symbols-rounded" style={{ fontSize: 36, color: 'var(--bg-3)' }}>bolt</span>
            <span style={{ fontSize: 14, color: 'var(--text-muted)', fontWeight: 500 }}>Waiting for agent activity</span>
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>Events appear here in real time as the L1 Engineer processes incidents</span>
          </div>
        ) : (
          events.map((evt, i) => {
            const color = TYPE_COLORS[evt.type] || 'var(--text-muted)'
            const icon = eventIcon(evt.type)
            return (
              <div key={i} style={{
                display: 'grid', gridTemplateColumns: '140px 1fr 90px',
                padding: '12px 20px',
                borderBottom: '1px solid var(--border-subtle)',
                borderLeft: `3px solid ${color}`,
                animation: i === 0 ? 'fadeUp 0.2s ease both' : undefined,
                background: i === 0 ? 'rgba(0,26,255,0.02)' : '#fff',
              }}>
                {/* Type chip */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span className="material-symbols-rounded" style={{ fontSize: 15, color, flexShrink: 0 }}>{icon}</span>
                  <span style={{ fontSize: 10, fontWeight: 600, color, textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                    {evt.type.replace(/_/g, ' ')}
                  </span>
                </div>
                {/* Message */}
                <div style={{ fontSize: 13, color: 'var(--text-primary)', alignSelf: 'center', paddingRight: 16, wordBreak: 'break-word' }}>
                  {eventMessage(evt)}
                </div>
                {/* Time */}
                <div style={{ fontSize: 11, color: 'var(--text-muted)', alignSelf: 'center' }}>
                  {formatRelativeTime(evt.ts)}
                </div>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
