import { useAgent } from '../context/AgentContext'

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

const TYPE_META = {
  incident_received:  { color: '#58a6ff', prefix: 'RECV', icon: 'inbox' },
  sop_matched:        { color: '#d2a8ff', prefix: 'SOP ', icon: 'checklist' },
  step_executing:     { color: '#8b949e', prefix: 'EXEC', icon: 'play_circle' },
  step_done:          { color: '#79c0ff', prefix: 'DONE', icon: 'done_all' },
  incident_resolved:  { color: '#3fb950', prefix: 'RSLV', icon: 'task_alt' },
  incident_escalated: { color: '#ffa657', prefix: 'ESCL', icon: 'escalator_warning' },
}

export default function ActivityFeed() {
  const { stream: { events, isConnected, clear } } = useAgent()

  return (
    <div style={{ padding: '28px 40px', display: 'flex', flexDirection: 'column', gap: 20 }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: 'var(--ss-navy)' }}>Activity Feed</h1>
          <p style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 2 }}>
            Real-time stream of L1 Engineer actions via Server-Sent Events
          </p>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 6, padding: '6px 12px',
            background: isConnected ? '#e8f5ee' : '#fff3e0',
            border: `1px solid ${isConnected ? '#b2dfcb' : '#ffd5a8'}`,
            borderRadius: 2, fontSize: 12, fontWeight: 600,
            color: isConnected ? '#00875A' : '#C35109',
          }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: isConnected ? '#00875A' : '#C35109' }} />
            {isConnected ? 'Live' : 'Reconnecting…'}
          </div>
          {events.length > 0 && (
            <button onClick={clear} style={{
              padding: '6px 12px', fontSize: 12, borderRadius: 2,
              border: '1px solid var(--border-subtle)',
              color: 'var(--text-secondary)', background: '#fff', cursor: 'pointer',
            }}>Clear</button>
          )}
        </div>
      </div>

      {/* ── Terminal Card ── */}
      <div style={{
        background: '#0d1117',
        border: '1px solid #30363d',
        borderRadius: 'var(--radius-md)',
        boxShadow: '0 4px 32px rgba(0,0,0,0.35)',
        overflow: 'hidden',
        minHeight: 480,
      }}>
        <style>{`
          @keyframes term-blink  { 0%,100%{opacity:1}  50%{opacity:0.2} }
          @keyframes term-slide  { from{opacity:0;transform:translateY(-5px)} to{opacity:1;transform:translateY(0)} }
        `}</style>

        {/* macOS title bar */}
        <div style={{
          padding: '10px 18px',
          background: '#161b22',
          borderBottom: '1px solid #30363d',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
            {/* Traffic lights */}
            <div style={{ display: 'flex', gap: 7 }}>
              {['#ff5f57', '#febc2e', '#28c840'].map((c, i) => (
                <span key={i} style={{ width: 13, height: 13, borderRadius: '50%', background: c, display: 'inline-block' }} />
              ))}
            </div>
            <span style={{ fontSize: 12, color: '#8b949e', fontFamily: 'monospace', letterSpacing: '0.03em' }}>
              nexus-agent — activity-stream
            </span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 11, color: '#3fb950', fontFamily: 'monospace' }}>
              {events.length} events
            </span>
            <span style={{
              width: 7, height: 7, borderRadius: '50%', display: 'inline-block',
              background: isConnected ? '#3fb950' : '#ffa657',
              animation: isConnected ? 'term-blink 2s ease-in-out infinite' : 'none',
            }} />
          </div>
        </div>

        {/* Terminal body */}
        <div style={{ padding: '14px 20px', fontFamily: '"JetBrains Mono","Fira Code","Cascadia Code",monospace' }}>
          {events.length === 0 ? (
            <div style={{ padding: '40px 0', textAlign: 'center' }}>
              <div style={{ fontSize: 13, color: '#8b949e', marginBottom: 8 }}>
                $ waiting for agent activity...
              </div>
              <span style={{ fontSize: 14, color: '#3fb950', animation: 'term-blink 1s step-end infinite', display: 'inline-block' }}>█</span>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
              {events.map((evt, i) => {
                const meta = TYPE_META[evt.type] || { color: '#8b949e', prefix: '?   ', icon: 'bolt' }
                const ts   = evt.ts
                  ? new Date(evt.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
                  : '??:??:??'
                return (
                  <div key={i} style={{
                    display: 'flex', gap: 10, alignItems: 'flex-start',
                    padding: '4px 6px',
                    paddingLeft: i === 0 ? 4 : 6,
                    borderLeft: i === 0 ? `2px solid ${meta.color}` : '2px solid transparent',
                    animation: i === 0 ? 'term-slide 0.2s ease' : undefined,
                    borderRadius: 2,
                    background: i === 0 ? `${meta.color}08` : 'transparent',
                  }}>
                    <span style={{ fontSize: 11, color: '#484f58', flexShrink: 0, width: 68, paddingTop: 1 }}>
                      {ts}
                    </span>
                    <span style={{
                      fontSize: 10, fontWeight: 700,
                      color: meta.color,
                      background: `${meta.color}18`,
                      padding: '1px 7px', borderRadius: 2,
                      flexShrink: 0, letterSpacing: '0.05em',
                      marginTop: 1,
                    }}>
                      {meta.prefix}
                    </span>
                    <span style={{ fontSize: 12, color: '#e6edf3', lineHeight: 1.55, wordBreak: 'break-word' }}>
                      {eventMessage(evt)}
                    </span>
                  </div>
                )
              })}
              {/* Blinking cursor at end */}
              <div style={{ paddingLeft: 8, marginTop: 4 }}>
                <span style={{ fontSize: 13, color: '#3fb950', animation: 'term-blink 1s step-end infinite', display: 'inline-block' }}>█</span>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
