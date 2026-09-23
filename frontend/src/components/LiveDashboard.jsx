import KpiCard from './KpiCard'
import { useAgent } from '../context/AgentContext'
import { formatDuration } from '../utils/formatters'

/* ── Card wrapper ────────────────────────────────────────────────── */
function Card({ title, titleRight, children, style }) {
  return (
    <div style={{
      background: '#fff',
      border: '1px solid var(--border-subtle)',
      borderRadius: 'var(--radius-md)',
      boxShadow: 'var(--shadow-card)',
      overflow: 'hidden',
      ...style,
    }}>
      {title && (
        <div style={{
          padding: '14px 20px',
          borderBottom: '1px solid var(--border-subtle)',
          fontSize: 12, fontWeight: 600,
          color: 'var(--text-muted)',
          textTransform: 'uppercase', letterSpacing: '0.06em',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <span>{title}</span>
          {titleRight}
        </div>
      )}
      <div style={{ padding: 20 }}>{children}</div>
    </div>
  )
}

/* ── Donut chart ─────────────────────────────────────────────────── */
function DonutChart({ data }) {
  if (!data || data.every(d => d.value === 0)) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: 160, color: 'var(--text-muted)', fontSize: 13 }}>
        No data yet
      </div>
    )
  }
  const total   = data.reduce((s, d) => s + d.value, 0)
  const resRate = total > 0 ? Math.round(((data.find(d => d.label === 'Resolved')?.value || 0) / total) * 100) : 0
  let cum = 0
  const segs = data.map(d => {
    const start = cum
    cum += (d.value / total) * 360
    return { ...d, start, end: cum }
  })
  const grad = segs.map(s => `${s.color} ${s.start.toFixed(1)}deg ${s.end.toFixed(1)}deg`).join(', ')

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 28 }}>
      <div style={{ position: 'relative', flexShrink: 0 }}>
        <div style={{
          width: 130, height: 130, borderRadius: '50%',
          background: `conic-gradient(${grad})`,
          mask: 'radial-gradient(circle at center, transparent 42px, black 43px)',
          WebkitMask: 'radial-gradient(circle at center, transparent 42px, black 43px)',
        }} />
        <div style={{
          position: 'absolute', inset: 0,
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
        }}>
          <span style={{ fontSize: 20, fontWeight: 700, color: 'var(--ss-navy)', lineHeight: 1 }}>{resRate}%</span>
          <span style={{ fontSize: 9, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.06em', marginTop: 2 }}>resolved</span>
        </div>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {data.map(d => (
          <div key={d.label} style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 13 }}>
            <span style={{ width: 10, height: 10, borderRadius: 2, background: d.color, flexShrink: 0 }} />
            <span style={{ color: 'var(--text-secondary)' }}>{d.label}</span>
            <span style={{ marginLeft: 'auto', fontWeight: 700, color: 'var(--ss-navy)', paddingLeft: 16 }}>{d.value}</span>
          </div>
        ))}
        <div style={{ fontSize: 11, color: 'var(--text-muted)', borderTop: '1px solid var(--border-subtle)', paddingTop: 6, marginTop: 2 }}>
          Total: {total}
        </div>
      </div>
    </div>
  )
}

/* ── Bar chart ─────────────────────────────────────────────────── */
function BarChart({ data }) {
  if (!data || data.length === 0) {
    return <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>No escalations yet</div>
  }
  const max = Math.max(...data.map(d => d.count), 1)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {data.map(d => (
        <div key={d.reason}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 4 }}>
            <span style={{ color: 'var(--text-secondary)' }}>{d.reason.replace(/_/g, ' ')}</span>
            <span style={{ fontWeight: 600, color: 'var(--ss-navy)' }}>{d.count}</span>
          </div>
          <div style={{ height: 6, background: 'var(--bg-2)', borderRadius: 3, overflow: 'hidden' }}>
            <div style={{
              height: '100%', borderRadius: 3,
              width: `${(d.count / max) * 100}%`,
              background: 'var(--ss-blue)',
              transition: 'width 0.6s ease',
            }} />
          </div>
        </div>
      ))}
    </div>
  )
}

