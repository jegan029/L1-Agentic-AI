import { createContext, useContext, useState } from 'react'
import { useAgentMetrics } from '../hooks/useAgentMetrics'
import { useActivityStream } from '../hooks/useActivityStream'
import { useSopStats } from '../hooks/useSopStats'

const Ctx = createContext(null)

export function AgentProvider({ children }) {
  const metrics = useAgentMetrics()
  const stream  = useActivityStream()
  const sops    = useSopStats()
  const [incidentFilters, setIncidentFilters] = useState({
    page: 1, perPage: 20, search: '', outcome: '', priority: '',
  })

  return (
    <Ctx.Provider value={{ metrics, stream, sops, incidentFilters, setIncidentFilters }}>
      {children}
    </Ctx.Provider>
  )
}

export function useAgent() {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useAgent must be used inside AgentProvider')
  return ctx
}
