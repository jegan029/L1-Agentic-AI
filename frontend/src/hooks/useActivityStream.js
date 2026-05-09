import { useState, useEffect, useReducer, useCallback } from 'react'
import { API_BASE_URL } from '../config'

const MAX_EVENTS = 50

function reducer(state, action) {
  switch (action.type) {
    case 'ADD_EVENT': {
      const next = [action.event, ...state].slice(0, MAX_EVENTS)
      return next
    }
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
    let es
    let retryTimer

    const connect = () => {
      es = new EventSource(`${API_BASE_URL}/api/stream`)

      es.onopen = () => setIsConnected(true)

      es.onmessage = (e) => {
        try {
          const evt = JSON.parse(e.data)
          if (evt.type !== 'heartbeat') {
            dispatch({ type: 'ADD_EVENT', event: evt })
          }
        } catch (_) {}
      }

      es.onerror = () => {
        setIsConnected(false)
        es.close()
        retryTimer = setTimeout(connect, 3000)
      }
    }

    connect()

    return () => {
      if (es) es.close()
      if (retryTimer) clearTimeout(retryTimer)
    }
  }, [])

  const clear = useCallback(() => dispatch({ type: 'CLEAR' }), [])

  return { events, isConnected, clear }
}
