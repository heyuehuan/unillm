import { useState, useEffect } from 'react'
import { api } from '../api.js'
import { IcPlus, IcTrash, IcX, IcEdit, IcRefresh } from '../components/Icons.jsx'

function fmtDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

function EditUserModal({ user, onClose, onSaved }) {
  const [form, setForm] = useState({
    name: user.name || '',
    global_role: user.global_role,
    new_password: '',
    active: user.active,
    password_login_disabled: user.password_login_disabled,
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  async function save(e) {
    e.preventDefault()
    setSaving(true); setError('')
    try {
      const payload = {
        name: form.name || null,
        global_role: form.global_role,
        active: form.active,
        password_login_disabled: form.password_login_disabled,
      }
      if (form.new_password) payload.new_password = form.new_password
      await api.updateUser(user.id, payload)
      onSaved()
    } catch (e) { setError(e.message) } finally { setSaving(false) }
  }

  return (
    <div className="card" style={{ marginBottom: 16, borderColor: 'var(--accent)' }}>
      <div className="card-h">
        <h3>Edit <span className="mono">{user.username}</span></h3>
        <button className="iconbtn" onClick={onClose}><IcX size={14} /></button>
      </div>
      <form className="card-b" onSubmit={save} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {error && <div className="alert error">{error}</div>}
        <div className="grid-2">
          <div>
            <label className="label">Name</label>
            <input className="input" placeholder="Display name"
              value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} />
          </div>
          <div>
            <label className="label">Role</label>
            <select className="select" value={form.global_role} onChange={e => setForm(f => ({ ...f, global_role: e.target.value }))}>
              <option value="user">User</option>
              <option value="viewer">Viewer</option>
              <option value="admin">Admin</option>
            </select>
          </div>
          <div>
            <label className="label">Reset password</label>
            <input className="input" type="password" placeholder="Leave blank to keep current"
              value={form.new_password} onChange={e => setForm(f => ({ ...f, new_password: e.target.value }))} />
          </div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, cursor: 'pointer' }}>
            <input type="checkbox" checked={!form.active}
              onChange={e => setForm(f => ({ ...f, active: !e.target.checked }))} />
            Disable account (user cannot log in)
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, cursor: 'pointer' }}>
            <input type="checkbox" checked={form.password_login_disabled}
              onChange={e => setForm(f => ({ ...f, password_login_disabled: e.target.checked }))} />
            Disable password login (SSH key auth only)
          </label>
        </div>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn primary" disabled={saving}>{saving ? 'Saving…' : 'Save changes'}</button>
        </div>
      </form>
    </div>
  )
}

