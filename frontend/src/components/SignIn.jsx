import { useState } from 'react'
import { API_BASE_URL } from '../config'
import { brand } from '../brand'
import { useSession } from '../context/SessionContext'
import Icon from './Icon'

function BreakGlass({ onDone }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE_URL}/auth/break-glass`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
        redirect: 'manual',
      })
      // Success answers with a redirect and the session cookie.
      if (res.status === 401) setError('That password was not accepted. The attempt was recorded.')
      else if (res.status === 429) setError('Too many attempts. Wait a minute and try again.')
      else onDone()
    } catch {
      setError("Can't reach the agent API")
    } finally {
      setBusy(false)
    }
  }

  return (
    <details className="signin__emergency">
      <summary>Emergency access</summary>
      <p>
        For use only when your identity provider is unavailable. Every attempt is written to
        the audit log.
      </p>
      <form onSubmit={submit}>
        <label htmlFor="bg-password">Break-glass password</label>
        <input
          id="bg-password"
          className="field"
          type="password"
          autoComplete="off"
          value={password}
          onChange={e => setPassword(e.target.value)}
          required
        />
        {error && <p className="signin__error" role="alert">{error}</p>}
        <button type="submit" className="btn" disabled={busy || !password}>
          {busy ? 'Checking…' : 'Sign in as administrator'}
        </button>
      </form>
    </details>
  )
}

export default function SignIn() {
  const { status, methods, error, reload } = useSession()

  if (status === 'loading') {
    return <main className="signin" aria-busy="true"><p>Checking your session…</p></main>
  }

  if (status === 'error') {
    return (
      <main className="signin">
        <div className="notice notice--error" role="alert">
          <Icon name="alert-triangle" />
          <div className="notice__text">
            <strong>Couldn&rsquo;t check your session</strong>
            {error}.
          </div>
          <button type="button" className="btn" onClick={reload}>Try again</button>
        </div>
      </main>
    )
  }

  const none = !methods.oidc && !methods.saml && !methods.break_glass
  return (
    <main className="signin">
      <div className="card signin__card">
        {brand.logo && <img className="signin__logo" src={brand.logo} alt={brand.org} />}
        <h1 className="page-title">{brand.product}</h1>
        <p className="signin__lead">Sign in to see incidents and agent activity.</p>

        {methods.oidc && (
          <a className="btn signin__method" href={`${API_BASE_URL}/auth/login`}>
            Sign in with single sign-on
          </a>
        )}
        {methods.saml && (
          <a className="btn signin__method" href={`${API_BASE_URL}/auth/saml/login`}>
            Sign in with SAML
          </a>
        )}
        {none && (
          <p className="signin__error" role="alert">
            No sign-in method is configured for this service. Ask its administrator to set up
            single sign-on.
          </p>
        )}
        {methods.break_glass && <BreakGlass onDone={reload} />}
      </div>
    </main>
  )
}
