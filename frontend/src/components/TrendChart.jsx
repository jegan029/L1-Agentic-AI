import { useState, useLayoutEffect, useId } from 'react'

// Series are told apart by line pattern and marker shape as well as colour (WCAG 1.4.1).
// "Escalated" here is the backend's bucket: escalated + partial + failed.
const SERIES = [
  { key: 'received',  label: 'Received',            color: 'var(--ss-blue)',          dash: null,  marker: 'circle' },
  { key: 'resolved',  label: 'Resolved',            color: 'var(--resolved-solid)',   dash: '7 5', marker: 'square' },
  { key: 'escalated', label: 'Escalated or failed', color: 'var(--escalated-solid)',  dash: '2 5', marker: 'diamond' },
]

const H = 190
const PAD = { l: 34, r: 14, t: 14, b: 30 }

function niceTicks(max) {
  if (max <= 4) return Array.from({ length: max + 1 }, (_, i) => i)
  const raw = max / 4
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 5, 10].map(m => m * mag).find(s => s >= raw)
  const top = Math.ceil(max / step) * step
  return Array.from({ length: top / step + 1 }, (_, i) => i * step)
}

// Callback ref (state, not useRef): the chart mounts after data arrives, so an effect keyed on
// a ref object would run once against null and never measure.
function useElementWidth(initial = 640) {
  const [el, setEl] = useState(null)
  const [width, setWidth] = useState(initial)
  useLayoutEffect(() => {
    if (!el) return undefined
    setWidth(el.clientWidth || initial)
    const ro = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width) || initial))
    ro.observe(el)
    return () => ro.disconnect()
  }, [el, initial])
  return [setEl, width]
}

function Marker({ kind, x, y, color, title }) {
  const r = 4
  const common = { fill: '#fff', stroke: color, strokeWidth: 2 }
  let shape
  if (kind === 'square') shape = <rect x={x - r} y={y - r} width={r * 2} height={r * 2} {...common} />
  else if (kind === 'diamond') shape = <polygon points={`${x},${y - r - 1} ${x + r + 1},${y} ${x},${y + r + 1} ${x - r - 1},${y}`} {...common} />
  else shape = <circle cx={x} cy={y} r={r} {...common} />
  return <g>{title && <title>{title}</title>}{shape}</g>
}

function LegendSwatch({ s }) {
  return (
    <svg width="28" height="12" viewBox="0 0 28 12" aria-hidden="true" focusable="false">
      <line x1="1" y1="6" x2="27" y2="6" stroke={s.color} strokeWidth="2.5" strokeLinecap="round" strokeDasharray={s.dash || undefined} />
    </svg>
  )
}

function dayLabel(iso, narrow) {
  const d = new Date(`${iso}T00:00:00Z`)
  const weekday = d.toLocaleDateString(undefined, { weekday: 'short', timeZone: 'UTC' })
  return narrow ? weekday : `${weekday} ${iso.slice(8)}`
}

export default function TrendChart({ data }) {
  const [wrapRef, width] = useElementWidth()
  const uid = useId()

  if (!data || data.length === 0) {
    return <p className="state__body">No daily data yet.</p>
  }

  const values = data.flatMap(d => SERIES.map(s => d[s.key] || 0))
  const ticks = niceTicks(Math.max(...values, 1))
  const yMax = ticks[ticks.length - 1]
  const innerW = Math.max(width - PAD.l - PAD.r, 40)
  const innerH = H - PAD.t - PAD.b
  const x = i => PAD.l + (data.length === 1 ? innerW / 2 : (i * innerW) / (data.length - 1))
  const y = v => PAD.t + (1 - v / yMax) * innerH
  const narrow = width < 480

  const totals = Object.fromEntries(SERIES.map(s => [s.key, data.reduce((n, d) => n + (d[s.key] || 0), 0)]))
  const first = data[0].date
  const last = data[data.length - 1].date
  const summary =
    `Daily incidents, ${first} to ${last}, UTC. ` +
    SERIES.map(s => `${s.label.toLowerCase()} ${totals[s.key]}`).join(', ') + ' in total.'

  return (
    <div className="trend" ref={wrapRef}>
      <ul className="trend__legend">
        {SERIES.map(s => (
          <li key={s.key}>
            <LegendSwatch s={s} />
            {s.label} <strong>{totals[s.key]}</strong>
          </li>
        ))}
      </ul>

      <svg
        className="trend__svg"
        viewBox={`0 0 ${width} ${H}`}
        width={width}
        height={H}
        role="img"
        aria-labelledby={`${uid}-t`}
      >
        <title id={`${uid}-t`}>{summary}</title>
        {ticks.map(t => (
          <g key={t}>
            <line className="trend__grid" x1={PAD.l} x2={width - PAD.r} y1={y(t)} y2={y(t)} />
            <text className="trend__axis" x={PAD.l - 8} y={y(t) + 4} textAnchor="end">{t}</text>
          </g>
        ))}
        {data.map((d, i) => (
          <text key={d.date} className="trend__axis" x={x(i)} y={H - 8} textAnchor="middle">
            {dayLabel(d.date, narrow)}
          </text>
        ))}
        {SERIES.map(s => (
          <g key={s.key}>
            <polyline
              fill="none" stroke={s.color} strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round"
              strokeDasharray={s.dash || undefined}
              points={data.map((d, i) => `${x(i)},${y(d[s.key] || 0)}`).join(' ')}
            />
            {data.map((d, i) => (
              <Marker
                key={d.date} kind={s.marker} x={x(i)} y={y(d[s.key] || 0)} color={s.color}
                title={`${d.date}: ${s.label.toLowerCase()} ${d[s.key] || 0}`}
              />
            ))}
          </g>
        ))}
      </svg>

      <details className="chart-table">
        <summary>View as table</summary>
        <table>
          <caption>Daily incidents, UTC. “Escalated or failed” also counts partial outcomes.</caption>
          <thead>
            <tr>
              <th scope="col">Day</th>
              {SERIES.map(s => <th scope="col" key={s.key}>{s.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {data.map(d => (
              <tr key={d.date}>
                <th scope="row">{d.date}</th>
                {SERIES.map(s => <td key={s.key}>{d[s.key] || 0}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  )
}
