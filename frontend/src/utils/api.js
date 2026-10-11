import { API_BASE_URL } from '../config'

/**
 * A state-changing request. Carries the CSRF token from /api/me, without which the API
 * refuses a cookie session. Never throws for an HTTP error: the caller shows `error`.
 */
export async function send(user, method, path, body) {
  const headers = { Accept: 'application/json' }
  if (user?.csrf_header && user?.csrf_token) headers[user.csrf_header] = user.csrf_token
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  try {
    const res = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    let data = null
    try { data = await res.json() } catch { /* an empty or non-JSON answer */ }
    if (res.ok) return { ok: true, data }
    return {
      ok: false,
      status: res.status,
      error: data?.error || `The agent API returned HTTP ${res.status}`,
      problems: Array.isArray(data?.problems) ? data.problems : [],
    }
  } catch {
    return { ok: false, status: 0, error: "Can't reach the agent API", problems: [] }
  }
}
