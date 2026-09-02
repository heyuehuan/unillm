import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { CopyButton, LoadError, fmtDateTime, fmtRelative } from '../components/ui.jsx'

// The credentials behind every Gemini call are a Google user login that expires
// roughly daily. This page is how whoever notices the outage fixes it: it says
// whether the credentials look dead, proves it with one tiny model call, and walks
// the gcloud sign-in without anybody needing shell access to the proxy host.

const ACTIVE_STATES = ['starting', 'awaiting_url', 'awaiting_code', 'finishing']

function StatusPill({ needsRefresh, unknown }) {
  const [label, color] = unknown
    ? ['Not checked', 'var(--text-3)']
    : needsRefresh
      ? ['Needs re-authentication', 'var(--red)']
      : ['Looks healthy', 'var(--green)']
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontWeight: 600, color }}>
      <span style={{ width: 8, height: 8, borderRadius: '50%', background: color }} />
      {label}
    </span>
  )
}

function Field({ label, children }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div style={{ fontSize: 13 }}>{children}</div>
    </div>
  )
}

export default function GcpAuth({ user }) {
  const [status, setStatus] = useState(null)
  const [loadError, setLoadError] = useState(null)
  const [session, setSession] = useState(null)
  const [health, setHealth] = useState(null)
  const [busy, setBusy] = useState(null)   // 'health' | 'start' | 'code' | 'cancel'
  const [msg, setMsg] = useState(null)
  const [code, setCode] = useState('')
  const [force, setForce] = useState(false)

  const isAdmin = user?.global_role === 'admin'
  const canAct = user?.global_role === 'user' || isAdmin
  const sessionRef = useRef(null)
  sessionRef.current = session

  const loadStatus = useCallback(async (signal) => {
    try {
      const s = await api.getGcpAdkStatus({ signal })
      setStatus(s)
      setLoadError(null)
      if (s.last_health && !health) setHealth(s.last_health)
      // A session another operator started is adopted rather than ignored: two
      // people reacting to the same outage should be looking at the same sign-in.
      if (s.active_session && !sessionRef.current) setSession(s.active_session)
    } catch (e) {
      if (e.name !== 'AbortError') setLoadError(e.message)
    }
  }, [health])

  useEffect(() => {
    const ctrl = new AbortController()
    loadStatus(ctrl.signal)
    return () => ctrl.abort()
  }, [])

  // Poll while a sign-in is in flight. This is also what notices a sign-in that
  // completed on its own — gcloud exits without ever asking for a code, and the
  // next poll comes back 'succeeded'.
  useEffect(() => {
    if (!session || !ACTIVE_STATES.includes(session.state)) return
    const ctrl = new AbortController()
    const timer = setInterval(async () => {
      try {
        const s = await api.getGcpAdkSession(session.session_id, { signal: ctrl.signal })
        setSession(s)
        if (!ACTIVE_STATES.includes(s.state)) {
          if (s.state === 'succeeded') {
            setMsg({ type: 'success', text: 'Credentials refreshed. Run the health test to confirm.' })
            setHealth(null)
          }
          loadStatus()
        }
      } catch (e) {
        if (e.name !== 'AbortError') setMsg({ type: 'error', text: e.message })
      }
    }, 2000)
    return () => { clearInterval(timer); ctrl.abort() }
  }, [session?.session_id, session?.state, loadStatus])

  async function runHealthTest() {
    setBusy('health'); setMsg(null)
    try {
      const result = await api.runGcpAdkHealthTest()
      setHealth(result)
      await loadStatus()
    } catch (e) {
      setMsg({ type: 'error', text: e.message })
    } finally {
      setBusy(null)
    }
  }

  async function startRefresh() {
    setBusy('start'); setMsg(null)
    try {
      const s = await api.startGcpAdkRefresh(force && isAdmin)
      setSession(s)
      if (s.state === 'failed') setMsg({ type: 'error', text: s.error || 'The sign-in could not be started' })
    } catch (e) {
      setMsg({ type: 'error', text: e.message })
    } finally {
      setBusy(null)
    }
  }

  async function submitCode(e) {
    e.preventDefault()
    setBusy('code'); setMsg(null)
    try {
      setSession(await api.submitGcpAdkCode(session.session_id, code))
      setCode('')
    } catch (e) {
      setMsg({ type: 'error', text: e.message })
    } finally {
      setBusy(null)
    }
  }

  async function cancelRefresh() {
    setBusy('cancel')
    try {
      setSession(await api.cancelGcpAdkRefresh(session.session_id))
      await loadStatus()
    } catch (e) {
      setMsg({ type: 'error', text: e.message })
    } finally {
      setBusy(null)
    }
  }

  if (loadError && !status) {
    return <div className="content"><LoadError message={loadError} onRetry={() => loadStatus()} /></div>
  }
  if (!status) {
    return <div className="content" style={{ color: 'var(--text-3)' }}>Loading…</div>
  }

  if (!status.enabled) {
    return (
      <div className="content" style={{ maxWidth: 760 }}>
        <div className="page-h"><h1 className="page-title">Google Cloud auth</h1></div>
        <div className="card"><div className="card-b">
          <div className="hint">
            Application Default Credentials refresh is turned off for this deployment.
            An operator enables it with <code>general_settings.gcp_adk.enabled: true</code> in
            the UniLLM config file.
          </div>
        </div></div>
      </div>
    )
  }

  const sessionActive = session && ACTIVE_STATES.includes(session.state)
  const canStart = canAct && status.available && (status.needs_refresh || (force && isAdmin))

  return (
    <div className="content" style={{ maxWidth: 860 }}>
      <div className="page-h"><h1 className="page-title">Google Cloud auth</h1></div>

      {msg && <div className={`alert ${msg.type}`}>{msg.text}</div>}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-h" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h3>Credential status</h3>
          <StatusPill needsRefresh={status.needs_refresh} unknown={!status.needs_refresh && !status.last_success_at && !health} />
        </div>
        <div className="card-b">
          {status.reasons?.length > 0 && (
            <div className="alert error" style={{ marginBottom: 14 }}>
              {status.reasons.map((r, i) => <div key={i}>{r}</div>)}
            </div>
          )}
          <div className="grid-2" style={{ gap: 14 }}>
            <Field label="Last successful Vertex request">
              {status.last_success_at
                ? <span title={fmtDateTime(status.last_success_at)}>{fmtRelative(status.last_success_at)}</span>
                : <span className="hint">None in the last {Math.round(status.window_seconds / 60)} minutes</span>}
            </Field>
            <Field label="Last credential failure">
              {status.last_auth_failure_at
                ? <span title={status.last_auth_failure_message || ''}>{fmtRelative(status.last_auth_failure_at)}</span>
                : <span className="hint">None recorded</span>}
            </Field>
            <Field label="Impersonated service account">
              {status.service_account || <span className="hint">None — signs in as the operator</span>}
            </Field>
            <Field label="Health model">{status.health_model}</Field>
          </div>
          {status.last_auth_failure_message && (
            <div className="hint" style={{ marginTop: 12, wordBreak: 'break-word' }}>
              {status.last_auth_failure_message}
            </div>
          )}
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-h"><h3>Health test</h3></div>
        <div className="card-b">
          <div className="hint" style={{ marginBottom: 12 }}>
            Sends one minimal request to <strong>{status.health_model}</strong> —
            “{status.health_prompt}” — through the same path a served request takes,
            so a pass means the token really did refresh.
          </div>
          {health && (
            <div className={`alert ${health.healthy ? 'success' : 'error'}`}>
              {health.healthy
                ? <>Healthy — replied “{health.reply?.trim() || '(empty)'}” in {health.latency_ms} ms.</>
                : <>Unhealthy{health.auth_related ? ' (credential error)' : ''} — {health.error}</>}
              <div className="hint" style={{ marginTop: 4 }}>
                Checked {fmtRelative(health.checked_at)}
                {health.checked_by ? ` by ${health.checked_by}` : ''}
              </div>
            </div>
          )}
          <button className="btn primary" disabled={!canAct || busy === 'health'} onClick={runHealthTest}>
            {busy === 'health' ? 'Testing…' : 'Run health test'}
          </button>
          {!canAct && <span className="hint" style={{ marginLeft: 10 }}>Viewers cannot run the test.</span>}
        </div>
      </div>

      <div className="card">
        <div className="card-h"><h3>Re-authenticate</h3></div>
        <div className="card-b">
          {!status.available && (
            <div className="alert error">
              gcloud is not installed on the proxy host, so the sign-in cannot be run from here.
            </div>
          )}

          {!sessionActive && (
            <>
              <div className="hint" style={{ marginBottom: 12 }}>
                Runs <code>gcloud auth application-default login</code> on the proxy host and
                shows you its sign-in URL. Available while the credentials look expired — run
                the health test first if the status above is unclear.
              </div>
              {isAdmin && (
                <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12, fontSize: 13 }}>
                  <input type="checkbox" checked={force} onChange={e => setForce(e.target.checked)} />
                  Force a refresh even though the credentials look healthy (admin)
                </label>
              )}
              <button className="btn primary" disabled={!canStart || busy === 'start'} onClick={startRefresh}>
                {busy === 'start' ? 'Starting…' : 'Re-authenticate'}
              </button>
              {canAct && status.available && !canStart && (
                <span className="hint" style={{ marginLeft: 10 }}>
                  Nothing looks broken yet.
                </span>
              )}
            </>
          )}

          {session && (
            <div style={{ marginTop: sessionActive ? 0 : 16 }}>
              {session.url && sessionActive && (
                <>
                  <div className="label">1. Open this URL and sign in</div>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 14 }}>
                    <input className="input" readOnly value={session.url} onFocus={e => e.target.select()} />
                    <CopyButton text={session.url} small={false}>Copy</CopyButton>
                    <a className="btn" href={session.url} target="_blank" rel="noreferrer noopener">Open</a>
                  </div>
                </>
              )}

              {session.state === 'awaiting_code' && (
                <form onSubmit={submitCode}>
                  <div className="label">2. Paste the authorization code Google gives you</div>
                  <div style={{ display: 'flex', gap: 8 }}>
                    <input className="input" value={code} autoComplete="off" spellCheck={false}
                      placeholder="4/0A…" onChange={e => setCode(e.target.value)} />
                    <button className="btn primary" type="submit" disabled={!code.trim() || busy === 'code'}>
                      {busy === 'code' ? 'Submitting…' : 'Submit'}
                    </button>
                  </div>
                </form>
              )}

              {session.state === 'awaiting_url' && (
                <div className="hint">Waiting for gcloud to print the sign-in URL…</div>
              )}
              {session.state === 'finishing' && (
                <div className="hint">Completing the sign-in…</div>
              )}
              {session.state === 'succeeded' && (
                <div className="alert success">
                  Credentials refreshed. The proxy picked them up without a restart.
                </div>
              )}
              {session.state === 'failed' && (
                <div className="alert error">{session.error || 'The sign-in failed.'}</div>
              )}
              {session.state === 'cancelled' && <div className="hint">Sign-in cancelled.</div>}

              {session.output && (
                <details style={{ marginTop: 14 }}>
                  <summary className="hint" style={{ cursor: 'pointer' }}>gcloud output</summary>
                  <pre style={{
                    marginTop: 8, padding: 10, background: 'var(--bg-2)', borderRadius: 6,
                    fontSize: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: 260,
                    overflow: 'auto',
                  }}>{session.output}</pre>
                </details>
              )}

              {sessionActive && (
                <div style={{ marginTop: 14 }}>
                  <button className="btn danger" disabled={busy === 'cancel'} onClick={cancelRefresh}>
                    {busy === 'cancel' ? 'Cancelling…' : 'Cancel sign-in'}
                  </button>
                  {session.started_by && session.started_by !== user?.username && (
                    <span className="hint" style={{ marginLeft: 10 }}>Started by {session.started_by}.</span>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
