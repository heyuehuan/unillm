import { useState } from 'react'
import { api } from '../api.js'
import { IcLock } from '../components/Icons.jsx'

export default function Login({ onLogin }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await api.login(username, password)
      api.setToken(res.access_token)
      onLogin()
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={submit}>
        <div className="login-brand">
          <div className="brand-mark">u</div>
          <div className="brand-name">unillm</div>
        </div>
        <h1 style={{ fontSize: 20, fontWeight: 600, textAlign: 'center', margin: '0 0 6px', letterSpacing: '-0.02em' }}>
          Welcome back
        </h1>
        <p style={{ color: 'var(--text-2)', textAlign: 'center', margin: '0 0 24px', fontSize: 13 }}>
          Sign in to manage your keys, projects and usage.
        </p>
        {error && <div className="alert error">{error}</div>}
        <div className="field">
          <label className="label">Username</label>
          <input className="input" type="text" value={username} onChange={e => setUsername(e.target.value)} placeholder="admin" autoFocus />
        </div>
        <div className="field">
          <label className="label">Password</label>
          <input className="input" type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="••••••••" />
        </div>
        <button className="btn primary" style={{ width: '100%', justifyContent: 'center', padding: '10px 12px' }} disabled={loading}>
          <IcLock size={14} /> {loading ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
