import Icon from './Icon'
import { formatClock } from '../utils/formatters'

/**
 * Failure is never rendered as "0". With no data yet it is an error panel with a retry; with
 * previously loaded data it is a stale-data banner so the numbers are not mistaken for current.
 */
export default function DataNotice({ error, hasData, updatedAt, onRetry, what = 'data' }) {
  if (!error) return null

  if (hasData) {
    return (
      <div className="notice notice--warn" role="status">
        <Icon name="wifi-off" />
        <div className="notice__text">
          <strong>Showing {what} from {updatedAt ? formatClock(updatedAt) : 'an earlier refresh'}</strong>
          {error}. Retrying automatically.
        </div>
        <button type="button" className="btn" onClick={onRetry}>Retry now</button>
      </div>
    )
  }

  return (
    <div className="notice notice--error" role="alert">
      <Icon name="alert-triangle" />
      <div className="notice__text">
        <strong>Couldn&rsquo;t load {what}</strong>
        {error}. The numbers on this page are unavailable, not zero.
      </div>
      <button type="button" className="btn" onClick={onRetry}>Try again</button>
    </div>
  )
}
