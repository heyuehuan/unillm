import { IcLayout, IcFolder, IcKey, IcLog, IcShield, IcSettings, IcUsers, IcSun, IcMoon, IcChart, IcZap, IcLogOut } from './Icons.jsx'

const NAV_ITEMS = [
  { id: 'dashboard', label: 'Dashboard', Icon: IcLayout },
  { id: 'projects', label: 'Projects', Icon: IcFolder },
  { id: 'models', label: 'Models', Icon: IcZap },
  { id: 'logs', label: 'Logs', Icon: IcLog },
  { id: 'usage', label: 'Usage', Icon: IcChart },
]
const ORG_ITEMS = [
  { id: 'admin', label: 'Admin', Icon: IcShield, adminOnly: true },
  { id: 'sshkeys', label: 'My SSH Keys', Icon: IcKey },
  { id: 'settings', label: 'Settings', Icon: IcSettings },
]

export function Sidebar({ route, setRoute, user, onLogout }) {
  const isAdmin = user?.global_role === 'admin'
  const initials = user ? user.username.slice(0, 2).toUpperCase() : '?'

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">u</div>
        <div className="brand-name">unillm</div>
      </div>

      <nav className="nav">
        {NAV_ITEMS.map(({ id, label, Icon }) => (
          <button key={id} className={`nav-item${route === id ? ' active' : ''}`} onClick={() => setRoute(id)}>
            <Icon className="nav-icon" size={16} />
            <span>{label}</span>
          </button>
        ))}
        <div className="nav-group">Account</div>
        {ORG_ITEMS.filter(x => !x.adminOnly || isAdmin).map(({ id, label, Icon }) => (
          <button key={id} className={`nav-item${route === id ? ' active' : ''}`} onClick={() => setRoute(id)}>
            <Icon className="nav-icon" size={16} />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-foot">
        <div className="avatar">{initials}</div>
        <div className="user-info">
          <div className="user-name">{user?.username || '—'}</div>
          <div className="user-role">{user?.global_role || ''}</div>
        </div>
        <button className="iconbtn" title="Sign out" onClick={onLogout} style={{ color: 'var(--red)', flexShrink: 0 }}>
          <IcLogOut size={15} />
        </button>
      </div>
    </aside>
  )
}

export function Topbar({ route, displayTheme, onToggleTheme, onLogout }) {
  const labels = {
    dashboard: 'Dashboard', projects: 'Projects', project: 'Projects',
    models: 'Models', logs: 'Logs', usage: 'Usage', admin: 'Admin', sshkeys: 'My SSH Keys', settings: 'Settings',
  }
  return (
    <header className="topbar">
      <div className="crumbs">
        <span className="here">{labels[route] || route}</span>
      </div>
      <div className="topbar-spacer" />
      <button className="iconbtn" title={displayTheme === 'dark' ? 'Switch to light' : 'Switch to dark'} onClick={onToggleTheme}>
        {displayTheme === 'dark' ? <IcSun size={15} /> : <IcMoon size={15} />}
      </button>
      <button className="btn sm danger" onClick={onLogout} style={{ marginLeft: 4 }}>Sign out</button>
    </header>
  )
}
