import { useState, useEffect } from 'react'
import { api } from '../api.js'
import { navigate } from '../router.js'
import { IcPlus, IcKey, IcTrash, IcX, IcChevRight, IcUsers, IcEdit, IcCheck, IcEye } from '../components/Icons.jsx'
import { fmtDate, useConfirm, CopyButton, LoadError } from '../components/ui.jsx'

// Checkbox multi-select over known models, with a free-text line for
// models the proxy hasn't seen yet. Empty selection = all models.
function ModelSelect({ models, selected, onChange, custom, onCustomChange }) {
  function toggle(name) {
    onChange(selected.includes(name) ? selected.filter(m => m !== name) : [...selected, name])
  }
  return (
    <div>
      {models.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
          {models.map(m => (
            <label key={m.name} className={`model-chip${selected.includes(m.name) ? ' on' : ''}`}>
              <input type="checkbox" checked={selected.includes(m.name)} onChange={() => toggle(m.name)}
                style={{ display: 'none' }} />
              {m.name}
            </label>
          ))}
        </div>
      )}
      <input className="input" value={custom} onChange={e => onCustomChange(e.target.value)}
        placeholder={models.length ? 'Other models, comma-separated (optional)' : 'Comma-separated model names'} />
      <div className="hint">Leave everything empty to allow all models.</div>
    </div>
  )
}

function combineModels(selected, custom) {
  const extra = custom.trim() ? custom.split(',').map(s => s.trim()).filter(Boolean) : []
  const all = [...new Set([...selected, ...extra])]
  return all.length ? all : null
}

