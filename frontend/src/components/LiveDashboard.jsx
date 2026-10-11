import { useMemo } from 'react'
import KpiCard from './KpiCard'
import TrendChart from './TrendChart'
import DataNotice from './DataNotice'
import Icon from './Icon'
import { useMetrics, useIncidentFilters, DEFAULT_FILTERS } from '../context/AgentContext'
import { formatDuration, formatUptime, formatClock, reasonLabel } from '../utils/formatters'

function Card({ title, children, note }) {
  return (
    <section className="card">
      <h2 className="card__title">{title}</h2>
      <div className="card__body">
        {children}
        {note && <p className="card__note">{note}</p>}
      </div>
    </section>
  )
}

function Empty({ icon = 'inbox', children }) {
  return (
    <div className="state" style={{ padding: '24px 8px' }}>
      <Icon name={icon} />
      <p className="state__body">{children}</p>
    </div>
  )
}

function Outcomes({ data }) {
  const total = data.reduce((s, d) => s + d.value, 0)
  if (total === 0) {
    return <Empty>No incident has finished since the agent started.</Empty>
  }
  let cum = 0
  const stops = data
    .filter(d => d.value > 0)
    .map(d => {
      const start = cum
      cum += (d.value / total) * 360
      return `${d.color} ${start.toFixed(2)}deg ${cum.toFixed(2)}deg`
    })
    .join(', ')

  return (
    <div className="donut-row">
      <div className="donut" aria-hidden="true" style={{ background: `conic-gradient(${stops})` }} />
      <ul className="legend">
        {data.map(d => (
          <li key={d.label}>
            <span className="legend__swatch" style={{ background: d.color }} aria-hidden="true" />
            {d.label}
            <span className="legend__value">{d.value}</span>
          </li>
        ))}
        <li className="legend__total">Total {total}</li>
      </ul>
    </div>
  )
}

function Reasons({ data }) {
  if (!data || data.length === 0) {
    return <Empty icon="check-circle">Nothing has been escalated since the agent started.</Empty>
  }
  const sorted = [...data].sort((a, b) => b.count - a.count)
  const max = Math.max(...sorted.map(d => d.count), 1)
  return (
    <ul className="bars">
      {sorted.map(d => (
        <li key={d.reason}>
          <div className="bar__head">
            <span>{reasonLabel(d.reason)}</span>
            <strong>{d.count}</strong>
          </div>
          <div className="bar__track" aria-hidden="true">
            <div className="bar__fill" style={{ transform: `scaleX(${d.count / max})` }} />
          </div>
        </li>
      ))}
    </ul>
  )
}

export default function LiveDashboard() {
  const { data, loading, error, updatedAt, refresh } = useMetrics()
  const { setFilters } = useIncidentFilters()

  const hasData = data !== null
  const kpi = data?.kpi
  const pending = loading && !hasData
  // null = still loading (skeleton), undefined = failed with nothing to show (em dash)
  const val = key => (hasData ? kpi?.[key] ?? 0 : pending ? null : undefined)

  const received = kpi?.incidents_received ?? 0
  const rate = kpi?.resolution_rate_pct
  const rateText = !hasData ? (pending ? null : '—') : received === 0 ? '—' : `${Number(rate.toFixed(1))}%`

  const outcomes = useMemo(
    () => [
      { label: 'Resolved', value: kpi?.incidents_resolved ?? 0, color: 'var(--resolved-solid)' },
      { label: 'Escalated', value: kpi?.incidents_escalated ?? 0, color: 'var(--escalated-solid)' },
      { label: 'Failed', value: kpi?.incidents_failed ?? 0, color: 'var(--failed-solid)' },
    ],
    [kpi],
  )

  const showOutcome = outcome => () =>
    setFilters(f => ({ ...DEFAULT_FILTERS, perPage: f.perPage, outcome }))

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 id="page-title" tabIndex={-1} className="page-title">Live dashboard</h1>
          <p className="page-sub">What the agent has handled since it started, and what needs a person.</p>
        </div>
        <div className="page-meta">
          {updatedAt && <span>Updated {formatClock(updatedAt)}</span>}
          <button type="button" className="btn btn--icon" onClick={refresh} aria-label="Refresh dashboard">
            <Icon name="refresh" />
          </button>
        </div>
      </header>

      <DataNotice error={error} hasData={hasData} updatedAt={updatedAt} onRetry={refresh} what="dashboard metrics" />

      <section className="strip" aria-label="Agent status">
        <div>
          <div className="strip__label">Auto-resolution rate</div>
          <div className="strip__value">{rateText === null ? <span className="skel" aria-label="Loading" /> : rateText}</div>
        </div>
        <div className="strip__meta">
          {hasData && received === 0 ? (
            <span>No incidents received since the agent started</span>
          ) : (
            <span>Avg SOP run <strong>{hasData ? formatDuration(kpi.avg_resolution_ms) : '—'}</strong></span>
          )}
          <span>Agent up <strong>{hasData ? formatUptime(data.uptime_seconds) : '—'}</strong></span>
        </div>
      </section>

      <section className="kpis" aria-label="Incident counts since the agent started">
        <KpiCard
          label="Escalated to L2" icon="arrow-up-right" tone="escalated"
          value={val('incidents_escalated')} loading={pending}
          active={hasData && kpi.incidents_escalated > 0}
          note="Needs a person" href="#/incidents" onClick={showOutcome('escalated')}
        />
        <KpiCard
          label="Failed" icon="x-circle" tone="failed"
          value={val('incidents_failed')} loading={pending}
          active={hasData && kpi.incidents_failed > 0}
          note="Execution errors" href="#/incidents" onClick={showOutcome('failed')}
        />
        <KpiCard
          label="Auto-resolved" icon="check-circle"
          value={val('incidents_resolved')} loading={pending}
          note={hasData && received > 0 ? `${Number(rate.toFixed(1))}% of received` : 'No SOP run completed yet'}
        />
        <KpiCard
          label="Received" icon="inbox"
          value={val('incidents_received')} loading={pending}
          note="Since agent start"
        />
      </section>

      <div className="grid-2">
        <Card title="Outcomes">
          <Outcomes data={outcomes} />
        </Card>
        <Card title="Why incidents were escalated">
          <Reasons data={data?.escalation_reasons} />
        </Card>
      </div>

      <Card title="Last 7 days" note="Days are UTC. This history is kept across agent restarts; the counts above are not.">
        <TrendChart data={data?.trend_7day} />
      </Card>
    </div>
  )
}
