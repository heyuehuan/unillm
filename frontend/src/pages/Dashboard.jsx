import { useState, useEffect } from 'react'
import { api } from '../api.js'
import { navigate } from '../router.js'
import { BarChart } from '../components/Charts.jsx'
import { IcZap, IcDollar, IcSliders, IcRefresh, IcChevRight } from '../components/Icons.jsx'
import FilterBar, { filtersToApiParams, describeFilters, ScopeToggle } from '../components/FilterBar.jsx'
import { HttpBadge, fmtTokens, LoadError } from '../components/ui.jsx'

function StatCard({ label, value, sub, icon }) {
  return (
    <div className="stat">
      <div className="stat-label">{icon}{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  )
}

function fmtTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export default function Dashboard({ user, filters, setFilters, scope, setScope }) {
  const [stats, setStats] = useState(null)
  const [recent, setRecent] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [projects, setProjects] = useState([])

  useEffect(() => {
    api.getProjects().then(setProjects).catch(() => {})
  }, [])

  async function load() {
    setLoading(true)
    setLoadError('')
    const p = filtersToApiParams(filters)
    if (scope === 'mine') p.mine = 'true'
    try {
      const [s, r] = await Promise.all([
        api.getStats(p),
        api.getRequests({ limit: 8, ...p }),
      ])
      setStats(s)
      setRecent(r.items || [])
    } catch (e) {
      setLoadError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [filters, scope])

  const modelChart = stats?.by_model?.slice(0, 6).map(m => ({
    label: m.model,
    value: m.requests,
    display: m.requests.toLocaleString(),
  })) || []

  const isAdmin = user?.global_role === 'admin'
  const scopeLabel = scope === 'mine'
    ? 'your SSH-signed requests'
    : isAdmin ? 'org-wide' : 'across your projects'

  return (
    <div className="content">
      <div className="page-h">
        <div>
          <h1 className="page-title">Dashboard</h1>
          <div className="page-sub">{describeFilters(filters)} · {scopeLabel}</div>
        </div>
        <div className="h-actions">
          <button className="btn" onClick={() => load()}><IcRefresh size={14} /> Refresh</button>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20, flexWrap: 'wrap' }}>
        <ScopeToggle scope={scope} onChange={setScope} />
        <div style={{ width: 1, height: 20, background: 'var(--border)' }} />
        <FilterBar projects={projects} filters={filters} onChange={setFilters} />
      </div>

      {loadError && <LoadError message={loadError} onRetry={load} />}

      {loading ? (
        <div style={{ color: 'var(--text-3)', padding: '40px 0' }}>Loading…</div>
      ) : loadError ? null : (
        <>
          <div className="stat-grid">
            <StatCard
              label="Total requests" icon={<IcZap size={14} />}
              value={(stats?.total_requests || 0).toLocaleString()}
              sub={`${Object.keys(stats?.by_status || {}).length} status codes`}
            />
            <StatCard
              label="Total tokens" icon={<IcSliders size={14} />}
              value={fmtTokens(stats?.total_tokens || 0)}
              sub={`${fmtTokens(stats?.total_prompt_tokens || 0)} in · ${fmtTokens(stats?.total_completion_tokens || 0)} out`}
            />
            <StatCard
              label="Total cost" icon={<IcDollar size={14} />}
              value={`$${(stats?.total_cost_usd || 0).toFixed(4)}`}
              sub="based on pricing table"
            />
            <StatCard
              label="Avg latency" icon={<IcSliders size={14} />}
              value={stats?.avg_latency_ms ? `${Math.round(stats.avg_latency_ms)} ms` : '—'}
              sub="across all requests"
            />
          </div>

          {modelChart.length > 0 && (
            <div className="card" style={{ marginBottom: 20 }}>
              <div className="card-h">
                <div><h3>Requests by model</h3></div>
              </div>
              <div className="card-b">
                <BarChart data={modelChart} height={Math.max(120, modelChart.length * 32)} />
              </div>
            </div>
          )}

          <div className="card">
            <div className="card-h">
              <div><h3>Recent requests</h3><div className="sub">Latest activity — click a row for details</div></div>
              <button className="btn sm ghost" onClick={() => navigate('logs')}>
                View all <IcChevRight size={13} />
              </button>
            </div>
            {recent.length === 0 ? (
              <div className="empty">
                <div className="empty-title">No requests yet</div>
                <div style={{ fontSize: 12 }}>Requests will appear here after your first API call.</div>
              </div>
            ) : (
              <div>
                {recent.map(r => (
                  <div key={r.id} className="log-row" onClick={() => navigate('logs', { sel: r.id })}>
                    <span className="log-time">{fmtTime(r.created_at)}</span>
                    <span className="log-status"><HttpBadge code={r.status_code} /></span>
                    <span className="log-model mono" style={{ fontSize: 12 }}>{r.model}</span>
                    {r.project_name && (
                      <span style={{ fontSize: 11, color: 'var(--text-3)', background: 'var(--bg-2)', borderRadius: 4, padding: '1px 6px', flexShrink: 0 }}>{r.project_name}</span>
                    )}
                    {r.api_key_name && (
                      <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', flexShrink: 0 }}>{r.api_key_name}</span>
                    )}
                    <span className="log-msg" style={{ color: 'var(--text-2)' }}>
                      {r.error_message || `${r.prompt_tokens} in · ${r.completion_tokens} out tokens`}
                    </span>
                    <span className="log-meta">{r.latency_ms ? `${r.latency_ms}ms` : '—'}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
