export function Sparkline({ data, color = 'var(--accent)', height = 36 }) {
  if (!data || data.length < 2) return null
  const w = 120, h = height
  const max = Math.max(...data), min = Math.min(...data)
  const rng = max - min || 1
  const pts = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w
    const y = h - ((v - min) / rng) * (h - 4) - 2
    return [x, y]
  })
  const path = pts.map((p, i) => (i === 0 ? `M${p[0]},${p[1]}` : `L${p[0]},${p[1]}`)).join(' ')
  const area = `${path} L${w},${h} L0,${h} Z`
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} preserveAspectRatio="none" style={{ display: 'block' }}>
      <path d={area} fill={color} opacity="0.12" />
      <path d={path} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  )
}

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
