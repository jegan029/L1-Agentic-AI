import { useSyncExternalStore } from 'react'
import { formatRelativeTime, formatTimestamp } from '../utils/formatters'

// One shared 15s ticker so "3 min ago" keeps moving without every instance owning a timer.
let tick = 0
let timer = null
const listeners = new Set()

function subscribe(cb) {
  listeners.add(cb)
  if (!timer) {
    timer = setInterval(() => {
      tick += 1
      listeners.forEach(l => l())
    }, 15000)
  }
  return () => {
    listeners.delete(cb)
    if (listeners.size === 0 && timer) {
      clearInterval(timer)
      timer = null
    }
  }
}
const getSnapshot = () => tick

export default function RelativeTime({ ts }) {
  useSyncExternalStore(subscribe, getSnapshot)
  if (!ts) return null
  return (
    <time dateTime={ts} title={formatTimestamp(ts)}>
      {formatRelativeTime(ts)}
    </time>
  )
}
