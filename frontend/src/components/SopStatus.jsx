export const STATUS = {
  draft:      { label: 'Draft',      tone: 'neutral' },
  in_review:  { label: 'In review',  tone: 'escalated' },
  approved:   { label: 'Approved',   tone: 'partial' },
  published:  { label: 'Published',  tone: 'resolved' },
  superseded: { label: 'Superseded', tone: 'neutral' },
  retired:    { label: 'Retired',    tone: 'neutral' },
}

export function SopStatus({ status }) {
  const s = STATUS[status] || { label: status, tone: 'neutral' }
  return <span className={`badge badge--${s.tone}`}>{s.label}</span>
}

/** "user:idp|alice" reads as "alice"; a system actor keeps its name. */
export function who(actor) {
  if (!actor) return '—'
  const [, id = actor] = actor.split(/:(.*)/)
  return id.includes('|') ? id.split('|').pop() : id
}
