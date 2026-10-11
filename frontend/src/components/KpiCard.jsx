import Icon from './Icon'

/**
 * value === null while loading renders a skeleton; value === undefined after a failed load
 * renders an em dash. A missing value is never shown as 0.
 */
export default function KpiCard({
  label, value, note, icon, loading = false, tone, active = false, href, onClick,
}) {
  const cls = ['kpi', tone && `kpi--${tone}`, active && 'is-active'].filter(Boolean).join(' ')
  const display =
    value === null || value === undefined
      ? (loading ? <span className="skel" aria-label="Loading" /> : '—')
      : value

  const body = (
    <>
      <div className="kpi__label"><Icon name={icon} />{label}</div>
      <div className="kpi__value">{display}</div>
      {note && (
        <div className="kpi__note">
          {note}
          {href && <Icon name="arrow-up-right" />}
        </div>
      )}
    </>
  )

  return href ? (
    <a className={cls} href={href} onClick={onClick}>{body}</a>
  ) : (
    <div className={cls}>{body}</div>
  )
}
