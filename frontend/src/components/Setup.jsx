import { useState } from 'react'
import { API_BASE_URL } from '../config'
import { useSession } from '../context/SessionContext'
import { usePolledJson } from '../hooks/usePolledJson'
import { send } from '../utils/api'
import DataNotice from './DataNotice'
import Icon from './Icon'
import RelativeTime from './RelativeTime'
import { who } from './SopStatus'

const FAMILIES = [
  ['itsm', 'Ticketing', 'Where incidents come from and where notes, hand-overs and change records go.'],
  ['knowledge', 'SOP sources', 'Articles from a source become drafts. A person reviews each one before it can run.'],
  ['operations', 'Operations', 'The systems an SOP step can look at and, where a write is declared, change.'],
]

const REFERENCE_HINT = 'vault:path/field, aws:name or file:/run/secrets/…'

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
        {failure.problems?.length > 0
          ? <ul className="plain-list">{failure.problems.map((p, i) => <li key={i}>{p}</li>)}</ul>
          : 'Nothing was changed.'}
      </div>
    </div>
  )
}

/* ── The checklist ─────────────────────────────────────────────────── */

function Checklist({ report }) {
  return (
    <Card title={<>Steps{' '}<span className="muted">{report.done} of {report.total} required done</span></>}>
      <ol className="setup-steps">
        {report.steps.map(step => (
          <li key={step.id} className="setup-step">
            <span className={`badge badge--${step.done ? 'resolved' : step.required ? 'escalated' : 'neutral'}`}>
              {step.done ? 'Done' : step.required ? 'To do' : 'Optional'}
            </span>
            <div>
              <p className="setup-step__title">{step.title}</p>
              <p className="setup-step__detail">{step.detail}</p>
              {!step.done && step.how && <p className="muted">{step.how}</p>}
            </div>
          </li>
        ))}
      </ol>
    </Card>
  )
}

/* ── Runners ───────────────────────────────────────────────────────── */

function runnerState(r) {
  if (r.revoked) return ['Revoked', 'neutral']
  if (r.connected) return ['Connected', 'resolved']
  return [r.last_seen_at ? 'Not connected' : 'Never connected', 'escalated']
}

