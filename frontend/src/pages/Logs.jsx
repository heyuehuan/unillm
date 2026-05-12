import { useState, useEffect } from 'react'
import { api } from '../api.js'
import { IcRefresh, IcX, IcSearch } from '../components/Icons.jsx'
import FilterBar, { DEFAULT_FILTERS, filtersToApiParams, ScopeToggle } from '../components/FilterBar.jsx'

function HttpBadge({ code }) {
  if (!code) return <span className="badge">—</span>
  if (code >= 500) return <span className="badge red">{code}</span>
  if (code >= 400) return <span className="badge amber">{code}</span>
  return <span className="badge green">{code}</span>
}

function fmtTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

function DetailPanel({ log, onClose }) {
  return (
    <div style={{ borderLeft: '1px solid var(--border)', background: 'var(--bg-1)', overflow: 'auto', padding: 20, width: 400, flexShrink: 0 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
        <div style={{ fontSize: 14, fontWeight: 600 }}>Request details</div>
        <button className="iconbtn" onClick={onClose}><IcX size={14} /></button>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10, fontSize: 12.5 }}>
        {[
          ['Request ID', <span className="mono" style={{ fontSize: 11 }}>{log.request_id || '—'}</span>],
          ['Time', fmtTime(log.created_at)],
          ['Status', <HttpBadge code={log.status_code} />],
          ['Model', <span className="mono">{log.model}</span>],
          ['Backend model', <span className="mono">{log.backend_model || '—'}</span>],
          ['Project', log.project_name || (log.project_id ? `#${log.project_id}` : '—')],
          ['API Key', log.api_key_name ? <span className="mono">{log.api_key_name}</span> : '—'],
          ['SSH User', log.ssh_username || '—'],
          ['Tokens in', log.prompt_tokens?.toLocaleString() || '0'],
          ['Tokens out', log.completion_tokens?.toLocaleString() || '0'],
          ['Total tokens', log.total_tokens?.toLocaleString() || '0'],
          ['Cost', log.cost_usd != null ? `$${log.cost_usd.toFixed(6)}` : '—'],
          ['Latency', log.latency_ms ? `${log.latency_ms} ms` : '—'],
          ['IP', log.ip_address || '—'],
          ['Stream', log.stream ? 'Yes' : 'No'],
        ].map(([k, v]) => (
          <div key={k} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
            <span style={{ color: 'var(--text-3)', flexShrink: 0 }}>{k}</span>
            <span style={{ textAlign: 'right' }}>{v}</span>
          </div>
        ))}
      </div>
      {log.error_message && (
        <>
          <hr className="hr" />
          <div style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-2)', marginBottom: 8 }}>Error</div>
          <pre style={{ background: 'var(--bg-2)', padding: 10, borderRadius: 6, fontSize: 11.5, lineHeight: 1.6, overflow: 'auto', margin: 0, color: 'var(--red)', fontFamily: 'JetBrains Mono, monospace', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            {log.error_message}
          </pre>
        </>
      )}
    </div>
  )
}

export default function Logs({ user }) {
  const [logs, setLogs] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [model, setModel] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [offset, setOffset] = useState(0)
  const [projects, setProjects] = useState([])
  const [filters, setFilters] = useState(DEFAULT_FILTERS)
  const [scope, setScope] = useState('all')
  const LIMIT = 50

  useEffect(() => {
    api.getProjects().then(setProjects).catch(() => {})
  }, [])

  async function load(overrides = {}) {
    setLoading(true)
    const p = filtersToApiParams(filters)
    if (scope === 'mine' && user?.username) p.ssh_username = user.username
    try {
      const res = await api.getRequests({
        limit: LIMIT,
        offset,
        model: model || undefined,
        status_code: statusFilter || undefined,
        ...p,
        ...overrides,
      })
      setLogs(res.items || [])
      setTotal(res.total || 0)
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [offset, filters, scope])

  function applyTextFilters(e) {
    e.preventDefault()
    setOffset(0)
    load({ offset: 0 })
  }

  function handleFiltersChange(f) {
    setFilters(f)
    setOffset(0)
    setSelected(null)
  }

  function handleScopeChange(s) {
    setScope(s)
    setOffset(0)
    setSelected(null)
  }

  return (
    <div className="content wide">
      <div style={{ padding: '24px 32px 0' }}>
        <div className="page-h" style={{ marginBottom: 14 }}>
          <div>
            <h1 className="page-title">Request logs</h1>
            <div className="page-sub">{total.toLocaleString()} total requests</div>
          </div>
          <div className="h-actions">
            <button className="btn" onClick={() => load()}><IcRefresh size={14} /> Refresh</button>
          </div>
        </div>
      </div>

      <div className="filter-bar" style={{ borderTop: '1px solid var(--border)', flexWrap: 'wrap', gap: 8 }}>
        <ScopeToggle scope={scope} onChange={handleScopeChange} />
        <div style={{ width: 1, height: 20, background: 'var(--border)' }} />
        <FilterBar projects={projects} filters={filters} onChange={handleFiltersChange} />
        <div style={{ width: '1px', background: 'var(--border)', alignSelf: 'stretch', margin: '0 4px' }} />
        <form style={{ display: 'flex', gap: 8, alignItems: 'center' }} onSubmit={applyTextFilters}>
          <div style={{ position: 'relative' }}>
            <IcSearch size={13} style={{ position: 'absolute', left: 9, top: 9, color: 'var(--text-3)' }} />
            <input className="input" placeholder="Filter by model…" value={model} onChange={e => setModel(e.target.value)} style={{ paddingLeft: 28, width: 180 }} />
          </div>
          <select className="select" style={{ width: 'auto' }} value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
            <option value="">All statuses</option>
            <option value="200">200 OK</option>
            <option value="400">400</option>
            <option value="401">401</option>
            <option value="429">429</option>
            <option value="500">500</option>
          </select>
          <button type="submit" className="btn sm">Apply</button>
        </form>
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
          {logs.length} / {total}
        </span>
      </div>

      <div style={{ display: 'flex', height: 'calc(100vh - 210px)' }}>
        <div style={{ flex: 1, overflow: 'auto' }}>
          {loading ? (
            <div style={{ padding: 40, color: 'var(--text-3)', textAlign: 'center' }}>Loading…</div>
          ) : logs.length === 0 ? (
            <div className="empty" style={{ padding: 60 }}>
              <div className="empty-title">No logs found</div>
              <div style={{ fontSize: 12 }}>Make some API requests to see logs here.</div>
            </div>
          ) : (
            <>
              {logs.map((l, i) => (
                <div
                  key={l.id}
                  className={`log-row${selected === i ? ' selected' : ''}`}
                  onClick={() => setSelected(selected === i ? null : i)}
                >
                  <span className="log-time">{new Date(l.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3 })}</span>
                  <span className="log-status"><HttpBadge code={l.status_code} /></span>
                  <span className="log-model"><span className="mono" style={{ fontSize: 12 }}>{l.model}</span></span>
                  {l.project_name && (
                    <span style={{ fontSize: 11, color: 'var(--text-3)', background: 'var(--bg-2)', borderRadius: 4, padding: '1px 6px', flexShrink: 0 }}>{l.project_name}</span>
                  )}
                  <span className="log-msg">{l.error_message || `${l.prompt_tokens} in · ${l.completion_tokens} out tokens`}</span>
                  <span className="log-meta">{l.latency_ms ? `${l.latency_ms}ms` : '—'}</span>
                </div>
              ))}
              {total > LIMIT && (
                <div style={{ display: 'flex', justifyContent: 'center', gap: 8, padding: 16 }}>
                  <button className="btn sm" disabled={offset === 0} onClick={() => setOffset(o => Math.max(0, o - LIMIT))}>← Prev</button>
                  <span style={{ fontSize: 12, color: 'var(--text-3)', alignSelf: 'center' }}>
                    {offset + 1}–{Math.min(offset + LIMIT, total)} of {total}
                  </span>
                  <button className="btn sm" disabled={offset + LIMIT >= total} onClick={() => setOffset(o => o + LIMIT)}>Next →</button>
                </div>
              )}
            </>
          )}
        </div>
        {selected !== null && logs[selected] && (
          <DetailPanel log={logs[selected]} onClose={() => setSelected(null)} />
        )}
      </div>
    </div>
  )
}
