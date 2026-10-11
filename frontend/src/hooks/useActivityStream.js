import { useState, useEffect, useReducer, useCallback } from 'react'
import { API_BASE_URL } from '../config'

const MAX_EVENTS = 50
const HEARTBEAT_TIMEOUT_MS = 60000 // server heartbeats every 25s
const RETRY_BASE_MS = 3000
const RETRY_MAX_MS = 15000

function reducer(state, action) {
  switch (action.type) {
    case 'ADD_EVENT':
      return [action.event, ...state].slice(0, MAX_EVENTS)
    case 'CLEAR':
      return []
    default:
      return state
  }
}

export function useActivityStream() {
  const [events, dispatch] = useReducer(reducer, [])
  const [isConnected, setIsConnected] = useState(false)

  useEffect(() => {
    let es = null
    let retryTimer = null
    let watchdog = null
    let attempt = 0
    let seq = 0
    let lastBeat = Date.now()
    let disposed = false

    const scheduleRetry = () => {
      if (disposed) return
      const delay = Math.min(RETRY_BASE_MS * 2 ** attempt, RETRY_MAX_MS)
      attempt += 1
      retryTimer = setTimeout(connect, delay)
    }

    const connect = () => {
      es = new EventSource(`${API_BASE_URL}/api/stream`)
      lastBeat = Date.now()

      es.onopen = () => {
        attempt = 0
        lastBeat = Date.now()
        setIsConnected(true)
      }

      es.onmessage = e => {
        lastBeat = Date.now()
        try {
          const evt = JSON.parse(e.data)
          if (evt.type === 'heartbeat') return
          // Stable key + arrival time (server `ts` is event time, not arrival time)
          dispatch({ type: 'ADD_EVENT', event: { ...evt, _id: `${lastBeat}-${seq++}`, _at: lastBeat } })
        } catch {
          // ignore malformed frames
        }
      }

      es.onerror = () => {
        setIsConnected(false)
        es?.close()
        scheduleRetry()
      }
    }

    // A half-open connection never fires onerror; a missed heartbeat is the only signal.
    watchdog = setInterval(() => {
      if (es && Date.now() - lastBeat > HEARTBEAT_TIMEOUT_MS) {
        setIsConnected(false)
        es.close()
        es = null
        clearTimeout(retryTimer)
        scheduleRetry()
      }
    }, 10000)

    connect()

    return () => {
      disposed = true
      es?.close()
      clearTimeout(retryTimer)
      clearInterval(watchdog)
    }
  }, [])

  const clear = useCallback(() => dispatch({ type: 'CLEAR' }), [])

  return { events, isConnected, clear }
}
