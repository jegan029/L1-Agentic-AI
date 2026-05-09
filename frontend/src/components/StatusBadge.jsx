import { outcomeColor, outcomeLabel } from '../utils/formatters'

export default function StatusBadge({ outcome }) {
  const { bg, text, border } = outcomeColor(outcome)
  return (
    <span style={{
      display: 'inline-flex',
      alignItems: 'center',
      padding: '2px 8px',
      borderRadius: 2,
      fontSize: 10,
      fontWeight: 700,
      letterSpacing: '0.07em',
      background: bg,
      color: text,
      border: `1px solid ${border}`,
      whiteSpace: 'nowrap',
      textTransform: 'uppercase',
    }}>
      {outcomeLabel(outcome)}
    </span>
  )
}
