const getToken = () => localStorage.getItem('unillm_token')

// FastAPI validation errors arrive as a list of {loc, msg} objects; rendering
// that straight into a string gives the user "[object Object]".
function errorMessage(detail) {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map(d => {
      const field = Array.isArray(d?.loc) ? d.loc.filter(x => x !== 'body').join('.') : ''
      const msg = d?.msg || 'invalid value'
      return field ? `${field}: ${msg}` : msg
    }).join('; ')
  }
  if (detail && typeof detail === 'object') return detail.msg || JSON.stringify(detail)
  return ''
}

async function req(method, path, body) {
  const token = getToken()
  const res = await fetch(path, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  // Session expiry: only treat it as a stale session when we actually had a token.
  // A 401 from the login call itself must surface as an error so the login form
  // can show it. No reload — App listens for this event and swaps to the login
  // screen in place, so the hash route (and any other tabs' state) survive.
  if (res.status === 401 && token && path !== '/api/auth/login') {
    localStorage.removeItem('unillm_token')
    window.dispatchEvent(new CustomEvent('unillm:session-expired'))
    throw new Error('Session expired — please sign in again')
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(errorMessage(err.detail) || `Request failed (${res.status})`)
  }
  if (res.status === 204) return null
  return res.json()
}

function qs(params) {
  const parts = []
  for (const [k, v] of Object.entries(params || {})) {
    if (v == null || v === '') continue
    if (Array.isArray(v)) {
      for (const item of v) parts.push([k, item])
    } else {
      parts.push([k, v])
    }
  }
  return parts.length ? '?' + new URLSearchParams(parts) : ''
}

export const api = {
  setToken: (t) => localStorage.setItem('unillm_token', t),
  clearToken: () => localStorage.removeItem('unillm_token'),
  hasToken: () => !!getToken(),

  login: (username, password) => req('POST', '/api/auth/login', { username, password }),

  me: () => req('GET', '/api/users/me'),
  updateMe: (data) => req('PUT', '/api/users/me', data),

  getUsers: () => req('GET', '/api/users'),
  createUser: (data) => req('POST', '/api/users', data),
  updateUser: (id, data) => req('PUT', `/api/users/${id}`, data),

  getProjects: (params) => req('GET', `/api/projects${qs(params)}`),
  getProject: (id) => req('GET', `/api/projects/${id}`),
  createProject: (data) => req('POST', '/api/projects', data),
  updateProject: (id, data) => req('PUT', `/api/projects/${id}`, data),

  getMembers: (projectId) => req('GET', `/api/projects/${projectId}/members`),
  // Users still addable to a project. Separate from getUsers() because project
  // admins may manage members without being global admins.
  getMemberCandidates: (projectId) => req('GET', `/api/projects/${projectId}/member-candidates`),
  addMember: (projectId, data) => req('POST', `/api/projects/${projectId}/members`, data),
  updateMemberRole: (projectId, userId, role) => req('PUT', `/api/projects/${projectId}/members/${userId}`, { role }),
  removeMember: (projectId, userId) => req('DELETE', `/api/projects/${projectId}/members/${userId}`),

  getKeys: (projectId) => req('GET', `/api/projects/${projectId}/keys`),
  createKey: (projectId, data) => req('POST', `/api/projects/${projectId}/keys`, data),
  updateKey: (keyId, data) => req('PUT', `/api/keys/${keyId}`, data),
  revealKey: (keyId) => req('GET', `/api/keys/${keyId}/reveal`),
  revokeKey: (keyId) => req('DELETE', `/api/keys/${keyId}`),

  getSSHKeys: () => req('GET', '/api/ssh-keys'),
  addSSHKey: (data) => req('POST', '/api/ssh-keys', data),
  updateSSHKey: (id, data) => req('PUT', `/api/ssh-keys/${id}`, data),
  deleteSSHKey: (id) => req('DELETE', `/api/ssh-keys/${id}`),
  validateSSHKey: (signedKey) => req('POST', '/api/ssh-keys/validate', { signed_key: signedKey }),

  getModels: () => req('GET', '/api/models'),

  getStats: (params) => req('GET', `/api/logs/stats${qs(params)}`),
  getRequests: (params) => req('GET', `/api/logs/requests${qs(params)}`),
  getAudit: (params) => req('GET', `/api/logs/audit${qs(params)}`),

  getPricing: () => req('GET', '/api/pricing'),
  upsertPricing: (model, data) => req('PUT', `/api/pricing/${encodeURIComponent(model)}`, data),
  deletePricing: (model) => req('DELETE', `/api/pricing/${encodeURIComponent(model)}`),
}