function Runners({ user, canManage }) {
  const { data, error, updatedAt, refresh } = usePolledJson(`${API_BASE_URL}/api/runners`, { intervalMs: 10000 })
  const [name, setName] = useState('')
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState(null)
  const [enrolled, setEnrolled] = useState(null)

  if (!data) {
    return <DataNotice error={error} hasData={false} updatedAt={updatedAt} onRetry={refresh} what="the runners" />
  }
  if (data.mode !== 'remote') {
    return (
      <Card title={<>Runner</>}>
        <p className="muted">
          This deployment makes connector calls itself (<span className="mono">RUNNER_MODE=embedded</span>).
          With a runner, the systems below are reached only from where the runner is installed, and
          their credentials never leave it.
        </p>
      </Card>
    )
  }

  const enrol = async e => {
    e.preventDefault()
    setBusy(true); setFailure(null); setEnrolled(null)
    const res = await send(user, 'POST', '/api/runners', { name: name.trim(), public_key: key.trim() })
    setBusy(false)
    if (res.ok) { setEnrolled(res.data); setName(''); setKey(''); refresh() } else setFailure(res)
  }
  const revoke = async r => {
    if (!window.confirm(`Revoke ${r.name}? It is refused from its next request, and connector calls stop until another runner is connected.`)) return
    setBusy(true); setFailure(null)
    const res = await send(user, 'DELETE', `/api/runners/${encodeURIComponent(r.id)}`)
    setBusy(false)
    if (res.ok) refresh(); else setFailure(res)
  }

  return (
    <Card title={<>Runners</>}>
      <p className="muted">
        A runner makes the connector calls from inside your network. It connects outwards to this
        server and accepts no connections. It holds its own limits, which nothing here can raise.
      </p>
      <Failure failure={failure} />
      {data.items.length === 0 ? <p>No runner is enrolled, so no connector call can be made.</p> : (
        <ul className="requests">
          {data.items.map(r => {
            const [label, tone] = runnerState(r)
            const rep = r.reported || {}
            return (
              <li key={r.id} className="request">
                <div className="request__head">
                  <span className="request__what">{r.name}</span>
                  <span className={`badge badge--${tone}`}>{label}</span>
                  {rep.mock && <span className="badge badge--neutral">Mock connectors</span>}
                </div>
                <dl className="request__facts">
                  <div><dt>Last asked for work</dt><dd>{r.last_seen_at ? <RelativeTime ts={r.last_seen_at} /> : '—'}</dd></div>
                  <div><dt>Its own ceiling</dt><dd>
                    {rep.max_tier
                      ? <>up to tier {rep.max_tier}; {rep.writes ? 'can check grants for writes' : 'makes no writes (no grant key)'}{rep.kill_switch ? '; its kill switch is on' : ''}</>
                      : '—'}
                  </dd></div>
                  <div><dt>Key fingerprint</dt><dd className="mono">{r.key_fingerprint}</dd></div>
                  <div><dt>Enrolled by</dt><dd>{who(r.enrolled_by)}</dd></div>
                </dl>
                {canManage && !r.revoked && (
                  <div className="actions">
                    <button type="button" className="btn btn--danger" disabled={busy} onClick={() => revoke(r)}>Revoke</button>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}

      <details>
        <summary>Keys the runner is configured with</summary>
        <p className="muted">
          Public keys. The runner uses them to check that a call came from this server and that
          a write was authorised by the policy engine.
        </p>
        <dl className="request__facts">
          <div><dt><span className="mono">RUNNER_DISPATCH_PUBLIC_KEY</span></dt><dd className="mono">{data.keys.dispatch || '—'}</dd></div>
          <div><dt><span className="mono">RUNNER_GRANT_PUBLIC_KEY</span></dt><dd className="mono">{data.keys.grant || 'not shown by this process; ask the operator'}</dd></div>
        </dl>
      </details>

      {canManage && (
        <details>
          <summary>Enrol a runner</summary>
          <p className="muted">
            On the machine where the runner will run: <span className="mono">l1-agent runner keygen --out runner.key</span>.
            Paste the public key it prints. The private key stays there.
          </p>
          <form className="setup-form" onSubmit={enrol}>
            <span className="field-group">
              <label className="field-label" htmlFor="runner-name">Name</label>
              <input id="runner-name" className="field" value={name} maxLength={100} required
                onChange={e => setName(e.target.value)} />
            </span>
            <span className="field-group">
              <label className="field-label" htmlFor="runner-key">Public key</label>
              <input id="runner-key" className="field mono" value={key} minLength={40} maxLength={64} required
                spellCheck={false} autoComplete="off" onChange={e => setKey(e.target.value)} />
            </span>
            <div className="actions">
              <button type="submit" className="btn btn--primary" disabled={busy}>Enrol</button>
            </div>
          </form>
          {enrolled && (
            <div className="notice notice--warn" role="status">
              <Icon name="check-circle" />
              <div className="notice__text">
                <strong>Enrolled.</strong>
                Start the runner with <span className="mono">RUNNER_ID={enrolled.identity}</span>, or leave
                <span className="mono"> RUNNER_ID</span> unset and it is recognised by its key.
              </div>
            </div>
          )}
        </details>
      )}
    </Card>
  )
}

/* ── Connectors ────────────────────────────────────────────────────── */

function connectionState(c) {
  if (c.error) return ['Not usable', 'failed']
  if (!c.configured) return ['Not connected', 'neutral']
  if (!c.enabled) return ['Switched off', 'neutral']
  if (c.origin === 'demo') return ['Demo stand-in', 'partial']
  if (c.origin === 'environment') return ['Connected by the deployment', 'resolved']
  return ['Connected', 'resolved']
}

/** A setting's text form and back, by its type in the connector's schema. */
function toText(spec, value) {
  if (value === undefined || value === null) return spec.type === 'boolean' ? false : ''
  if (spec.type === 'array') return Array.isArray(value) ? value.map(v => (typeof v === 'string' ? v : JSON.stringify(v))).join('\n') : ''
  if (spec.type === 'object') return JSON.stringify(value)
  return value
}

function fromText(spec, text) {
  if (spec.type === 'boolean') return { value: Boolean(text) }
  if (text === '' || text === null) return { skip: true }
  if (spec.type === 'array') return { value: String(text).split('\n').map(s => s.trim()).filter(Boolean) }
  if (spec.type === 'integer' || spec.type === 'number') {
    const n = Number(text)
    return Number.isFinite(n) ? { value: n } : { error: 'is not a number' }
  }
  if (spec.type === 'object') {
    try { return { value: JSON.parse(text) } } catch { return { error: 'is not valid JSON' } }
  }
  return { value: String(text) }
}

function Wizard({ connector, user, onSaved, onClose }) {
  const props = connector.config?.properties || {}
  const required = connector.config?.required || []
  const [settings, setSettings] = useState(() =>
    Object.fromEntries(Object.entries(props).map(([k, spec]) => [k, toText(spec, connector.config_values?.[k])])))
  const [refs, setRefs] = useState(() =>
    Object.fromEntries(connector.secrets.map(s => [s.name, connector.credentials?.[s.name] || ''])))
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState(null)
  const [checks, setChecks] = useState(null)
  const id = connector.id

  const save = async e => {
    e.preventDefault()
    const config = {}
    for (const [k, spec] of Object.entries(props)) {
      const parsed = fromText(spec, settings[k])
      if (parsed.error) { setFailure({ error: `${k} ${parsed.error}`, problems: [] }); return }
      if (!parsed.skip) config[k] = parsed.value
    }
    const secrets = Object.fromEntries(Object.entries(refs).map(([k, v]) => [k, v.trim()]).filter(([, v]) => v))
    setBusy(true); setFailure(null); setChecks(null)
    const saved = await send(user, 'PUT', `/api/connectors/${encodeURIComponent(id)}/connection`, { config, secrets, enabled: true })
    if (!saved.ok) { setBusy(false); setFailure(saved); return }
    // Saved. Now find out whether it works, from where the calls will really be made.
    const tested = await send(user, 'POST', `/api/connectors/${encodeURIComponent(id)}/test`)
    setBusy(false)
    if (tested.ok) setChecks(tested.data.checks); else setFailure({ ...tested, error: `Saved, but the test could not be run: ${tested.error}` })
    onSaved()
  }

  return (
    <form className="setup-form" onSubmit={save} aria-label={`Connect ${connector.display_name}`}>
      {connector.notes && <p className="muted">{connector.notes}</p>}
      {Object.entries(props).map(([k, spec]) => (
        <span key={k} className="field-group">
          <label className="field-label" htmlFor={`${id}-${k}`}>
            <span className="mono">{k}</span>{required.includes(k) ? ' (required)' : ''}
            {spec.type === 'array' && ', one per line'}
          </label>
          {spec.type === 'boolean' ? (
            <input id={`${id}-${k}`} type="checkbox" checked={Boolean(settings[k])}
              onChange={e => setSettings(s => ({ ...s, [k]: e.target.checked }))} />
          ) : spec.enum ? (
            <select id={`${id}-${k}`} className="field" value={settings[k]}
              onChange={e => setSettings(s => ({ ...s, [k]: e.target.value }))}>
              <option value="">(default)</option>
              {spec.enum.map(v => <option key={v} value={v}>{v}</option>)}
            </select>
          ) : spec.type === 'array' ? (
            <textarea id={`${id}-${k}`} className="field setup-form__list mono" rows={3} value={settings[k]}
              spellCheck={false} onChange={e => setSettings(s => ({ ...s, [k]: e.target.value }))} />
          ) : (
            <input id={`${id}-${k}`} className="field" value={settings[k]} required={required.includes(k)}
              inputMode={spec.type === 'integer' ? 'numeric' : undefined} spellCheck={false} autoComplete="off"
              onChange={e => setSettings(s => ({ ...s, [k]: e.target.value }))} />
          )}
        </span>
      ))}

      {connector.secrets.length > 0 && (
        <fieldset className="setup-form__group">
          <legend className="field-label">Credentials: where each one is kept, never the credential itself</legend>
          {connector.secrets.map(slot => (
            <span key={slot.name} className="field-group">
              <label className="field-label" htmlFor={`${id}-ref-${slot.name}`}>
                {slot.purpose}{slot.required ? '' : ' (optional)'}
              </label>
              <input id={`${id}-ref-${slot.name}`} className="field mono" value={refs[slot.name]}
                placeholder={REFERENCE_HINT} spellCheck={false} autoComplete="off"
                onChange={e => setRefs(r => ({ ...r, [slot.name]: e.target.value }))} />
            </span>
          ))}
        </fieldset>
      )}

      <Failure failure={failure} />
      {checks && (
        <ul className="plain-list" aria-label="Connection test">
          {checks.map((c, i) => (
            <li key={i}>
              <span className={`badge badge--${c.ok ? 'resolved' : 'failed'}`}>{c.ok ? 'Passed' : 'Failed'}</span>{' '}
              {c.name}{c.detail ? `: ${c.detail}` : ''}
            </li>
          ))}
        </ul>
      )}
      <div className="actions">
        <button type="submit" className="btn btn--primary" disabled={busy}>{busy ? 'Saving and testing…' : 'Save and test'}</button>
        <button type="button" className="btn" disabled={busy} onClick={onClose}>Close</button>
      </div>
    </form>
  )
}

function Connector({ connector, user, canManage, onChanged }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState(null)
  const [checks, setChecks] = useState(null)
  const [label, tone] = connectionState(connector)
  const stored = connector.origin === 'connection'
  const writes = connector.capabilities.filter(c => c.kind === 'write')
  // The stored settings travel with the list; the form needs them under a name of its own.
  const forForm = { ...connector, config: connector.schema, config_values: connector.values }

  const test = async () => {
    setBusy(true); setFailure(null); setChecks(null)
    const res = await send(user, 'POST', `/api/connectors/${encodeURIComponent(connector.id)}/test`)
    setBusy(false)
    if (res.ok) setChecks(res.data.checks); else setFailure(res)
  }
  const remove = async () => {
    if (!window.confirm(`Remove the ${connector.display_name} connection? SOP steps that use it will stop with an error until it is connected again.`)) return
    setBusy(true); setFailure(null)
    const res = await send(user, 'DELETE', `/api/connectors/${encodeURIComponent(connector.id)}/connection`)
    setBusy(false)
    if (res.ok) { setChecks(null); onChanged() } else setFailure(res)
  }

  return (
    <li className="request">
      <div className="request__head">
        <span className="request__what">{connector.display_name}</span>
        <span className={`badge badge--${tone}`}>{label}</span>
        {connector.preview && <span className="badge badge--partial" title="Read only. Not verified against a real system, and not in the default image.">Preview</span>}
        {writes.length > 0 && <span className="badge badge--neutral">Can make changes</span>}
      </div>
      <dl className="request__facts">
        <div><dt>Calls are made by</dt><dd>{connector.runs_on === 'runner' ? 'the runner' : 'this server'}</dd></div>
        {connector.error && <div><dt>Problem</dt><dd>{connector.error}</dd></div>}
        {Object.keys(connector.credentials || {}).length > 0 && (
          <div><dt>Credentials kept at</dt><dd className="mono">{Object.entries(connector.credentials).map(([k, v]) => `${k}: ${v}`).join(', ')}</dd></div>
        )}
        <div><dt>What it can do</dt><dd>
          {connector.capabilities.length === 0 ? 'Intake, notes and change records' : (
            <ul className="plain-list">
              {connector.capabilities.map(c => (
                <li key={c.id}><span className="mono">{c.id}</span> {c.kind === 'write' ? `(change, tier ${c.tier})` : '(read)'}: {c.summary}</li>
              ))}
            </ul>
          )}
        </dd></div>
      </dl>
      <Failure failure={failure} />
      {checks && !open && (
        <ul className="plain-list" aria-label="Connection test">
          {checks.map((c, i) => (
            <li key={i}>
              <span className={`badge badge--${c.ok ? 'resolved' : 'failed'}`}>{c.ok ? 'Passed' : 'Failed'}</span>{' '}
              {c.name}{c.detail ? `: ${c.detail}` : ''}
            </li>
          ))}
        </ul>
      )}
      {open ? (
        <Wizard connector={forForm} user={user} onSaved={onChanged} onClose={() => setOpen(false)} />
      ) : canManage && (
        <div className="actions">
          {connector.origin !== 'demo' && (
            <button type="button" className={connector.configured ? 'btn' : 'btn btn--primary'} disabled={busy}
              onClick={() => { setChecks(null); setOpen(true) }}>
              {stored ? 'Change' : connector.configured ? 'Replace the deployment’s settings' : 'Connect'}
            </button>
          )}
          {connector.configured && <button type="button" className="btn" disabled={busy} onClick={test}>{busy ? 'Testing…' : 'Test'}</button>}
          {stored && <button type="button" className="btn btn--danger" disabled={busy} onClick={remove}>Remove</button>}
        </div>
      )}
    </li>
  )
}

function Connectors({ user, canManage, onChanged }) {
  const { data, error, loading, updatedAt, refresh } = usePolledJson(
    `${API_BASE_URL}/api/connectors`,
    // `config` is the connector's settings schema; the stored values arrive beside it.
    { intervalMs: 15000, select: j => (j.items || []).map(c => ({ ...c, schema: c.config, values: c.settings })) },
  )
  const changed = () => { refresh(); onChanged() }
  if (!data) {
    return loading
      ? <span className="skel skel--block" aria-hidden="true" />
      : <DataNotice error={error} hasData={false} updatedAt={updatedAt} onRetry={refresh} what="the connectors" />
  }
  return (
    <>
      <DataNotice error={error} hasData updatedAt={updatedAt} onRetry={refresh} what="the connectors" />
      {FAMILIES.map(([family, title, lead]) => {
        const items = data.filter(c => c.family === family)
        if (items.length === 0) return null
        return (
          <Card key={family} title={<>{title}</>}>
            <p className="muted">{lead}</p>
            <ul className="requests">
              {items.map(c => <Connector key={c.id} connector={c} user={user} canManage={canManage} onChanged={changed} />)}
            </ul>
          </Card>
        )
      })}
    </>
  )
}

/* ── The page ──────────────────────────────────────────────────────── */

function SetupBody({ user }) {
  const canManage = user.permissions.includes('connectors.manage')
  const { data, error, loading, updatedAt, refresh } = usePolledJson(
    `${API_BASE_URL}/api/onboarding`, { intervalMs: 15000 })

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 id="page-title" tabIndex={-1} className="page-title">Set-up</h1>
          <p className="page-sub">
            What is connected, what is not, and what is left before the agent can take a first incident.
            {data && (data.complete ? ' Every required step is done.' : ` ${data.total - data.done} required step(s) left.`)}
          </p>
        </div>
      </header>

      <DataNotice error={error} hasData={data !== null} updatedAt={updatedAt} onRetry={refresh} what="the set-up steps" />
      {loading && !data ? <span className="skel skel--block" aria-hidden="true" /> : data && <Checklist report={data} />}
      {!canManage && <p className="muted">You can read this page. Connecting and changing things needs the tenant administrator role.</p>}
      <Runners user={user} canManage={canManage} />
      <Connectors user={user} canManage={canManage} onChanged={refresh} />
    </div>
  )
}

export default function Setup() {
  const { user } = useSession()
  if (user.permissions.includes('system.read')) return <SetupBody user={user} />
  return (
    <div className="page">
      <header className="page-header">
        <h1 id="page-title" tabIndex={-1} className="page-title">Set-up</h1>
      </header>
      <div className="state">
        <Icon name="server" />
        <p className="state__title">Set-up is for operators and administrators</p>
        <p className="state__body">Your role does not include reading the system&rsquo;s configuration.</p>
      </div>
    </div>
  )
}
