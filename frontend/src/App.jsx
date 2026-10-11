import { useEffect, useRef } from 'react'
import { AgentProvider, useEvents } from './context/AgentContext'
import { SessionProvider, useSession } from './context/SessionContext'
import { useHashRoute } from './hooks/useHashRoute'
import { eventText } from './utils/eventText'
import { brand } from './brand'
import NavBar, { NAV_ITEMS } from './components/NavBar'
import LiveDashboard from './components/LiveDashboard'
import IncidentFeed from './components/IncidentFeed'
import SopLibrary from './components/SopLibrary'
import Actions from './components/Actions'
import SopPerformance from './components/SopPerformance'
import ActivityFeed from './components/ActivityFeed'
import SignIn from './components/SignIn'

const PAGE_TITLES = Object.fromEntries(NAV_ITEMS.map(i => [i.key, i.label]))

/** Announces new escalations only, from any page. Routine steps stay silent. */
function EscalationAnnouncer() {
  const { events } = useEvents()
  const latest = events.find(e => e.type === 'incident_escalated')
  return (
    <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {latest ? `Escalation: ${eventText(latest)}` : ''}
    </div>
  )
}

function Shell() {
  const { page, incident, version } = useHashRoute()
  const lastPage = useRef(page)

  useEffect(() => {
    const label = PAGE_TITLES[page]
    document.title = `${incident ? `${incident} · ` : ''}${label} · ${brand.product}`
  }, [page, incident])

  // Route changes are not page loads: move focus to the new page's heading so keyboard and
  // screen-reader users land on the new content instead of the link they just activated.
  useEffect(() => {
    if (lastPage.current === page) return
    lastPage.current = page
    document.getElementById('page-title')?.focus({ preventScroll: true })
    window.scrollTo(0, 0)
  }, [page])

  return (
    <div className="app">
      <a className="skip-link" href="#main" onClick={e => {
        e.preventDefault()
        document.getElementById('main')?.focus()
      }}>
        Skip to main content
      </a>
      <NavBar activePage={page} />
      <main id="main" className="main" tabIndex={-1}>
        {page === 'dashboard' && <LiveDashboard />}
        {page === 'incidents' && <IncidentFeed />}
        {page === 'actions' && <Actions />}
        {page === 'library' && <SopLibrary version={version} />}
        {page === 'sops' && <SopPerformance />}
        {page === 'activity' && <ActivityFeed />}
      </main>

      <footer className="footer">
        {brand.logo && <img src={brand.logo} alt="" />}
        <span>{brand.footer}</span>
      </footer>
      <EscalationAnnouncer />
    </div>
  )
}

/** Nothing that calls the API is mounted until someone is signed in. */
function Gate() {
  const { status } = useSession()
  if (status !== 'signed-in') return <SignIn />
  return (
    <AgentProvider>
      <Shell />
    </AgentProvider>
  )
}

export default function App() {
  return (
    <SessionProvider>
      <Gate />
    </SessionProvider>
  )
}
