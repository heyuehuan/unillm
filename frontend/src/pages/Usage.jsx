import { useState, useEffect } from 'react'
import { api } from '../api.js'
import { useLatestRequest, isAbort } from '../requests.js'
import { BarChart } from '../components/Charts.jsx'
import { IcZap, IcDollar, IcSliders, IcRefresh } from '../components/Icons.jsx'
import FilterBar, { filtersToApiParams, describeFilters, ScopeToggle } from '../components/FilterBar.jsx'
import { fmtTokens, LoadError } from '../components/ui.jsx'

export default function Usage({ user, filters, setFilters, scope, setScope }) {
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [projects, setProjects] = useState([])
  const requests = useLatestRequest()

  useEffect(() => {
    api.getProjects().then(setProjects).catch(() => {})
  }, [])

  async function load() {
    const attempt = requests.next()
    setLoading(true)
    setLoadError('')
    try {
      const params = filtersToApiParams(filters)
      if (scope === 'mine') params.mine = 'true'
      const st = await api.getStats(params, { signal: attempt.signal })
      setStats(st)
    } catch (e) {
      if (isAbort(e)) return
      setLoadError(e.message)
    } finally {
      if (requests.isCurrent(attempt)) setLoading(false)
    }
  }

  useEffect(() => { load() }, [filters, scope])

  const totalReq = stats?.total_requests || 0
  const byStatus = stats?.by_status || {}
  // 2xx/3xx are successes — don't count a 201 or 304 as an error.
  const okCount = Object.entries(byStatus)
    .filter(([code]) => Number(code) < 400)
    .reduce((sum, [, count]) => sum + count, 0)
  const errRate = totalReq > 0 ? (((totalReq - okCount) / totalReq) * 100).toFixed(1) : '0.0'

  // Stable color per model: same model gets the same color in both charts,
  // using the theme's chart palette (dark-mode aware).
  const byModel = stats?.by_model || []
  const colorFor = (() => {
    const names = [...new Set(byModel.map(m => m.model))].sort()
    const map = new Map(names.map((n, i) => [n, `var(--chart-${(i % 8) + 1})`]))
    return name => map.get(name)
  })()

  const modelChart = [...byModel]
    .sort((a, b) => b.requests - a.requests)
    .slice(0, 8)
    .map(m => ({ label: m.model, value: m.requests, display: m.requests.toLocaleString(), color: colorFor(m.model) }))

  const costChart = byModel
    .filter(m => m.cost_usd > 0)
    .sort((a, b) => b.cost_usd - a.cost_usd)
    .slice(0, 8)
    .map(m => ({ label: m.model, value: m.cost_usd, display: `$${m.cost_usd.toFixed(4)}`, color: colorFor(m.model) }))

  return (
    <div className="content">
      <div className="page-h">
        <div>
          <h1 className="page-title">Usage</h1>
          <div className="page-sub">
            {describeFilters(filters)} · {scope === 'mine' ? 'your SSH-signed requests' : 'across projects and models'}
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

      {loadError && <LoadError message={loadError} onRetry={load} />}

      {loading ? (
        <div style={{ color: 'var(--text-3)', padding: '40px 0' }}>Loading…</div>
      ) : loadError ? null : (
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

          <div className="grid-2" style={{ marginBottom: 16 }}>
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
