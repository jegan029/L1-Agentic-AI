import { useState } from 'react'
import { API_BASE_URL } from '../config'
import { useSession } from '../context/SessionContext'
import { usePolledJson } from '../hooks/usePolledJson'
import { incidentHref } from '../hooks/useHashRoute'
import { send } from '../utils/api'
import { formatTimestamp } from '../utils/formatters'
import DataNotice from './DataNotice'
import Icon from './Icon'
import RelativeTime from './RelativeTime'
import { who } from './SopStatus'

const STATUS = {
  pending:         { label: 'Waiting for approval', tone: 'escalated' },
  approved:        { label: 'Approved',             tone: 'partial' },
  allowed:         { label: 'Allowed by policy',    tone: 'partial' },
  executing:       { label: 'Executing',            tone: 'partial' },
  executed:        { label: 'Verifying',            tone: 'partial' },
  verified:        { label: 'Done and verified',    tone: 'resolved' },
  shadowed:        { label: 'Shadow: not done',     tone: 'neutral' },
  recommended:     { label: 'Recommended only',     tone: 'neutral' },
  denied:          { label: 'Denied by policy',     tone: 'failed' },
  rejected:        { label: 'Rejected',             tone: 'failed' },
  expired:         { label: 'Request expired',      tone: 'neutral' },
  invalid:         { label: 'No longer valid',      tone: 'failed' },
  not_done:        { label: 'Not done',             tone: 'neutral' },
  failed:          { label: 'Failed',               tone: 'failed' },
  rolling_back:    { label: 'Rolling back',         tone: 'failed' },
  rolled_back:     { label: 'Rolled back',          tone: 'failed' },
  rollback_failed: { label: 'Rollback failed',      tone: 'failed' },
  aborted:         { label: 'Stopped',              tone: 'neutral' },
}

const LEVELS = [
  ['shadow', 'Shadow: record what would be done, change nothing'],
  ['recommend', 'Recommend: post the proposed action to the ticket, then escalate'],
  ['approval', 'Approval: ask a person, act when they approve'],
  ['automatic', 'Automatic: act without a person for listed T1 actions only'],
]

function ActionStatus({ status }) {
  const s = STATUS[status] || { label: status, tone: 'neutral' }
  return <span className={`badge badge--${s.tone}`}>{s.label}</span>
}

function Card({ title, children }) {
  return (
    <section className="card">
      <h2 className="card__title">{title}</h2>
      <div className="card__body">{children}</div>
    </section>
  )
}

function Failure({ failure }) {
  if (!failure) return null
  return (
    <div className="notice notice--error" role="alert">
      <Icon name="alert-triangle" />
      <div className="notice__text">
        <strong>{failure.error}</strong>
        {failure.problems.length > 0
          ? <ul className="plain-list">{failure.problems.map((p, i) => <li key={i}>{p}</li>)}</ul>
          : 'Nothing was changed.'}
      </div>
    </div>
  )
}

