import { useState, useEffect } from 'react'
import { api } from '../api.js'
import { Sparkline, BarChart } from '../components/Charts.jsx'
import { IcZap, IcDollar, IcSliders, IcRefresh, IcChevRight } from '../components/Icons.jsx'
import FilterBar, { DEFAULT_FILTERS, filtersToApiParams, ScopeToggle } from '../components/FilterBar.jsx'

function StatCard({ label, value, sub, icon, sparkData }) {
  return (
    <div className="stat">
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between' }}>
        <div>
          <div className="stat-label">{icon}{label}</div>
          <div className="stat-value">{value}</div>
          {sub && <div className="stat-sub">{sub}</div>}
        </div>
        {sparkData && sparkData.length > 1 && (
          <div style={{ width: 80, opacity: 0.8, marginTop: 4 }}>
            <Sparkline data={sparkData} height={34} />
          </div>
        )}
      </div>
    </div>
  )
}

function HttpBadge({ code }) {
  if (!code) return <span className="badge">—</span>
  if (code >= 500) return <span className="badge red">{code}</span>
  if (code >= 400) return <span className="badge amber">{code}</span>
  return <span className="badge green">{code}</span>
}

function fmtTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function fmtTokens(n) {
  if (!n) return '0'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k'
  return String(n)
}

export default function Dashboard({ user, setRoute }) {
  const [stats, setStats] = useState(null)
  const [recent, setRecent] = useState([])
  const [loading, setLoading] = useState(true)
  const [projects, setProjects] = useState([])
  const [filters, setFilters] = useState(DEFAULT_FILTERS)
  const [scope, setScope] = useState('all')

  useEffect(() => {
    api.getProjects().then(setProjects).catch(() => {})
  }, [])

  async function load(f = filters, sc = scope) {
    setLoading(true)
    const p = filtersToApiParams(f)
    if (sc === 'mine' && user?.username) p.ssh_username = user.username
    try {
      const [s, r] = await Promise.all([
        api.getStats(p),
        api.getRequests({ limit: 8, ...p }),
      ])
      setStats(s)
      setRecent(r.items || [])
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load(filters, scope) }, [filters, scope])

  function handleFiltersChange(f) {
    setFilters(f)
  }

  const modelChart = stats?.by_model?.slice(0, 6).map(m => ({
    label: m.model,
    value: m.requests,
    display: m.requests.toLocaleString(),
  })) || []

  const isAdmin = user?.global_role === 'admin'

  return (
    <div className="content">
      <div className="page-h">
        <div>
          <h1 className="page-title">Dashboard</h1>
          <div className="page-sub">
            {isAdmin ? 'All-time stats · org-wide' : 'Stats across your projects'}
          </div>
        </div>
        <div className="h-actions">
          <button className="btn" onClick={() => load()}><IcRefresh size={14} /> Refresh</button>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20, flexWrap: 'wrap' }}>
        <ScopeToggle scope={scope} onChange={setScope} />
        <div style={{ width: 1, height: 20, background: 'var(--border)' }} />
        <FilterBar projects={projects} filters={filters} onChange={handleFiltersChange} />
      </div>

      {loading ? (
        <div style={{ color: 'var(--text-3)', padding: '40px 0' }}>Loading…</div>
      ) : (
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
              <div><h3>Recent requests</h3><div className="sub">Latest activity</div></div>
              <button className="btn sm ghost" onClick={() => setRoute('logs')}>
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
                  <div key={r.id} className="log-row" style={{ cursor: 'default' }}>
                    <span className="log-time">{fmtTime(r.created_at)}</span>
                    <span className="log-status"><HttpBadge code={r.status_code} /></span>
                    <span className="log-model mono" style={{ fontSize: 12 }}>{r.model}</span>
                    {r.project_name && (
                      <span style={{ fontSize: 11, color: 'var(--text-3)', background: 'var(--bg-2)', borderRadius: 4, padding: '1px 6px', flexShrink: 0 }}>{r.project_name}</span>
                    )}
                    {r.api_key_name && (
                      <span style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'monospace', flexShrink: 0 }}>{r.api_key_name}</span>
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