function KeyRow({ k, models, onRevoke, onSaved, canManage, canReveal }) {
  const [revealed, setRevealed] = useState(null)
  const [revealing, setRevealing] = useState(false)
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState(k.name)
  const [editModels, setEditModels] = useState([])
  const [editCustom, setEditCustom] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const restricted = k.allowed_models?.length && !k.allowed_models.includes('all')

  function openEdit() {
    const current = restricted ? k.allowed_models : []
    const known = new Set(models.map(m => m.name))
    setEditName(k.name)
    setEditModels(current.filter(m => known.has(m)))
    setEditCustom(current.filter(m => !known.has(m)).join(', '))
    setError('')
    setEditing(true)
  }

  async function saveEdit(e) {
    e.preventDefault()
    setSaving(true); setError('')
    try {
      await api.updateKey(k.id, { name: editName, allowed_models: combineModels(editModels, editCustom) })
      setEditing(false)
      await onSaved()
    } catch (err) { setError(err.message) }
    finally { setSaving(false) }
  }

  async function reveal() {
    setRevealing(true)
    setError('')
    try {
      const res = await api.revealKey(k.id)
      setRevealed(res.api_key)
    } catch (e) {
      setError(e.message)
    } finally {
      setRevealing(false)
    }
  }

  return (
    <>
      <tr className="row-hover">
        <td>
          <div style={{ fontWeight: 500 }}>{k.name}</div>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
            Created {fmtDate(k.created_at)}
            {restricted ? ` · ${k.allowed_models.join(', ')}` : ' · all models'}
          </div>
        </td>
        <td>
          <span className="mono" style={{ fontSize: 12, color: 'var(--text-2)' }}>{k.key_prefix}••••</span>
        </td>
        <td>
          <span className={`badge ${k.active ? 'green' : 'red'}`}>
            <span className="dot" />
            {k.active ? 'Active' : 'Revoked'}
          </span>
        </td>
        <td style={{ fontSize: 12, color: 'var(--text-3)' }}>
          {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : 'Never'}
        </td>
        <td>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            {k.active && canReveal && k.recoverable && !revealed && (
              <button className="btn sm" onClick={reveal} disabled={revealing}>
                <IcEye size={12} /> {revealing ? '…' : 'Reveal'}
              </button>
            )}
            {k.active && canReveal && !k.recoverable && (
              <span style={{ fontSize: 11.5, color: 'var(--text-3)' }}
                title="This key was shown once at creation and no recoverable copy was stored.">
                Shown once
              </span>
            )}
            {k.active && canManage && (
              <button className="iconbtn" title="Edit key" aria-label="Edit key" onClick={openEdit}>
                <IcEdit size={13} />
              </button>
            )}
            {k.active && canManage && (
              <button className="btn sm danger" onClick={() => onRevoke(k)}>
                <IcTrash size={12} /> Revoke
              </button>
            )}
          </div>
        </td>
      </tr>
      {error && (
        <tr><td colSpan={5} style={{ padding: '0 16px 12px' }}>
          <div className="alert error" style={{ margin: 0 }}>{error}</div>
        </td></tr>
      )}
      {editing && (
        <tr>
          <td colSpan={5} style={{ padding: '0 16px 14px' }}>
            <form onSubmit={saveEdit} style={{ display: 'flex', flexDirection: 'column', gap: 10, background: 'var(--bg-1)', border: '1px solid var(--border)', borderRadius: 8, padding: 14 }}>
              <div className="grid-2">
                <div>
                  <label className="label">Key name</label>
                  <input className="input" value={editName} onChange={e => setEditName(e.target.value)} required />
                </div>
                <div>
                  <label className="label">Allowed models</label>
                  <ModelSelect models={models} selected={editModels} onChange={setEditModels}
                    custom={editCustom} onCustomChange={setEditCustom} />
                </div>
              </div>
              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                <button type="button" className="btn sm" onClick={() => setEditing(false)}>Cancel</button>
                <button type="submit" className="btn sm primary" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
              </div>
            </form>
          </td>
        </tr>
      )}
      {revealed && (
        <tr>
          <td colSpan={5} style={{ padding: '0 16px 12px' }}>
            <div className="key-display">
              <span className="key-val">{revealed}</span>
              <CopyButton text={revealed} />
              <button className="btn sm" onClick={() => setRevealed(null)}>
                <IcX size={12} /> Hide
              </button>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

// ── Keys tab ──────────────────────────────────────────────
function KeysTab({ project, canSeeKeys, canManage, canReveal }) {
  const confirm = useConfirm()
  const [keys, setKeys] = useState([])
  const [models, setModels] = useState([])
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [showRevoked, setShowRevoked] = useState(false)
  const [newKeyName, setNewKeyName] = useState('')
  const [newKeyModels, setNewKeyModels] = useState([])
  const [newKeyCustom, setNewKeyCustom] = useState('')
  const [newKeyRecoverable, setNewKeyRecoverable] = useState(false)
  const [recoverableAllowed, setRecoverableAllowed] = useState(false)
  const [justCreated, setJustCreated] = useState(null)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')

  async function loadKeys() {
    if (!canSeeKeys) return
    setLoading(true)
    try { setKeys(await api.getKeys(project.id)) }
    catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }

  useEffect(() => { loadKeys() }, [project.id, canSeeKeys])
  useEffect(() => {
    if (canManage) api.getModels().then(setModels).catch(() => {})
  }, [canManage])
  // Whether this deployment permits recoverable keys at all. Hide the opt-in
  // rather than offering a choice the server would reject.
  useEffect(() => {
    if (canManage) api.getConfig().then(c => setRecoverableAllowed(c.recoverable_keys_allowed)).catch(() => {})
  }, [canManage])

  async function createKey(e) {
    e.preventDefault()
    setCreating(true); setError('')
    try {
      const res = await api.createKey(project.id, {
        name: newKeyName,
        allowed_models: combineModels(newKeyModels, newKeyCustom),
        recoverable: newKeyRecoverable,
      })
      setJustCreated({ key: res.api_key, recoverable: res.key.recoverable })
      setShowCreate(false); setNewKeyName(''); setNewKeyModels([]); setNewKeyCustom('')
      setNewKeyRecoverable(false)
      await loadKeys()
    } catch (e) { setError(e.message) }
    finally { setCreating(false) }
  }

  async function revoke(key) {
    const ok = await confirm(
      `Revoke the API key "${key.name}" (${key.key_prefix}…)? Requests using it will start failing immediately. This cannot be undone.`,
      { title: 'Revoke API key', confirmLabel: 'Revoke key', danger: true },
    )
    if (!ok) return
    try { await api.revokeKey(key.id); await loadKeys() }
    catch (e) { setError(e.message) }
  }

  if (!canSeeKeys) {
    return (
      <div className="card">
        <div className="empty">
          <div className="empty-title">No access</div>
          <div style={{ fontSize: 12 }}>Viewers cannot access API keys.</div>
        </div>
      </div>
    )
  }

  const revokedCount = keys.filter(k => !k.active).length
  const visibleKeys = showRevoked ? keys : keys.filter(k => k.active)

  return (
    <div>
      {canManage && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
          <button className="btn primary sm" onClick={() => setShowCreate(true)}><IcPlus size={13} /> Create key</button>
        </div>
      )}

      {error && <div className="alert error">{error}</div>}

      {justCreated && (
        <div className="card" style={{ marginBottom: 16, borderColor: 'var(--accent)' }}>
          <div className="card-b">
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
              <div style={{ width: 32, height: 32, borderRadius: 8, background: 'var(--accent-soft)', color: 'var(--accent-2)', display: 'grid', placeItems: 'center', flexShrink: 0 }}>
                <IcKey size={16} />
              </div>
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600, fontSize: 14 }}>API key created</div>
                <div className="hint" style={{ marginTop: 2 }}>
                  {justCreated.recoverable
                    ? 'A project admin can reveal this key again later.'
                    : 'Copy it now — this is the only time it will be shown.'}
                </div>
                <div className="key-display" style={{ marginTop: 8 }}>
                  <span className="key-val">{justCreated.key}</span>
                  <CopyButton text={justCreated.key} />
                </div>
              </div>
              <button className="iconbtn" aria-label="Dismiss" onClick={() => setJustCreated(null)}><IcX size={14} /></button>
            </div>
          </div>
        </div>
      )}

      {showCreate && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>Create API key</h3>
            <button className="iconbtn" aria-label="Close" onClick={() => setShowCreate(false)}><IcX size={14} /></button>
          </div>
          <form className="card-b" onSubmit={createKey}>
            <div className="grid-2" style={{ marginBottom: 14 }}>
              <div>
                <label className="label">Key name</label>
                <input className="input" value={newKeyName} onChange={e => setNewKeyName(e.target.value)} placeholder="e.g. production-server" required />
              </div>
              <div>
                <label className="label">Allowed models</label>
                <ModelSelect models={models} selected={newKeyModels} onChange={setNewKeyModels}
                  custom={newKeyCustom} onCustomChange={setNewKeyCustom} />
              </div>
            </div>
            {recoverableAllowed && (
              <div style={{ marginBottom: 14 }}>
                <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, cursor: 'pointer' }}>
                  <input type="checkbox" checked={newKeyRecoverable} style={{ marginTop: 2 }}
                    onChange={e => setNewKeyRecoverable(e.target.checked)} />
                  <span>
                    <span style={{ fontSize: 13 }}>Let project admins reveal this key later</span>
                    <span className="hint" style={{ display: 'block', marginTop: 2 }}>
                      Stores an encrypted copy so the key can be read back. Leave this off and the
                      key is shown once here and nowhere else — safer, but a lost key must be replaced.
                    </span>
                  </span>
                </label>
              </div>
            )}
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button type="button" className="btn" onClick={() => setShowCreate(false)}>Cancel</button>
              <button type="submit" className="btn primary" disabled={creating}>{creating ? 'Creating…' : 'Create key'}</button>
            </div>
          </form>
        </div>
      )}

      <div className="card">
        <div className="card-h">
          <h3>API Keys</h3>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 12, color: 'var(--text-3)' }}>
            <span>{keys.filter(k => k.active).length} active</span>
            {revokedCount > 0 && (
              <label style={{ display: 'flex', alignItems: 'center', gap: 5, cursor: 'pointer' }}>
                <input type="checkbox" checked={showRevoked} onChange={e => setShowRevoked(e.target.checked)} />
                Show {revokedCount} revoked
              </label>
            )}
          </div>
        </div>
        {loading ? (
          <div className="card-b" style={{ color: 'var(--text-3)' }}>Loading…</div>
        ) : visibleKeys.length === 0 ? (
          <div className="empty">
            <div className="empty-title">{keys.length === 0 ? 'No keys yet' : 'No active keys'}</div>
            <div style={{ fontSize: 12 }}>
              {keys.length === 0 ? 'Create your first API key to start making requests.' : 'All keys are revoked.'}
            </div>
          </div>
        ) : (
          <table className="table">
            <thead><tr><th>Name</th><th>Prefix</th><th>Status</th><th>Last used</th><th></th></tr></thead>
            <tbody>{visibleKeys.map(k => (
              <KeyRow key={k.id} k={k} models={models} onRevoke={revoke} onSaved={loadKeys}
                canManage={canManage} canReveal={canReveal} />
            ))}</tbody>
          </table>
        )}
      </div>
    </div>
  )
}

