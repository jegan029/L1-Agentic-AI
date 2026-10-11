import { brand } from '../brand'
import { useConnection } from '../context/AgentContext'
import Icon from './Icon'

export const NAV_ITEMS = [
  { key: 'dashboard', label: 'Live dashboard',  short: 'Dashboard',  icon: 'activity',   href: '#/dashboard' },
  { key: 'incidents', label: 'Incident feed',   short: 'Incidents',  icon: 'clock',      href: '#/incidents' },
  { key: 'sops',      label: 'SOP performance', short: 'SOPs',       icon: 'list-check', href: '#/sops' },
  { key: 'activity',  label: 'Activity feed',   short: 'Activity',   icon: 'zap',        href: '#/activity' },
]

function ConnectionStatus() {
  const { isConnected } = useConnection()
  return (
    <span className="utilbar__status" role="status">
      <span className={`dot${isConnected ? '' : ' dot--off'}`} aria-hidden="true" />
      {isConnected ? 'Live feed connected' : 'Reconnecting to live feed…'}
    </span>
  )
}

export default function NavBar({ activePage }) {
  return (
    <>
      <div className="utilbar">
        <span className="utilbar__org">{brand.orgBanner || brand.product}</span>
        <ConnectionStatus />
      </div>

      <header className="masthead">
        <div className="masthead__row">
          <div className="brand">
            {brand.logo && <img className="brand__logo" src={brand.logo} alt={brand.org} />}
            <span className="brand__name">{brand.product}</span>
          </div>

          <nav className="nav" aria-label="Primary">
            {NAV_ITEMS.map(item => (
              <a
                key={item.key}
                href={item.href}
                className="nav__item"
                aria-label={item.label}
                aria-current={activePage === item.key ? 'page' : undefined}
              >
                <Icon name={item.icon} />
                <span className="nav__label nav__label--full" aria-hidden="true">{item.label}</span>
                <span className="nav__label nav__label--short" aria-hidden="true">{item.short}</span>
              </a>
            ))}
          </nav>
        </div>
      </header>
    </>
  )
}
