import { useState, useMemo } from 'react'
import { useSops } from '../context/AgentContext'
import DataNotice from './DataNotice'
import Icon from './Icon'
import { formatDuration } from '../utils/formatters'

function rateColor(pct) {
  if (pct >= 80) return 'var(--resolved-solid)'
  if (pct >= 50) return 'var(--partial-solid)'
  return 'var(--failed-solid)'
}

const SORT_OPTIONS = [
  ['total_runs', 'Runs'],
  ['success_rate_pct', 'Resolution rate'],
  ['resolved_count', 'Resolved'],
  ['escalated_count', 'Escalated'],
  ['avg_duration_ms', 'Avg duration'],
  ['sop_id', 'SOP'],
]

function SortHeader({ col, label, sortKey, asc, onSort, className }) {
  const active = sortKey === col
  const ariaSort = active ? (asc ? 'ascending' : 'descending') : 'none'
  return (
    <th role="columnheader" scope="col" aria-sort={ariaSort} className={className}>
      <button type="button" className="th-sort" onClick={() => onSort(col)}>
        {label}
        {active && <Icon name={asc ? 'arrow-up' : 'arrow-down'} />}
      </button>
    </th>
  )
}

export default function SopPerformance() {
  const { items, hasData, loading, error, updatedAt, refresh } = useSops()
  const [sortKey, setSortKey] = useState('total_runs')
  const [asc, setAsc] = useState(false)

  const sorted = useMemo(() => {
    const dir = asc ? 1 : -1
    return [...items].sort((a, b) => {
      const av = a[sortKey]
      const bv = b[sortKey]
      if (typeof av === 'string' || typeof bv === 'string') {
        return String(av ?? '').localeCompare(String(bv ?? '')) * dir
      }
      return ((av ?? 0) - (bv ?? 0)) * dir
    })
  }, [items, sortKey, asc])

  const summary = useMemo(() => {
    const runs = items.reduce((s, i) => s + (i.total_runs || 0), 0)
    const resolved = items.reduce((s, i) => s + (i.resolved_count || 0), 0)
    return { runs, resolved, rate: runs ? Math.round((resolved / runs) * 100) : null }
  }, [items])

  const onSort = col => {
    if (col === sortKey) setAsc(a => !a)
    else { setSortKey(col); setAsc(col === 'sop_id') }
  }
  const pending = loading && !hasData
  const sortProps = { sortKey, asc, onSort }

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 id="page-title" tabIndex={-1} className="page-title">SOP performance</h1>
          <p className="page-sub">
            How each Standard Operating Procedure has fared across processed incidents.
            {hasData && ` ${items.length} ${items.length === 1 ? 'SOP has' : 'SOPs have'} been used.`}
          </p>
        </div>
      </header>

      <DataNotice error={error} hasData={hasData} updatedAt={updatedAt} onRetry={refresh} what="SOP statistics" />

      {hasData && items.length > 0 && (
        <dl className="summary">
          <div><dt>SOP runs</dt><dd>{summary.runs}</dd></div>
          <div><dt>Resolved</dt><dd>{summary.resolved}</dd></div>
          <div><dt>Resolution rate</dt><dd>{summary.rate === null ? '—' : `${summary.rate}%`}</dd></div>
          <div><dt>SOPs used</dt><dd>{items.length}</dd></div>
        </dl>
      )}

      {hasData && sorted.length > 1 && (
        <div className="toolbar sort-bar">
          <div className="field-group">
            <label className="field-label" htmlFor="sop-sort">Sort by</label>
            <select id="sop-sort" className="field" value={sortKey} onChange={e => { setSortKey(e.target.value); setAsc(e.target.value === 'sop_id') }}>
              {SORT_OPTIONS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select>
          </div>
          <button
            type="button" className="btn btn--icon" onClick={() => setAsc(a => !a)}
            aria-label={asc ? 'Sorted ascending, switch to descending' : 'Sorted descending, switch to ascending'}
          >
            <Icon name={asc ? 'arrow-up' : 'arrow-down'} />
          </button>
        </div>
      )}

      <div className="table-wrap">
        {pending ? (
          <div style={{ padding: 16, display: 'grid', gap: 14 }} aria-hidden="true">
            {[0, 1, 2].map(i => <span key={i} className="skel skel--block" />)}
          </div>
        ) : !hasData ? (
          <div className="state">
            <Icon name="wifi-off" />
            <p className="state__title">SOP statistics unavailable</p>
            <p className="state__body">Nothing is shown here until the agent API responds.</p>
          </div>
        ) : sorted.length === 0 ? (
          <div className="state">
            <Icon name="book" />
            <p className="state__title">No SOP has run yet</p>
            <p className="state__body">Statistics appear after the agent executes its first SOP against an incident.</p>
          </div>
        ) : (
          <div className="table-scroll" tabIndex={0} role="region" aria-label="SOP performance table">
            <table className="dtable dtable--sops" role="table">
              <thead role="rowgroup">
                <tr role="row">
                  <SortHeader col="sop_id" label="SOP" {...sortProps} />
                  <SortHeader col="sop_title" label="Title" {...sortProps} />
                  <SortHeader col="total_runs" label="Runs" className="num" {...sortProps} />
                  <SortHeader col="resolved_count" label="Resolved" className="num" {...sortProps} />
                  <SortHeader col="escalated_count" label="Escalated" className="num" {...sortProps} />
                  <SortHeader col="success_rate_pct" label="Resolution rate" {...sortProps} />
                  <SortHeader col="avg_duration_ms" label="Avg duration" className="num" {...sortProps} />
                </tr>
              </thead>
              <tbody role="rowgroup">
                {sorted.map(r => (
                  <tr role="row" key={r.sop_id}>
                    <td role="cell" className="mono" data-label="SOP" style={{ fontWeight: 600, color: 'var(--ss-blue)', whiteSpace: 'nowrap' }}>{r.sop_id}</td>
                    <td role="cell" className="c-title clip" data-label="Title" title={r.sop_title}>{r.sop_title}</td>
                    <td role="cell" className="num" data-label="Runs">{r.total_runs}</td>
                    <td role="cell" className="num" data-label="Resolved">{r.resolved_count}</td>
                    <td role="cell" className="num" data-label="Escalated">{r.escalated_count}</td>
                    <td role="cell" className="c-rate" data-label="Resolution rate">
                      <div className="progress">
                        <div className="progress__track" aria-hidden="true">
                          <div
                            className="progress__fill"
                            style={{
                              transform: `scaleX(${Math.min(Math.max(r.success_rate_pct, 0), 100) / 100})`,
                              background: rateColor(r.success_rate_pct),
                            }}
                          />
                        </div>
                        <span className="progress__value">{r.success_rate_pct}%</span>
                      </div>
                    </td>
                    <td role="cell" className="num" data-label="Avg duration">{formatDuration(r.avg_duration_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
