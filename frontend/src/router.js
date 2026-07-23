import { useState, useEffect } from 'react'

// Minimal hash router: '#/projects/3?sel=42' → { parts: ['projects','3'], query: {sel:'42'} }.
// Hash-based so the FastAPI static server needs no catch-all route.

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
