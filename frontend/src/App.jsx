import { useState, useEffect } from 'react'
import { api } from './api.js'
import { useHashRoute, navigate } from './router.js'
import { Sidebar, Topbar } from './components/Layout.jsx'
import { ConfirmProvider } from './components/ui.jsx'
import { DEFAULT_FILTERS } from './components/FilterBar.jsx'
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
  const [sessionExpired, setSessionExpired] = useState(false)
  const { parts, query } = useHashRoute()
  const route = parts[0] || 'dashboard'

  // Single theme preference — the topbar toggle and Settings both update it.
  const [theme, setThemeState] = useState(() => localStorage.getItem('unillm_theme') || 'system')
  const displayTheme = resolveTheme(theme)

  function setTheme(t) {
    setThemeState(t)
    localStorage.setItem('unillm_theme', t)
  }

  function toggleDisplayTheme() {
    setTheme(displayTheme === 'dark' ? 'light' : 'dark')
  }

  // Apply resolved theme to <html> so overscroll areas are themed too.
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', displayTheme)
  }, [displayTheme])

  // Re-render when the OS theme changes while preference is 'system'.
  const [, forceRender] = useState(0)
  useEffect(() => {
    if (theme !== 'system') return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const handler = () => forceRender(n => n + 1)
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

  // Expired/invalidated token: drop to the login screen without a reload, so the
  // current hash route survives and the user returns where they left off.
  useEffect(() => {
    function onExpired() {
      setUser(null)
      setSessionExpired(true)
    }
    window.addEventListener('unillm:session-expired', onExpired)
    return () => window.removeEventListener('unillm:session-expired', onExpired)
  }, [])

  // Filters and scope are shared across Dashboard / Logs / Usage so switching
  // pages (or "View all" links) keeps the selection.
  const [filters, setFilters] = useState(DEFAULT_FILTERS)
  const [scope, setScope] = useState('all')

  function handleLogin() {
    setSessionExpired(false)
    api.me().then(u => setUser(u))
  }

  function handleLogout() {
    api.clearToken()
    setUser(null)
    setSessionExpired(false)
    navigate('dashboard')
  }

  if (authLoading) {
    return (
      <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', color: 'var(--text-3)' }}>
        Loading…
      </div>
    )
  }

  if (!user) return <Login onLogin={handleLogin} sessionExpired={sessionExpired} />

  const isAdmin = user.global_role === 'admin'
  const filterProps = { filters, setFilters, scope, setScope }

  let page
  if (route === 'dashboard') page = <Dashboard user={user} {...filterProps} />
  else if (route === 'projects') page = <Projects user={user} projectId={parts[1] ? parseInt(parts[1]) : null} tab={parts[2] || null} />
  else if (route === 'models') page = <Models />
  else if (route === 'logs') page = <Logs user={user} selectId={query.sel ? parseInt(query.sel) : null} {...filterProps} />
  else if (route === 'usage') page = <Usage user={user} {...filterProps} />
  else if (route === 'admin' && isAdmin) page = <Admin currentUser={user} tab={parts[1] || 'users'} />
  else if (route === 'sshkeys') page = <SSHKeys user={user} />
  else if (route === 'settings') page = <Settings user={user} theme={theme} setTheme={setTheme} onUserUpdated={setUser} onLogout={handleLogout} />
  else page = <Dashboard user={user} {...filterProps} />

  return (
    <ConfirmProvider>
      <div className="app">
        <Sidebar route={route} user={user} onLogout={handleLogout} />
        <main className="main">
          <Topbar route={route} displayTheme={displayTheme} onToggleTheme={toggleDisplayTheme} />
          {page}
        </main>
      </div>
    </ConfirmProvider>
  )
}
