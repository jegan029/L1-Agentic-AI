import { useState, useEffect, useCallback } from 'react'
import { API_BASE_URL } from '../config'

export function useIncidentHistory(filters = {}) {
  const { page = 1, perPage = 20, search = '', outcome = '', priority = '' } = filters
  const [total, setTotal] = useState(0)
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const fetch_ = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ page, per_page: perPage })
      if (search)   params.set('search', search)
      if (outcome)  params.set('outcome', outcome)
      if (priority) params.set('priority', priority)
      const res = await fetch(`${API_BASE_URL}/api/incidents?${params}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const json = await res.json()
      setTotal(json.total)
      setItems(json.items)
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [page, perPage, search, outcome, priority])

  useEffect(() => { fetch_() }, [fetch_])

  return { total, items, loading, error, refresh: fetch_ }
}

export async function fetchIncidentDetail(incidentNumber) {
  const res = await fetch(`${API_BASE_URL}/api/incidents/${incidentNumber}`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}
