import { useCallback, useEffect, useState } from 'react'
import { API_BASE_URL } from '../config'
import { useSession } from '../context/SessionContext'
import { navigate, versionHref } from '../hooks/useHashRoute'
import { send } from '../utils/api'
import Icon from './Icon'
import { SopStatus, who } from './SopStatus'

const TEMPLATE = {
  sop_id: '',
  title: '',
  keywords: [],
  applicable_categories: [],
  applicable_services: [],
  applicable_assignment_groups: [],
  parameters: [],
  steps: [],
}

const pretty = value => JSON.stringify(value, null, 2)

function Card({ title, children }) {
  return (
    <section className="card">
      <h2 className="card__title">{title}</h2>
      <div className="card__body">{children}</div>
    </section>
  )
}

function Problems({ title, items }) {
  if (!items?.length) return null
  return (
    <div className="notice notice--error" role="alert">
      <Icon name="alert-triangle" />
      <div className="notice__text">
        <strong>{title}</strong>
        <ul className="plain-list">{items.map((p, i) => <li key={i}>{p}</li>)}</ul>
      </div>
    </div>
  )
}

function Editor({ value, onChange, onSave, saving, label }) {
  let parseError = ''
  try { JSON.parse(value) } catch (e) { parseError = e.message }
  return (
    <Card title={<><label htmlFor="sop-json">SOP definition (JSON)</label></>}>
      <textarea
        id="sop-json" className="field code-editor" spellCheck={false} value={value}
        onChange={e => onChange(e.target.value)} aria-describedby="sop-json-state"
      />
      <div className="actions">
        <button type="button" className="btn btn--primary" disabled={saving || !!parseError} onClick={onSave}>
          {label}
        </button>
        <span id="sop-json-state" className="muted" role="status">
          {parseError ? `Not valid JSON: ${parseError}` : 'Valid JSON'}
        </span>
      </div>
    </Card>
  )
}

function NewDraft() {
  const { user } = useSession()
  const [text, setText] = useState(pretty(TEMPLATE))
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState(null)

  const create = async () => {
    setBusy(true)
    const res = await send(user, 'POST', '/api/sops', { content: JSON.parse(text) })
    setBusy(false)
    if (res.ok) navigate(versionHref(res.data.id))
    else setFailure(res)
  }

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <a className="back-link" href="#/library"><Icon name="arrow-left" />SOP library</a>
          <h1 id="page-title" tabIndex={-1} className="page-title">New draft</h1>
          <p className="page-sub">
            A draft never runs. Save it, check the validation results, then submit it for review.
          </p>
        </div>
      </header>
      {failure && <Problems title={failure.error} items={failure.problems.length ? failure.problems : ['Nothing was saved.']} />}
      <Editor value={text} onChange={setText} onSave={create} saving={busy} label="Save draft" />
    </div>
  )
}

