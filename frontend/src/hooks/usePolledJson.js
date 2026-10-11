import { useState, useEffect, useCallback, useRef } from 'react'

function describeError(e) {
  if (e instanceof TypeError) return "Can't reach the agent API"
  return e?.message || 'Unknown error'
}

/**
 * Poll a JSON endpoint. Keeps the last good payload when a refresh fails (so the UI can
 * say "stale" instead of showing zeros), aborts superseded requests, and pauses while the
 * tab is hidden.
 */
export function usePolledJson(url, { intervalMs, select } = {}) {
  const [state, setState] = useState({ data: null, error: null, loading: true, updatedAt: null })
  const controller = useRef(null)
  const selectRef = useRef(select)
  selectRef.current = select

  const load = useCallback(async () => {
    controller.current?.abort()
    const c = new AbortController()
    controller.current = c
    try {
      const res = await fetch(url, { signal: c.signal, headers: { Accept: 'application/json' } })
      if (!res.ok) throw new Error(`The agent API returned HTTP ${res.status}`)
      const json = await res.json()
      const data = selectRef.current ? selectRef.current(json) : json
      setState({ data, error: null, loading: false, updatedAt: Date.now() })
    } catch (e) {
      if (e.name === 'AbortError') return
      setState(s => ({ ...s, error: describeError(e), loading: false }))
    }
  }, [url])

  useEffect(() => {
    load()
    const timer = intervalMs ? setInterval(() => { if (!document.hidden) load() }, intervalMs) : null
    const onVisible = () => { if (!document.hidden) load() }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      if (timer) clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
      controller.current?.abort()
    }
  }, [load, intervalMs])

  return { ...state, refresh: load }
}
