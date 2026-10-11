import { useMemo, useSyncExternalStore } from 'react'

export const PAGES = ['dashboard', 'incidents', 'actions', 'library', 'sops', 'activity']

function subscribe(cb) {
  window.addEventListener('hashchange', cb)
  return () => window.removeEventListener('hashchange', cb)
}
const getHash = () => window.location.hash

function decode(part) {
  try { return decodeURIComponent(part) } catch { return part }
}

/**
 * Routes: #/dashboard  #/incidents  #/incidents/INC0070001  #/library  #/library/<version id>
 * #/library/new  #/actions  #/sops  #/activity
 */
export function parseHash(hash) {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean)
  const page = PAGES.includes(parts[0]) ? parts[0] : 'dashboard'
  const incident = page === 'incidents' && parts[1] ? decode(parts[1]) : null
  const version = page === 'library' && parts[1] ? decode(parts[1]) : null
  return { page, incident, version }
}

export function useHashRoute() {
  const hash = useSyncExternalStore(subscribe, getHash, () => '')
  return useMemo(() => parseHash(hash), [hash])
}

export function navigate(hash) {
  if (window.location.hash !== hash) window.location.hash = hash
}

export const incidentHref = number => `#/incidents/${encodeURIComponent(number)}`
export const versionHref = id => `#/library/${encodeURIComponent(id)}`
