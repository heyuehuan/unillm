import { Fragment, useState, useEffect } from 'react'
import { api } from '../api.js'
import { navigate } from '../router.js'
import { IcPlus, IcTrash, IcX, IcEdit, IcRefresh } from '../components/Icons.jsx'
import { fmtDateTime, useConfirm, CopyButton } from '../components/ui.jsx'
import { useLatestRequest, isAbort } from '../requests.js'

const GLOBAL_ROLES = [
  { id: 'user', label: 'User', hint: 'Normal account — gets a personal project and API key' },
  { id: 'viewer', label: 'Viewer', hint: 'Read-only — no personal project or API key' },
  { id: 'admin', label: 'Admin', hint: 'Full access to all projects, users, and settings' },
]

function GlobalRoleSelect({ value, onChange }) {
  return (
    <>
      <select className="select" value={value} onChange={e => onChange(e.target.value)}>
        {GLOBAL_ROLES.map(r => <option key={r.id} value={r.id}>{r.label}</option>)}
      </select>
      <div className="hint">{GLOBAL_ROLES.find(r => r.id === value)?.hint}</div>
    </>
  )
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
      onSaved(await api.updateUser(user.id, payload))
    } catch (e) { setError(e.message) } finally { setSaving(false) }
  }

  return (
    <div className="card" style={{ marginBottom: 16, borderColor: 'var(--accent)' }}>
      <div className="card-h">
        <h3>Edit <span className="mono">{user.username}</span></h3>
        <button className="iconbtn" aria-label="Close" onClick={onClose}><IcX size={14} /></button>
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
            <GlobalRoleSelect value={form.global_role} onChange={v => setForm(f => ({ ...f, global_role: v }))} />
          </div>
          <div>
            <label className="label">Reset password</label>
            <input className="input" type="password" placeholder="Leave blank to keep current"
              minLength={8} maxLength={72} autoComplete="new-password"
              value={form.new_password} onChange={e => setForm(f => ({ ...f, new_password: e.target.value }))} />
            <div className="hint">At least 8 characters. Resetting signs the user out everywhere.</div>
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
                <div style={{ fontWeight: 600 }}>User <span className="mono">{justCreated.user.username}</span> {justCreated.action || 'created'}</div>
                {justCreated.key ? (
                  <>
                    <div style={{ fontSize: 12.5, color: 'var(--text-2)', marginTop: 4 }}>Initial API key (shown once):</div>
                    <div className="key-display" style={{ marginTop: 8 }}>
                      <span className="key-val">{justCreated.key}</span>
                      <CopyButton text={justCreated.key} />
                    </div>
                  </>
                ) : (
                  <div style={{ fontSize: 12.5, color: 'var(--text-2)', marginTop: 4 }}>Viewer account — no personal project or API key.</div>
                )}
              </div>
              <button className="iconbtn" aria-label="Dismiss" onClick={() => setJustCreated(null)}><IcX size={14} /></button>
            </div>
          </div>
        </div>
      )}

      {editingUser && (
        <EditUserModal
          user={editingUser}
          onClose={() => setEditingUser(null)}
          onSaved={(updated) => {
            setEditingUser(null)
            // A promotion out of viewer creates the personal project and its first
            // key server-side; that key is only ever returned here.
            if (updated?.api_key) {
              setJustCreated({ user: updated, key: updated.api_key, action: 'promoted — new personal project' })
            }
            load()
          }}
        />
      )}

      {showCreate && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>Create user</h3>
            <button className="iconbtn" aria-label="Close" onClick={() => setShowCreate(false)}><IcX size={14} /></button>
          </div>
          <form className="card-b" onSubmit={create}>
            <div className="grid-2" style={{ marginBottom: 14 }}>
              <div>
                <label className="label">Username</label>
                <input className="input" required value={form.username}
                  pattern="[a-zA-Z0-9_.\-]{2,32}" title="2–32 characters: letters, numbers, dot, dash, underscore"
                  onChange={e => setForm(f => ({ ...f, username: e.target.value }))} />
                <div className="hint">2–32 characters: letters, numbers, <span className="mono">. - _</span></div>
              </div>
              <div>
                <label className="label">Name</label>
                <input className="input" placeholder="Display name" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} />
              </div>
              <div>
                <label className="label">Password</label>
                <input className="input" type="password" required minLength={8} maxLength={72}
                  autoComplete="new-password"
                  value={form.password} onChange={e => setForm(f => ({ ...f, password: e.target.value }))} />
                <div className="hint">At least 8 characters.</div>
              </div>
              <div>
                <label className="label">Email</label>
                <input className="input" type="email" value={form.email} onChange={e => setForm(f => ({ ...f, email: e.target.value }))} />
              </div>
              <div>
                <label className="label">Role</label>
                <GlobalRoleSelect value={form.global_role} onChange={v => setForm(f => ({ ...f, global_role: v }))} />
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
                    <span className={`badge ${u.global_role === 'admin' ? 'accent' : ''}`}
                      title={GLOBAL_ROLES.find(r => r.id === u.global_role)?.hint}>{u.global_role}</span>
                  </td>
                  <td style={{ color: 'var(--text-2)' }}>{u.email || '—'}</td>
                  <td style={{ color: 'var(--text-3)', fontSize: 12 }}>{fmtDateTime(u.created_at)}</td>
                  <td>
                    {u.id !== currentUser.id ? (
                      <button className="iconbtn" title="Edit" aria-label={`Edit ${u.username}`} onClick={() => { setEditingUser(u); setShowCreate(false) }}>
                        <IcEdit size={14} />
                      </button>
                    ) : (
                      <span style={{ fontSize: 11, color: 'var(--text-3)' }} title="Edit your own profile in Settings">you</span>
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
  const confirm = useConfirm()
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
    const ok = await confirm(
      `Delete pricing for ${modelName}? New requests for this model will no longer get a cost estimate.`,
      { title: 'Delete pricing', confirmLabel: 'Delete', danger: true },
    )
    if (!ok) return
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
            <button className="iconbtn" aria-label="Close" onClick={() => setShowAdd(false)}><IcX size={14} /></button>
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
                <input className="input" type="number" step="0.01" min="0" required value={form.input_per_1m} onChange={e => setForm(f => ({ ...f, input_per_1m: e.target.value }))} placeholder="e.g. 2.50" />
              </div>
              <div>
                <label className="label">Output price ($/1M tokens)</label>
                <input className="input" type="number" step="0.01" min="0" required value={form.output_per_1m} onChange={e => setForm(f => ({ ...f, output_per_1m: e.target.value }))} placeholder="e.g. 10.00" />
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
                  <td style={{ color: 'var(--text-3)', fontSize: 12 }}>{fmtDateTime(p.updated_at)}</td>
                  <td>
                    <button className="btn sm danger" aria-label={`Delete pricing for ${p.model_name}`} onClick={() => del(p.model_name)}><IcTrash size={12} /></button>
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

// ── Settings tab ───────────────────────────────────────────

// The API returns bytes. Operators think in MB, so the field takes MB and this
// converts, rather than asking anyone to type 1048576.
function bytesToMb(bytes) {
  return (bytes / (1024 * 1024)).toFixed(bytes % (1024 * 1024) === 0 ? 0 : 2)
}

const SOURCE_LABEL = {
  database: 'Set here',
  config: 'From config file',
  default: 'Built-in default',
}

function SettingsTab() {
  const confirm = useConfirm()
  const [settings, setSettings] = useState([])
  const [loading, setLoading] = useState(true)
  const [drafts, setDrafts] = useState({})
  const [savingKey, setSavingKey] = useState(null)
  const [error, setError] = useState('')

  async function load() {
    setLoading(true)
    try {
      const rows = await api.getSettings()
      setSettings(rows)
      setDrafts(Object.fromEntries(rows.map(r => [r.key, String(bytesToMb(r.value))])))
    } catch (e) { setError(e.message) } finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  async function save(setting) {
    const mb = parseFloat(drafts[setting.key])
    if (!Number.isFinite(mb) || mb <= 0) { setError('Enter a size in MB greater than zero.'); return }
    setSavingKey(setting.key)
    setError('')
    try {
      await api.updateSetting(setting.key, Math.round(mb * 1024 * 1024))
      await load()
    } catch (e) { setError(e.message) } finally { setSavingKey(null) }
  }

  async function reset(setting) {
    const fallback = setting.config_value != null ? 'the value in the config file' : 'the built-in default'
    const ok = await confirm(
      `Reset ${setting.key} to ${fallback}?`,
      { title: 'Reset setting', confirmLabel: 'Reset' },
    )
    if (!ok) return
    setSavingKey(setting.key)
    try { await api.resetSetting(setting.key); await load() } catch (e) { setError(e.message) } finally { setSavingKey(null) }
  }

  return (
    <div>
      <div style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 16 }}>
        Changes take effect within a few seconds, without a restart. Resetting a setting
        falls back to the config file, or to the built-in default if the file is silent.
      </div>

      {error && <div className="alert error">{error}</div>}

      {loading ? (
        <div style={{ color: 'var(--text-3)' }}>Loading…</div>
      ) : (
        <div className="card">
          <table className="table">
            <thead>
              <tr><th>Setting</th><th>Value (MB)</th><th>Source</th><th></th></tr>
            </thead>
            <tbody>
              {settings.map(s => (
                <tr key={s.key}>
                  <td style={{ maxWidth: 420 }}>
                    <div className="mono" style={{ fontSize: 12 }}>{s.key}</div>
                    <div style={{ color: 'var(--text-3)', fontSize: 12, marginTop: 4 }}>{s.description}</div>
                  </td>
                  <td style={{ width: 140 }}>
                    <input
                      className="input"
                      type="number"
                      step="0.5"
                      min="0.01"
                      aria-label={`${s.key} in MB`}
                      value={drafts[s.key] ?? ''}
                      onChange={e => setDrafts(d => ({ ...d, [s.key]: e.target.value }))}
                    />
                    <div className="hint">Default {bytesToMb(s.default)} MB</div>
                  </td>
                  <td style={{ color: 'var(--text-3)', fontSize: 12 }}>{SOURCE_LABEL[s.source] || s.source}</td>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <button className="btn primary sm" disabled={savingKey === s.key} onClick={() => save(s)}>
                      {savingKey === s.key ? 'Saving…' : 'Save'}
                    </button>
                    {s.source === 'database' && (
                      <button className="btn sm" style={{ marginLeft: 8 }} disabled={savingKey === s.key} onClick={() => reset(s)}>
                        <IcRefresh size={12} /> Reset
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

// ── Audit tab ──────────────────────────────────────────────
const SEVERITIES = ['info', 'warning', 'error', 'critical']

function AuditTab() {
  const [logs, setLogs] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [offset, setOffset] = useState(0)
  const [severity, setSeverity] = useState('')
  const [action, setAction] = useState('')
  const [userId, setUserId] = useState('')
  const [users, setUsers] = useState([])
  const [expanded, setExpanded] = useState(null)
  const LIMIT = 50
  const requests = useLatestRequest()

  useEffect(() => {
    api.getUsers().then(setUsers).catch(() => {})
  }, [])

  // Debounce the free-text action filter.
  const [debouncedAction, setDebouncedAction] = useState('')
  useEffect(() => {
    const t = setTimeout(() => { setDebouncedAction(action); setOffset(0) }, 350)
    return () => clearTimeout(t)
  }, [action])

  async function load() {
    const attempt = requests.next()
    setLoading(true)
    setLoadError('')
    try {
      const res = await api.getAudit({
        limit: LIMIT, offset,
        severity: severity || undefined,
        action: debouncedAction || undefined,
        user_id: userId || undefined,
      }, { signal: attempt.signal })
      setLogs(res.items || [])
      setTotal(res.total || 0)
      // A filter change (or a shrinking retention window) can leave the current page
      // past the end of the results, which reads as "no logs found" with only Prev
      // to get back. Land on the last page that exists instead.
      const lastPage = Math.max(0, Math.ceil((res.total || 0) / LIMIT) - 1) * LIMIT
      if (offset > lastPage) setOffset(lastPage)
    } catch (e) {
      if (!isAbort(e)) setLoadError(e.message)
    } finally {
      if (requests.isCurrent(attempt)) setLoading(false)
    }
  }
  useEffect(() => { load() }, [offset, severity, debouncedAction, userId])

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
        <input className="input" style={{ width: 180 }} placeholder="Filter by action…" aria-label="Filter by action"
          value={action} onChange={e => setAction(e.target.value)} />
        <select className="select" style={{ width: 'auto' }} aria-label="Filter by severity"
          value={severity} onChange={e => { setSeverity(e.target.value); setOffset(0) }}>
          <option value="">All severities</option>
          {SEVERITIES.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <select className="select" style={{ width: 'auto' }} aria-label="Filter by actor"
          value={userId} onChange={e => { setUserId(e.target.value); setOffset(0) }}>
          <option value="">All actors</option>
          {users.map(u => <option key={u.id} value={u.id}>{u.username}</option>)}
        </select>
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{total} events</span>
        <button className="btn sm" onClick={load}><IcRefresh size={13} /> Refresh</button>
      </div>

      {loadError && <div className="alert error">{loadError}</div>}

      {loading ? (
        <div style={{ color: 'var(--text-3)' }}>Loading…</div>
      ) : logs.length === 0 ? (
        <div className="card">
          <div className="empty">
            <div className="empty-title">No audit events</div>
            <div style={{ fontSize: 12 }}>No events match the current filters.</div>
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
                <Fragment key={a.id}>
                  <tr className="row-hover" onClick={() => setExpanded(expanded === a.id ? null : a.id)}>
                    <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{fmtDateTime(a.created_at)}</td>
                    <td style={{ fontSize: 13 }}>{a.username || <span style={{ color: 'var(--text-3)' }}>system</span>}</td>
                    <td style={{ fontWeight: 500 }}>{a.action.replace(/_/g, ' ')}</td>
                    <td style={{ fontSize: 12, color: 'var(--text-2)' }}>
                      {a.resource_type && <span className="mono">{a.resource_type}/{a.resource_id || '—'}</span>}
                    </td>
                    <td>
                      <span className={`badge ${a.severity === 'warning' ? 'amber' : (a.severity === 'error' || a.severity === 'critical') ? 'red' : ''}`}>
                        {a.severity}
                      </span>
                    </td>
                  </tr>
                  {expanded === a.id && (
                    <tr>
                      <td colSpan={5} style={{ background: 'var(--bg-1)', fontSize: 12 }}>
                        <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', color: 'var(--text-2)' }}>
                          <span>IP: <span className="mono">{a.ip_address || '—'}</span></span>
                          <span style={{ maxWidth: 420, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            Agent: <span className="mono">{a.user_agent || '—'}</span>
                          </span>
                        </div>
                        {a.detail && (
                          <pre style={{ margin: '8px 0 0', padding: 10, background: 'var(--bg-2)', borderRadius: 6, fontSize: 11.5, fontFamily: 'JetBrains Mono, monospace', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                            {JSON.stringify(a.detail, null, 2)}
                          </pre>
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
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
export default function Admin({ currentUser, tab = 'users' }) {
  const activeTab = ['users', 'pricing', 'settings', 'audit'].includes(tab) ? tab : 'users'
  return (
    <div className="content">
      <div className="page-h">
        <div>
          <h1 className="page-title">Admin</h1>
          <div className="page-sub">User management, model pricing, server settings, and audit trail</div>
        </div>
      </div>

      <div className="tabs">
        {[['users', 'Users'], ['pricing', 'Model Pricing'], ['settings', 'Server Settings'], ['audit', 'Audit Log']].map(([id, label]) => (
          <button key={id} className={`tab${activeTab === id ? ' active' : ''}`} onClick={() => navigate(`admin/${id}`)}>
            {label}
          </button>
        ))}
      </div>

      {activeTab === 'users' && <UsersTab currentUser={currentUser} />}
      {activeTab === 'pricing' && <PricingTab />}
      {activeTab === 'settings' && <SettingsTab />}
      {activeTab === 'audit' && <AuditTab />}
    </div>
  )
}