// ── Users tab ──────────────────────────────────────────────
function UsersTab({ currentUser }) {
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [editingUser, setEditingUser] = useState(null)
  const [form, setForm] = useState({ username: '', name: '', password: '', email: '', global_role: 'user' })
  const [creating, setCreating] = useState(false)
  const [justCreated, setJustCreated] = useState(null)
  const [error, setError] = useState('')

  async function load() {
    setLoading(true)
    try { setUsers(await api.getUsers()) } catch (e) { setError(e.message) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  async function create(e) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const res = await api.createUser(form)
      setJustCreated({ user: res.user, key: res.api_key })
      setShowCreate(false)
      setForm({ username: '', name: '', password: '', email: '', global_role: 'user' })
      await load()
    } catch (e) { setError(e.message) } finally { setCreating(false) }
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div style={{ fontSize: 13, color: 'var(--text-2)' }}>{users.length} users</div>
        <button className="btn primary sm" onClick={() => { setShowCreate(true); setEditingUser(null) }}><IcPlus size={13} /> New user</button>
      </div>

      {error && <div className="alert error">{error}</div>}

      {justCreated && (
        <div className="card" style={{ marginBottom: 16, borderColor: 'var(--accent)' }}>
          <div className="card-b">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600 }}>User <span className="mono">{justCreated.user.username}</span> created</div>
                {justCreated.key ? (
                  <>
                    <div style={{ fontSize: 12.5, color: 'var(--text-2)', marginTop: 4 }}>Initial API key (shown once):</div>
                    <div className="key-display" style={{ marginTop: 8 }}>
                      <span className="key-val">{justCreated.key}</span>
                      <button className="btn sm" onClick={() => navigator.clipboard.writeText(justCreated.key)}>Copy</button>
                    </div>
                  </>
                ) : (
                  <div style={{ fontSize: 12.5, color: 'var(--text-2)', marginTop: 4 }}>Viewer account — no personal project or API key.</div>
                )}
              </div>
              <button className="iconbtn" onClick={() => setJustCreated(null)}><IcX size={14} /></button>
            </div>
          </div>
        </div>
      )}

      {editingUser && (
        <EditUserModal
          user={editingUser}
          onClose={() => setEditingUser(null)}
          onSaved={() => { setEditingUser(null); load() }}
        />
      )}

      {showCreate && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>Create user</h3>
            <button className="iconbtn" onClick={() => setShowCreate(false)}><IcX size={14} /></button>
          </div>
          <form className="card-b" onSubmit={create}>
            <div className="grid-2" style={{ marginBottom: 14 }}>
              <div>
                <label className="label">Username</label>
                <input className="input" required value={form.username} onChange={e => setForm(f => ({ ...f, username: e.target.value }))} />
              </div>
              <div>
                <label className="label">Name</label>
                <input className="input" placeholder="Display name" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} />
              </div>
              <div>
                <label className="label">Password</label>
                <input className="input" type="password" required value={form.password} onChange={e => setForm(f => ({ ...f, password: e.target.value }))} />
              </div>
              <div>
                <label className="label">Email</label>
                <input className="input" type="email" value={form.email} onChange={e => setForm(f => ({ ...f, email: e.target.value }))} />
              </div>
              <div>
                <label className="label">Role</label>
                <select className="select" value={form.global_role} onChange={e => setForm(f => ({ ...f, global_role: e.target.value }))}>
                  <option value="user">User</option>
                  <option value="viewer">Viewer</option>
                  <option value="admin">Admin</option>
                </select>
              </div>
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button type="button" className="btn" onClick={() => setShowCreate(false)}>Cancel</button>
              <button type="submit" className="btn primary" disabled={creating}>{creating ? 'Creating…' : 'Create user'}</button>
            </div>
          </form>
        </div>
      )}

      {loading ? (
        <div style={{ color: 'var(--text-3)' }}>Loading…</div>
      ) : (
        <div className="card">
          <table className="table">
            <thead>
              <tr><th>User</th><th>Name</th><th>Role</th><th>Email</th><th>Created</th><th></th></tr>
            </thead>
            <tbody>
              {users.map(u => (
                <tr key={u.id} className="row-hover" style={{ opacity: u.active ? 1 : 0.5 }}>
                  <td>
                    <div style={{ fontWeight: 500 }} className="mono">{u.username}</div>
                    <div style={{ display: 'flex', gap: 4, marginTop: 2, flexWrap: 'wrap' }}>
                      {!u.active && <span className="badge red" style={{ fontSize: 10 }}>disabled</span>}
                      {u.password_login_disabled && <span className="badge" style={{ fontSize: 10 }}>pw disabled</span>}
                    </div>
                  </td>
                  <td style={{ color: 'var(--text-2)' }}>{u.name || '—'}</td>
                  <td>
                    <span className={`badge ${u.global_role === 'admin' ? 'accent' : ''}`}>{u.global_role}</span>
                  </td>
                  <td style={{ color: 'var(--text-2)' }}>{u.email || '—'}</td>
                  <td style={{ color: 'var(--text-3)', fontSize: 12 }}>{fmtDate(u.created_at)}</td>
                  <td>
                    {u.id !== currentUser.id && (
                      <button className="iconbtn" title="Edit" onClick={() => { setEditingUser(u); setShowCreate(false) }}>
                        <IcEdit size={14} />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── Pricing tab ────────────────────────────────────────────
function PricingTab() {
  const [pricing, setPricing] = useState([])
  const [loading, setLoading] = useState(true)
  const [showAdd, setShowAdd] = useState(false)
  const [form, setForm] = useState({ model_name: '', input_per_1m: '', output_per_1m: '', notes: '' })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  async function load() {
    setLoading(true)
    try { setPricing(await api.getPricing()) } catch (e) { setError(e.message) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  async function save(e) {
    e.preventDefault()
    setSaving(true)
    setError('')
    try {
      await api.upsertPricing(form.model_name, {
        input_per_1m: parseFloat(form.input_per_1m),
        output_per_1m: parseFloat(form.output_per_1m),
        notes: form.notes || null,
      })
      setShowAdd(false)
      setForm({ model_name: '', input_per_1m: '', output_per_1m: '', notes: '' })
      await load()
    } catch (e) { setError(e.message) } finally { setSaving(false) }
  }

  async function del(modelName) {
    if (!confirm(`Delete pricing for ${modelName}?`)) return
    try { await api.deletePricing(modelName); await load() } catch (e) { setError(e.message) }
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div style={{ fontSize: 13, color: 'var(--text-2)' }}>
          Used to compute cost per request. Prices in USD per 1M tokens.
        </div>
        <button className="btn primary sm" onClick={() => setShowAdd(true)}><IcPlus size={13} /> Add pricing</button>
      </div>

      {error && <div className="alert error">{error}</div>}

      {showAdd && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>Add / update pricing</h3>
            <button className="iconbtn" onClick={() => setShowAdd(false)}><IcX size={14} /></button>
          </div>
          <form className="card-b" onSubmit={save}>
            <div className="grid-2" style={{ marginBottom: 14 }}>
              <div>
                <label className="label">Model name</label>
                <input className="input" required value={form.model_name} onChange={e => setForm(f => ({ ...f, model_name: e.target.value }))} placeholder="e.g. gpt-4o" />
                <div className="hint">Must match the model name used in requests.</div>
              </div>
              <div>
                <label className="label">Notes</label>
                <input className="input" value={form.notes} onChange={e => setForm(f => ({ ...f, notes: e.target.value }))} placeholder="Optional" />
              </div>
              <div>
                <label className="label">Input price ($/1M tokens)</label>
                <input className="input" type="number" step="0.01" required value={form.input_per_1m} onChange={e => setForm(f => ({ ...f, input_per_1m: e.target.value }))} placeholder="e.g. 2.50" />
              </div>
              <div>
                <label className="label">Output price ($/1M tokens)</label>
                <input className="input" type="number" step="0.01" required value={form.output_per_1m} onChange={e => setForm(f => ({ ...f, output_per_1m: e.target.value }))} placeholder="e.g. 10.00" />
              </div>
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button type="button" className="btn" onClick={() => setShowAdd(false)}>Cancel</button>
              <button type="submit" className="btn primary" disabled={saving}>{saving ? 'Saving…' : 'Save pricing'}</button>
            </div>
          </form>
        </div>
      )}

      {loading ? (
        <div style={{ color: 'var(--text-3)' }}>Loading…</div>
      ) : pricing.length === 0 ? (
        <div className="card">
          <div className="empty">
            <div className="empty-title">No pricing configured</div>
            <div style={{ fontSize: 12 }}>Add pricing to enable cost tracking per request.</div>
          </div>
        </div>
      ) : (
        <div className="card">
          <table className="table">
            <thead>
              <tr><th>Model</th><th>Input $/1M</th><th>Output $/1M</th><th>Notes</th><th>Updated</th><th></th></tr>
            </thead>
            <tbody>
              {pricing.map(p => (
                <tr key={p.id} className="row-hover">
                  <td><span className="mono" style={{ fontSize: 12 }}>{p.model_name}</span></td>
                  <td className="num mono">${p.input_per_1m.toFixed(2)}</td>
                  <td className="num mono">${p.output_per_1m.toFixed(2)}</td>
                  <td style={{ color: 'var(--text-3)', fontSize: 12 }}>{p.notes || '—'}</td>
                  <td style={{ color: 'var(--text-3)', fontSize: 12 }}>{fmtDate(p.updated_at)}</td>
                  <td>
                    <button className="btn sm danger" onClick={() => del(p.model_name)}><IcTrash size={12} /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── Audit tab ──────────────────────────────────────────────
function AuditTab() {
  const [logs, setLogs] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [offset, setOffset] = useState(0)
  const LIMIT = 50

  async function load() {
    setLoading(true)
    try {
      const res = await api.getAudit({ limit: LIMIT, offset })
      setLogs(res.items || [])
      setTotal(res.total || 0)
    } catch (e) { console.error(e) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [offset])

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div style={{ fontSize: 13, color: 'var(--text-2)' }}>{total} events</div>
        <button className="btn sm" onClick={load}><IcRefresh size={13} /> Refresh</button>
      </div>

      {loading ? (
        <div style={{ color: 'var(--text-3)' }}>Loading…</div>
      ) : logs.length === 0 ? (
        <div className="card">
          <div className="empty">
            <div className="empty-title">No audit events</div>
          </div>
        </div>
      ) : (
        <div className="card">
          <table className="table">
            <thead>
              <tr><th>Time</th><th>Actor</th><th>Action</th><th>Resource</th><th>Severity</th></tr>
            </thead>
            <tbody>
              {logs.map(a => (
                <tr key={a.id} className="row-hover">
                  <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{fmtDate(a.created_at)}</td>
                  <td style={{ fontSize: 13 }}>{a.username || <span style={{ color: 'var(--text-3)' }}>system</span>}</td>
                  <td style={{ fontWeight: 500 }}>{a.action.replace(/_/g, ' ')}</td>
                  <td style={{ fontSize: 12, color: 'var(--text-2)' }}>
                    {a.resource_type && <span className="mono">{a.resource_type}/{a.resource_id || '—'}</span>}
                  </td>
                  <td>
                    <span className={`badge ${a.severity === 'warning' ? 'amber' : a.severity === 'error' ? 'red' : ''}`}>
                      {a.severity}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {total > LIMIT && (
            <div style={{ display: 'flex', justifyContent: 'center', gap: 8, padding: 16 }}>
              <button className="btn sm" disabled={offset === 0} onClick={() => setOffset(o => Math.max(0, o - LIMIT))}>← Prev</button>
              <span style={{ fontSize: 12, color: 'var(--text-3)', alignSelf: 'center' }}>
                {offset + 1}–{Math.min(offset + LIMIT, total)} of {total}
              </span>
              <button className="btn sm" disabled={offset + LIMIT >= total} onClick={() => setOffset(o => o + LIMIT)}>Next →</button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Main Admin page ────────────────────────────────────────
export default function Admin({ currentUser }) {
  const [tab, setTab] = useState('users')
  return (
    <div className="content">
      <div className="page-h">
        <div>
          <h1 className="page-title">Admin</h1>
          <div className="page-sub">User management, model pricing, and audit trail</div>
        </div>
      </div>

      <div className="tabs">
        {[['users', 'Users'], ['pricing', 'Model Pricing'], ['audit', 'Audit Log']].map(([id, label]) => (
          <button key={id} className={`tab${tab === id ? ' active' : ''}`} onClick={() => setTab(id)}>
            {label}
          </button>
        ))}
      </div>

      {tab === 'users' && <UsersTab currentUser={currentUser} />}
      {tab === 'pricing' && <PricingTab />}
      {tab === 'audit' && <AuditTab />}
    </div>
  )
}
