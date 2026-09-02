import { createContext, useContext, useEffect, useRef, useState } from 'react'

// ── Formatting helpers (single source — pages must not re-implement these) ──

// Every timestamp is rendered in the deployment's timezone, which the server sends
// in /api/config. Two people in different places reading the same log line should
// see the same wall-clock time, and the one comparing it against a Slack message or
// a colleague's screenshot should not have to do the arithmetic.
//
// Undefined until the config arrives, which makes toLocale* fall back to the
// browser's own zone — the old behaviour, and a safe default if the call fails.
let displayTimezone

export function setDisplayTimezone(tz) {
  displayTimezone = tz || undefined
}

// Short name of the active zone right now, e.g. "EDT". Worth showing on pages full
// of timestamps, so a reader knows which clock they are looking at.
export function timezoneLabel(at = new Date()) {
  try {
    return new Intl.DateTimeFormat('en-US', { timeZone: displayTimezone, timeZoneName: 'short' })
      .formatToParts(at).find(p => p.type === 'timeZoneName')?.value || ''
  } catch {
    return ''
  }
}

// A <input type="datetime-local"> value carries no timezone: "2026-09-02T09:30"
// means whatever zone the person typing it had in mind. Since every timestamp on the
// page is printed in the deployment's zone, that is the zone the input has to be read
// in too — otherwise the same custom range selects different rows for each viewer, and
// the rows it returns disagree with the times printed beside them.
export function zonedInputToUtcISO(local) {
  if (!local) return undefined
  const guess = new Date(local)               // read in the browser's own zone
  if (Number.isNaN(guess.getTime())) return undefined
  if (!displayTimezone) return guess.toISOString()
  // How far the browser's zone sits from the deployment's at that moment. Rendering
  // the instant into the target zone and re-reading it as browser-local is the only
  // way to get a zone offset out of Intl, DST included.
  const asWallClock = new Date(guess.toLocaleString('en-US', { timeZone: displayTimezone }))
  return new Date(guess.getTime() + (guess.getTime() - asWallClock.getTime())).toISOString()
}

export function fmtDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString(undefined, { timeZone: displayTimezone })
}

export function fmtDateTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, { timeZone: displayTimezone })
}

// Clock time only, for log rows where the date is already established by the page.
export function fmtTimeOfDay(iso, { millis = false } = {}) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    ...(millis ? { fractionalSecondDigits: 3 } : {}),
    timeZone: displayTimezone,
  })
}

export function fmtRelative(iso) {
  if (!iso) return 'never'
  const diff = Date.now() - new Date(iso).getTime()
  // A timestamp in the future is either clock skew or a timestamp the server sent
  // without a timezone, and saying "just now" for it is how the second one stayed
  // invisible: every event, however old, read as current. Name it instead.
  if (diff < -60000) return `in ${fmtSpan(-diff)}`
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'just now'
  return `${fmtSpan(diff)} ago`
}

function fmtSpan(ms) {
  const m = Math.floor(ms / 60000)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  return `${Math.floor(h / 24)}d`
}

export function fmtTokens(n) {
  if (!n) return '0'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k'
  return String(n)
}

export function fmtCost(usd) {
  if (usd == null) return '—'
  return `$${usd.toFixed(4)}`
}

export function HttpBadge({ code }) {
  if (!code) return <span className="badge">—</span>
  if (code >= 500) return <span className="badge red">{code}</span>
  if (code >= 400) return <span className="badge amber">{code}</span>
  return <span className="badge green">{code}</span>
}

// ── Clipboard with insecure-context fallback ────────────────
// navigator.clipboard only exists on HTTPS/localhost; intranet HTTP deploys
// need the textarea/execCommand path. Resolves to true on success.

export async function copyText(text) {
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      /* fall through to legacy path */
    }
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

// Button that copies `text` and reports success/failure inline.
export function CopyButton({ text, small = true, children }) {
  const [state, setState] = useState('idle') // idle | copied | failed
  async function onCopy() {
    const ok = await copyText(text)
    setState(ok ? 'copied' : 'failed')
    setTimeout(() => setState('idle'), 1800)
  }
  return (
    <button type="button" className={`btn${small ? ' sm' : ''}`} onClick={onCopy}>
      {state === 'copied' ? 'Copied!' : state === 'failed' ? 'Copy failed — select manually' : (children || 'Copy')}
    </button>
  )
}

// ── Confirm dialog (replaces native confirm/alert) ──────────

const ConfirmContext = createContext(null)

export function ConfirmProvider({ children }) {
  const [dialog, setDialog] = useState(null)
  const resolver = useRef(null)

  function confirm(message, { title = 'Are you sure?', confirmLabel = 'Confirm', danger = false } = {}) {
    return new Promise(resolve => {
      resolver.current = resolve
      setDialog({ message, title, confirmLabel, danger })
    })
  }

  function close(result) {
    setDialog(null)
    resolver.current?.(result)
    resolver.current = null
  }

  useEffect(() => {
    if (!dialog) return
    // Escape only. Enter used to confirm globally, which turned a stray keypress
    // into a delete; the focused button handles Enter on its own.
    function onKey(e) {
      if (e.key === 'Escape') close(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [dialog])

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {dialog && (
        <div className="overlay-backdrop" onClick={() => close(false)} role="presentation">
          <div className="overlay-panel confirm-panel" role="alertdialog" aria-modal="true"
               aria-label={dialog.title} onClick={e => e.stopPropagation()}>
            <div className="card-b">
              <div style={{ fontWeight: 600, fontSize: 15, marginBottom: 8 }}>{dialog.title}</div>
              <div style={{ fontSize: 13, color: 'var(--text-2)', lineHeight: 1.55 }}>{dialog.message}</div>
              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 18 }}>
                <button className="btn" onClick={() => close(false)} autoFocus={dialog.danger}>Cancel</button>
                <button className={`btn ${dialog.danger ? 'danger' : 'primary'}`} onClick={() => close(true)}
                        autoFocus={!dialog.danger}>
                  {dialog.confirmLabel}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  )
}

export function useConfirm() {
  const confirm = useContext(ConfirmContext)
  if (!confirm) throw new Error('useConfirm must be used inside ConfirmProvider')
  return confirm
}

// ── Load-error banner with retry (pages must not fake empty states) ─────────

export function LoadError({ message, onRetry }) {
  return (
    <div className="alert error" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
      <span style={{ flex: 1 }}>Failed to load: {message || 'request failed'}</span>
      {onRetry && <button type="button" className="btn sm" onClick={onRetry}>Retry</button>}
    </div>
  )
}
