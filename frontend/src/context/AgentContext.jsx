import { createContext, useContext, useMemo, useState, useCallback } from 'react'
import { useAgentMetrics } from '../hooks/useAgentMetrics'
import { useActivityStream } from '../hooks/useActivityStream'
import { useSopStats } from '../hooks/useSopStats'

// One context per update rate, so a 1/sec SSE event does not re-render the KPI cards and a
// 10s metrics poll does not re-render the activity feed.
const MetricsCtx = createContext(null)
const SopsCtx = createContext(null)
const StatusCtx = createContext(null)
const EventsCtx = createContext(null)
const FiltersCtx = createContext(null)

export const DEFAULT_FILTERS = { page: 1, perPage: 20, search: '', outcome: '', priority: '' }

export function AgentProvider({ children }) {
  const m = useAgentMetrics()
  const s = useSopStats()
  const { events, isConnected, clear } = useActivityStream()
  const [filters, setFilters] = useState(DEFAULT_FILTERS)

  const metrics = useMemo(
    () => m,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [m.data, m.error, m.loading, m.updatedAt, m.refresh],
  )
  const sops = useMemo(
    () => s,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [s.items, s.error, s.loading, s.updatedAt, s.refresh],
  )
  const status = useMemo(() => ({ isConnected }), [isConnected])
  const stream = useMemo(() => ({ events, clear }), [events, clear])

  const resetFilters = useCallback(() => setFilters(DEFAULT_FILTERS), [])
  const filterApi = useMemo(() => ({ filters, setFilters, resetFilters }), [filters, resetFilters])

  return (
    <MetricsCtx.Provider value={metrics}>
      <SopsCtx.Provider value={sops}>
        <StatusCtx.Provider value={status}>
          <EventsCtx.Provider value={stream}>
            <FiltersCtx.Provider value={filterApi}>{children}</FiltersCtx.Provider>
          </EventsCtx.Provider>
        </StatusCtx.Provider>
      </SopsCtx.Provider>
    </MetricsCtx.Provider>
  )
}

function useRequired(ctx, name) {
  const value = useContext(ctx)
  if (!value) throw new Error(`${name} must be used inside AgentProvider`)
  return value
}

export const useMetrics = () => useRequired(MetricsCtx, 'useMetrics')
export const useSops = () => useRequired(SopsCtx, 'useSops')
export const useConnection = () => useRequired(StatusCtx, 'useConnection')
export const useEvents = () => useRequired(EventsCtx, 'useEvents')
export const useIncidentFilters = () => useRequired(FiltersCtx, 'useIncidentFilters')
