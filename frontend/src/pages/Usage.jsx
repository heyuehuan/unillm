import { useState, useEffect } from 'react'
import { api } from '../api.js'
import { BarChart } from '../components/Charts.jsx'
import { IcZap, IcDollar, IcSliders, IcRefresh } from '../components/Icons.jsx'
import FilterBar, { DEFAULT_FILTERS, filtersToApiParams, ScopeToggle } from '../components/FilterBar.jsx'

function fmtTokens(n) {
  if (!n) return '0'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k'
  return String(n)
}

export default function Usage({ user }) {
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)
  const [projects, setProjects] = useState([])
  const [filters, setFilters] = useState(DEFAULT_FILTERS)
  const [scope, setScope] = useState('all')  // 'all' | 'mine'

  useEffect(() => {
    api.getProjects().then(setProjects).catch(() => {})
  }, [])

  async function load(f = filters, s = scope) {
    setLoading(true)
    try {
      const params = filtersToApiParams(f)
      if (s === 'mine' && user?.username) params.ssh_username = user.username
      const st = await api.getStats(params)
      setStats(st)
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load(filters, scope) }, [filters, scope])

  const totalReq = stats?.total_requests || 0
  const byStatus = stats?.by_status || {}
  const okCount = byStatus['200'] || 0
  const errRate = totalReq > 0 ? (((totalReq - okCount) / totalReq) * 100).toFixed(1) : '0.0'

  const COLORS = ['#f97316', '#2563eb', '#16a34a', '#9333ea', '#d97706', '#0891b2', '#db2777', '#65a30d']

  const modelChart = (stats?.by_model || [])
    .sort((a, b) => b.requests - a.requests)
    .slice(0, 8)
    .map((m, i) => ({ label: m.model, value: m.requests, display: m.requests.toLocaleString(), color: COLORS[i] }))

  const costChart = (stats?.by_model || [])
    .filter(m => m.cost_usd > 0)
    .sort((a, b) => b.cost_usd - a.cost_usd)
    .slice(0, 8)
    .map((m, i) => ({ label: m.model, value: m.cost_usd, display: `$${m.cost_usd.toFixed(4)}`, color: COLORS[i] }))

  return (
    <div className="content">
      <div className="page-h">
        <div>
          <h1 className="page-title">Usage</h1>
          <div className="page-sub">
            {scope === 'mine' ? 'Requests signed with your SSH key' : 'Cumulative breakdown across projects and models'}
          </div>
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

      {loading ? (
        <div style={{ color: 'var(--text-3)', padding: '40px 0' }}>Loading…</div>
      ) : (
        <>
          <div className="stat-grid">
            <div className="stat">
              <div className="stat-label"><IcZap size={14} />Total requests</div>
              <div className="stat-value">{totalReq.toLocaleString()}</div>
              <div className="stat-sub">{errRate}% error rate</div>
            </div>
            <div className="stat">
              <div className="stat-label"><IcSliders size={14} />Tokens in</div>
              <div className="stat-value">{fmtTokens(stats?.total_prompt_tokens || 0)}</div>
              <div className="stat-sub">prompt tokens</div>
            </div>
            <div className="stat">
              <div className="stat-label"><IcSliders size={14} />Tokens out</div>
              <div className="stat-value">{fmtTokens(stats?.total_completion_tokens || 0)}</div>
              <div className="stat-sub">completion tokens</div>
            </div>
            <div className="stat">
              <div className="stat-label"><IcDollar size={14} />Total cost</div>
              <div className="stat-value">${(stats?.total_cost_usd || 0).toFixed(4)}</div>
              <div className="stat-sub">avg {stats?.avg_latency_ms ? Math.round(stats.avg_latency_ms) + 'ms' : '—'} latency</div>
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 16 }}>
            <div className="card">
              <div className="card-h"><h3>Requests by model</h3></div>
              <div className="card-b">
                {modelChart.length === 0
                  ? <div style={{ color: 'var(--text-3)', fontSize: 13 }}>No data yet</div>
                  : <BarChart data={modelChart} height={Math.max(100, modelChart.length * 32)} />}
              </div>
            </div>
            <div className="card">
              <div className="card-h"><h3>Cost by model</h3></div>
              <div className="card-b">
                {costChart.length === 0
                  ? <div style={{ color: 'var(--text-3)', fontSize: 13 }}>No cost data — add model pricing in Admin.</div>
                  : <BarChart data={costChart} height={Math.max(100, costChart.length * 32)} />}
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-h"><h3>Status breakdown</h3></div>
            <table className="table">
              <thead>
                <tr>
                  <th>Status code</th>
                  <th>Count</th>
                  <th>% of total</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(byStatus).sort((a, b) => b[1] - a[1]).map(([code, count]) => (
                  <tr key={code} className="row-hover">
                    <td>
                      <span className={`badge ${code.startsWith('2') ? 'green' : code.startsWith('4') ? 'amber' : code.startsWith('5') ? 'red' : ''}`}>
                        {code}
                      </span>
                    </td>
                    <td className="num mono">{count.toLocaleString()}</td>
                    <td>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <div style={{ width: 80, height: 4, background: 'var(--bg-2)', borderRadius: 2, overflow: 'hidden' }}>
                          <div style={{ height: '100%', width: `${(count / totalReq) * 100}%`, background: 'var(--accent)' }} />
                        </div>
                        <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
                          {((count / totalReq) * 100).toFixed(1)}%
                        </span>
                      </div>
                    </td>
                  </tr>
                ))}
                {Object.keys(byStatus).length === 0 && (
                  <tr><td colSpan={3} style={{ color: 'var(--text-3)', textAlign: 'center', padding: 32 }}>No data yet</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