// ── Members tab ───────────────────────────────────────────
const ROLES = [
  { id: 'admin', hint: 'Manage members and keys, reveal key plaintext' },
  { id: 'developer', hint: 'View and use project keys' },
  { id: 'viewer', hint: 'See the project, no key access' },
]

function RoleSelect({ value, onChange, style }) {
  return (
    <select className="select" style={style} value={value} onChange={e => onChange(e.target.value)}>
      {ROLES.map(r => <option key={r.id} value={r.id} title={r.hint}>{r.id} — {r.hint}</option>)}
    </select>
  )
}

function MembersTab({ project, members, canManage, onReload }) {
  const confirm = useConfirm()
  const [showAdd, setShowAdd] = useState(false)
  const [addUserId, setAddUserId] = useState('')
  const [addRole, setAddRole] = useState('developer')
  const [adding, setAdding] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [editRole, setEditRole] = useState('')
  const [error, setError] = useState('')
  // Addable users come from the project's own endpoint (not GET /users, which is
  // global-admin only) and are loaded when the form opens, so the list is fresh.
  const [candidates, setCandidates] = useState([])
  const [candidatesLoading, setCandidatesLoading] = useState(false)
  const [candidatesError, setCandidatesError] = useState('')

  async function openAdd() {
    setShowAdd(true); setAddUserId(''); setAddRole('developer')
    setCandidatesLoading(true); setCandidatesError('')
    try { setCandidates(await api.getMemberCandidates(project.id)) }
    catch (e) { setCandidatesError(e.message) }
    finally { setCandidatesLoading(false) }
  }

  async function addMember(e) {
    e.preventDefault()
    setAdding(true); setError('')
    try {
      await api.addMember(project.id, { user_id: parseInt(addUserId), role: addRole })
      setShowAdd(false); setAddUserId(''); setAddRole('developer')
      await onReload()
    } catch (e) { setError(e.message) }
    finally { setAdding(false) }
  }

  async function saveRole(userId) {
    try {
      await api.updateMemberRole(project.id, userId, editRole)
      setEditingId(null)
      await onReload()
    } catch (e) { setError(e.message) }
  }

  async function remove(member) {
    const ok = await confirm(
      `Remove ${member.username} from "${project.name}"? They will lose access to the project's keys and usage.`,
      { title: 'Remove member', confirmLabel: 'Remove', danger: true },
    )
    if (!ok) return
    try { await api.removeMember(project.id, member.user_id); await onReload() }
    catch (e) { setError(e.message) }
  }

  return (
    <div>
      {canManage && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
          <button className="btn primary sm" onClick={openAdd}><IcPlus size={13} /> Add member</button>
        </div>
      )}

      {error && <div className="alert error">{error}</div>}

      {showAdd && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>Add member</h3>
            <button className="iconbtn" aria-label="Close" onClick={() => setShowAdd(false)}><IcX size={14} /></button>
          </div>
          <form className="card-b" onSubmit={addMember}>
            <div className="grid-2" style={{ marginBottom: 14 }}>
              <div>
                <label className="label">User</label>
                <select className="select" required value={addUserId} disabled={candidatesLoading || !!candidatesError}
                  onChange={e => setAddUserId(e.target.value)}>
                  <option value="">{candidatesLoading ? 'Loading users…' : 'Select a user…'}</option>
                  {candidates.map(u => (
                    <option key={u.id} value={u.id}>{u.username}{u.email ? ` (${u.email})` : ''}</option>
                  ))}
                </select>
                {candidatesError ? (
                  <div className="hint" style={{ color: 'var(--red)' }}>Could not load users: {candidatesError}</div>
                ) : !candidatesLoading && candidates.length === 0 ? (
                  <div className="hint">Every active user is already a member.</div>
                ) : null}
              </div>
              <div>
                <label className="label">Role</label>
                <RoleSelect value={addRole} onChange={setAddRole} />
              </div>
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button type="button" className="btn" onClick={() => setShowAdd(false)}>Cancel</button>
              <button type="submit" className="btn primary" disabled={adding || candidatesLoading}>{adding ? 'Adding…' : 'Add member'}</button>
            </div>
          </form>
        </div>
      )}

      <div className="card">
        <div className="card-h">
          <h3>Members</h3>
          <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{members.length} total</div>
        </div>
        {members.length === 0 ? (
          <div className="empty">
            <div className="empty-title">No members</div>
          </div>
        ) : (
          <table className="table">
            <thead><tr><th>User</th><th>Email</th><th>Role</th>{canManage && <th></th>}</tr></thead>
            <tbody>
              {members.map(m => (
                <tr key={m.user_id} className="row-hover">
                  <td>
                    <span className="mono" style={{ fontWeight: 500 }}>{m.username}</span>
                    {m.active === false && (
                      <span className="badge red" style={{ fontSize: 10, marginLeft: 6 }}>disabled</span>
                    )}
                  </td>
                  <td style={{ color: 'var(--text-3)', fontSize: 12 }}>{m.email || '—'}</td>
                  <td>
                    {canManage && editingId === m.user_id ? (
                      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                        <RoleSelect value={editRole} onChange={setEditRole} style={{ width: 'auto' }} />
                        <button className="iconbtn" title="Save" aria-label="Save role" onClick={() => saveRole(m.user_id)}><IcCheck size={13} /></button>
                        <button className="iconbtn" title="Cancel" aria-label="Cancel" onClick={() => setEditingId(null)}><IcX size={13} /></button>
                      </div>
                    ) : (
                      <span className={`badge ${m.role === 'admin' ? 'accent' : ''}`}
                        title={ROLES.find(r => r.id === m.role)?.hint}>{m.role}</span>
                    )}
                  </td>
                  {canManage && (
                    <td>
                      <div style={{ display: 'flex', gap: 4 }}>
                        <button className="iconbtn" title="Edit role" aria-label="Edit role" onClick={() => { setEditingId(m.user_id); setEditRole(m.role) }}>
                          <IcEdit size={13} />
                        </button>
                        <button className="iconbtn" title="Remove" aria-label="Remove member" style={{ color: 'var(--red)' }} onClick={() => remove(m)}>
                          <IcTrash size={13} />
                        </button>
                      </div>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

// ── Project detail with tabs ───────────────────────────────
function ProjectDetail({ projectId, user, tab }) {
  const confirm = useConfirm()
  const [project, setProject] = useState(null)
  const [members, setMembers] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [saving, setSaving] = useState(false)
  const [editError, setEditError] = useState('')

  async function loadData() {
    setLoading(true)
    setLoadError('')
    try {
      // Both are required: the caller's project role is derived from the member
      // list, so swallowing a failure here would silently render them a viewer.
      const [p, ms] = await Promise.all([
        api.getProject(projectId), api.getMembers(projectId),
      ])
      setProject(p)
      setMembers(ms)
    } catch (e) {
      setLoadError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadData() }, [projectId])

  async function saveProject(e) {
    e.preventDefault()
    setSaving(true); setEditError('')
    try {
      const p = await api.updateProject(projectId, { name: editName, description: editDesc || null })
      setProject(p)
      setEditing(false)
    } catch (err) { setEditError(err.message) }
    finally { setSaving(false) }
  }

  async function toggleArchive() {
    const archiving = !project.archived
    const ok = await confirm(
      archiving
        ? `Archive "${project.name}"? Its API keys stop working and it is hidden from lists. Usage history is kept, and you can unarchive later.`
        : `Unarchive "${project.name}"? Its API keys start working again.`,
      { title: archiving ? 'Archive project' : 'Unarchive project', confirmLabel: archiving ? 'Archive' : 'Unarchive', danger: archiving },
    )
    if (!ok) return
    try {
      const p = await api.updateProject(projectId, { archived: archiving })
      setProject(p)
    } catch (err) { setEditError(err.message) }
  }

  if (loading) {
    return <div className="content"><div style={{ color: 'var(--text-3)', padding: '40px 0' }}>Loading…</div></div>
  }
  if (loadError || !project) {
    return (
      <div className="content">
        <LoadError message={loadError || 'Project not found'} onRetry={loadData} />
        <button className="btn sm" onClick={() => navigate('projects')}>← Back to projects</button>
      </div>
    )
  }

  const isGlobalAdmin = user?.global_role === 'admin'
  const myMembership = members.find(m => m.user_id === user?.id)
  const myProjectRole = isGlobalAdmin ? 'admin' : (myMembership?.role ?? 'viewer')
  const canManage = myProjectRole === 'admin'
  const canSeeKeys = myProjectRole !== 'viewer'
  // Revealing key plaintext is admin-only on the backend; don't offer the button
  // to developers only for it to 403.
  const canReveal = myProjectRole === 'admin'
  const activeTab = tab === 'members' ? 'members' : 'keys'

  return (
    <div className="content">
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text-3)', marginBottom: 20 }}>
        <button className="btn ghost sm" onClick={() => navigate('projects')} style={{ padding: '2px 6px' }}>Projects</button>
        <IcChevRight size={12} />
        <span style={{ color: 'var(--text)', fontWeight: 500 }}>{project.name}</span>
      </div>

      <div className="page-h">
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h1 className="page-title">{project.name}</h1>
            {project.archived && <span className="badge amber">Archived</span>}
          </div>
          {project.description && <div className="page-sub">{project.description}</div>}
          <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4 }}>Created {fmtDate(project.created_at)}</div>
        </div>
        {canManage && (
          <div className="h-actions">
            <button className="btn sm" onClick={() => { setEditName(project.name); setEditDesc(project.description || ''); setEditError(''); setEditing(true) }}>
              <IcEdit size={13} /> Edit
            </button>
            <button className={`btn sm${project.archived ? '' : ' danger'}`} onClick={toggleArchive}>
              {project.archived ? 'Unarchive' : 'Archive'}
            </button>
          </div>
        )}
      </div>

      {editError && !editing && <div className="alert error">{editError}</div>}

      {editing && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>Edit project</h3>
            <button className="iconbtn" aria-label="Close" onClick={() => setEditing(false)}><IcX size={14} /></button>
          </div>
          <form className="card-b" onSubmit={saveProject}>
            {editError && <div className="alert error">{editError}</div>}
            <div className="grid-2" style={{ marginBottom: 14 }}>
              <div>
                <label className="label">Name</label>
                <input className="input" value={editName} onChange={e => setEditName(e.target.value)} required />
              </div>
              <div>
                <label className="label">Description</label>
                <input className="input" value={editDesc} onChange={e => setEditDesc(e.target.value)} placeholder="Optional" />
              </div>
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button type="button" className="btn" onClick={() => setEditing(false)}>Cancel</button>
              <button type="submit" className="btn primary" disabled={saving}>{saving ? 'Saving…' : 'Save changes'}</button>
            </div>
          </form>
        </div>
      )}

      <div className="tabs">
        <button className={`tab${activeTab === 'keys' ? ' active' : ''}`} onClick={() => navigate(`projects/${projectId}`)}>
          <IcKey size={13} style={{ marginRight: 5 }} />API Keys
        </button>
        <button className={`tab${activeTab === 'members' ? ' active' : ''}`} onClick={() => navigate(`projects/${projectId}/members`)}>
          <IcUsers size={13} style={{ marginRight: 5 }} />Members
        </button>
      </div>

      {activeTab === 'keys' && <KeysTab project={project} canSeeKeys={canSeeKeys} canManage={canManage} canReveal={canReveal} />}
      {activeTab === 'members' && (
        <MembersTab
          project={project}
          members={members}
          canManage={canManage}
          onReload={loadData}
        />
      )}
    </div>
  )
}

export default function Projects({ user, projectId = null, tab = null }) {
  const [projects, setProjects] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [showArchived, setShowArchived] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')

  const isAdmin = user?.global_role === 'admin'

  async function loadProjects() {
    setLoading(true)
    setLoadError('')
    try {
      const data = await api.getProjects(showArchived ? { include_archived: 'true' } : undefined)
      setProjects(data)
    } catch (e) {
      setLoadError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { if (projectId == null) loadProjects() }, [projectId, showArchived])

  if (projectId != null) {
    return <ProjectDetail projectId={projectId} user={user} tab={tab} />
  }

  async function createProject(e) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const p = await api.createProject({ name: newName, description: newDesc || null })
      // The list is newest-first, so a new project belongs at the top.
      setProjects(prev => [p, ...prev])
      setShowCreate(false)
      setNewName('')
      setNewDesc('')
    } catch (e) {
      setError(e.message)
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="content">
      <div className="page-h">
        <div>
          <h1 className="page-title">Projects</h1>
          <div className="page-sub">Isolate keys and usage per workload</div>
        </div>
        <div className="h-actions">
          {/* Not admin-only: the list is scoped per user either way, and a member
              otherwise has no way to see a project after it is archived. */}
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text-3)', cursor: 'pointer' }}>
            <input type="checkbox" checked={showArchived} onChange={e => setShowArchived(e.target.checked)} />
            Show archived
          </label>
          {isAdmin && (
            <button className="btn primary" onClick={() => setShowCreate(true)}><IcPlus size={14} /> New project</button>
          )}
        </div>
      </div>

      {error && <div className="alert error">{error}</div>}
      {loadError && <LoadError message={loadError} onRetry={loadProjects} />}

      {showCreate && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>Create project</h3>
            <button className="iconbtn" aria-label="Close" onClick={() => setShowCreate(false)}><IcX size={14} /></button>
          </div>
          <form className="card-b" onSubmit={createProject}>
            <div className="grid-2" style={{ marginBottom: 14 }}>
              <div>
                <label className="label">Name</label>
                <input className="input" value={newName} onChange={e => setNewName(e.target.value)} placeholder="e.g. production-api" required />
              </div>
              <div>
                <label className="label">Description</label>
                <input className="input" value={newDesc} onChange={e => setNewDesc(e.target.value)} placeholder="Optional" />
              </div>
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button type="button" className="btn" onClick={() => setShowCreate(false)}>Cancel</button>
              <button type="submit" className="btn primary" disabled={creating}>{creating ? 'Creating…' : 'Create project'}</button>
            </div>
          </form>
        </div>
      )}

      {loading ? (
        <div style={{ color: 'var(--text-3)', padding: '40px 0' }}>Loading…</div>
      ) : loadError ? null : projects.length === 0 ? (
        <div className="card">
          <div className="empty">
            <div className="empty-title">No projects</div>
            <div style={{ fontSize: 12 }}>
              {isAdmin ? 'Projects isolate API keys and usage tracking.' : 'Ask an admin to add you to a project.'}
            </div>
          </div>
        </div>
      ) : (
        <div className="project-grid">
          {projects.map(p => (
            <div key={p.id} className="card" style={{ padding: 18, cursor: 'pointer', opacity: p.archived ? 0.6 : 1 }}
              onClick={() => navigate(`projects/${p.id}`)}>
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
                <div style={{ width: 36, height: 36, borderRadius: 8, background: 'var(--bg-3)', display: 'grid', placeItems: 'center', fontFamily: 'JetBrains Mono, monospace', fontWeight: 600, flexShrink: 0 }}>
                  {p.name.slice(0, 2).toUpperCase()}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <div style={{ fontSize: 15, fontWeight: 600 }}>{p.name}</div>
                    {p.archived && <span className="badge amber" style={{ fontSize: 10 }}>archived</span>}
                  </div>
                  {p.description && (
                    <div style={{ fontSize: 13, color: 'var(--text-2)', marginTop: 4 }}>{p.description}</div>
                  )}
                  <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 8, display: 'flex', gap: 12 }}>
                    <span>Created {fmtDate(p.created_at)}</span>
                    {p.member_count != null && <span>{p.member_count} member{p.member_count === 1 ? '' : 's'}</span>}
                    {p.key_count != null && <span>{p.key_count} active key{p.key_count === 1 ? '' : 's'}</span>}
                  </div>
                </div>
                <IcChevRight size={14} style={{ color: 'var(--text-3)', marginTop: 4 }} />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
