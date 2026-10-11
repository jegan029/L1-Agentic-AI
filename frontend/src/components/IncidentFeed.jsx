import { useState, useEffect, useCallback, memo } from 'react'
import { useIncidentHistory } from '../hooks/useIncidentHistory'
import { useIncidentFilters } from '../context/AgentContext'
import { useDebouncedValue } from '../hooks/useDebouncedValue'
import { useHashRoute, navigate, incidentHref } from '../hooks/useHashRoute'
import StatusBadge, { PriorityBadge } from './StatusBadge'
import IncidentDetail from './IncidentDetail'
import RelativeTime from './RelativeTime'
import Icon from './Icon'
import { formatDuration, csvField } from '../utils/formatters'

const CSV_COLUMNS = ['incident_number', 'short_description', 'sop_title', 'outcome', 'priority', 'duration_ms', 'started_at']

function exportCsv(items) {
  const rows = [CSV_COLUMNS.map(csvField).join(',')]
  items.forEach(r => rows.push(CSV_COLUMNS.map(c => csvField(r[c])).join(',')))
  const blob = new Blob([`${rows.join('\r\n')}\r\n`], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'incidents.csv'
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

const IncidentRow = memo(function IncidentRow({ r }) {
  const href = incidentHref(r.incident_number)
  return (
    <tr role="row" className="is-clickable" onClick={() => navigate(href)}>
      <td role="cell" className="c-prio"><PriorityBadge value={r.priority} /></td>
      <td role="cell" className="c-num">
        <a className="row-link" href={href} onClick={e => e.stopPropagation()}>{r.incident_number}</a>
      </td>
      <td role="cell" className="c-desc clip" title={r.short_description}>{r.short_description}</td>
      <td role="cell" className="c-sop clip muted" title={r.sop_title || undefined}>{r.sop_title || 'No SOP'}</td>
      <td role="cell" className="c-out"><StatusBadge outcome={r.outcome} /></td>
      <td role="cell" className="c-dur muted">{formatDuration(r.duration_ms)}</td>
      <td role="cell" className="c-time muted"><RelativeTime ts={r.started_at} /></td>
    </tr>
  )
})

function Skeleton() {
  return (
    <div style={{ padding: 16, display: 'grid', gap: 14 }} aria-hidden="true">
      {[0, 1, 2, 3, 4].map(i => <span key={i} className="skel skel--block" />)}
    </div>
  )
}

export default function IncidentFeed() {
  const { filters, setFilters, resetFilters } = useIncidentFilters()
  const { total, items, loading, refreshing, error, refresh } = useIncidentHistory(filters)
  const { incident } = useHashRoute()

  // Typing updates the box immediately; the query follows 300 ms after the last keystroke.
  const [query, setQuery] = useState(filters.search)
  const debounced = useDebouncedValue(query, 300)
  useEffect(() => {
    if (debounced !== filters.search) setFilters(f => ({ ...f, search: debounced, page: 1 }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced])
  useEffect(() => { setQuery(filters.search) }, [filters.search])

  const setFilter = useCallback(
    (key, val) => setFilters(f => ({ ...f, [key]: val, page: key === 'page' ? val : 1 })),
    [setFilters],
  )

  const perPage = filters.perPage
  const totalPages = Math.max(1, Math.ceil(total / perPage))
  const from = total === 0 ? 0 : (filters.page - 1) * perPage + 1
  const to = Math.min(total, filters.page * perPage)
  const filtered = Boolean(filters.search || filters.outcome || filters.priority)

  let body
  if (loading) {
    body = <Skeleton />
  } else if (error && items.length === 0) {
    body = (
      <div className="state" role="alert">
        <Icon name="wifi-off" />
        <p className="state__title">Couldn&rsquo;t load incidents</p>
        <p className="state__body">{error}. This is a loading problem, not an empty history.</p>
        <button type="button" className="btn" onClick={refresh}>Try again</button>
      </div>
    )
  } else if (items.length === 0) {
    body = filtered ? (
      <div className="state">
        <Icon name="search" />
        <p className="state__title">No incidents match these filters</p>
        <p className="state__body">Try a different search term, or show every outcome and priority.</p>
        <button type="button" className="btn" onClick={resetFilters}>Clear filters</button>
      </div>
    ) : (
      <div className="state">
        <Icon name="inbox" />
        <p className="state__title">No incidents processed yet</p>
        <p className="state__body">Incidents appear here as the agent receives them from ServiceNow, each with its full execution trace.</p>
      </div>
    )
  }

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 id="page-title" tabIndex={-1} className="page-title">Incident feed</h1>
          <p className="page-sub">
            {loading ? 'Loading incidents…' : `${total} ${total === 1 ? 'incident' : 'incidents'}${filtered ? ' match' : ' processed'}, newest first.`}
            {' '}Select an incident number to see its execution trace.
          </p>
        </div>

        <form className="toolbar" role="search" aria-label="Filter incidents" onSubmit={e => e.preventDefault()}>
          <div className="field-group">
            <label className="field-label" htmlFor="incident-search">Search</label>
            <input
              id="incident-search" className="field field--search" type="search"
              value={query} onChange={e => setQuery(e.target.value)}
              placeholder="Number, description, SOP"
              autoComplete="off"
            />
          </div>
          <div className="field-group">
            <label className="field-label" htmlFor="incident-outcome">Outcome</label>
            <select
              id="incident-outcome" className="field"
              value={filters.outcome} onChange={e => setFilter('outcome', e.target.value)}
            >
              <option value="">All outcomes</option>
              <option value="resolved">Resolved</option>
              <option value="escalated">Escalated</option>
              <option value="failed">Failed</option>
              <option value="partial">Partial</option>
            </select>
          </div>
          <div className="field-group">
            <label className="field-label" htmlFor="incident-priority">Priority</label>
            <select
              id="incident-priority" className="field"
              value={filters.priority} onChange={e => setFilter('priority', e.target.value)}
            >
              <option value="">All priorities</option>
              <option value="1">P1 Critical</option>
              <option value="2">P2 High</option>
              <option value="3">P3 Moderate</option>
              <option value="4">P4 Low</option>
              <option value="5">P5 Planning</option>
            </select>
          </div>
          <button
            type="button" className="btn" onClick={() => exportCsv(items)}
            disabled={items.length === 0} title="Exports the rows on this page"
          >
            <Icon name="download" />Export page (CSV)
          </button>
          <button
            type="button" className={`btn btn--icon${refreshing ? ' is-spinning' : ''}`}
            onClick={refresh} aria-label="Refresh incidents"
          >
            <Icon name="refresh" />
          </button>
        </form>
      </header>

      {error && items.length > 0 && (
        <div className="notice notice--warn" role="status">
          <Icon name="wifi-off" />
          <div className="notice__text"><strong>Couldn&rsquo;t refresh</strong>{error}. Showing the last loaded page.</div>
          <button type="button" className="btn" onClick={refresh}>Retry</button>
        </div>
      )}

      <div className="table-wrap">
        {body ? body : (
          <div className="table-scroll" tabIndex={0} role="region" aria-label="Incidents table">
            <table className="dtable dtable--incidents" role="table" aria-busy={refreshing}>
              <thead role="rowgroup">
                <tr role="row">
                  <th role="columnheader" scope="col">Priority</th>
                  <th role="columnheader" scope="col">Incident</th>
                  <th role="columnheader" scope="col">Description</th>
                  <th role="columnheader" scope="col">SOP</th>
                  <th role="columnheader" scope="col">Outcome</th>
                  <th role="columnheader" scope="col">Duration</th>
                  <th role="columnheader" scope="col">Started</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {items.map(r => <IncidentRow key={r.incident_number} r={r} />)}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {!loading && total > 0 && (
        <nav className="pager" aria-label="Pagination">
          <button
            type="button" className="btn"
            onClick={() => setFilter('page', filters.page - 1)} disabled={filters.page <= 1}
          >
            Previous
          </button>
          <span aria-live="polite">
            {from}&ndash;{to} of {total} &middot; page {filters.page} of {totalPages}
          </span>
          <button
            type="button" className="btn"
            onClick={() => setFilter('page', filters.page + 1)} disabled={filters.page >= totalPages}
          >
            Next
          </button>
        </nav>
      )}

      {incident && (
        <IncidentDetail
          key={incident}
          incidentNumber={incident}
          onClose={() => navigate('#/incidents')}
        />
      )}
    </div>
  )
}
