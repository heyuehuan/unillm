import { useState, useEffect, useMemo } from 'react'
import { api } from '../api.js'
import { IcRefresh, IcX, IcSearch } from '../components/Icons.jsx'
import FilterBar, { filtersToApiParams, ScopeToggle } from '../components/FilterBar.jsx'
import { HttpBadge, fmtDateTime, LoadError } from '../components/ui.jsx'

function DetailPanel({ log, onClose }) {
  return (
    <div className="log-detail">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
        <div style={{ fontSize: 14, fontWeight: 600 }}>Request details</div>
        <button className="iconbtn" aria-label="Close details" onClick={onClose}><IcX size={14} /></button>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10, fontSize: 12.5 }}>
        {[
          ['Request ID', <span className="mono" style={{ fontSize: 11 }}>{log.request_id || '—'}</span>],
          ['Time', fmtDateTime(log.created_at)],
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

// Fallback status options when no data has loaded yet.
const COMMON_STATUSES = [200, 400, 401, 403, 404, 429, 500, 502, 503]

export default function Logs({ user, filters, setFilters, scope, setScope, selectId = null }) {
  const [logs, setLogs] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [selectedId, setSelectedId] = useState(selectId)
  const [model, setModel] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [offset, setOffset] = useState(0)
  const [projects, setProjects] = useState([])
  const [seenStatuses, setSeenStatuses] = useState([])
  const LIMIT = 50

  useEffect(() => {
    api.getProjects().then(setProjects).catch(() => {})
  }, [])

  // Debounce the free-text model filter so typing doesn't fire a request per key.
  const [debouncedModel, setDebouncedModel] = useState('')
  useEffect(() => {
    const t = setTimeout(() => { setDebouncedModel(model); setOffset(0) }, 350)
    return () => clearTimeout(t)
  }, [model])

  async function load() {
    setLoading(true)
    setLoadError('')
    const p = filtersToApiParams(filters)
    if (scope === 'mine') p.mine = 'true'
    try {
      const [res, stats] = await Promise.all([
        api.getRequests({
          limit: LIMIT,
          offset,
          model: debouncedModel || undefined,
          status_code: statusFilter || undefined,
          ...p,
        }),
        // Status codes present in the current (status-unfiltered) selection,
        // so the dropdown only offers codes that actually exist.
        api.getStats(p).catch(() => null),
      ])
      setLogs(res.items || [])
      setTotal(res.total || 0)
      if (stats?.by_status) setSeenStatuses(Object.keys(stats.by_status).map(Number).sort((a, b) => a - b))
    } catch (e) {
      setLoadError(e.message)
      setLogs([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [offset, filters, scope, debouncedModel, statusFilter])

  function handleFiltersChange(f) {
    setFilters(f)
    setOffset(0)
    setSelectedId(null)
  }

  function handleScopeChange(s) {
    setScope(s)
    setOffset(0)
    setSelectedId(null)
  }

  const statusOptions = seenStatuses.length ? seenStatuses : COMMON_STATUSES
  const selected = useMemo(() => logs.find(l => l.id === selectedId) || null, [logs, selectedId])

  return (
    <div className="content wide logs-page">
      <div style={{ padding: '24px 32px 0' }}>
        <div className="page-h" style={{ marginBottom: 14 }}>
          <div>
            <h1 className="page-title">Request logs</h1>
            <div className="page-sub">{total.toLocaleString()} matching requests</div>
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
        <div style={{ position: 'relative' }}>
          <IcSearch size={13} style={{ position: 'absolute', left: 9, top: 9, color: 'var(--text-3)' }} />
          <input className="input" placeholder="Filter by model…" aria-label="Filter by model" value={model}
            onChange={e => setModel(e.target.value)} style={{ paddingLeft: 28, width: 180 }} />
        </div>
        <select className="select" style={{ width: 'auto' }} aria-label="Filter by status code"
          value={statusFilter} onChange={e => { setStatusFilter(e.target.value); setOffset(0) }}>
          <option value="">All statuses</option>
          {statusOptions.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
          {total === 0 ? '0' : `${offset + 1}–${Math.min(offset + LIMIT, total)}`} of {total}
        </span>
      </div>

      <div className="logs-body">
        <div style={{ flex: 1, overflow: 'auto', minWidth: 0 }}>
          {loadError ? (
            <div style={{ padding: 24 }}><LoadError message={loadError} onRetry={load} /></div>
          ) : loading ? (
            <div style={{ padding: 40, color: 'var(--text-3)', textAlign: 'center' }}>Loading…</div>
          ) : logs.length === 0 ? (
            <div className="empty" style={{ padding: 60 }}>
              <div className="empty-title">No logs found</div>
              <div style={{ fontSize: 12 }}>No requests match the current filters.</div>
            </div>
          ) : (
            <>
              {logs.map(l => (
                <div
                  key={l.id}
                  className={`log-row${selectedId === l.id ? ' selected' : ''}`}
                  onClick={() => setSelectedId(selectedId === l.id ? null : l.id)}
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
        {selected && <DetailPanel log={selected} onClose={() => setSelectedId(null)} />}
      </div>
    </div>
  )
}
