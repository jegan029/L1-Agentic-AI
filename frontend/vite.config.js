import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// The dev proxy serves /api and /auth from the same origin as the page, so the session
// cookie set at sign-in is sent with every request.
// Optional: set L1_API_TOKEN to a token from `l1-agent token create` and the proxy adds it
// on the server side, which skips sign-in during development. It is never shipped to the
// browser. Leave it unset to sign in normally, or when the backend runs in demo mode.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const token = env.L1_API_TOKEN || process.env.L1_API_TOKEN || ''
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: env.L1_API_TARGET || 'http://127.0.0.1:8080',
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        },
        '/auth': { target: env.L1_API_TARGET || 'http://127.0.0.1:8080' },
      },
    },
  }
})
