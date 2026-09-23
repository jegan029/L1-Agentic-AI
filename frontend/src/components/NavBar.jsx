const NAV_ITEMS = [  { key: 'dashboard', label: 'Live Dashboard',  icon: 'monitoring' },  { key: 'incidents', label: 'Incident Feed',    icon: 'history' },  { key: 'sops',      label: 'SOP Performance',  icon: 'checklist' },  { key: 'activity',  label: 'Activity Feed',    icon: 'bolt' },]

export default function NavBar({ activePage, onNavigate, isConnected, isDark = false, onToggleDark }) {
  const bg             = isDark ? '#0d1117' : '#fff'
  const border         = isDark ? '#30363d' : '#dde0ec'
  const textSecondary  = isDark ? 'rgba(230,237,243,0.6)' : 'var(--text-secondary)'

  return (
    <header style={{ background: bg, borderBottom: `1px solid ${border}`, position: 'sticky', top: 0, zIndex: 100 }}>
      <style>{`
        @keyframes nv-pulse {
          0%,100% { box-shadow: 0 0 0 0 rgba(118,185,0,0.5); }
          50%      { box-shadow: 0 0 0 6px rgba(118,185,0,0); }
        }
        @keyframes conn-blink {
          0%,100% { opacity: 1; }
          50%      { opacity: 0.4; }
        }
      `}</style>

      {/* ── Top utility bar ── */}
      <div style={{
        background: 'var(--ss-navy)', padding: '0 40px', height: 36,
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <span style={{ fontSize: 11, color: 'rgba(255,255,255,0.6)', letterSpacing: '0.04em' }}>
          STATE STREET CORPORATION · INTERNAL OPERATIONS
        </span>

        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          {/* NVIDIA NIM Active Badge */}
          <div style={{
            display: 'flex', alignItems: 'center', gap: 7,
            padding: '3px 10px',
            background: 'rgba(118,185,0,0.1)',
            border: '1px solid rgba(118,185,0,0.3)',
            borderRadius: 3,
          }}>
            <span style={{
              width: 7, height: 7, borderRadius: '50%',
              background: '#76b900', display: 'inline-block',
              animation: 'nv-pulse 2.5s ease-in-out infinite',
            }} />
            <span style={{ fontSize: 10, color: '#76b900', fontWeight: 700, letterSpacing: '0.07em' }}>
              NVIDIA NIM
            </span>
            <span style={{ width: 1, height: 12, background: 'rgba(118,185,0,0.3)' }} />
            <span style={{ fontSize: 10, color: 'rgba(118,185,0,0.75)', fontFamily: 'monospace' }}>
              meta/llama-3.2-11b
            </span>
          </div>

          {/* Connection dot */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
            <span style={{
              width: 7, height: 7, borderRadius: '50%', display: 'inline-block',
              background: isConnected ? '#00c853' : '#ff6d00',
              animation: isConnected ? 'conn-blink 2s ease-in-out infinite' : 'none',
            }} />
            <span style={{ color: 'rgba(255,255,255,0.6)' }}>
              {isConnected ? 'Live feed connected' : 'Reconnecting…'}
            </span>
          </div>
        </div>
      </div>

      {/* ── Main nav row ── */}
      <div style={{
        padding: '0 40px', height: 64,
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        {/* Logo + wordmark */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
          <img
            src="/state-street-logo-final.svg"
            alt="State Street"
            style={{ height: 36, width: 'auto', display: 'block', filter: isDark ? 'brightness(0) invert(1)' : 'none' }}
          />
          <div style={{ borderLeft: `1px solid ${border}`, paddingLeft: 18 }}>
            <div style={{ fontSize: 11, color: 'var(--ss-blue)', fontWeight: 600, letterSpacing: '0.06em' }}>
              NEXUS — Detect. Decide. Resolve. Autonomously
            </div>
          </div>
        </div>

        {/* Nav links */}
        <nav style={{ display: 'flex', alignItems: 'center', height: '100%' }}>
          {NAV_ITEMS.map(item => {
            const active = activePage === item.key
            return (
              <button
                key={item.key}
                onClick={() => onNavigate(item.key)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 6,
                  padding: '0 18px', height: 64, fontSize: 13,
                  fontWeight: active ? 600 : 400,
                  color: active ? 'var(--ss-blue)' : textSecondary,
                  borderBottom: active ? '3px solid var(--ss-blue)' : '3px solid transparent',
                  borderTop: '3px solid transparent',
                  background: 'none', cursor: 'pointer',
                  transition: 'color 0.15s, border-color 0.15s',
                  whiteSpace: 'nowrap',
                }}
                onMouseEnter={e => { if (!active) e.currentTarget.style.color = 'var(--ss-blue)' }}
                onMouseLeave={e => { if (!active) e.currentTarget.style.color = textSecondary }}
              >
                <span className="material-symbols-rounded" style={{ fontSize: 16 }}>{item.icon}</span>
                {item.label}
              </button>
            )
          })}
        </nav>

        {/* Right controls */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {/* Dark mode toggle */}
          {onToggleDark && (
            <button
              onClick={onToggleDark}
              title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
              style={{
                width: 36, height: 36, borderRadius: '50%',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                background: isDark ? '#21262d' : 'var(--bg-1)',
                border: `1px solid ${border}`,
                cursor: 'pointer',
                color: isDark ? '#e6edf3' : 'var(--text-secondary)',
                transition: 'background 0.2s, color 0.2s',
              }}
            >
              <span className="material-symbols-rounded" style={{ fontSize: 18 }}>
                {isDark ? 'light_mode' : 'dark_mode'}
              </span>
            </button>
          )}

          {/* Agent status chip */}
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8, padding: '6px 14px',
            background: isDark ? 'rgba(33,85,165,0.12)' : 'var(--ss-light-blue)',
            border: `1px solid ${isDark ? 'rgba(33,85,165,0.25)' : 'var(--ss-mid-blue)'}`,
            borderRadius: 3, fontSize: 12,
          }}>
            <span className="material-symbols-rounded" style={{ fontSize: 16, color: 'var(--ss-blue)' }}>smart_toy</span>
            <span style={{ color: isDark ? '#e6edf3' : 'var(--ss-navy)', fontWeight: 500 }}>Support Engineer</span>
            <span style={{
              width: 6, height: 6, borderRadius: '50%',
              background: isConnected ? '#00875A' : '#C35109', marginLeft: 2,
            }} />
          </div>
        </div>
      </div>
    </header>
  )
}
