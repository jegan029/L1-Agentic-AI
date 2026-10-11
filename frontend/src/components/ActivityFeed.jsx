import { memo } from 'react'
import { useEvents, useConnection } from '../context/AgentContext'
import { incidentHref } from '../hooks/useHashRoute'
import { eventText } from '../utils/eventText'
import RelativeTime from './RelativeTime'
import Icon from './Icon'

const META = {
  incident_received:  { label: 'Received',     icon: 'inbox',          tone: 'received' },
  sop_matched:        { label: 'SOP matched',  icon: 'book',           tone: 'matched' },
  step_executing:     { label: 'Step running', icon: 'play-circle',    tone: 'running' },
  step_done:          { label: 'Step done',    icon: 'check-circle',   tone: 'done' },
  incident_resolved:  { label: 'Resolved',     icon: 'check-circle',   tone: 'resolved' },
  incident_escalated: { label: 'Escalated',    icon: 'arrow-up-right', tone: 'escalated' },
}

/** eventText always begins with the incident number, so it can be swapped for a link. */
function Message({ evt }) {
  const text = eventText(evt)
  const n = evt.incident_number
  if (n && text.startsWith(n)) {
    return <><a href={incidentHref(n)}>{n}</a>{text.slice(n.length)}</>
  }
  return text
}

const EventRow = memo(function EventRow({ evt }) {
  const meta = META[evt.type] || { label: String(evt.type).replace(/_/g, ' '), icon: 'zap', tone: 'running' }
  const fresh = Date.now() - evt._at < 2000
  return (
    <li className={`event event--${meta.tone}${fresh ? ' event--new' : ''}`}>
      <div className="event__type"><Icon name={meta.icon} />{meta.label}</div>
      <div className="event__msg"><Message evt={evt} /></div>
      <div className="event__time"><RelativeTime ts={evt.ts} /></div>
    </li>
  )
})

export default function ActivityFeed() {
  const { events, clear } = useEvents()
  const { isConnected } = useConnection()

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 id="page-title" tabIndex={-1} className="page-title">Activity feed</h1>
          <p className="page-sub">
            Every step the agent takes, as it happens. The feed holds the latest 50 events and
            clears when you reload the page.
          </p>
        </div>
        <div className="toolbar">
          <span className={`badge ${isConnected ? 'badge--resolved' : 'badge--escalated'}`}>
            {isConnected ? 'Live' : 'Reconnecting'}
          </span>
          {events.length > 0 && (
            <button type="button" className="btn" onClick={clear}>Clear feed</button>
          )}
        </div>
      </header>

      <section className="events" aria-label="Agent activity">
        {events.length === 0 ? (
          <div className="state">
            <Icon name={isConnected ? 'zap' : 'wifi-off'} />
            <p className="state__title">
              {isConnected ? 'Waiting for agent activity' : 'Not connected to the live feed'}
            </p>
            <p className="state__body">
              {isConnected
                ? 'Events appear here the moment the agent receives or works on an incident.'
                : 'Reconnecting automatically. Events that happen while disconnected are not replayed; see the incident feed for history.'}
            </p>
          </div>
        ) : (
          <>
            <div className="events__head" aria-hidden="true">
              <span>Event</span><span>Details</span><span style={{ textAlign: 'right' }}>When</span>
            </div>
            {/* aria-live stays off: announcing every step would bury the escalations that matter */}
            <ol aria-live="off">
              {events.map(evt => <EventRow key={evt._id} evt={evt} />)}
            </ol>
          </>
        )}
      </section>
    </div>
  )
}
