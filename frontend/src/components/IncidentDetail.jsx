import { useState, useEffect, useRef, useCallback, useId } from 'react'
import { fetchIncidentDetail } from '../hooks/useIncidentHistory'
import StatusBadge, { PriorityBadge } from './StatusBadge'
import Icon from './Icon'
import {
  formatDuration, formatTimestamp, stepLabel, stepStatusLabel, stepStatusTone,
} from '../utils/formatters'

const STEP_ICONS = {
  SPLUNK_SEARCH: 'search', MQ_CHECK: 'server', FILE_CHECK: 'folder',
  AUTOSYS_STATUS: 'clock', DYNATRACE_VM_CHECK: 'activity',
  DYNATRACE_METRICS: 'bar-chart', WEB_UI_CHECK: 'globe',
  MAINFRAME_CHECK: 'terminal', DECISION: 'branch', NOTE: 'file-text',
}

/**
 * Modal side drawer on a native <dialog>: the browser supplies the focus trap, Escape,
 * inert background and top-layer stacking. Focus returns to the element that opened it.
 */
export default function IncidentDetail({ incidentNumber, onClose }) {
  const dialogRef = useRef(null)
  const opener = useRef(document.activeElement)
  const uid = useId()
  const [state, setState] = useState({ status: 'loading', record: null, error: null })
  const [expanded, setExpanded] = useState({})

  const load = useCallback(() => {
    const c = new AbortController()
    setState({ status: 'loading', record: null, error: null })
    fetchIncidentDetail(incidentNumber, c.signal)
      .then(record => setState({ status: 'ready', record, error: null }))
      .catch(e => {
        if (e.name === 'AbortError') return
        setState({
          status: e.status === 404 ? 'missing' : 'error',
          record: null,
          error: e instanceof TypeError ? "Can't reach the agent API" : e.message,
        })
      })
    return () => c.abort()
  }, [incidentNumber])

  useEffect(() => load(), [load])

  useEffect(() => {
    const d = dialogRef.current
    if (d && !d.open) d.showModal()
    const el = opener.current
    return () => { if (el && el.isConnected && typeof el.focus === 'function') el.focus() }
  }, [])

  const { status, record, error } = state
  const titleId = `${uid}-title`

  return (
    <dialog
      ref={dialogRef}
      className="drawer"
      aria-labelledby={titleId}
      onClose={onClose}
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="drawer__head on-navy">
        <div style={{ minWidth: 0 }}>
          <div className="drawer__eyebrow">Incident detail</div>
          <h2 id={titleId} className="drawer__title">{incidentNumber}</h2>
          {record && <p className="drawer__desc">{record.short_description}</p>}
        </div>
        <button type="button" className="drawer__close" onClick={onClose} aria-label="Close incident detail">
          <Icon name="close" />
        </button>
      </div>

      {status === 'loading' && (
        <div className="drawer__body" aria-busy="true">
          <span className="skel skel--block" /><span className="skel skel--block" /><span className="skel skel--block" />
        </div>
      )}

      {status === 'missing' && (
        <div className="state" role="status">
          <Icon name="search" />
          <p className="state__title">Incident not found</p>
          <p className="state__body">
            {incidentNumber} isn&rsquo;t in the agent&rsquo;s history. The agent keeps the most recent 1,000 incidents, so older ones age out.
          </p>
        </div>
      )}

      {status === 'error' && (
        <div className="state" role="alert">
          <Icon name="wifi-off" />
          <p className="state__title">Couldn&rsquo;t load this incident</p>
          <p className="state__body">{error}.</p>
          <button type="button" className="btn" onClick={load}>Try again</button>
        </div>
      )}

      {status === 'ready' && record && (
        <div className="drawer__body">
          <div className="chips">
            <StatusBadge outcome={record.outcome} />
            <PriorityBadge value={record.priority} />
            {record.category && <span className="chip">{record.category}</span>}
            <span className="chip">{formatDuration(record.duration_ms)}</span>
          </div>

          {record.sop_id ? (
            <div className="panel">
              <div className="panel__label">SOP executed</div>
              <div className="panel__value">{record.sop_title}</div>
              <div className="panel__sub mono">{record.sop_id}</div>
            </div>
          ) : (
            <div className="panel">
              <div className="panel__label">SOP executed</div>
              <div className="panel__value">None. No SOP was selected for this incident.</div>
            </div>
          )}

          <div className="times">
            <div className="panel">
              <div className="panel__label">Started</div>
              <div className="panel__value">{formatTimestamp(record.started_at)}</div>
            </div>
            <div className="panel">
              <div className="panel__label">Completed</div>
              <div className="panel__value">{formatTimestamp(record.completed_at)}</div>
            </div>
          </div>

          {record.escalation_reason && (
            <div className="reason">
              <Icon name="alert-triangle" />
              <div>
                <strong>Escalation reason</strong>
                <p>{record.escalation_reason}</p>
              </div>
            </div>
          )}

          {record.step_results?.length > 0 ? (
            <section aria-labelledby={`${uid}-trace`}>
              <h3 id={`${uid}-trace`} className="section-label">
                Execution trace &middot; {record.step_results.length} {record.step_results.length === 1 ? 'step' : 'steps'}
              </h3>
              <ol className="steps">
                {record.step_results.map((step, idx) => {
                  const tone = stepStatusTone(step.status)
                  const open = Boolean(expanded[idx])
                  const regionId = `${uid}-step-${idx}`
                  const icon = STEP_ICONS[(step.step_type || '').toUpperCase()] || 'play-circle'
                  return (
                    <li key={idx} className={`step step--${tone}`}>
                      <div className="step__rail">
                        <Icon name={icon} className="step__icon" />
                        {idx < record.step_results.length - 1 && <div className="step__line" />}
                      </div>
                      <div className="step__card">
                        <button
                          type="button" className="step__toggle"
                          aria-expanded={open} aria-controls={regionId}
                          onClick={() => setExpanded(p => ({ ...p, [idx]: !p[idx] }))}
                        >
                          <span className="step__id mono">{step.step_id}</span>
                          <span className="step__summary">
                            <span className="sr-only">{stepLabel(step.step_type)}: </span>
                            {step.output_summary || stepLabel(step.step_type)}
                          </span>
                          <span className="step__status">{stepStatusLabel(step.status)}</span>
                          <span className="step__dur">{formatDuration(step.duration_ms)}</span>
                          <Icon name="chevron-down" className="step__chev" />
                        </button>
                        {open && (
                          <div id={regionId} className="step__evidence mono">
                            <strong>{stepLabel(step.step_type)}</strong>
                            {'\n'}
                            {step.output_summary}
                            {step.evidence_snippet
                              && step.evidence_snippet.trim() !== (step.output_summary || '').trim()
                              ? `\n\n${step.evidence_snippet}`
                              : ''}
                          </div>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ol>
            </section>
          ) : (
            <p className="state__body">No steps were executed for this incident.</p>
          )}
        </div>
      )}
    </dialog>
  )
}
