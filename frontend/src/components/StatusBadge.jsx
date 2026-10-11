import { outcomeLabel, outcomeTone, priorityNumber, formatPriority } from '../utils/formatters'

export default function StatusBadge({ outcome }) {
  return <span className={`badge badge--${outcomeTone(outcome)}`}>{outcomeLabel(outcome)}</span>
}

/** "P2" with the full priority name available to assistive tech and on hover. */
export function PriorityBadge({ value }) {
  const p = priorityNumber(value)
  if (!p) return <span className="muted">—</span>
  const full = formatPriority(p)
  return (
    <span className={`prio prio--${p}`} title={full}>
      P{p}
      <span className="sr-only"> {full.slice(3)}</span>
    </span>
  )
}
