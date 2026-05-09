import KpiCard from './KpiCard'
import { useAgent } from '../context/AgentContext'
import { formatDuration } from '../utils/formatters'

/* ── Shared card wrapper ─────────────────────────────────────────── */
function Card({ title, children, style }) {
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
          fontSize: 12,
          fontWeight: 600,
          color: 'var(--text-muted)',
          textTransform: 'uppercase',
          letterSpacing: '0.06em',
        }}>
          {title}
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
  const total = data.reduce((s, d) => s + d.value, 0)
  let cum = 0
  const segs = data.map(d => {
    const start = cum
    cum += (d.value / total) * 360
    return { ...d, start, end: cum }
  })
  const grad = segs.map(s => `${s.color} ${s.start.toFixed(1)}deg ${s.end.toFixed(1)}deg`).join(', ')

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 28 }}>
      <div style={{
        width: 130, height: 130, borderRadius: '50%', flexShrink: 0,
        background: `conic-gradient(${grad})`,
        mask: 'radial-gradient(circle at center, transparent 42px, black 43px)',
        WebkitMask: 'radial-gradient(circle at center, transparent 42px, black 43px)',
      }} />
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

/* ── Bar chart (escalation reasons) ─────────────────────────────── */
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

/* ── 7-day trend ─────────────────────────────────────────────────── */
function TrendChart({ data }) {
  if (!data || data.length === 0) return null
  const max = Math.max(...data.flatMap(d => [d.received, d.resolved, d.escalated]), 1)
  const W = 100, H = 55
  const pts = key => data.map((d, i) => `${(i / (data.length - 1)) * W},${H - (d[key] / max) * H * 0.88}`).join(' ')

  return (
    <div>
      <div style={{ display: 'flex', gap: 20, marginBottom: 14 }}>
        {[['received','var(--ss-blue)','Received'],['resolved','var(--accent-green)','Resolved'],['escalated','var(--accent-orange)','Escalated']].map(([k,c,l]) => (
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

/* ── Page ────────────────────────────────────────────────────────── */
export default function LiveDashboard() {
  const { metrics: { data, loading } } = useAgent()
  const kpi = data?.kpi || {}
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
          <p style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 2 }}>Real-time L1 Engineer activity and resolution metrics</p>
        </div>
        {loading && (
          <span style={{ fontSize: 11, color: 'var(--ss-blue)', background: 'var(--ss-light-blue)', padding: '4px 10px', borderRadius: 2, fontWeight: 500 }}>
            Updating…
          </span>
        )}
      </div>

      {/* Blue hero banner — mirrors State Street's section titles */}
      <div style={{
        background: 'var(--ss-navy)',
        borderRadius: 'var(--radius-md)',
        padding: '20px 28px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        flexWrap: 'wrap',
        gap: 16,
      }}>
        <div>
          <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.55)', letterSpacing: '0.07em', textTransform: 'uppercase', marginBottom: 4 }}>
            Agent Performance Summary
          </div>
          <div style={{ fontSize: 28, fontWeight: 700, color: '#fff' }}>
            {resRate}% Auto-Resolution Rate
          </div>
          <div style={{ fontSize: 13, color: 'rgba(255,255,255,0.65)', marginTop: 4 }}>
            {kpi.incidents_received ?? 0} incidents processed · avg {formatDuration(kpi.avg_resolution_ms)} per resolution
          </div>
        </div>
        <div style={{ display: 'flex', gap: 24 }}>
          {[
            ['Received',  kpi.incidents_received  ?? 0, 'rgba(255,255,255,0.15)'],
            ['Resolved',  kpi.incidents_resolved  ?? 0, '#00875A'],
            ['Escalated', kpi.incidents_escalated ?? 0, '#C35109'],
          ].map(([l, v, c]) => (
            <div key={l} style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 26, fontWeight: 700, color: '#fff' }}>{v}</div>
              <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.55)', marginTop: 2 }}>{l}</div>
              <div style={{ height: 3, background: c, borderRadius: 2, marginTop: 6 }} />
            </div>
          ))}
        </div>
      </div>

      {/* KPI Cards */}
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
        <KpiCard title="Incidents Received" value={kpi.incidents_received ?? 0} icon="inbox" accentColor="var(--ss-blue)" subtitle="Total lifetime" />
        <KpiCard title="Auto-Resolved" value={kpi.incidents_resolved ?? 0} icon="verified" accentColor="#00875A" subtitle={`${resRate}% resolution rate`} />
        <KpiCard title="Escalated to L2" value={kpi.incidents_escalated ?? 0} icon="escalator_warning" accentColor="#C35109" subtitle="Required human review" />
        <KpiCard title="Avg Resolution" value={formatDuration(kpi.avg_resolution_ms)} icon="timer" accentColor="var(--ss-navy)" subtitle="Per incident" />
      </div>

      {/* Charts row */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
        <Card title="Outcome Breakdown">
          <DonutChart data={outcomeData} />
        </Card>
        <Card title="Escalation Reasons">
          <BarChart data={data?.escalation_reasons} />
        </Card>
      </div>

      {/* Trend chart */}
      <Card title="7-Day Activity Trend">
        <TrendChart data={data?.trend_7day} />
      </Card>
    </div>
  )
}
