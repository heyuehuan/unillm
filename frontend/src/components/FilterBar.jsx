import { useState, useRef, useEffect } from 'react'
import { zonedInputToUtcISO, timezoneLabel } from './ui.jsx'

const TIME_PRESETS = [
  { key: '24h', label: '24h' },
  { key: '3d', label: '3d' },
  { key: '7d', label: '7d' },
  { key: 'all', label: 'All' },
  { key: 'custom', label: 'Custom' },
]

export const DEFAULT_FILTERS = {
  timeRange: 'all',
  customFrom: '',
  customTo: '',
  projectIds: [],
}

// Human-readable summary of the active filters, for page subtitles.
export function describeFilters(filters) {
  const time = {
    '24h': 'Last 24 hours', '3d': 'Last 3 days', '7d': 'Last 7 days',
    all: 'All time', custom: 'Custom range',
  }[filters.timeRange] || 'All time'
  if (filters.projectIds.length === 0) return time
  const proj = filters.projectIds.length === 1 ? '1 project' : `${filters.projectIds.length} projects`
  return `${time} · ${proj}`
}

export function filtersToApiParams(filters) {
  const params = {}
  if (filters.projectIds.length) params.project_ids = filters.projectIds
  if (filters.timeRange === 'custom') {
    const from = zonedInputToUtcISO(filters.customFrom)
    const to = zonedInputToUtcISO(filters.customTo)
    if (from) params.from_date = from
    if (to) params.to_date = to
  } else if (filters.timeRange !== 'all') {
    const hours = { '24h': 24, '3d': 72, '7d': 168 }[filters.timeRange]
    params.from_date = new Date(Date.now() - hours * 3600 * 1000).toISOString()
  }
  return params
}

function ProjectDropdown({ projects, selected, onChange }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return
    function handle(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', handle)
    return () => document.removeEventListener('mousedown', handle)
  }, [open])

  function toggle(id) {
    onChange(selected.includes(id) ? selected.filter(i => i !== id) : [...selected, id])
  }

  const label = selected.length === 0
    ? 'All projects'
    : selected.length === 1
      ? (projects.find(p => p.id === selected[0])?.name ?? '1 project')
      : `${selected.length} projects`

  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <button
        type="button"
        className="btn sm"
        onClick={() => setOpen(o => !o)}
        style={{ minWidth: 110, justifyContent: 'space-between', gap: 6 }}
      >
        <span>{label}</span>
        <span style={{ opacity: 0.45, fontSize: 9 }}>▾</span>
      </button>
      {open && (
        <div style={{
          position: 'absolute', top: 'calc(100% + 4px)', left: 0,
          background: 'var(--bg)', border: '1px solid var(--border)',
          borderRadius: 8, boxShadow: 'var(--shadow-md)', zIndex: 50,
          minWidth: 170, padding: '4px 0', maxHeight: 240, overflowY: 'auto',
        }}>
          <button
            type="button"
            onClick={() => { onChange([]); setOpen(false) }}
            style={{
              display: 'block', width: '100%', textAlign: 'left',
              padding: '7px 12px', fontSize: 12, border: 'none', cursor: 'pointer',
              background: selected.length === 0 ? 'var(--accent-soft)' : 'transparent',
              color: selected.length === 0 ? 'var(--accent-2)' : 'var(--text-2)',
              fontWeight: selected.length === 0 ? 500 : 400,
            }}
          >
            All projects
          </button>
          {projects.map(p => (
            <label
              key={p.id}
              style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '7px 12px', cursor: 'pointer', fontSize: 13,
                background: selected.includes(p.id) ? 'var(--accent-soft)' : 'transparent',
              }}
            >
              <input
                type="checkbox"
                checked={selected.includes(p.id)}
                onChange={() => toggle(p.id)}
                style={{ accentColor: 'var(--accent)', margin: 0 }}
              />
              <span style={{ color: selected.includes(p.id) ? 'var(--accent-2)' : 'var(--text)' }}>
                {p.name}
              </span>
            </label>
          ))}
        </div>
      )}
    </div>
  )
}

export function ScopeToggle({ scope, onChange }) {
  const btn = (key, label, title) => (
    <button
      key={key}
      type="button"
      onClick={() => onChange(key)}
      title={title}
      aria-label={title}
      style={{
        padding: '3px 10px', fontSize: 12, fontWeight: 500, border: 'none', cursor: 'pointer',
        borderRadius: 4,
        background: scope === key ? 'var(--bg)' : 'transparent',
        color: scope === key ? 'var(--text)' : 'var(--text-3)',
        boxShadow: scope === key ? 'var(--shadow)' : 'none',
      }}
    >
      {label}
    </button>
  )
  return (
    <div style={{ display: 'flex', gap: 2, background: 'var(--bg-2)', borderRadius: 6, padding: 2 }}>
      {btn('all', 'All', 'All requests you can see')}
      {btn('mine', 'My SSH', 'Only requests signed with your SSH key — API-key-only requests are not included')}
    </div>
  )
}

export default function FilterBar({ projects = [], filters, onChange }) {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
      <div style={{ display: 'flex', gap: 2, background: 'var(--bg-2)', borderRadius: 6, padding: 2 }}>
        {TIME_PRESETS.map(p => (
          <button
            key={p.key}
            type="button"
            onClick={() => onChange({ ...filters, timeRange: p.key })}
            style={{
              padding: '3px 9px', fontSize: 12, fontWeight: 500, border: 'none', cursor: 'pointer',
              borderRadius: 4,
              background: filters.timeRange === p.key ? 'var(--bg)' : 'transparent',
              color: filters.timeRange === p.key ? 'var(--text)' : 'var(--text-3)',
              boxShadow: filters.timeRange === p.key ? 'var(--shadow)' : 'none',
            }}
          >
            {p.label}
          </button>
        ))}
      </div>

      {filters.timeRange === 'custom' && (
        <>
          <input
            type="datetime-local"
            className="input"
            style={{ width: 170, fontSize: 12, padding: '4px 8px' }}
            value={filters.customFrom}
            onChange={e => onChange({ ...filters, customFrom: e.target.value })}
          />
          <span style={{ color: 'var(--text-3)', fontSize: 12 }}>→</span>
          <input
            type="datetime-local"
            className="input"
            style={{ width: 170, fontSize: 12, padding: '4px 8px' }}
            value={filters.customTo}
            onChange={e => onChange({ ...filters, customTo: e.target.value })}
          />
          <span style={{ color: 'var(--text-3)', fontSize: 11 }}>{timezoneLabel()}</span>
        </>
      )}

      {projects.length > 0 && (
        <ProjectDropdown
          projects={projects}
          selected={filters.projectIds}
          onChange={ids => onChange({ ...filters, projectIds: ids })}
        />
      )}
    </div>
  )
}
