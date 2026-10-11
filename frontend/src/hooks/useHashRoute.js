import { useMemo, useSyncExternalStore } from 'react'

export const PAGES = ['dashboard', 'incidents', 'sops', 'activity']

function subscribe(cb) {
  window.addEventListener('hashchange', cb)
  return () => window.removeEventListener('hashchange', cb)
}
const getHash = () => window.location.hash

/** Routes: #/dashboard  #/incidents  #/incidents/INC0070001  #/sops  #/activity */
export function parseHash(hash) {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean)
  const page = PAGES.includes(parts[0]) ? parts[0] : 'dashboard'
  let incident = null
  if (page === 'incidents' && parts[1]) {
    try { incident = decodeURIComponent(parts[1]) } catch { incident = parts[1] }
  }
  return { page, incident }
}

export function useHashRoute() {
  const hash = useSyncExternalStore(subscribe, getHash, () => '')
  return useMemo(() => parseHash(hash), [hash])
}

export function navigate(hash) {
  if (window.location.hash !== hash) window.location.hash = hash
}

export const incidentHref = number => `#/incidents/${encodeURIComponent(number)}`
