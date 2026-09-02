import { useState, useEffect } from 'react'

// Minimal hash router: '#/projects/3?sel=42' → { parts: ['projects','3'], query: {sel:'42'} }.
// Hash-based so the FastAPI static server needs no catch-all route.

// The app is served from '/', but a shared link like /documentation (or any path
// the SPA fallback answers with index.html) leaves the path in the URL, so every
// later click reads as /documentation#/models. Fold the path into the hash once
// on load and put the URL back to '/'.
function normalizePath() {
  const { pathname, hash, search } = window.location
  const path = pathname.replace(/^\/+|\/+$/g, '')
  if (!path || path === 'index.html') return
  const target = hash.length > 1 ? hash : '#/' + path + search
  window.history.replaceState(null, '', '/' + target)
}

normalizePath()

function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, '')
  const [pathPart, queryPart] = raw.split('?')
  const parts = pathPart.split('/').filter(Boolean).map(decodeURIComponent)
  const query = Object.fromEntries(new URLSearchParams(queryPart || ''))
  return { parts, query }
}

export function useHashRoute() {
  const [route, setRoute] = useState(parseHash)
  useEffect(() => {
    const onChange = () => setRoute(parseHash())
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return route
}

export function navigate(path, query) {
  const clean = String(path).replace(/^[#/]+/, '')
  const q = query && Object.keys(query).length ? '?' + new URLSearchParams(query) : ''
  window.location.hash = '#/' + clean + q
}