function Steps({ steps }) {
  if (!steps.length) return <p className="muted">This draft has no steps yet. A person has to write them.</p>
  return (
    <div className="table-scroll" tabIndex={0} role="region" aria-label="Steps">
      <table className="dtable">
        <thead>
          <tr>
            <th scope="col">Step</th><th scope="col">Type</th><th scope="col">What it does</th>
            <th scope="col">Then</th><th scope="col">On failure</th>
          </tr>
        </thead>
        <tbody>
          {steps.map(s => (
            <tr key={s.step_id}>
              <td className="mono">{s.step_id}</td>
              <td><span className="chip">{s.capability || s.step_type}</span></td>
              <td>{s.description || '—'}</td>
              <td className="mono">{s.on_success || '—'}</td>
              <td className="mono">{s.on_failure || (s.on_success ? 'escalate' : '—')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Capabilities({ items }) {
  if (!items.length) return <p className="muted">This version calls no connector.</p>
  return (
    <div className="table-scroll" tabIndex={0} role="region" aria-label="Capabilities used">
      <table className="dtable">
        <thead>
          <tr>
            <th scope="col">Step</th><th scope="col">Capability</th><th scope="col">Connector</th>
            <th scope="col">Kind</th><th scope="col">Risk tier</th>
          </tr>
        </thead>
        <tbody>
          {items.map((c, i) => (
            <tr key={`${c.step_id}-${i}`}>
              <td className="mono">{c.step_id}</td>
              <td className="mono">{c.capability}</td>
              <td>{c.known ? c.connector : 'Unknown capability'}{c.preview && <span className="chip chip--inline">preview</span>}</td>
              <td>{c.kind || '—'}</td>
              <td>{c.tier || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Changes({ diff }) {
  if (diff.first_version) return <p className="muted">This SOP has no published version. Everything here is new.</p>
  if (!diff.fields.length && !diff.steps.length) return <p className="muted">Identical to the published version.</p>
  return (
    <ul className="plain-list">
      {diff.fields.map(f => <li key={f}>Field <span className="mono">{f}</span> changed</li>)}
      {diff.steps.map(s => <li key={s.step_id}>Step <span className="mono">{s.step_id}</span> {s.change}</li>)}
    </ul>
  )
}

export default function SopReview({ versionId }) {
  const { user } = useSession()
  const [state, setState] = useState({ loading: true, data: null, error: null })
  const [text, setText] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState(null)
  const isNew = versionId === 'new'

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/sop-versions/${encodeURIComponent(versionId)}`, {
        headers: { Accept: 'application/json' },
      })
      if (!res.ok) throw new Error(res.status === 404 || res.status === 422 ? 'This SOP version does not exist.' : `The agent API returned HTTP ${res.status}`)
      const data = await res.json()
      setState({ loading: false, data, error: null })
      setText(pretty(data.content))
    } catch (e) {
      setState({ loading: false, data: null, error: e instanceof TypeError ? "Can't reach the agent API" : e.message })
    }
  }, [versionId])

  useEffect(() => { if (!isNew) load() }, [isNew, load])

  if (isNew) return <NewDraft />

  const act = async (method, suffix, body, after) => {
    setBusy(true)
    setFailure(null)
    const res = await send(user, method, `/api/sop-versions/${encodeURIComponent(versionId)}${suffix}`, body)
    setBusy(false)
    if (!res.ok) { setFailure(res); return }
    if (after) after()
    else load()
  }

  const v = state.data
  const can = p => user.permissions.includes(p)
  const mine = v && v.author === `user:${user.id}`

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <a className="back-link" href="#/library"><Icon name="arrow-left" />SOP library</a>
          <h1 id="page-title" tabIndex={-1} className="page-title">
            {v ? (v.title || v.sop_id) : 'SOP version'}
          </h1>
          {v && (
            <p className="page-sub">
              <span className="mono">{v.sop_id}</span>
              {v.version != null && ` · version ${v.version}`} · written by {who(v.author)}
              {v.approver && ` · approved by ${who(v.approver)}`}
            </p>
          )}
        </div>
        {v && <SopStatus status={v.status} />}
      </header>

      {state.loading && <span className="skel skel--block" aria-hidden="true" />}
      {state.error && (
        <div className="notice notice--error" role="alert">
          <Icon name="alert-triangle" />
          <div className="notice__text"><strong>Couldn&rsquo;t load this version</strong>{state.error}</div>
          <button type="button" className="btn" onClick={load}>Try again</button>
        </div>
      )}
      {failure && <Problems title={failure.error} items={failure.problems.length ? failure.problems : ['Nothing was changed.']} />}

      {v && (
        <>
          {v.origin === 'model_drafted' && (
            <div className="notice notice--warn" role="status">
              <Icon name="alert-triangle" />
              <div className="notice__text">
                <strong>The steps were drafted by a language model</strong>
                They were written from the article shown below and have passed the automatic
                checks only. Read every step against the article. Whoever submits this version
                is its author of record.
              </div>
            </div>
          )}
          {v.review_note && v.status === 'draft' && (
            <div className="notice notice--warn" role="status">
              <Icon name="alert-triangle" />
              <div className="notice__text"><strong>Changes requested by the reviewer</strong>{v.review_note}</div>
            </div>
          )}
          <Problems title="This version is not valid and cannot be submitted" items={v.problems} />

          <Card title={<>What changed from the published version</>}>
            <Changes diff={v.diff} />
          </Card>

          <Card title={<>Connector capabilities this version calls</>}>
            <Capabilities items={v.capabilities} />
          </Card>

          <Card title={<>Steps</>}>
            <Steps steps={v.content.steps || []} />
          </Card>

          {v.source.id && (
            <Card title={<>Source article</>}>
              <p className="muted">
                From <span className="mono">{v.source.id}</span>, article{' '}
                <span className="mono">{v.source.article}</span>, revision{' '}
                <span className="mono">{v.source.revision}</span>. Shown as plain text.
              </p>
              {/* Article text is untrusted. It is rendered as text, never as markup. */}
              <pre className="source-text">{v.source_text || '(empty)'}</pre>
            </Card>
          )}

          {v.status === 'draft' && can('sops.author') ? (
            <Editor
              value={text} onChange={setText} saving={busy} label="Save draft"
              onSave={() => act('PUT', '', { content: JSON.parse(text) })}
            />
          ) : (
            <Card title={<>SOP definition (JSON)</>}>
              <pre className="source-text" tabIndex={0}>{pretty(v.content)}</pre>
              <p className="muted">Content hash <span className="mono">{v.content_hash || v.current_hash}</span></p>
            </Card>
          )}

          <Card title={<>Actions</>}>
            <div className="actions">
              {v.status === 'draft' && can('sops.author') && (
                <>
                  <button type="button" className="btn btn--primary" disabled={busy || v.problems.length > 0}
                    onClick={() => act('POST', '/submit')}>Submit for review</button>
                  <button type="button" className="btn" disabled={busy}
                    onClick={() => act('DELETE', '', undefined, () => navigate('#/library'))}>Discard draft</button>
                </>
              )}
              {v.status === 'in_review' && can('sops.approve') && (
                <>
                  <button type="button" className="btn btn--primary" disabled={busy || mine}
                    onClick={() => act('POST', '/approve', { content_hash: v.content_hash })}>Approve</button>
                  <span className="field-group actions__note">
                    <label className="field-label" htmlFor="review-note">What needs to change</label>
                    <input id="review-note" className="field" value={note} onChange={e => setNote(e.target.value)} maxLength={2000} />
                  </span>
                  <button type="button" className="btn" disabled={busy || !note.trim()}
                    onClick={() => act('POST', '/request-changes', { note: note.trim() })}>Request changes</button>
                </>
              )}
              {v.status === 'approved' && can('sops.approve') && (
                <button type="button" className="btn btn--primary" disabled={busy}
                  onClick={() => act('POST', '/publish')}>Publish</button>
              )}
              {v.status === 'published' && can('sops.approve') && (
                <button type="button" className="btn" disabled={busy}
                  onClick={async () => {
                    setBusy(true)
                    const res = await send(user, 'POST', `/api/sops/${encodeURIComponent(v.sop_id)}/retire`)
                    setBusy(false)
                    if (res.ok) load(); else setFailure(res)
                  }}>Retire</button>
              )}
            </div>
            {v.status === 'in_review' && can('sops.approve') && mine && (
              <p className="muted">You wrote this version, so someone else has to approve it.</p>
            )}
            {v.status === 'in_review' && !can('sops.approve') && (
              <p className="muted">Waiting for someone with the SOP approver role.</p>
            )}
            {['superseded', 'retired'].includes(v.status) && (
              <p className="muted">This version is no longer run and cannot be changed.</p>
            )}
          </Card>
        </>
      )}
    </div>
  )
}
