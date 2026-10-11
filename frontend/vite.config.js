import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// The API requires a bearer token. The dev proxy adds it on the server side so the
// token is never shipped to the browser. Set L1_API_TOKEN to one of AGENT_API_TOKENS;
// leave it unset when the backend runs in demo mode without credentials.
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
      },
    },
  }
})