/* ── Trend chart ─────────────────────────────────────────────────── */
function TrendChart({ data }) {
  if (!data || data.length === 0) return null
  const max = Math.max(...data.flatMap(d => [d.received, d.resolved, d.escalated]), 1)
  const W = 100, H = 55
  const pts = key => data.map((d, i) => `${(i / (data.length - 1)) * W},${H - (d[key] / max) * H * 0.88}`).join(' ')

  return (
    <div>
      <div style={{ display: 'flex', gap: 20, marginBottom: 14 }}>
        {[['received','var(--ss-blue)','Received'],['resolved','#00875A','Resolved'],['escalated','#C35109','Escalated']].map(([k,c,l]) => (
          <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
            <span style={{ width: 14, height: 3, background: c, borderRadius: 2, display: 'inline-block' }} />
            <span style={{ color: 'var(--text-secondary)' }}>{l}</span>
          </div>
        ))}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ width: '100%', height: 110, display: 'block' }}>
        {[['received','var(--ss-blue)'],['resolved','#00875A'],['escalated','#C35109']].map(([k,c]) => (
          <polyline key={k} points={pts(k)} fill="none" stroke={c} strokeWidth="0.9" strokeLinejoin="round" strokeLinecap="round" />
        ))}
      </svg>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6 }}>
        {data.map(d => (
          <span key={d.date} style={{ fontSize: 10, color: 'var(--text-muted)' }}>{d.date.slice(5)}</span>
        ))}
      </div>
    </div>
  )
}

/* ── AI Engine Live Widget ────────────────────────────────────────── */
const EVT_STYLE = {
  incident_received:  { icon: 'inbox',             color: '#2196F3', label: 'Received' },
  sop_matched:        { icon: 'checklist',         color: '#7B1FA2', label: 'SOP Matched' },
  incident_resolved:  { icon: 'task_alt',          color: '#00875A', label: 'Resolved' },
  incident_escalated: { icon: 'escalator_warning', color: '#C35109', label: 'Escalated' },
  step_done:          { icon: 'done',              color: '#0288D1', label: 'Step Done' },
}

