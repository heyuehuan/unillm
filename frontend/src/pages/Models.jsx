import { useState, useEffect } from 'react'
import { api } from '../api.js'
import { IcX, IcZap } from '../components/Icons.jsx'

function fmtTime(iso) {
  if (!iso) return null
  const d = new Date(iso)
  const now = Date.now()
  const diff = now - d.getTime()
  if (diff < 60_000) return 'just now'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`
  return `${Math.floor(diff / 86_400_000)}d ago`
}

const STATUS = {
  healthy: { label: 'Healthy', cls: 'green' },
  issues:  { label: 'Issues',  cls: 'red'   },
  unsure:  { label: 'No data', cls: 'amber'  },
}

function PricingOverlay({ model, onClose }) {
  const p = model.pricing
  const inherited = model.pricing_source === 'inherited'
  useEffect(() => {
    function onKey(e) { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="overlay-backdrop" onClick={onClose} role="presentation">
      <div className="overlay-panel" role="dialog" aria-modal="true" aria-label={`${model.name} pricing`}
        onClick={e => e.stopPropagation()}>
        <div className="card-h">
          <div>
            <h3 style={{ margin: 0 }}>{model.name}</h3>
            <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 2 }}>
              {inherited ? <>Inherited from <code>{model.pricing_from}</code></> : 'Pricing details'}
            </div>
          </div>
          <button className="iconbtn" aria-label="Close" onClick={onClose}><IcX size={14} /></button>
        </div>
        <div className="card-b">
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: p.notes ? 16 : 0 }}>
            <div style={{ background: 'var(--bg-1)', borderRadius: 8, padding: '14px 16px' }}>
              <div style={{ fontSize: 11, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 6 }}>Input</div>
              <div style={{ fontSize: 22, fontWeight: 600, fontFamily: 'JetBrains Mono, monospace' }}>
                ${p.input_per_1m.toFixed(2)}
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>per 1M tokens · {p.currency}</div>
            </div>
            <div style={{ background: 'var(--bg-1)', borderRadius: 8, padding: '14px 16px' }}>
              <div style={{ fontSize: 11, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 6 }}>Output</div>
              <div style={{ fontSize: 22, fontWeight: 600, fontFamily: 'JetBrains Mono, monospace' }}>
                ${p.output_per_1m.toFixed(2)}
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>per 1M tokens · {p.currency}</div>
            </div>
          </div>
          {inherited && (
            <div style={{ background: 'var(--amber-soft)', borderRadius: 8, padding: '12px 14px', fontSize: 13, color: 'var(--text-2)', marginBottom: p.notes ? 12 : 0 }}>
              No price is set for <code>{model.name}</code>, so requests are costed at the
              rate for <code>{model.pricing_from}</code>. Set a price on this model if the
              variant bills differently.
            </div>
          )}
          {p.notes && (
            <div style={{ background: 'var(--bg-2)', borderRadius: 8, padding: '12px 14px', fontSize: 13, color: 'var(--text-2)' }}>
              {p.notes}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function ModelCard({ model, onPricing }) {
  const st = STATUS[model.status] || STATUS.unsure
  // Worth flagging only once traffic exists — an unused model costs nothing yet.
  const unpriced = model.pricing_source === 'none' && model.total_requests > 0

  return (
    <div className="card" style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 600, fontSize: 14, fontFamily: 'JetBrains Mono, monospace', marginBottom: 4 }}>{model.name}</div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {model.model_type && (
              <span className="badge blue" style={{ fontSize: 11 }}>{model.model_type}</span>
            )}
            {model.backend_model && model.backend_model !== model.name && (
              <span style={{ fontSize: 11, color: 'var(--text-3)', fontFamily: 'JetBrains Mono, monospace' }}>→ {model.backend_model}</span>
            )}
            {model.pricing_source === 'inherited' && (
              <span className="badge amber" style={{ fontSize: 11 }}
                title={`Priced at the rate for ${model.pricing_from}`}>Inherited pricing</span>
            )}
            {unpriced && (
              <span className="badge amber" style={{ fontSize: 11 }}
                title="Requests to this model are logged without a cost">No pricing</span>
            )}
          </div>
          {model.description && (
            <div style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.5, marginTop: 4 }}>{model.description}</div>
          )}
        </div>
        <span className={`badge ${st.cls}`} style={{ flexShrink: 0 }}>
          <span className="dot" />{st.label}
        </span>
      </div>

      <div style={{ display: 'flex', gap: 16, fontSize: 12, color: 'var(--text-3)' }}>
        {model.last_success_at && (
          <div>Last success <span style={{ color: 'var(--green)' }}>{fmtTime(model.last_success_at)}</span></div>
        )}
        {model.last_failure_at && (
          <div>Last fail <span style={{ color: 'var(--red)' }}>{fmtTime(model.last_failure_at)}</span></div>
        )}
        {!model.last_success_at && !model.last_failure_at && (
          <div>No requests recorded</div>
        )}
      </div>

      {model.pricing && (
        <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12 }}>
          <button className="btn sm" onClick={() => onPricing(model)} style={{ gap: 5 }}>
            <IcZap size={12} /> Pricing
          </button>
        </div>
      )}
    </div>
  )
}

export default function Models() {
  const [models, setModels] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [overlay, setOverlay] = useState(null)

  useEffect(() => {
    api.getModels()
      .then(setModels)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="content">
      <div className="page-h">
        <div>
          <h1 className="page-title">Models</h1>
        </div>
      </div>

      {error && <div className="alert error">{error}</div>}

      {loading ? (
        <div style={{ color: 'var(--text-3)', padding: '40px 0' }}>Loading…</div>
      ) : models.length === 0 ? (
        <div className="card">
          <div className="empty">
            <div className="empty-title">No models yet</div>
            <div style={{ fontSize: 12 }}>Models appear here once requests are proxied through UniLLM.</div>
          </div>
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 14 }}>
          {models.map(m => (
            <ModelCard key={m.name} model={m} onPricing={setOverlay} />
          ))}
        </div>
      )}

      {overlay && <PricingOverlay model={overlay} onClose={() => setOverlay(null)} />}
    </div>
  )
}
