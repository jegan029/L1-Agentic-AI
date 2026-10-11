import { API_BASE_URL } from '../config'
import { usePolledJson } from './usePolledJson'

const selectItems = json => json.items || []

export function useSopStats(intervalMs = 30000) {
  const { data, ...rest } = usePolledJson(`${API_BASE_URL}/api/sops/stats`, {
    intervalMs,
    select: selectItems,
  })
  return { items: data || [], hasData: data !== null, ...rest }
}