/** One request, showing exactly what the approval would be for. */
function Request({ action, user, onDone }) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState(null)
  const canDecide = user.permissions.includes('actions.approve')
  const isAuthor = action.sop_author === `user:${user.id}`
  const blocked = action.tier === 'T2' && isAuthor

  const decide = async verb => {
    setBusy(true)
    setFailure(null)
    // The hash sent back is the one shown here. If the action changed, the API refuses.
    const res = await send(user, 'POST', `/api/actions/${encodeURIComponent(action.id)}/${verb}`, {
      action_hash: action.action_hash,
      note: note.trim(),
    })
    setBusy(false)
    if (res.ok) onDone()
    else setFailure(res)
  }

  return (
    <li className="request">
      <div className="request__head">
        <span className="mono request__what">{action.capability}</span>
        <span className={`badge badge--${action.tier === 'T2' ? 'failed' : 'partial'}`}>Tier {action.tier}</span>
        <a className="row-link mono" href={incidentHref(action.incident_number)}>{action.incident_number}</a>
      </div>
      <dl className="request__facts">
        <div><dt>Parameters</dt><dd className="mono">{JSON.stringify(action.parameters)}</dd></div>
        <div><dt>SOP</dt><dd><span className="mono">{action.sop_id}</span> version {action.sop_version}, written by {who(action.sop_author)}</dd></div>
        <div><dt>Why approval is needed</dt><dd>{action.rule}</dd></div>
        <div><dt>Change record</dt><dd className="mono">{action.change_number || '—'}</dd></div>
        <div><dt>Expires</dt><dd>{formatTimestamp(action.expires_at)}</dd></div>
        <div><dt>Action hash</dt><dd className="mono request__hash">{action.action_hash}</dd></div>
      </dl>
      <Failure failure={failure} />
      {canDecide ? (
        <div className="actions">
          <span className="field-group actions__note">
            <label className="field-label" htmlFor={`note-${action.id}`}>Note (optional for approval)</label>
            <input id={`note-${action.id}`} className="field" value={note} maxLength={1000}
              onChange={e => setNote(e.target.value)} />
          </span>
          <button type="button" className="btn btn--primary" disabled={busy || blocked}
            onClick={() => decide('approve')}>Approve</button>
          <button type="button" className="btn" disabled={busy} onClick={() => decide('reject')}>Reject</button>
        </div>
      ) : (
        <p className="muted">Waiting for someone with the approver role.</p>
      )}
      {canDecide && blocked && (
        <p className="muted">You wrote this SOP, so someone else has to approve a tier 2 action from it.</p>
      )}
    </li>
  )
}

