import { useState, useEffect } from 'react'
import { api } from '../api.js'
import { IcPlus, IcKey, IcCopy, IcTrash, IcX, IcChevRight, IcUsers, IcEdit, IcCheck, IcEye } from '../components/Icons.jsx'

function fmtDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString()
}

function KeyRow({ k, onRevoke, canManage, canReveal }) {
  const [revealed, setRevealed] = useState(null)
  const [revealing, setRevealing] = useState(false)
  const [copied, setCopied] = useState(false)

  async function reveal() {
    setRevealing(true)
    try {
      const res = await api.revealKey(k.id)
      setRevealed(res.api_key)
    } catch (e) {
      alert(e.message)
    } finally {
      setRevealing(false)
    }
  }

  function copy(text) {
    navigator.clipboard.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500) })
  }

  return (
    <>
      <tr className="row-hover">
        <td>
          <div style={{ fontWeight: 500 }}>{k.name}</div>
          <div style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
            Created {fmtDate(k.created_at)}
            {k.allowed_models?.length ? ` · ${k.allowed_models.join(', ')}` : ' · all models'}
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
            {k.active && canReveal && !revealed && (
              <button className="btn sm" onClick={reveal} disabled={revealing}>
                <IcEye size={12} /> {revealing ? '…' : 'Reveal'}
              </button>
            )}
            {k.active && canManage && (
              <button className="btn sm danger" onClick={() => onRevoke(k.id)}>
                <IcTrash size={12} /> Revoke
              </button>
            )}
          </div>
        </td>
      </tr>
      {revealed && (
        <tr>
          <td colSpan={5} style={{ padding: '0 16px 12px' }}>
            <div className="key-display">
              <span className="key-val">{revealed}</span>
              <button className="btn sm" onClick={() => copy(revealed)}>
                <IcCopy size={12} /> {copied ? 'Copied!' : 'Copy'}
              </button>
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
  const [keys, setKeys] = useState([])
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [newKeyName, setNewKeyName] = useState('')
  const [newKeyModels, setNewKeyModels] = useState('')
  const [justCreated, setJustCreated] = useState(null)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  async function loadKeys() {
    if (!canSeeKeys) return
    setLoading(true)
    try { setKeys(await api.getKeys(project.id)) }
    catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }

  useEffect(() => { loadKeys() }, [project.id, canSeeKeys])

  async function createKey(e) {
    e.preventDefault()
    setCreating(true); setError('')
    try {
      const allowed = newKeyModels.trim()
        ? newKeyModels.split(',').map(s => s.trim()).filter(Boolean)
        : null
      const res = await api.createKey(project.id, { name: newKeyName, allowed_models: allowed })
      setJustCreated(res.api_key)
      setShowCreate(false); setNewKeyName(''); setNewKeyModels('')
      await loadKeys()
    } catch (e) { setError(e.message) }
    finally { setCreating(false) }
  }

  async function revoke(keyId) {
    if (!confirm('Revoke this key? This cannot be undone.')) return
    try { await api.revokeKey(keyId); await loadKeys() }
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
                <div className="key-display" style={{ marginTop: 8 }}>
                  <span className="key-val">{justCreated}</span>
                  <button className="btn sm" onClick={() => { navigator.clipboard.writeText(justCreated); setCopied(true); setTimeout(() => setCopied(false), 1500) }}>
                    <IcCopy size={12} /> {copied ? 'Copied!' : 'Copy'}
                  </button>
                </div>
              </div>
              <button className="iconbtn" onClick={() => setJustCreated(null)}><IcX size={14} /></button>
            </div>
          </div>
        </div>
      )}

      {showCreate && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>Create API key</h3>
            <button className="iconbtn" onClick={() => setShowCreate(false)}><IcX size={14} /></button>
          </div>
          <form className="card-b" onSubmit={createKey}>
            <div className="grid-2" style={{ marginBottom: 14 }}>
              <div>
                <label className="label">Key name</label>
                <input className="input" value={newKeyName} onChange={e => setNewKeyName(e.target.value)} placeholder="e.g. production-server" required />
              </div>
              <div>
                <label className="label">Allowed models</label>
                <input className="input" value={newKeyModels} onChange={e => setNewKeyModels(e.target.value)} placeholder="Leave blank for all models" />
                <div className="hint">Comma-separated, or blank for unrestricted.</div>
              </div>
            </div>
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
          <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{keys.filter(k => k.active).length} active</div>
        </div>
        {loading ? (
          <div className="card-b" style={{ color: 'var(--text-3)' }}>Loading…</div>
        ) : keys.length === 0 ? (
          <div className="empty">
            <div className="empty-title">No keys yet</div>
            <div style={{ fontSize: 12 }}>Create your first API key to start making requests.</div>
          </div>
        ) : (
          <table className="table">
            <thead><tr><th>Name</th><th>Prefix</th><th>Status</th><th>Last used</th><th></th></tr></thead>
            <tbody>{keys.map(k => <KeyRow key={k.id} k={k} onRevoke={revoke} canManage={canManage} canReveal={canReveal} />)}</tbody>
          </table>
        )}
      </div>
    </div>
  )
}

// ── Members tab ───────────────────────────────────────────
const ROLES = ['admin', 'developer', 'viewer']

function MembersTab({ project, members, allUsers, canManage, onReload }) {
  const [showAdd, setShowAdd] = useState(false)
  const [addUserId, setAddUserId] = useState('')
  const [addRole, setAddRole] = useState('developer')
  const [adding, setAdding] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [editRole, setEditRole] = useState('')
  const [error, setError] = useState('')

  async function load() { await onReload() }

  async function addMember(e) {
    e.preventDefault()
    setAdding(true); setError('')
    try {
      await api.addMember(project.id, { user_id: parseInt(addUserId), role: addRole })
      setShowAdd(false); setAddUserId(''); setAddRole('developer')
      await load()
    } catch (e) { setError(e.message) }
    finally { setAdding(false) }
  }

  async function saveRole(userId) {
    try {
      await api.updateMemberRole(project.id, userId, editRole)
      setEditingId(null)
      await load()
    } catch (e) { setError(e.message) }
  }

  async function remove(userId) {
    if (!confirm('Remove this member from the project?')) return
    try { await api.removeMember(project.id, userId); await load() }
    catch (e) { setError(e.message) }
  }

  const memberUserIds = new Set(members.map(m => m.user_id))
  const addableUsers = allUsers.filter(u => !memberUserIds.has(u.id))

  return (
    <div>
      {canManage && (
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
          <button className="btn primary sm" onClick={() => setShowAdd(true)}><IcPlus size={13} /> Add member</button>
        </div>
      )}

      {error && <div className="alert error">{error}</div>}

      {showAdd && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>Add member</h3>
            <button className="iconbtn" onClick={() => setShowAdd(false)}><IcX size={14} /></button>
          </div>
          <form className="card-b" onSubmit={addMember}>
            <div className="grid-2" style={{ marginBottom: 14 }}>
              <div>
                <label className="label">User</label>
                <select className="select" required value={addUserId} onChange={e => setAddUserId(e.target.value)}>
                  <option value="">Select a user…</option>
                  {addableUsers.map(u => (
                    <option key={u.id} value={u.id}>{u.username}{u.email ? ` (${u.email})` : ''}</option>
                  ))}
                </select>
                {addableUsers.length === 0 && <div className="hint">All users are already members.</div>}
              </div>
              <div>
                <label className="label">Role</label>
                <select className="select" value={addRole} onChange={e => setAddRole(e.target.value)}>
                  {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
                </select>
              </div>
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button type="button" className="btn" onClick={() => setShowAdd(false)}>Cancel</button>
              <button type="submit" className="btn primary" disabled={adding}>{adding ? 'Adding…' : 'Add member'}</button>
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
                  <td><span className="mono" style={{ fontWeight: 500 }}>{m.username}</span></td>
                  <td style={{ color: 'var(--text-3)', fontSize: 12 }}>{m.email || '—'}</td>
                  <td>
                    {canManage && editingId === m.user_id ? (
                      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                        <select className="select" style={{ width: 'auto' }} value={editRole} onChange={e => setEditRole(e.target.value)}>
                          {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
                        </select>
                        <button className="iconbtn" title="Save" onClick={() => saveRole(m.user_id)}><IcCheck size={13} /></button>
                        <button className="iconbtn" title="Cancel" onClick={() => setEditingId(null)}><IcX size={13} /></button>
                      </div>
                    ) : (
                      <span className={`badge ${m.role === 'admin' ? 'accent' : ''}`}>{m.role}</span>
                    )}
                  </td>
                  {canManage && (
                    <td>
                      <div style={{ display: 'flex', gap: 4 }}>
                        <button className="iconbtn" title="Edit role" onClick={() => { setEditingId(m.user_id); setEditRole(m.role) }}>
                          <IcEdit size={13} />
                        </button>
                        <button className="iconbtn" title="Remove" style={{ color: 'var(--red)' }} onClick={() => remove(m.user_id)}>
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
function ProjectDetail({ project, user, onBack }) {
  const [tab, setTab] = useState('keys')
  const [members, setMembers] = useState([])
  const [allUsers, setAllUsers] = useState([])
  const [membersLoading, setMembersLoading] = useState(true)

  async function loadData() {
    setMembersLoading(true)
    const [mr, ur] = await Promise.allSettled([api.getMembers(project.id), api.getUsers()])
    if (mr.status === 'fulfilled') setMembers(mr.value)
    if (ur.status === 'fulfilled') setAllUsers(ur.value)
    setMembersLoading(false)
  }

  useEffect(() => { loadData() }, [project.id])

  const isGlobalAdmin = user?.global_role === 'admin'
  const myMembership = members.find(m => m.user_id === user?.id)
  const myProjectRole = isGlobalAdmin ? 'admin' : (myMembership?.role ?? 'viewer')
  const canManage = myProjectRole === 'admin'
  const canSeeKeys = myProjectRole !== 'viewer'
  // Revealing key plaintext is admin-only on the backend; don't offer the button
  // to developers only for it to 403.
  const canReveal = myProjectRole === 'admin'

  return (
    <div className="content">
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text-3)', marginBottom: 20 }}>
        <button className="btn ghost sm" onClick={onBack} style={{ padding: '2px 6px' }}>Projects</button>
        <IcChevRight size={12} />
        <span style={{ color: 'var(--text)', fontWeight: 500 }}>{project.name}</span>
      </div>

      <div className="page-h">
        <div>
          <h1 className="page-title">{project.name}</h1>
          {project.description && <div className="page-sub">{project.description}</div>}
          <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4 }}>Created {fmtDate(project.created_at)}</div>
        </div>
      </div>

      <div className="tabs">
        <button className={`tab${tab === 'keys' ? ' active' : ''}`} onClick={() => setTab('keys')}>
          <IcKey size={13} style={{ marginRight: 5 }} />API Keys
        </button>
        <button className={`tab${tab === 'members' ? ' active' : ''}`} onClick={() => setTab('members')}>
          <IcUsers size={13} style={{ marginRight: 5 }} />Members
        </button>
      </div>

      {tab === 'keys' && <KeysTab project={project} canSeeKeys={canSeeKeys} canManage={canManage} canReveal={canReveal} />}
      {tab === 'members' && (
        membersLoading ? (
          <div style={{ color: 'var(--text-3)', padding: '40px 0' }}>Loading…</div>
        ) : (
          <MembersTab
            project={project}
            members={members}
            allUsers={allUsers}
            canManage={canManage}
            onReload={loadData}
          />
        )
      )}
    </div>
  )
}

export default function Projects({ user }) {
  const [projects, setProjects] = useState([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')

  const isAdmin = user?.global_role === 'admin'

  async function loadProjects() {
    setLoading(true)
    try {
      const data = await api.getProjects()
      setProjects(data)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadProjects() }, [])

  if (selected) {
    return <ProjectDetail project={selected} user={user} onBack={() => setSelected(null)} />
  }

  async function createProject(e) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const p = await api.createProject({ name: newName, description: newDesc || null })
      setProjects(prev => [...prev, p])
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
        {isAdmin && (
          <div className="h-actions">
            <button className="btn primary" onClick={() => setShowCreate(true)}><IcPlus size={14} /> New project</button>
          </div>
        )}
      </div>

      {error && <div className="alert error">{error}</div>}

      {showCreate && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>Create project</h3>
            <button className="iconbtn" onClick={() => setShowCreate(false)}><IcX size={14} /></button>
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
      ) : projects.length === 0 ? (
        <div className="card">
          <div className="empty">
            <div className="empty-title">No projects</div>
            <div style={{ fontSize: 12 }}>Projects isolate API keys and usage tracking.</div>
          </div>
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 14 }}>
          {projects.map(p => (
            <div key={p.id} className="card" style={{ padding: 18, cursor: 'pointer' }} onClick={() => setSelected(p)}>
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
                <div style={{ width: 36, height: 36, borderRadius: 8, background: 'var(--bg-3)', display: 'grid', placeItems: 'center', fontFamily: 'JetBrains Mono, monospace', fontWeight: 600, flexShrink: 0 }}>
                  {p.name.slice(0, 2).toUpperCase()}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <div style={{ fontSize: 15, fontWeight: 600 }}>{p.name}</div>
                  </div>
                  {p.description && (
                    <div style={{ fontSize: 13, color: 'var(--text-2)', marginTop: 4 }}>{p.description}</div>
                  )}
                  <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 8 }}>
                    Created {fmtDate(p.created_at)}
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
