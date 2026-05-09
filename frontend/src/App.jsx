import { useState } from 'react'
import { AgentProvider, useAgent } from './context/AgentContext'
import NavBar from './components/NavBar'
import LiveDashboard from './components/LiveDashboard'
import IncidentFeed from './components/IncidentFeed'
import SopPerformance from './components/SopPerformance'
import ActivityFeed from './components/ActivityFeed'

function Shell() {
  const [page, setPage] = useState('dashboard')
  const { stream: { isConnected } } = useAgent()

  return (
    <div style={{ minHeight: '100vh', background: 'var(--bg-1)' }}>
      <NavBar activePage={page} onNavigate={setPage} isConnected={isConnected} />
      <main style={{ maxWidth: 1400, margin: '0 auto' }}>
        {page === 'dashboard' && <LiveDashboard />}
        {page === 'incidents' && <IncidentFeed />}
        {page === 'sops'      && <SopPerformance />}
        {page === 'activity'  && <ActivityFeed />}
      </main>

      {/* Footer — mirrors State Street footer style */}
      <footer style={{
        marginTop: 40,
        borderTop: '1px solid var(--border-subtle)',
        background: '#fff',
        padding: '16px 40px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
      }}>
        <img src="/state-street-logo-final.svg" alt="State Street" style={{ height: 22, width: 'auto', display: 'block', opacity: 0.75 }} />
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          L1 Engineer Agent &middot; Internal Operations Platform
        </span>
      </footer>
    </div>
  )
}

export default function App() {
  return (
    <AgentProvider>
      <Shell />
    </AgentProvider>
  )
}
