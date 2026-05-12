import { useState, useEffect } from 'react'
import { api } from './api.js'
import { Sidebar, Topbar } from './components/Layout.jsx'
import Login from './pages/Login.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Projects from './pages/Projects.jsx'
import Logs from './pages/Logs.jsx'
import Usage from './pages/Usage.jsx'
import Admin from './pages/Admin.jsx'
import Settings from './pages/Settings.jsx'
import Models from './pages/Models.jsx'
import SSHKeys from './pages/SSHKeys.jsx'

function resolveTheme(pref) {
  if (pref === 'system') {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  }
  return pref
}

export default function App() {
  const [user, setUser] = useState(null)
  const [authLoading, setAuthLoading] = useState(true)
  const [route, setRoute] = useState('dashboard')

  // Saved preference — persisted to localStorage, used in Settings
  const [theme, setThemeState] = useState(() => localStorage.getItem('unillm_theme') || 'system')
  // Active display theme — what's actually applied; can be toggled without touching preference
  const [displayTheme, setDisplayTheme] = useState(() => resolveTheme(localStorage.getItem('unillm_theme') || 'system'))

  // Settings changes saved preference and resets display theme
  function setTheme(t) {
    setThemeState(t)
    localStorage.setItem('unillm_theme', t)
    setDisplayTheme(resolveTheme(t))
  }

  // Topbar quick toggle — flips display only, does not touch saved preference
  function toggleDisplayTheme() {
    setDisplayTheme(d => {
      const next = d === 'dark' ? 'light' : 'dark'
      document.body.setAttribute('data-theme', next)
      return next
    })
  }

  // Apply display theme — login page always light
  useEffect(() => {
    if (!authLoading && !user) {
      document.body.setAttribute('data-theme', 'light')
    } else {
      document.body.setAttribute('data-theme', displayTheme)
    }
  }, [user, authLoading, displayTheme])

  // Track OS theme changes when preference is 'system'
  useEffect(() => {
    if (theme !== 'system') return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const handler = () => {
      const resolved = resolveTheme('system')
      setDisplayTheme(resolved)
      document.body.setAttribute('data-theme', resolved)
    }
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [theme])

  useEffect(() => {
    if (!api.hasToken()) { setAuthLoading(false); return }
    api.me()
      .then(u => setUser(u))
      .catch(() => api.clearToken())
      .finally(() => setAuthLoading(false))
  }, [])

  function handleLogin() { api.me().then(u => setUser(u)) }

  function handleLogout() {
    api.clearToken()
    setUser(null)
    setRoute('dashboard')
  }

  if (authLoading) {
    return (
      <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', color: 'var(--text-3)' }}>
        Loading…
      </div>
    )
  }

  if (!user) return <Login onLogin={handleLogin} />

  const isAdmin = user.global_role === 'admin'

  let page
  if (route === 'dashboard') page = <Dashboard user={user} setRoute={setRoute} />
  else if (route === 'projects') page = <Projects user={user} />
  else if (route === 'models') page = <Models />
  else if (route === 'logs') page = <Logs user={user} />
  else if (route === 'usage') page = <Usage user={user} />
  else if (route === 'admin' && isAdmin) page = <Admin currentUser={user} />
  else if (route === 'sshkeys') page = <SSHKeys user={user} />
  else if (route === 'settings') page = <Settings user={user} theme={theme} setTheme={setTheme} onLogout={handleLogout} />
  else page = <Dashboard user={user} setRoute={setRoute} />

  return (
    <div className="app">
      <Sidebar route={route} setRoute={setRoute} user={user} onLogout={handleLogout} />
      <main className="main">
        <Topbar route={route} displayTheme={displayTheme} onToggleTheme={toggleDisplayTheme} onLogout={handleLogout} />
        {page}
      </main>
    </div>
  )
}
