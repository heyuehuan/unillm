import { useState, useEffect } from 'react'
import { api } from '../api.js'
import { IcPlus, IcTrash, IcEdit, IcX, IcCheck } from '../components/Icons.jsx'
import { fmtDate, fmtRelative, copyText, useConfirm } from '../components/ui.jsx'

const SUFFIX_MAX = 20
const MAX_KEYS = 3
const CHALLENGE = 'sk-12345678'

function keyPrefix(username) { return `${username}--` }

function keyPreview(pub) {
  if (!pub) return ''
  const parts = pub.trim().split(/\s+/)
  const type = parts[0] || ''
  const data = parts[1] || ''
  const preview = data.length > 20 ? `${data.slice(0, 10)}…${data.slice(-6)}` : data
  return `${type} ${preview}`
}

function toSuffix(name, username) {
  const prefix = keyPrefix(username)
  return name.startsWith(prefix) ? name.slice(prefix.length) : name
}

function sanitizeSuffix(val) {
  return val.replace(/[^a-zA-Z0-9-]/g, '').slice(0, SUFFIX_MAX)
}

function nextDefaultSuffix(keys, username) {
  const taken = new Set(keys.map(k => k.key_name))
  for (let i = 1; i <= 3; i++) {
    if (!taken.has(`${keyPrefix(username)}${i}`)) return String(i)
  }
  return String(keys.length + 1)
}

function KeyNameInput({ username, suffix, onChange, autoFocus }) {
  const prefix = keyPrefix(username)
  return (
    <div style={{ display: 'flex', alignItems: 'stretch', border: '1px solid var(--border)', borderRadius: 6, overflow: 'hidden', background: 'var(--bg)' }}>
      <span style={{
        padding: '0 10px', display: 'flex', alignItems: 'center',
        background: 'var(--bg-2)', borderRight: '1px solid var(--border)',
        fontFamily: 'JetBrains Mono, monospace', fontSize: 13, color: 'var(--text-3)',
        userSelect: 'none', flexShrink: 0,
      }}>
        {prefix}
      </span>
      <input
        style={{ border: 'none', background: 'transparent', flex: 1, padding: '7px 10px', fontSize: 13, fontFamily: 'JetBrains Mono, monospace', outline: 'none', color: 'var(--text)' }}
        value={suffix}
        onChange={e => onChange(sanitizeSuffix(e.target.value))}
        placeholder="1"
        autoFocus={autoFocus}
        maxLength={SUFFIX_MAX}
        required
      />
      <span style={{ padding: '0 10px', display: 'flex', alignItems: 'center', fontSize: 11, color: 'var(--text-3)', flexShrink: 0 }}>
        {suffix.length}/{SUFFIX_MAX}
      </span>
    </div>
  )
}

function KeyCard({ k, username, onSave, onDelete }) {
  const [editing, setEditing] = useState(false)
  const [suffix, setSuffix] = useState(toSuffix(k.key_name, username))
  const [pub, setPub] = useState(k.public_key)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  async function save() {
    if (!suffix) return
    setSaving(true); setError('')
    try {
      await onSave(k.id, { key_name: keyPrefix(username) + suffix, public_key: pub })
      setEditing(false)
    } catch (e) { setError(e.message) }
    finally { setSaving(false) }
  }

  function cancel() {
    setSuffix(toSuffix(k.key_name, username)); setPub(k.public_key); setError(''); setEditing(false)
  }

  return (
    <div className="card" style={{ marginBottom: 12 }}>
      {editing ? (
        <div className="card-b" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {error && <div className="alert error">{error}</div>}
          <div>
            <label className="label">Key name</label>
            <KeyNameInput username={username} suffix={suffix} onChange={setSuffix} autoFocus />
          </div>
          <div>
            <label className="label">Public key</label>
            <textarea
              className="input"
              style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11.5, minHeight: 80, resize: 'vertical' }}
              value={pub}
              onChange={e => setPub(e.target.value)}
            />
          </div>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button className="btn" onClick={cancel}><IcX size={12} /> Cancel</button>
            <button className="btn primary" onClick={save} disabled={saving || !suffix}>
              <IcCheck size={12} /> {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      ) : (
        <div className="card-b" style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 13, fontFamily: 'JetBrains Mono, monospace' }}>{k.key_name}</div>
            <div style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11.5, color: 'var(--text-3)', marginTop: 3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {keyPreview(k.public_key)}
            </div>
            <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 3, display: 'flex', gap: 14 }}>
              <span>Added {fmtDate(k.created_at)}</span>
              <span>Last used: {fmtRelative(k.last_used_at)}</span>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
            <button className="iconbtn" title="Edit" onClick={() => setEditing(true)}><IcEdit size={14} /></button>
            <button className="iconbtn" title="Delete" style={{ color: 'var(--red)' }} onClick={() => onDelete(k.id)}>
              <IcTrash size={14} />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

