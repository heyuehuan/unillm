import { useState, useEffect } from 'react'
import { api } from './api.js'
import { useHashRoute, navigate } from './router.js'
import { Sidebar, Topbar } from './components/Layout.jsx'
import { ConfirmProvider, setDisplayTimezone } from './components/ui.jsx'
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
import Documentation from './pages/Documentation.jsx'
import GcpAuth from './pages/GcpAuth.jsx'

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
  const [authError, setAuthError] = useState('')
  // Deployment switches from /api/config. Null until it answers, which is why the
  // features it gates stay hidden rather than flashing into view and disappearing.
  const [serverConfig, setServerConfig] = useState(null)
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

  // Load the signed-in user, both on first paint and straight after a login.
  //
  // Only the server actually rejecting the credentials ends the session. This used
  // to clear the token on any failure at all, so a dropped connection or a 500
  // signed the user out for good — the token was gone, and the next reload showed
  // the login screen with no explanation. Anything else is reported and retryable.
  function loadCurrentUser() {
    if (!api.hasToken()) { setAuthLoading(false); return }
    setAuthLoading(true)
    setAuthError('')
    // The timezone every timestamp renders in comes from the server, and it has to
    // be known before the first page paints — otherwise log pages appear in the
    // browser's zone and then jump. A failed config call is not fatal: formatting
    // falls back to the browser's own zone, which is where it was before.
    Promise.all([api.me(), api.getConfig().catch(() => null)])
      .then(([u, config]) => {
        if (config) setDisplayTimezone(config.display_timezone)
        setServerConfig(config)
        setUser(u)
      })
      .catch(e => {
        if (e.status === 401 || e.status === 403) api.clearToken()
        else setAuthError(e.message || 'Could not reach the server')
      })
      .finally(() => setAuthLoading(false))
  }

  useEffect(() => { loadCurrentUser() }, [])

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
    loadCurrentUser()
  }

  function handleLogout() {
    api.clearToken()
    setUser(null)
    setSessionExpired(false)
    setAuthError('')
    navigate('dashboard')
  }

  if (authLoading) {
    return (
      <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', color: 'var(--text-3)' }}>
        Loading…
      </div>
    )
  }

  // A session we could not verify, rather than one that was rejected. The token is
  // still here, so offer the retry before offering the sign-out.
  if (!user && authError) {
    return (
      <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', padding: 24 }}>
        <div style={{ maxWidth: 420, textAlign: 'center' }}>
          <h2 style={{ marginBottom: 8 }}>Could not reach the server</h2>
          <div className="hint" style={{ marginBottom: 16 }}>{authError}</div>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}>
            <button className="btn primary" onClick={loadCurrentUser}>Try again</button>
            <button className="btn" onClick={handleLogout}>Sign out</button>
          </div>
        </div>
      </div>
    )
  }

  if (!user) return <Login onLogin={handleLogin} sessionExpired={sessionExpired} />

  const isAdmin = user.global_role === 'admin'
  const filterProps = { filters, setFilters, scope, setScope }
  // The sidebar hides what a deployment has not enabled, but a hash typed by hand
  // reaches the router anyway, so the route needs the same gate.
  const features = { gcpAuth: !!serverConfig?.allow_gcp_adc_token_refresh }

  let page
  if (route === 'dashboard') page = <Dashboard user={user} {...filterProps} />
  else if (route === 'projects') page = <Projects user={user} projectId={parts[1] ? parseInt(parts[1]) : null} tab={parts[2] || null} />
  else if (route === 'models') page = <Models />
  else if (route === 'logs') page = <Logs user={user} selectId={query.sel ? parseInt(query.sel) : null} {...filterProps} />
  else if (route === 'usage') page = <Usage user={user} {...filterProps} />
  else if (route === 'admin' && isAdmin) page = <Admin currentUser={user} tab={parts[1] || 'users'} />
  else if (route === 'sshkeys') page = <SSHKeys user={user} />
  else if (route === 'documentation') page = <Documentation section={parts[1] || null} />
  else if (route === 'gcp-auth' && features.gcpAuth) page = <GcpAuth user={user} />
  else if (route === 'settings') page = <Settings user={user} theme={theme} setTheme={setTheme} onUserUpdated={setUser} onLogout={handleLogout} />
  else page = <Dashboard user={user} {...filterProps} />

  return (
    <ConfirmProvider>
      <div className="app">
        <Sidebar route={route} user={user} features={features} onLogout={handleLogout} />
        <main className="main">
          <Topbar route={route} displayTheme={displayTheme} onToggleTheme={toggleDisplayTheme} />
          {page}
        </main>
      </div>
    </ConfirmProvider>
  )
}
