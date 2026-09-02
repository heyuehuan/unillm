import { useState } from 'react'
import { copyText } from './ui.jsx'

// Minimal markdown renderer for the documentation wiki.
//
// The docs are markdown files on the server (unillm/documentation), so the console
// needs to render them. This covers the subset those files use: headings,
// paragraphs, lists, fenced code, tables, blockquotes and inline links. It builds
// React elements rather than HTML strings, so nothing is injected into the page and
// the app's strict CSP stays intact.

// ── Inline ────────────────────────────────────────────────
// `code`, **bold**, *italic*, [text](href). Links starting with '#' stay in the
// console (the app routes on the hash); anything else opens in a new tab.

const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\n]+\*|\[[^\]]+\]\([^)\s]+\))/g

function inline(text, keyPrefix = 'i') {
  const out = []
  const parts = String(text).split(INLINE)
  parts.forEach((part, i) => {
    if (!part) return
    const key = `${keyPrefix}-${i}`
    if (part.startsWith('`') && part.endsWith('`')) {
      out.push(<code key={key} className="doc-k">{part.slice(1, -1)}</code>)
      return
    }
    if (part.startsWith('**') && part.endsWith('**')) {
      out.push(<strong key={key}>{part.slice(2, -2)}</strong>)
      return
    }
    if (part.startsWith('*') && part.endsWith('*') && part.length > 2) {
      out.push(<em key={key}>{part.slice(1, -1)}</em>)
      return
    }
    const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(part)
    if (link) {
      const [, label, href] = link
      const internal = href.startsWith('#')
      out.push(
        <a key={key} className="doc-a" href={href}
           {...(internal ? {} : { target: '_blank', rel: 'noreferrer' })}>
          {inline(label, key)}
        </a>
      )
      return
    }
    out.push(part)
  })
  return out
}

// ── Code block with a copy button ─────────────────────────

function CodeBlock({ code, lang }) {
  const [state, setState] = useState('idle')
  async function copy() {
    const ok = await copyText(code)
    setState(ok ? 'copied' : 'failed')
    setTimeout(() => setState('idle'), 1600)
  }
  return (
    <div className="doc-code">
      <div className="doc-code-bar">
        {lang && <span className="doc-code-lang">{lang}</span>}
        <button type="button" className="doc-copy" onClick={copy}>
          {state === 'copied' ? 'Copied' : state === 'failed' ? 'Select manually' : 'Copy'}
        </button>
      </div>
      <pre><code>{code}</code></pre>
    </div>
  )
}

// ── Block parser ──────────────────────────────────────────

const BULLET = /^\s*[-*]\s+(.*)$/
const NUMBERED = /^\s*\d+\.\s+(.*)$/
const HEADING = /^(#{1,4})\s+(.*)$/
const TABLE_SEP = /^\s*\|?[\s:|-]+\|[\s:|-]*$/

function splitRow(line) {
  return line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim())
}

// Headings get an id so a page can be linked to a section later.
export function slugifyHeading(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
}

function parse(markdown) {
  const lines = String(markdown).replace(/\r\n/g, '\n').split('\n')
  const blocks = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]

    if (!line.trim()) { i++; continue }

    // Fenced code. An unterminated fence runs to the end of the file rather than
    // swallowing the rest as prose.
    const fence = /^```(\w*)\s*$/.exec(line)
    if (fence) {
      const lang = fence[1] || ''
      const body = []
      i++
      while (i < lines.length && !/^```\s*$/.test(lines[i])) { body.push(lines[i]); i++ }
      i++
      blocks.push({ type: 'code', lang, code: body.join('\n') })
      continue
    }

    const heading = HEADING.exec(line)
    if (heading) {
      blocks.push({ type: 'heading', level: heading[1].length, text: heading[2].trim() })
      i++
      continue
    }

    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      blocks.push({ type: 'hr' })
      i++
      continue
    }

    // Table: a pipe row followed by a separator row.
    if (line.includes('|') && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const head = splitRow(line)
      i += 2
      const rows = []
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(splitRow(lines[i]))
        i++
      }
      blocks.push({ type: 'table', head, rows })
      continue
    }

    if (/^\s*>\s?/.test(line)) {
      const body = []
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        body.push(lines[i].replace(/^\s*>\s?/, ''))
        i++
      }
      blocks.push({ type: 'quote', text: body.join(' ').trim() })
      continue
    }

    if (BULLET.test(line) || NUMBERED.test(line)) {
      const ordered = !BULLET.test(line) && NUMBERED.test(line)
      const items = []
      while (i < lines.length) {
        const cur = lines[i]
        const m = ordered ? NUMBERED.exec(cur) : BULLET.exec(cur)
        if (m) {
          items.push(m[1])
          i++
          continue
        }
        // Wrapped continuation of the item above, indented under it.
        if (items.length && cur.trim() && /^\s{2,}\S/.test(cur)) {
          items[items.length - 1] += ' ' + cur.trim()
          i++
          continue
        }
        break
      }
      blocks.push({ type: 'list', ordered, items })
      continue
    }

    // Paragraph: runs until a blank line or the start of another block.
    const para = []
    while (i < lines.length && lines[i].trim()
           && !HEADING.test(lines[i])
           && !/^```/.test(lines[i])
           && !/^\s*>\s?/.test(lines[i])
           && !BULLET.test(lines[i])
           && !NUMBERED.test(lines[i])) {
      para.push(lines[i].trim())
      i++
    }
    if (para.length) blocks.push({ type: 'para', text: para.join(' ') })
    else i++
  }

  return blocks
}

export default function Markdown({ text }) {
  const blocks = parse(text || '')
  return (
    <div className="doc-md">
      {blocks.map((b, n) => {
        const key = `b${n}`
        switch (b.type) {
          case 'heading': {
            const Tag = b.level <= 2 ? 'h2' : b.level === 3 ? 'h3' : 'h4'
            return <Tag key={key} id={slugifyHeading(b.text)}>{inline(b.text, key)}</Tag>
          }
          case 'code':
            return <CodeBlock key={key} code={b.code} lang={b.lang} />
          case 'list':
            return b.ordered
              ? <ol key={key}>{b.items.map((it, j) => <li key={j}>{inline(it, `${key}-${j}`)}</li>)}</ol>
              : <ul key={key}>{b.items.map((it, j) => <li key={j}>{inline(it, `${key}-${j}`)}</li>)}</ul>
          case 'table':
            return (
              <div key={key} className="doc-table">
                <table className="table">
                  <thead>
                    <tr>{b.head.map((h, j) => <th key={j}>{inline(h, `${key}-h${j}`)}</th>)}</tr>
                  </thead>
                  <tbody>
                    {b.rows.map((r, j) => (
                      <tr key={j}>{r.map((c, k) => <td key={k}>{inline(c, `${key}-${j}-${k}`)}</td>)}</tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          case 'quote':
            return <div key={key} className="doc-note">{inline(b.text, key)}</div>
          case 'hr':
            return <div key={key} className="hr" />
          default:
            return <p key={key}>{inline(b.text, key)}</p>
        }
      })}
    </div>
  )
}