const SIGN_METHODS = [
  { id: 'ssh',    label: 'SSH (native)' },
  { id: 'python', label: 'Python' },
]

function CodeBlock({ children }) {
  const [state, setState] = useState('idle')
  async function copy() {
    const ok = await copyText(children)
    setState(ok ? 'copied' : 'failed')
    setTimeout(() => setState('idle'), 1800)
  }
  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 6, overflow: 'hidden' }}>
      <div style={{ display: 'flex', justifyContent: 'flex-end', background: 'var(--bg-2)', borderBottom: '1px solid var(--border)', padding: '3px 6px' }}>
        <button type="button" className="btn" style={{ fontSize: 11, padding: '2px 8px' }} onClick={copy}>
          {state === 'copied' ? <><IcCheck size={11} /> Copied</> : state === 'failed' ? 'Copy failed — select manually' : 'Copy'}
        </button>
      </div>
      <div style={{ background: 'var(--bg-2)', padding: '8px 12px', fontFamily: 'JetBrains Mono, monospace', fontSize: 11.5, color: 'var(--text)', whiteSpace: 'pre', overflowX: 'auto' }}>
        {children}
      </div>
    </div>
  )
}

function ValidatePanel({ onClose, keys }) {
  const [method, setMethod] = useState('ssh')
  const [signedKey, setSignedKey] = useState('')
  const [checking, setChecking] = useState(false)
  const [result, setResult] = useState(null)
  const [keyName, setKeyName] = useState(keys[0]?.key_name || '')
  const [keyPath, setKeyPath] = useState('~/.ssh/id_rsa')

  async function validate(e) {
    e.preventDefault()
    setChecking(true); setResult(null)
    try {
      const res = await api.validateSSHKey(signedKey.trim())
      setResult({ ok: true, message: res.message })
    } catch (err) {
      setResult({ ok: false, message: err.message })
    } finally {
      setChecking(false)
    }
  }

  const kn = keyName || keys[0]?.key_name || 'your-key-name'
  const kp = keyPath || '~/.ssh/id_rsa'

  const opensslPemCmd     = `echo "${CHALLENGE}||${kn}||$(printf '%s' '${CHALLENGE}' | openssl dgst -sha256 -sign ${kp} | base64 | tr -d '\\n')"`
  const opensslOpensshCmd = `cp ${kp} ~/.unillm_tmp_key && chmod 600 ~/.unillm_tmp_key && ssh-keygen -p -N "" -m PEM -f ~/.unillm_tmp_key -q && echo "${CHALLENGE}||${kn}||$(printf '%s' '${CHALLENGE}' | openssl dgst -sha256 -sign ~/.unillm_tmp_key | base64 | tr -d '\\n')" && rm ~/.unillm_tmp_key`
  const pyCmd             = `python -m unillm.client.ssh_signer ${CHALLENGE} --key-name ${kn}`
  const placeholder       = `${CHALLENGE}||${kn}||BASE64SIGNATURE`

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="card-h">
        <h3>Validate my key</h3>
        <button className="iconbtn" onClick={onClose}><IcX size={14} /></button>
      </div>
      <form className="card-b" onSubmit={validate} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>

        <div style={{ display: 'flex', gap: 12 }}>
          <div style={{ flex: 1 }}>
            <label className="label">Key name</label>
            <select className="select" style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12 }}
              value={keyName} onChange={e => setKeyName(e.target.value)}>
              {keys.map(k => <option key={k.id} value={k.key_name}>{k.key_name}</option>)}
            </select>
          </div>
          <div style={{ flex: 2 }}>
            <label className="label">Key path</label>
            <input className="input" style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 12 }}
              value={keyPath} onChange={e => setKeyPath(e.target.value)} placeholder="~/.ssh/id_rsa" />
          </div>
        </div>

        <div>
          <div className="label" style={{ marginBottom: 6 }}>Step 1 — Sign the challenge</div>
          <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
            {SIGN_METHODS.map(m => (
              <button key={m.id} type="button"
                className={`btn${method === m.id ? ' primary' : ''}`}
                style={{ fontSize: 12 }}
                onClick={() => { setMethod(m.id); setSignedKey(''); setResult(null) }}>
                {m.label}
              </button>
            ))}
          </div>

          {method === 'ssh' ? (
            <>
              <div className="hint" style={{ marginBottom: 4 }}>
                Check your key format: <code style={{ fontFamily: 'JetBrains Mono, monospace' }}>head -1 {kp}</code>
              </div>
              <div className="hint" style={{ marginBottom: 4 }}>
                <strong>PEM format</strong> (<code>-----BEGIN RSA PRIVATE KEY-----</code>):
              </div>
              <CodeBlock>{opensslPemCmd}</CodeBlock>
              <div className="hint" style={{ margin: '10px 0 4px' }}>
                <strong>OpenSSH format</strong> (<code>-----BEGIN OPENSSH PRIVATE KEY-----</code>) — converts to a temp file in your home directory, then removes it:
              </div>
              <CodeBlock>{opensslOpensshCmd}</CodeBlock>
              <div className="hint" style={{ marginTop: 8 }}>
                Prefer no temp file? Use the <strong>Python</strong> tab — reads OpenSSH keys directly.
              </div>
            </>
          ) : (
            <>
              <div className="hint" style={{ marginBottom: 4 }}>Works for all key types:</div>
              <CodeBlock>{pyCmd}</CodeBlock>
            </>
          )}
        </div>

        <div>
          <label className="label">Step 2 — Paste the output</label>
          <textarea
            className="input"
            style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11.5, minHeight: 70, resize: 'vertical' }}
            value={signedKey}
            onChange={e => setSignedKey(e.target.value)}
            placeholder={placeholder}
            required
          />
        </div>

        {result && (
          <div className={`alert ${result.ok ? 'success' : 'error'}`}>{result.message}</div>
        )}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button type="button" className="btn" onClick={onClose}>Close</button>
          <button type="submit" className="btn primary" disabled={checking || !signedKey.trim()}>
            {checking ? 'Verifying…' : 'Verify'}
          </button>
        </div>
      </form>
    </div>
  )
}

