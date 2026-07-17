import { useState } from 'react'
import { api } from '../api.js'

function fmtDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString()
}

export default function Settings({ user, theme, setTheme, onLogout }) {
  const [pwForm, setPwForm] = useState({ current_password: '', new_password: '', confirm: '' })
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState(null)

  async function changePassword(e) {
    e.preventDefault()
    if (pwForm.new_password !== pwForm.confirm) {
      setMsg({ type: 'error', text: 'Passwords do not match' })
      return
    }
    setSaving(true)
    setMsg(null)
    try {
      const res = await api.updateMe({ current_password: pwForm.current_password, new_password: pwForm.new_password })
      // The password change invalidates the old token; store the re-issued one so
      // the session survives instead of 401-ing on the next request.
      if (res?.access_token) api.setToken(res.access_token)
      setMsg({ type: 'success', text: 'Password updated successfully' })
      setPwForm({ current_password: '', new_password: '', confirm: '' })
    } catch (e) {
      setMsg({ type: 'error', text: e.message })
    } finally {
      setSaving(false)
    }
  }

  const initials = user ? user.username.slice(0, 2).toUpperCase() : '?'

  return (
    <div className="content" style={{ maxWidth: 720 }}>
      <div className="page-h">
        <div>
          <h1 className="page-title">Settings</h1>
          <div className="page-sub">Account preferences</div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-h"><h3>Profile</h3></div>
        <div className="card-b">
          <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 18 }}>
            <div className="avatar" style={{ width: 54, height: 54, fontSize: 20, background: 'var(--accent)', color: '#fff' }}>
              {initials}
            </div>
            <div>
              <div style={{ fontWeight: 600, fontSize: 16 }}>{user?.username}</div>
              <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>
                {user?.email || 'No email set'} · {user?.global_role}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>
                Joined {fmtDate(user?.created_at)}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-h"><h3>Appearance</h3></div>
        <div className="card-b">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div>
              <div style={{ fontWeight: 500, fontSize: 13 }}>Preferred appearance</div>
              <div style={{ fontSize: 12, color: 'var(--text-3)' }}>Controls how the app looks when you're signed in</div>
            </div>
            <div style={{ display: 'flex', gap: 4, background: 'var(--bg-2)', padding: 3, borderRadius: 6 }}>
              {[['light', 'Light'], ['dark', 'Dark'], ['system', 'Same as system']].map(([k, l]) => (
                <button
                  key={k}
                  className="btn sm"
                  style={{
                    border: 'none',
                    background: theme === k ? 'var(--bg)' : 'transparent',
                    boxShadow: theme === k ? 'var(--shadow)' : 'none',
                  }}
                  onClick={() => setTheme(k)}
                >
                  {l}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-h"><h3>Change password</h3></div>
        <form className="card-b" onSubmit={changePassword}>
          {msg && <div className={`alert ${msg.type}`}>{msg.text}</div>}
          <div className="stack">
            <div>
              <label className="label">Current password</label>
              <input className="input" type="password" required value={pwForm.current_password}
                onChange={e => setPwForm(f => ({ ...f, current_password: e.target.value }))} />
            </div>
            <div className="grid-2">
              <div>
                <label className="label">New password</label>
                <input className="input" type="password" required value={pwForm.new_password}
                  onChange={e => setPwForm(f => ({ ...f, new_password: e.target.value }))} />
              </div>
              <div>
                <label className="label">Confirm new password</label>
                <input className="input" type="password" required value={pwForm.confirm}
                  onChange={e => setPwForm(f => ({ ...f, confirm: e.target.value }))} />
              </div>
            </div>
            <div>
              <button type="submit" className="btn primary" disabled={saving}>
                {saving ? 'Saving…' : 'Update password'}
              </button>
            </div>
          </div>
        </form>
      </div>

      <div className="card">
        <div className="card-h"><h3>Session</h3></div>
        <div className="card-b">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div>
              <div style={{ fontWeight: 500, fontSize: 13 }}>Sign out</div>
              <div style={{ fontSize: 12, color: 'var(--text-3)' }}>Clear your session token and return to login</div>
            </div>
            <button className="btn danger" onClick={onLogout}>Sign out</button>
          </div>
        </div>
      </div>
    </div>
  )
}
