import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import { navigate } from '../router.js'
import { IcSearch } from '../components/Icons.jsx'
import { LoadError } from '../components/ui.jsx'
import Markdown from '../components/Markdown.jsx'

// The wiki itself lives in markdown on the server (unillm/documentation) and arrives
// in one response, so switching pages and searching need no further requests.

function groupPages(pages) {
  const groups = []
  for (const page of pages) {
    const existing = groups.find(g => g.title === page.group)
    if (existing) existing.pages.push(page)
    else groups.push({ title: page.group, pages: [page] })
  }
  return groups
}

function matches(page, query) {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return page.title.toLowerCase().includes(q)
    || page.group.toLowerCase().includes(q)
    || page.keywords.some(k => k.toLowerCase().includes(q))
    || page.body.toLowerCase().includes(q)
}

export default function Documentation({ section }) {
  const [pages, setPages] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [query, setQuery] = useState('')

  function load() {
    setLoading(true)
    setLoadError('')
    api.getDocs()
      .then(setPages)
      .catch(e => setLoadError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [])

  const current = useMemo(
    () => pages.find(p => p.slug === section) || pages[0] || null,
    [pages, section],
  )

  // A new page starts at the top, the way following a link in a wiki should behave.
  useEffect(() => { window.scrollTo({ top: 0 }) }, [current?.slug])

  const visible = useMemo(() => pages.filter(p => matches(p, query)), [pages, query])
  const groups = useMemo(() => groupPages(visible), [visible])

  const index = current ? pages.findIndex(p => p.slug === current.slug) : -1
  const prev = index > 0 ? pages[index - 1] : null
  const next = index >= 0 && index < pages.length - 1 ? pages[index + 1] : null

  if (loading) {
    return <div className="content"><div className="empty">Loading documentation…</div></div>
  }

  if (loadError) {
    return <div className="content"><LoadError message={loadError} onRetry={load} /></div>
  }

  if (!current) {
    return (
      <div className="content">
        <div className="empty">
          <div className="empty-title">No documentation found</div>
          <div>The server has no pages in <code className="doc-k">unillm/documentation</code>.</div>
        </div>
      </div>
    )
  }

  return (
    <div className="content docs-page">
      <aside className="docs-toc">
        <div className="docs-search">
          <IcSearch size={13} />
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search docs"
            aria-label="Search documentation"
          />
        </div>

        {groups.length === 0 && <div className="hint" style={{ padding: '4px 10px' }}>No page matches.</div>}

        {groups.map(group => (
          <div key={group.title} className="docs-toc-group">
            <div className="docs-toc-title">{group.title}</div>
            {group.pages.map(page => (
              <button
                key={page.slug}
                className={`docs-toc-item${page.slug === current.slug ? ' active' : ''}`}
                onClick={() => navigate(`documentation/${page.slug}`)}
              >
                {page.title}
              </button>
            ))}
          </div>
        ))}
      </aside>

      <article className="doc-body">
        <div className="doc-eyebrow">{current.group}</div>
        <h1 className="page-title" style={{ marginBottom: 20 }}>{current.title}</h1>

        <Markdown text={current.body} />

        <div className="doc-nav">
          {prev
            ? <button className="doc-nav-item" onClick={() => navigate(`documentation/${prev.slug}`)}>
                <span className="doc-nav-dir">Previous</span>
                <span>{prev.title}</span>
              </button>
            : <span />}
          {next && (
            <button className="doc-nav-item next" onClick={() => navigate(`documentation/${next.slug}`)}>
              <span className="doc-nav-dir">Next</span>
              <span>{next.title}</span>
            </button>
          )}
        </div>
      </article>
    </div>
  )
}