function AIEngineWidget({ events }) {
  const relevant = (events || []).filter(e => EVT_STYLE[e.type]).slice(0, 7)

  return (
    <Card
      title="AI Engine — Live"
      titleRight={
        <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
          <span style={{
            width: 7, height: 7, borderRadius: '50%',
            background: '#76b900', display: 'inline-block',
            animation: 'nv-pulse 2.5s ease-in-out infinite',
          }} />
          <span style={{ fontSize: 10, color: '#76b900', fontWeight: 700, letterSpacing: '0.05em' }}>NVIDIA NIM</span>
        </div>
      }
    >
      <style>{`
        @keyframes nv-pulse {
          0%,100% { box-shadow: 0 0 0 0 rgba(118,185,0,0.5); }
          50%      { box-shadow: 0 0 0 6px rgba(118,185,0,0); }
        }
        @keyframes fadeSlide {
          from { opacity:0; transform:translateY(-6px); }
          to   { opacity:1; transform:translateY(0); }
        }
      `}</style>

      {relevant.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '24px 0', color: 'var(--text-muted)' }}>
          <span className="material-symbols-rounded" style={{ fontSize: 32, display: 'block', marginBottom: 6, color: 'var(--bg-3)' }}>psychology</span>
          <span style={{ fontSize: 13 }}>Waiting for AI activity…</span>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {relevant.map((evt, i) => {
            const s = EVT_STYLE[evt.type]
            const ts = evt.ts ? new Date(evt.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : ''
            return (
              <div key={i} style={{
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '7px 12px',
                background: i === 0 ? `${s.color}0e` : 'var(--bg-1)',
                borderRadius: 6,
                border: `1px solid ${i === 0 ? s.color + '35' : 'var(--border-subtle)'}`,
                animation: i === 0 ? 'fadeSlide 0.3s ease' : undefined,
              }}>
                <span className="material-symbols-rounded" style={{ fontSize: 15, color: s.color, flexShrink: 0 }}>{s.icon}</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 10, fontWeight: 700, color: s.color, textTransform: 'uppercase', letterSpacing: '0.04em' }}>{s.label}</div>
                  <div style={{ fontSize: 12, color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {evt.incident_number}
                    {evt.short_description && ` — ${evt.short_description.substring(0, 42)}`}
                    {!evt.short_description && evt.sop_title && ` — ${evt.sop_title}`}
                    {!evt.short_description && !evt.sop_title && evt.reason && ` — ${evt.reason}`}
                  </div>
                </div>
                <span style={{ fontSize: 10, color: 'var(--text-muted)', flexShrink: 0, fontFamily: 'monospace' }}>{ts}</span>
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}

/* ── Page ────────────────────────────────────────────────────────── */
export default function LiveDashboard() {
  const { metrics: { data, loading }, stream: { events } } = useAgent()
  const kpi     = data?.kpi || {}
  const resRate = kpi.resolution_rate_pct ?? 0

  const outcomeData = [
    { label: 'Resolved',  value: kpi.incidents_resolved  ?? 0, color: '#00875A' },
    { label: 'Escalated', value: kpi.incidents_escalated ?? 0, color: '#C35109' },
    { label: 'Failed',    value: kpi.incidents_failed    ?? 0, color: '#D32F2F' },
  ]

  return (
    <div style={{ padding: '28px 40px', display: 'flex', flexDirection: 'column', gap: 20 }}>

      {/* Page header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: 'var(--ss-navy)' }}>Live Dashboard</h1>
          <p style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 2 }}>
            Real-time Support Engineer activity and resolution metrics
          </p>
        </div>
        {loading && (
          <span style={{ fontSize: 11, color: 'var(--ss-blue)', background: 'var(--ss-light-blue)', padding: '4px 10px', borderRadius: 2, fontWeight: 500 }}>
            Updating…
          </span>
        )}
      </div>

      {/* ── Gradient Hero Banner ── */}
      <div style={{
        background: 'linear-gradient(135deg, #020b5b 0%, #1a3a8f 55%, #0d2a6e 100%)',
        borderRadius: 'var(--radius-md)',
        padding: '24px 28px',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        flexWrap: 'wrap', gap: 16,
        position: 'relative', overflow: 'hidden',
      }}>
        {/* Dot-grid overlay */}
        <div style={{
          position: 'absolute', inset: 0, pointerEvents: 'none',
          backgroundImage: 'radial-gradient(circle at 1px 1px, rgba(255,255,255,0.04) 1px, transparent 0)',
          backgroundSize: '28px 28px',
        }} />

        <div style={{ position: 'relative' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
            <span style={{ fontSize: 11, color: 'rgba(255,255,255,0.5)', letterSpacing: '0.07em', textTransform: 'uppercase' }}>
              Agent Performance Summary
            </span>
            <div style={{
              display: 'flex', alignItems: 'center', gap: 5,
              padding: '2px 8px',
              background: 'rgba(118,185,0,0.12)',
              border: '1px solid rgba(118,185,0,0.3)',
              borderRadius: 3,
            }}>
              <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#76b900', display: 'inline-block' }} />
              <span style={{ fontSize: 9, color: '#76b900', fontWeight: 700, letterSpacing: '0.05em' }}>NVIDIA NIM</span>
            </div>
          </div>
          <div style={{ fontSize: 30, fontWeight: 700, color: '#fff', lineHeight: 1.2 }}>
            {resRate}% Auto-Resolution Rate
          </div>
          <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.6)', marginTop: 6 }}>
            {kpi.incidents_received ?? 0} incidents processed · avg {formatDuration(kpi.avg_resolution_ms)} per resolution
          </div>
        </div>

        <div style={{ display: 'flex', gap: 24, position: 'relative' }}>
          {[            ['Received',  kpi.incidents_received  ?? 0, 'rgba(255,255,255,0.2)'],
            ['Resolved',  kpi.incidents_resolved  ?? 0, '#00875A'],
            ['Escalated', kpi.incidents_escalated ?? 0, '#C35109'],
          ].map(([l, v, c]) => (
            <div key={l} style={{ textAlign: 'center', minWidth: 64 }}>
              <div style={{ fontSize: 28, fontWeight: 700, color: '#fff' }}>{v}</div>
              <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.5)', marginTop: 2 }}>{l}</div>
              <div style={{ height: 3, background: c, borderRadius: 2, marginTop: 8 }} />
            </div>
          ))}
        </div>
      </div>

      {/* KPI Cards */}
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
        <KpiCard title="Incidents Received" value={kpi.incidents_received ?? 0} icon="inbox"             accentColor="var(--ss-blue)" subtitle="Total lifetime" />
        <KpiCard title="Auto-Resolved"      value={kpi.incidents_resolved  ?? 0} icon="verified"         accentColor="#00875A"        subtitle={`${resRate}% resolution rate`} />
        <KpiCard title="Escalated to L2"    value={kpi.incidents_escalated ?? 0} icon="escalator_warning" accentColor="#C35109"       subtitle="Required human review" />
        <KpiCard title="Avg Resolution"     value={formatDuration(kpi.avg_resolution_ms)} icon="timer"   accentColor="var(--ss-navy)" subtitle="Per incident" />
      </div>

      {/* Charts + AI Engine row */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14 }}>
        <Card title="Outcome Breakdown">
          <DonutChart data={outcomeData} />
        </Card>
        <Card title="Escalation Reasons">
          <BarChart data={data?.escalation_reasons} />
        </Card>
        <AIEngineWidget events={events} />
      </div>

      {/* Trend */}
      <Card title="7-Day Activity Trend">
        <TrendChart data={data?.trend_7day} />
      </Card>
    </div>
  )
}
