import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { API_BASE_URL } from '../config'

const SessionContext = createContext(null)

async function getJson(path) {
  const res = await fetch(`${API_BASE_URL}${path}`, { headers: { Accept: 'application/json' } })
  return { status: res.status, body: res.ok ? await res.json() : null }
}

/**
 * Who is signed in. The session lives in an HttpOnly cookie the page cannot read, so the
 * only way to know is to ask the API. Nothing else is fetched until this has answered.
 */
export function SessionProvider({ children }) {
  const [state, setState] = useState({ status: 'loading', user: null, methods: null, error: null })

  const load = useCallback(async () => {
    try {
      const me = await getJson('/api/me')
      if (me.status === 200) {
        setState({ status: 'signed-in', user: me.body, methods: null, error: null })
        return
      }
      if (me.status !== 401) throw new Error(`The agent API returned HTTP ${me.status}`)
      const methods = await getJson('/auth/methods')
      setState({ status: 'signed-out', user: null, methods: methods.body || {}, error: null })
    } catch (e) {
      const error = e instanceof TypeError ? "Can't reach the agent API" : e.message
      setState({ status: 'error', user: null, methods: null, error })
    }
  }, [])

  useEffect(() => { load() }, [load])

  const signOut = useCallback(async () => {
    const user = state.user
    if (!user) return
    await fetch(`${API_BASE_URL}/auth/logout`, {
      method: 'POST',
      // State-changing requests carry the token from /api/me; the cookie alone is refused.
      headers: { [user.csrf_header]: user.csrf_token },
    })
    load()
  }, [state.user, load])

  return (
    <SessionContext.Provider value={{ ...state, reload: load, signOut }}>
      {children}
    </SessionContext.Provider>
  )
}

export function useSession() {
  return useContext(SessionContext)
}
