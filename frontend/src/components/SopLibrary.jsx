import { useMemo, useState } from 'react'
import { API_BASE_URL } from '../config'
import { useSession } from '../context/SessionContext'
import { usePolledJson } from '../hooks/usePolledJson'
import { versionHref } from '../hooks/useHashRoute'
import DataNotice from './DataNotice'
import Icon from './Icon'
import RelativeTime from './RelativeTime'
import SopReview from './SopReview'
import { SopStatus, who } from './SopStatus'

const FILTERS = [
  ['', 'All versions'],
  ['in_review', 'Waiting for review'],
  ['draft', 'Drafts'],
  ['approved', 'Approved, not published'],
  ['published', 'Published'],
]

function List() {
  const { user } = useSession()
  const [filter, setFilter] = useState('')
  const { data, error, loading, updatedAt, refresh } = usePolledJson(
    `${API_BASE_URL}/api/sops`, { intervalMs: 15000, select: j => j.items || [] },
  )
  const items = useMemo(
    () => (data || []).filter(v => !filter || v.status === filter),
    [data, filter],
  )
  const waiting = (data || []).filter(v => v.status === 'in_review').length
  const canAuthor = user.permissions.includes('sops.author')
  const hasData = data !== null

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 id="page-title" tabIndex={-1} className="page-title">SOP library</h1>
          <p className="page-sub">
            Only a published version is ever run. A version is published after one person
            submits it and a different person approves it.
            {hasData && waiting > 0 && ` ${waiting} waiting for review.`}
          </p>
        </div>
        {canAuthor && <a className="btn" href="#/library/new"><Icon name="book" />New draft</a>}
      </header>

      <DataNotice error={error} hasData={hasData} updatedAt={updatedAt} onRetry={refresh} what="the SOP library" />

      <div className="toolbar">
        <div className="field-group">
          <label className="field-label" htmlFor="sop-filter">Show</label>
          <select id="sop-filter" className="field" value={filter} onChange={e => setFilter(e.target.value)}>
            {FILTERS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
        </div>
      </div>

      <div className="table-wrap">
        {loading && !hasData ? (
          <div style={{ padding: 16, display: 'grid', gap: 14 }} aria-hidden="true">
            {[0, 1, 2].map(i => <span key={i} className="skel skel--block" />)}
          </div>
        ) : !hasData ? (
          <div className="state">
            <Icon name="wifi-off" />
            <p className="state__title">SOP library unavailable</p>
            <p className="state__body">Nothing is shown here until the agent API responds.</p>
          </div>
        ) : items.length === 0 ? (
          <div className="state">
            <Icon name="book" />
            <p className="state__title">{filter ? 'Nothing in this state' : 'No SOP yet'}</p>
            <p className="state__body">
              Drafts arrive from the knowledge sources, or an author writes one here.
            </p>
          </div>
        ) : (
          <div className="table-scroll" tabIndex={0} role="region" aria-label="SOP versions">
            <table className="dtable dtable--library">
              <thead>
                <tr>
                  <th scope="col">SOP</th>
                  <th scope="col">Title</th>
                  <th scope="col">State</th>
                  <th scope="col" className="num">Version</th>
                  <th scope="col">Author</th>
                  <th scope="col">Approver</th>
                  <th scope="col">Changed</th>
                </tr>
              </thead>
              <tbody>
                {items.map(v => (
                  <tr key={v.id}>
                    <td data-label="SOP"><a className="row-link mono" href={versionHref(v.id)}>{v.sop_id}</a></td>
                    <td data-label="Title" className="c-title clip" title={v.title}>{v.title || '—'}</td>
                    <td data-label="State">
                      <SopStatus status={v.status} />
                      {v.origin === 'model_drafted' && <>{' '}<span className="badge badge--neutral">Model-drafted</span></>}
                    </td>
                    <td data-label="Version" className="num">{v.version ?? '—'}</td>
                    <td data-label="Author">{who(v.author)}</td>
                    <td data-label="Approver">{who(v.approver)}</td>
                    <td data-label="Changed"><RelativeTime ts={v.updated_at} /></td>
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

export default function SopLibrary({ version }) {
  return version ? <SopReview key={version} versionId={version} /> : <List />
}