function Policy({ user }) {
  const { data, error, updatedAt, refresh } = usePolledJson(`${API_BASE_URL}/api/policy`, { intervalMs: 10000 })
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState(null)
  const canManage = user.permissions.includes('policy.manage')
  const canStop = user.permissions.includes('actions.stop')

  const call = async (method, path, body) => {
    setBusy(true)
    setFailure(null)
    const res = await send(user, method, path, body)
    setBusy(false)
    if (res.ok) { setReason(''); refresh() } else setFailure(res)
  }

  if (!data) {
    return <DataNotice error={error} hasData={false} updatedAt={updatedAt} onRetry={refresh} what="the policy" />
  }
  const tenantOn = data.kill_switch.on
  const anyOn = tenantOn || data.global_kill_switch.on || data.local_kill_switch

  return (
    <>
      {anyOn && (
        <div className="notice notice--error" role="alert">
          <Icon name="x-circle" />
          <div className="notice__text">
            <strong>The kill switch is on. The agent makes no changes.</strong>
            {tenantOn && `Set by ${who(data.kill_switch.set_by)}${data.kill_switch.reason ? `: ${data.kill_switch.reason}` : ''}. `}
            {data.global_kill_switch.on && `Set for every tenant by the platform operator${data.global_kill_switch.reason ? `: ${data.global_kill_switch.reason}` : ''}. `}
            {data.local_kill_switch && 'Set on this server by its operator. '}
            Read-only checks continue.
          </div>
        </div>
      )}
      <Failure failure={failure} />

      <Card title={<>Kill switch</>}>
        <p className="muted">
          Stops every change the agent could make for this tenant, at once. Runs that are waiting
          or in progress stop before their next change and are handed to a person.
        </p>
        {(canStop || canManage) && (
          <div className="actions">
            <span className="field-group actions__note">
              <label className="field-label" htmlFor="kill-reason">Reason</label>
              <input id="kill-reason" className="field" value={reason} maxLength={1000}
                onChange={e => setReason(e.target.value)} />
            </span>
            {!tenantOn && canStop && (
              <button type="button" className="btn btn--danger" disabled={busy}
                onClick={() => call('POST', '/api/kill-switch/engage', { reason: reason.trim() })}>
                Stop all changes
              </button>
            )}
            {tenantOn && canManage && (
              <button type="button" className="btn" disabled={busy}
                onClick={() => call('POST', '/api/kill-switch/release', { reason: reason.trim() })}>
                Allow changes again
              </button>
            )}
          </div>
        )}
        {tenantOn && !canManage && <p className="muted">Only a tenant administrator can switch it off.</p>}
      </Card>

      <Card title={<label htmlFor="autonomy">How far the agent may go</label>}>
        <select id="autonomy" className="field" value={data.autonomy} disabled={!canManage || busy}
          onChange={e => call('PUT', '/api/policy', { autonomy: e.target.value })}>
          {LEVELS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
        <p className="muted">
          Each SOP version also has its own level, and a new version starts in shadow. The lowest
          level applies. Tier 2 actions always need an approval. This server allows at most{' '}
          <strong>{data.ceiling.max_autonomy}</strong> and tier <strong>{data.ceiling.max_tier}</strong>.
        </p>
        <details>
          <summary>Rules (read only here; change them with the API or <span className="mono">l1-agent policy set</span>)</summary>
          <pre className="source-text" tabIndex={0}>{JSON.stringify(data.rules, null, 2)}</pre>
        </details>
      </Card>
    </>
  )
}

export default function Actions() {
  const { user } = useSession()
  const { data, error, loading, updatedAt, refresh } = usePolledJson(
    `${API_BASE_URL}/api/actions?limit=100`, { intervalMs: 5000, select: j => j.items || [] },
  )
  const hasData = data !== null
  const waiting = (data || []).filter(a => a.status === 'pending')
  const rest = (data || []).filter(a => a.status !== 'pending')
  const canSeePolicy = user.permissions.includes('system.read')

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 id="page-title" tabIndex={-1} className="page-title">Actions and approvals</h1>
          <p className="page-sub">
            Every change the agent makes, or was stopped from making, with the policy decision behind it.
            {hasData && waiting.length > 0 && ` ${waiting.length} waiting for approval.`}
          </p>
        </div>
      </header>

      <DataNotice error={error} hasData={hasData} updatedAt={updatedAt} onRetry={refresh} what="the actions" />

      <Card title={<>Waiting for approval</>}>
        {loading && !hasData ? <span className="skel skel--block" aria-hidden="true" />
          : waiting.length === 0 ? <p className="muted">Nothing is waiting.</p>
          : (
            <ul className="requests">
              {waiting.map(a => <Request key={a.id} action={a} user={user} onDone={refresh} />)}
            </ul>
          )}
      </Card>

      {canSeePolicy && <Policy user={user} />}

      <div className="table-wrap">
        {hasData && rest.length === 0 ? (
          <div className="state">
            <Icon name="check-circle" />
            <p className="state__title">No actions yet</p>
            <p className="state__body">An action appears here when a run reaches an SOP step that changes something.</p>
          </div>
        ) : hasData && (
          <div className="table-scroll" tabIndex={0} role="region" aria-label="Recent actions">
            <table className="dtable">
              <thead>
                <tr>
                  <th scope="col">Incident</th>
                  <th scope="col">Action</th>
                  <th scope="col">Tier</th>
                  <th scope="col">Result</th>
                  <th scope="col">Policy</th>
                  <th scope="col">Approved by</th>
                  <th scope="col">Change</th>
                  <th scope="col">When</th>
                </tr>
              </thead>
              <tbody>
                {rest.map(a => (
                  <tr key={a.id}>
                    <td data-label="Incident"><a className="row-link mono" href={incidentHref(a.incident_number)}>{a.incident_number}</a></td>
                    <td data-label="Action" className="clip" title={JSON.stringify(a.parameters)}>
                      <span className="mono">{a.capability}</span> <span className="mono">{JSON.stringify(a.parameters)}</span>
                    </td>
                    <td data-label="Tier">{a.tier}</td>
                    <td data-label="Result"><ActionStatus status={a.status} /></td>
                    <td data-label="Policy" className="clip" title={a.rule}>{a.rule || '—'}</td>
                    <td data-label="Approved by">{a.status === 'rejected' ? '—' : who(a.approver)}</td>
                    <td data-label="Change" className="mono">{a.change_number || '—'}</td>
                    <td data-label="When"><RelativeTime ts={a.created_at} /></td>
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
