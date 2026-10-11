import { formatDuration, stepLabel, stepStatusLabel, escalationReasonText } from './formatters'

/** One-line plain-text description of an SSE event. Always starts with the incident number. */
export function eventText(evt) {
  const n = evt.incident_number || 'Event'
  switch (evt.type) {
    case 'incident_received':
      return `${n} received${evt.short_description ? `: ${evt.short_description}` : ''}`
    case 'sop_matched': {
      const conf = typeof evt.confidence === 'number' ? ` at ${Math.round(evt.confidence * 100)}% confidence` : ''
      return `${n} matched ${evt.sop_title || evt.sop_id}${conf}`
    }
    case 'step_executing':
      return `${n} running ${evt.step_id} (${stepLabel(evt.step_type)})`
    case 'step_done':
      return `${n} ${evt.step_id} ${stepStatusLabel(evt.status).toLowerCase()} in ${formatDuration(evt.duration_ms)}`
    case 'incident_resolved':
      return `${n} resolved via ${evt.sop_id} in ${formatDuration(evt.duration_ms)}`
    case 'incident_escalated': {
      const mail = evt.email_sent === false ? ' (notification email not sent)' : ''
      return `${n} escalated to ${evt.l2_team_name || 'L2'}: ${escalationReasonText(evt.reason)}${mail}`
    }
    default:
      return `${n}: ${evt.type}`
  }
}
