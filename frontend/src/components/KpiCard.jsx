export default function KpiCard({ title, value, subtitle, icon, accentColor = 'var(--ss-blue)' }) {
  return (
    <div style={{
      background: '#fff',
      border: '1px solid var(--border-subtle)',
      borderTop: `3px solid ${accentColor}`,
      borderRadius: 'var(--radius-md)',
      padding: '20px 24px',
      display: 'flex',
      flexDirection: 'column',
      gap: 10,
      boxShadow: 'var(--shadow-card)',
      animation: 'fadeUp 0.35s ease both',
      flex: 1,
      minWidth: 160,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span className="material-symbols-rounded" style={{ fontSize: 18, color: accentColor }}>{icon}</span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 500, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{title}</span>
      </div>
      <div style={{ fontSize: 34, fontWeight: 700, color: 'var(--ss-navy)', letterSpacing: '-0.02em', lineHeight: 1 }}>
        {value ?? '—'}
      </div>
      {subtitle && (
        <div style={{ fontSize: 12, color: 'var(--text-muted)', borderTop: '1px solid var(--border-subtle)', paddingTop: 8 }}>
          {subtitle}
        </div>
      )}
    </div>
  )
}