export default function SSHKeys({ user }) {
  const confirm = useConfirm()
  const [keys, setKeys] = useState([])
  const [loading, setLoading] = useState(true)
  const [showAdd, setShowAdd] = useState(false)
  const [showValidate, setShowValidate] = useState(false)
  const [suffix, setSuffix] = useState('')
  const [newPub, setNewPub] = useState('')
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState('')

  async function load() {
    // Clear first: an error from the previous attempt has nothing to say about this one.
    setError('')
    try { setKeys(await api.getSSHKeys()) }
    catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  function openAdd() {
    setSuffix(nextDefaultSuffix(keys, user.username))
    setNewPub(''); setError(''); setShowAdd(true); setShowValidate(false)
  }

  function openValidate() {
    setShowValidate(true); setShowAdd(false)
  }

  async function addKey(e) {
    e.preventDefault()
    if (!suffix) return
    setAdding(true); setError('')
    try {
      await api.addSSHKey({ key_name: keyPrefix(user.username) + suffix, public_key: newPub.trim() })
      setShowAdd(false)
      await load()
    } catch (e) { setError(e.message) }
    finally { setAdding(false) }
  }

  async function saveKey(id, data) {
    await api.updateSSHKey(id, data)
    await load()
  }

  async function deleteKey(id) {
    const key = keys.find(k => k.id === id)
    const ok = await confirm(
      `Delete the SSH key "${key?.key_name || ''}"? Requests signed with it will stop authenticating. This cannot be undone.`,
      { title: 'Delete SSH key', confirmLabel: 'Delete key', danger: true },
    )
    if (!ok) return
    setError('')
    try { await api.deleteSSHKey(id); await load() }
    catch (e) { setError(e.message) }
  }

  function closeValidate() {
    setShowValidate(false)
    load()
  }

  const canAdd = keys.length < MAX_KEYS

  return (
    <div className="content" style={{ maxWidth: 720 }}>
      <div className="page-h">
        <div>
          <h1 className="page-title">My SSH Keys</h1>
        </div>
        <div className="h-actions">
          {keys.length > 0 && !showValidate && (
            <button className="btn" onClick={openValidate}>Validate my key</button>
          )}
          {!showAdd && (
            <button className="btn primary" onClick={openAdd} disabled={!canAdd}
              title={canAdd ? undefined : `Limit of ${MAX_KEYS} keys reached — delete one to add another`}>
              <IcPlus size={14} /> Add key
            </button>
          )}
        </div>
      </div>

      {error && <div className="alert error">{error}</div>}

      {showValidate && <ValidatePanel onClose={closeValidate} keys={keys} />}

      {showAdd && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>Add SSH key</h3>
            <button className="iconbtn" onClick={() => setShowAdd(false)}><IcX size={14} /></button>
          </div>
          <form className="card-b" onSubmit={addKey} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div>
              <label className="label">Key name</label>
              <KeyNameInput username={user.username} suffix={suffix} onChange={setSuffix} autoFocus />
              <div className="hint">Letters, numbers, and hyphens, max {SUFFIX_MAX} characters.</div>
            </div>
            <div>
              <label className="label">Public key</label>
              <textarea
                className="input"
                style={{ fontFamily: 'JetBrains Mono, monospace', fontSize: 11.5, minHeight: 90, resize: 'vertical' }}
                value={newPub}
                onChange={e => setNewPub(e.target.value)}
                placeholder="ssh-ed25519 AAAA… or ssh-rsa AAAA…"
                required
              />
              <div className="hint">Paste your public key — the server will validate the format.</div>
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button type="button" className="btn" onClick={() => setShowAdd(false)}>Cancel</button>
              <button type="submit" className="btn primary" disabled={adding || !suffix}>
                {adding ? 'Adding…' : 'Add key'}
              </button>
            </div>
          </form>
        </div>
      )}

      {loading ? (
        <div style={{ color: 'var(--text-3)', padding: '40px 0' }}>Loading…</div>
      ) : keys.length === 0 && !showAdd ? (
        <div className="card">
          <div className="empty">
            <div className="empty-title">No SSH keys</div>
            <div style={{ fontSize: 12 }}>Add up to 3 SSH keys to authenticate requests.</div>
          </div>
        </div>
      ) : (
        <>
          {keys.map(k => <KeyCard key={k.id} k={k} username={user.username} onSave={saveKey} onDelete={deleteKey} />)}
          <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4 }}>
            {keys.length}/{MAX_KEYS} keys used{!canAdd && ' — delete a key to add another'}
          </div>
        </>
      )}
    </div>
  )
}
