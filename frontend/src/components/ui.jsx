import { createContext, useContext, useEffect, useRef, useState } from 'react'

// ── Formatting helpers (single source — pages must not re-implement these) ──

export function fmtDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString()
}

export function fmtDateTime(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

export function fmtRelative(iso) {
  if (!iso) return 'never'
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
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
