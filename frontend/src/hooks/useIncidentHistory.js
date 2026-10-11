import { useState, useEffect, useCallback, useRef } from 'react'
import { API_BASE_URL } from '../config'

function describeError(e) {
  if (e instanceof TypeError) return "Can't reach the agent API"
  return e?.message || 'Unknown error'
}

/**
 * Paginated incident list. Keeps the previous page on screen while a new query is in
 * flight (`refreshing`), and drops responses from superseded queries.
 */
export function useIncidentHistory(filters = {}) {
  const { page = 1, perPage = 20, search = '', outcome = '', priority = '' } = filters
  const [state, setState] = useState({ total: 0, items: [], loading: true, refreshing: false, error: null })
  const controller = useRef(null)

  const load = useCallback(async () => {
    controller.current?.abort()
    const c = new AbortController()
    controller.current = c
    setState(s => ({ ...s, refreshing: true }))
    try {
      const params = new URLSearchParams({ page, per_page: perPage })
      if (search) params.set('search', search)
      if (outcome) params.set('outcome', outcome)
      if (priority) params.set('priority', priority)
      const res = await fetch(`${API_BASE_URL}/api/incidents?${params}`, { signal: c.signal })
      if (!res.ok) throw new Error(`The agent API returned HTTP ${res.status}`)
      const json = await res.json()
      setState({ total: json.total, items: json.items, loading: false, refreshing: false, error: null })
    } catch (e) {
      if (e.name === 'AbortError') return
      setState(s => ({ ...s, loading: false, refreshing: false, error: describeError(e) }))
    }
  }, [page, perPage, search, outcome, priority])

  useEffect(() => {
    load()
    return () => controller.current?.abort()
  }, [load])

  return { ...state, refresh: load }
}

export async function fetchIncidentDetail(incidentNumber, signal) {
  const res = await fetch(`${API_BASE_URL}/api/incidents/${encodeURIComponent(incidentNumber)}`, { signal })
  if (!res.ok) {
    const err = new Error(`The agent API returned HTTP ${res.status}`)
    err.status = res.status
    throw err
  }
  return res.json()
}
