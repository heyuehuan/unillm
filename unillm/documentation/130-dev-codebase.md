---
title: Codebase and maintenance
group: Developer guide
keywords: [layout, contributing, tests, migrations, frontend, build, conventions, docs]
---

## Layout

```
unillm/
├── config.py            # secret resolution (JWT, Fernet)
├── types.py             # Pydantic request and response models
├── documentation/       # the markdown behind these pages
├── client/ssh_signer.py # client-side SSH signing utility
├── db/                  # SQLAlchemy models, CRUD, sessions
├── llm/                 # backend handlers: vertex_ai, vertex_ai_kms, vllm
├── static/              # built frontend, served at /
└── proxy/
    ├── proxy_server.py    # FastAPI app, /v1 endpoints, request logging
    ├── api_routes.py      # /api management endpoints
    ├── auth.py            # API key auth and model access control
    ├── ssh_auth.py        # SSH signature verification
    ├── ratelimit.py       # login and docs limits
    ├── security_headers.py
    ├── server_settings.py # runtime settings with database, config, default resolution
    ├── docs_pages.py      # loads unillm/documentation at startup
    └── proxy_cli.py       # CLI entry point
frontend/                  # React console (Vite), builds into unillm/static
alembic/                   # database migrations
tests/                     # pytest suite
```

## Request path

1. `proxy_server.py` receives the request and validates the body against `types.py`.
2. `auth.py` resolves the API key, applies SSH verification through `ssh_auth.py`, and
   checks the model against the key allowed list.
3. The alias is looked up in the loaded config, and the matching handler in `llm/` is
   called.
4. The handler translates the request, calls the backend, and translates the response back
   into the OpenAI shape. Shared translation lives in `llm/messages.py`,
   `llm/params.py`, `llm/logprobs.py` and `llm/finish_reasons.py`.
5. `proxy_server.py` writes the request log row, including for failures, and returns.

## Common changes

**Add a model.** Edit `unillm_config.yaml` and restart. No code change.

**Add a backend type.** Add a handler in `llm/`, following the shape of `vllm.py`, and
register the `model_type` where the existing ones are dispatched in `proxy_server.py`.

**Add a management endpoint.** Add it to `api_routes.py` next to related routes, reuse the
`get_current_user` and project role dependencies, and call `_audit(...)` for anything that
changes state. Then add the matching call to `frontend/src/api.js`.

**Add a console page.** Create it in `frontend/src/pages/`, register the route in
`App.jsx`, and add the nav entry and topbar label in `components/Layout.jsx`. Reuse the
helpers in `components/ui.jsx` for dates, token and cost formatting, copy buttons, confirm
dialogs and load errors, so pages stay consistent.

**Change a database table.** Edit `db/models.py`, then generate a migration:

```bash
alembic revision --autogenerate -m "describe the change"
```

Review the generated file before committing. Migrations run automatically on startup, and
`tests/test_migrations.py` guards that path.

## Editing this documentation

These pages are markdown files in `unillm/documentation/`. One file is one page. The
server reads the directory at startup and serves it to the console, so an edit takes effect
on the next restart with no frontend build.

Each file starts with front matter:

```
---
title: Codebase and maintenance
group: Developer guide
keywords: [layout, tests, migrations]
---
```

- `title` is the page heading and the entry in the sidebar.
- `group` is the section it sits under. Groups appear in the order their first page does.
- `keywords` feed the search box, alongside the title and body text.

The numeric filename prefix sets the order, and the rest of the name is the URL slug, so
`130-dev-codebase.md` is served at `#/documentation/dev-codebase`. To add a page, drop in a
new file. To reorder, renumber. Nothing else needs to change.

Supported markdown: headings, paragraphs, bullet and numbered lists, fenced code blocks,
tables, blockquotes and inline links. Links starting with `#/` navigate inside the console,
so `#/logs` opens the Logs page and `#/documentation/quickstart` opens another doc page.

### Settings-aware pages

A page can read the running server's configuration, so it describes this deployment
instead of every possible one. Insert a value with a token:

```
This server runs in {{ssh_mode}} mode.
```

Keep or drop a whole block with a condition. The markers each need their own line, and
a comma-separated list matches any of the values:

```
{{#if ssh_mode=warning,enforce}}
Register a key before you call the API.
{{/if}}

{{#if ssh_mode!=none}}
Signing is not optional here.
{{/if}}
```

Fenced code is left alone, so the examples above survive. Blocks do not nest. An unknown
name stays visible on the page as `{{...}}`, so a typo shows up instead of silently
dropping a paragraph.

`context()` in `unillm/proxy/docs_pages.py` decides which values exist. It publishes
`ssh_mode` today. Add a key there for a new one. Values are read per request, so a page
always reflects the config the server started with.

## Frontend

```bash
cd frontend
npm install
npm run build      # writes unillm/static, served by the proxy at /
npm run dev        # hot reload, proxies /api and /v1 to :4000
```

The console has no runtime dependencies beyond React. There are no web fonts and no CDN
assets, because the app ships a strict Content-Security-Policy and often runs on isolated
networks. Styling is plain CSS with variables in `frontend/src/styles.css`, and both themes
are defined there.

## Tests

```bash
pytest tests/
```

The suite covers access control, key recoverability, SSH signature handling, rate limits,
security headers, logprobs limits, migrations and the request logging path. New behavior
that touches auth, logging or limits should arrive with a test next to the existing ones.

## Conventions

- Comments explain why a decision was made. The code already says what it does.
- Fail loudly on a parameter that cannot be honored, rather than returning a 200 that
  quietly ignored it.
- Every state-changing management action writes an audit row.
- Prompts and completions are never written to the database.
- Frontend pages do not re-implement formatting or error handling that `components/ui.jsx`
  already provides.
