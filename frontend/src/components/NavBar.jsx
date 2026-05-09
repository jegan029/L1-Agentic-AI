const NAV_ITEMS = [
  { key: 'dashboard', label: 'Live Dashboard',  icon: 'monitoring' },
  { key: 'incidents', label: 'Incident Feed',    icon: 'history' },
  { key: 'sops',      label: 'SOP Performance',  icon: 'checklist' },
  { key: 'activity',  label: 'Activity Feed',    icon: 'bolt' },
]

export default function NavBar({ activePage, onNavigate, isConnected }) {
  return (
    <header style={{ background: '#fff', borderBottom: '1px solid #dde0ec', position: 'sticky', top: 0, zIndex: 100 }}>

      {/* Top utility bar — mirrors State Street's CLR bar */}
      <div style={{
        background: 'var(--ss-navy)',
        padding: '0 40px',
        height: 36,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
      }}>
        <span style={{ fontSize: 11, color: 'rgba(255,255,255,0.65)', letterSpacing: '0.04em' }}>
          STATE STREET CORPORATION  ·  INTERNAL OPERATIONS
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
          <span style={{
            width: 7, height: 7, borderRadius: '50%', display: 'inline-block',
            background: isConnected ? '#00c853' : '#ff6d00',
          }} />
          <span style={{ color: 'rgba(255,255,255,0.65)' }}>
            {isConnected ? 'Live feed connected' : 'Reconnecting…'}
          </span>
        </div>
      </div>

      {/* Main nav row */}
      <div style={{
        padding: '0 40px',
        height: 64,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
      }}>

        {/* Logo block */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
          <img src="/state-street-logo-final.svg" alt="State Street" style={{ height: 36, width: 'auto', display: 'block' }} />
          <div style={{ borderLeft: '1px solid #dde0ec', paddingLeft: 18 }}>
            <div style={{ fontSize: 11, color: 'var(--ss-blue)', fontWeight: 600, letterSpacing: '0.06em' }}>
              L1 ENGINEER AGENT
            </div>
          </div>
        </div>

        {/* Navigation links */}
        <nav style={{ display: 'flex', alignItems: 'center', height: '100%' }}>
          {NAV_ITEMS.map(item => {
            const active = activePage === item.key
            return (
              <button
                key={item.key}
                onClick={() => onNavigate(item.key)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 6,
                  padding: '0 18px',
                  height: 64,
                  fontSize: 13,
                  fontWeight: active ? 600 : 400,
                  color: active ? 'var(--ss-blue)' : 'var(--text-secondary)',
                  borderBottom: active ? '3px solid var(--ss-blue)' : '3px solid transparent',
                  borderTop: '3px solid transparent',
                  background: 'none',
                  transition: 'color 0.15s, border-color 0.15s',
                  whiteSpace: 'nowrap',
                }}
                onMouseEnter={e => { if (!active) e.currentTarget.style.color = 'var(--ss-blue)' }}
                onMouseLeave={e => { if (!active) e.currentTarget.style.color = 'var(--text-secondary)' }}
              >
                <span className="material-symbols-rounded" style={{ fontSize: 16 }}>{item.icon}</span>
                {item.label}
              </button>
            )
          })}
        </nav>

        {/* Right side — agent status chip */}
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '6px 14px',
          background: 'var(--ss-light-blue)',
          border: '1px solid var(--ss-mid-blue)',
          borderRadius: 3,
          fontSize: 12,
        }}>
          <span className="material-symbols-rounded" style={{ fontSize: 16, color: 'var(--ss-blue)' }}>smart_toy</span>
          <span style={{ color: 'var(--ss-navy)', fontWeight: 500 }}>L1 Engineer</span>
          <span style={{
            width: 6, height: 6, borderRadius: '50%',
            background: isConnected ? '#00875A' : '#C35109',
            marginLeft: 2,
          }} />
        </div>
      </div>
    </header>
  )
}
