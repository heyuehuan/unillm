export function BarChart({ data, height = 200 }) {
  if (!data || data.length === 0) return null
  const w = 720, h = height, pad = { l: 120, r: 12, t: 8, b: 8 }
  const cw = w - pad.l - pad.r
  const max = Math.max(...data.map(d => d.value)) * 1.05 || 1
  const rowH = (h - pad.t - pad.b) / data.length
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h}>
      {data.map((d, i) => {
        const y = pad.t + i * rowH + rowH * 0.2
        const bh = rowH * 0.6
        const bw = (d.value / max) * cw
        return (
          <g key={i}>
            <text x={pad.l - 8} y={y + bh / 2 + 4} textAnchor="end" fontSize="11" fill="var(--text-2)">{d.label}</text>
            <rect x={pad.l} y={y} width={Math.max(bw, 2)} height={bh} rx="2" fill={d.color || 'var(--accent)'} />
            <text x={pad.l + bw + 6} y={y + bh / 2 + 4} fontSize="11" fill="var(--text-3)" fontFamily="JetBrains Mono">{d.display || d.value.toLocaleString()}</text>
          </g>
        )
      })}
    </svg>
  )
}
