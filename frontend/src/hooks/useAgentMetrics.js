import { API_BASE_URL } from '../config'
import { usePolledJson } from './usePolledJson'

export function useAgentMetrics(intervalMs = 10000) {
  return usePolledJson(`${API_BASE_URL}/api/dashboard/summary`, { intervalMs })
}
