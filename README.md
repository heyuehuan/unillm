# UniLLM

A self-hosted, OpenAI-compatible LLM proxy with team management, usage metering, and audit logging. Backends: **Vertex AI Gemini** (Application Default Credentials, optional CMEK/KMS) and **vLLM** (or any OpenAI-compatible server).

Point the OpenAI SDK at UniLLM, and it handles authentication, per-project API keys, model access control, request logging, and cost tracking — with a built-in React admin UI.

## Features

**OpenAI-compatible proxy**
- `/v1/chat/completions` and `/v1/completions` (streaming and non-streaming)
- `/v1/models` filtered by each API key's allowed models
- Model aliases mapped to Vertex AI Gemini, Vertex AI with CMEK (KMS), or vLLM backends via YAML config

**Team & access management**
- Users with global roles (`admin`, `user`, `viewer`) and per-project roles (`admin`, `developer`, `viewer`)
- Projects with per-project API keys; keys can be restricted to specific models
- API keys stored as SHA-256 hashes and shown once by default; a key can opt in at creation to keeping an encrypted-at-rest copy so project admins can reveal it later (audited). A deployment can forbid the opt-in entirely with `UNILLM_RECOVERABLE_KEYS=false`
- JWT-based management API (`/api/*`) and React web console
- Optional SSH-signature attribution: requests signed with a user's SSH key are attributed to that user in logs

**Observability & governance**
- Per-request logs: tokens, latency, status, cost, model, client IP, key prefix, SSH user, custom labels
- Cost computed from an admin-managed per-model pricing table
- Append-only audit trail for every management action (logins, key reveals, role changes, …), including failed and rate-limited login attempts
- Built-in login brute-force protection: per-IP attempt limits plus per-username failure limits, both configurable
- Security response headers on every response: a strict CSP (`script-src 'self'`), plus frame, sniffing, referrer and permissions policies, and HSTS over HTTPS
- Usage dashboard with per-model/per-project stats

## Architecture

```
OpenAI SDK / curl ──► FastAPI proxy (unillm/proxy) ──► Vertex AI Gemini
        │                    │                     ├──► Vertex AI + CMEK (KMS)
React admin UI ──► /api/* ───┤                     └──► vLLM / OpenAI-compatible
                             ▼
                SQLite or PostgreSQL (SQLAlchemy + Alembic)
                users · projects · api_keys · ssh_keys
                request_logs · audit_logs · model_pricing
```

## Installation

Requires Python ≥ 3.10.

```bash
pip install -r requirements_unillm.txt
# or, as a package:
pip install -e .
```

## Quick start

### 1. Configure Google Cloud credentials (for Vertex AI backends)

```bash
gcloud auth application-default login
```

### 2. Create a config file

Copy `unillm_config.yaml_example` to `unillm_config.yaml`:

```yaml
model_list:
  # Vertex AI Gemini (default model_type: vertex-ai)
  - model_name: gemini-2.5-flash-lite     # alias clients use
    unillm_params:
      model: gemini-2.5-flash-lite        # actual Gemini model
      project: your-gcp-project-id
      location: us-central1

  # Vertex AI with customer-managed encryption key
  - model_name: gemini-secure
    unillm_params:
      model: gemini-2.5-pro
      model_type: vertex-ai-kms
      project: your-gcp-project-id
      location: us-central1
      kms_key_name: projects/PROJ/locations/LOC/keyRings/RING/cryptoKeys/KEY

  # vLLM or any OpenAI-compatible server
  - model_name: tinyllama
    unillm_params:
      model: Qwen/Qwen2.5-0.5B-Instruct
      model_type: vllm
      base_url: http://localhost:8000
      api_key: ""            # only if vLLM started with --api-key

general_settings:
  port: 4000
  # ssh_required: none | warning | enforce   (see SSH attribution below)
```

### 3. Set required secrets and bootstrap the first admin

```bash
# REQUIRED in production — signs management-console JWTs.
# If unset, an ephemeral secret is generated: sessions won't survive restarts.
export UNILLM_JWT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"

# Recommended: dedicated Fernet key for API-key-at-rest encryption
# (otherwise derived from the JWT secret).
export UNILLM_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

# First admin account (created on startup if no such user exists;
# its API key is printed once to stdout)
export UNILLM_ADMIN_USERNAME=admin
export UNILLM_ADMIN_PASSWORD='a-strong-password'
```

### 4. Run

```bash
python -m unillm.proxy.proxy_cli --config unillm_config.yaml --port 4000
```

Database migrations (Alembic) run automatically on startup. The default database is `sqlite:///./unillm.db`; set `DATABASE_URL` to use PostgreSQL for team deployments.

Open `http://localhost:4000/` for the admin console (after building the frontend, see below) — or `http://localhost:4000/docs` for the interactive API docs.

## Using the proxy

```python
import openai

client = openai.OpenAI(
    api_key="sk-your-key",            # created in the console, per project
    base_url="http://localhost:4000/v1",
)

response = client.chat.completions.create(
    model="gemini-2.5-flash-lite",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

```bash
curl -X POST http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model": "gemini-2.5-flash-lite", "messages": [{"role": "user", "content": "Hello!"}]}'
```

Requests may include an optional `labels` object (`{"labels": {"team": "search"}}`) that is stored with the request log but never forwarded to the backend.

Currently forwarded generation parameters: `temperature`, `top_p`, `max_tokens`, `stop`, `stream`. Other OpenAI parameters (`n`, `tools`, `response_format`, penalties, …) are accepted but ignored.

## Web console (frontend)

```bash
cd frontend
npm install
npm run build        # outputs to unillm/static/, served by the proxy at /
# or for development with hot reload (proxies /api and /v1 to :4000):
npm run dev
```

Pages: Dashboard, Usage (stats & charts), Logs (request + audit), Models (health/pricing), Projects (members & API keys), SSH Keys, Admin (user management), Settings.

## Authentication model

Two separate credential types:

| Credential | Used for | Issued by |
|---|---|---|
| **API key** (`sk-…`) | `/v1/*` inference endpoints | Project admins via console/API; hashed in DB |
| **JWT** (24 h) | `/api/*` management endpoints & console | `POST /api/auth/login` with username/password (bcrypt) |

API-key resolution order: database key → env-var keys (`UNILLM_MASTER_KEY`, `UNILLM_API_KEYS`) → reject. An explicit `UNILLM_DEV_MODE=true` allows unauthenticated requests, but only when no database is configured.

Password changes, admin resets, role changes, and deactivation bump a per-user `token_version`, invalidating outstanding JWTs.

## SSH-signature attribution (optional)

Project API keys are shared; SSH mode attributes each request to an individual. Clients send `api-key||key-name||base64-signature`, where the signature is the raw signature of the API key string made with the user's SSH private key (Ed25519, RSA/PKCS1v15-SHA256, or ECDSA-SHA256). Users register public keys in the console (max 3, named `<username>--<suffix>`).

```yaml
general_settings:
  ssh_required: enforce   # none (default) | warning | enforce
```

Sign with the bundled client:

```bash
python -m unillm.client.ssh_signer sk-your-api-key            # auto-detects ~/.ssh keys
python -m unillm.client.ssh_signer --list-keys
```

```python
from unillm.client import sign_api_key
signed = sign_api_key("sk-your-api-key")   # use signed.full_key as your api_key
```

> **Security limitations (by design):** the signature covers the *static* API key, so a captured signed key is replayable — SSH mode provides **attribution, not theft protection**. The signature identifies who signed, and is not cross-checked against project membership. Note that `ssh-keygen -Y sign` output (SSHSIG format) is **not** compatible; use the bundled signer.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `UNILLM_JWT_SECRET` | **Production** | JWT signing secret. Unset → random ephemeral secret (sessions/reveals break on restart). |
| `UNILLM_ENCRYPTION_KEY` | Recommended | Fernet key for API-key-at-rest encryption. Unset → derived from JWT secret. |
| `UNILLM_RECOVERABLE_KEYS` | No | Default `true`: keys *may* opt in to being revealable later. Recoverability is a per-key choice made at creation and is **off by default** — an ordinary key is shown once and never stored in recoverable form. Set this to `false` to forbid the opt-in deployment-wide: creating a recoverable key returns 400, and `/api/keys/{id}/reveal` returns 404 even for keys that already have a stored copy. |
| `UNILLM_ADMIN_USERNAME` / `UNILLM_ADMIN_PASSWORD` | Bootstrap | Seed the first admin on startup (no-op if the user exists). |
| `UNILLM_ADMIN_SYNC` | No | `true` → reset the seeded admin's password/role on every restart (off by default). |
| `DATABASE_URL` | No | SQLAlchemy URL; default `sqlite:///./unillm.db`. Use PostgreSQL for teams. |
| `UNILLM_CONFIG` | No | Path to the YAML config (also settable via `--config`). |
| `UNILLM_MASTER_KEY` / `UNILLM_API_KEYS` | No | Static env-var API keys (fallback when not in DB). |
| `UNILLM_CORS_ORIGINS` | No | Comma-separated cross-origin allowlist. Default: same-origin only. |
| `UNILLM_TRUST_PROXY_HEADERS` | No | `true` → trust `X-Forwarded-For` for client IPs (only behind a trusted reverse proxy). |
| `UNILLM_LOGIN_RATE_LIMIT` | No | Login attempts allowed per client IP, as `<count>/<seconds>`. Default `30/60`. `off` disables. |
| `UNILLM_LOGIN_FAILURE_LIMIT` | No | Failed logins allowed per username across all IPs. Default `5/900`. Cleared on a successful login; `off` disables. |
| `UNILLM_DOCS_RATE_LIMIT` | No | Requests per client IP to `/docs`, `/redoc` and `/openapi.json`. Default `30/60`. `off` disables. |
| `UNILLM_SECURITY_HEADERS` | No | Default `true`: send CSP, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy` and HSTS. `false` sends none of them. |
| `UNILLM_CSP` | No | Replace the app Content-Security-Policy outright. `off` sends no policy but keeps the other headers. The `/docs` and `/redoc` policy is separate and unaffected. |
| `UNILLM_HSTS_MAX_AGE` | No | HSTS max-age in seconds, default `31536000`. `0` disables. Only sent over HTTPS. |
| `UNILLM_DEV_MODE` | No | `true` → allow unauthenticated requests (disabled whenever a DB is configured). |
| `UNILLM_SSH_KEYS` | No | Env-var fallback for SSH public keys: `name:pubkey:user,…` (DB is preferred). |

## API surface

| Endpoint | Auth | Description |
|---|---|---|
| `GET /health` | none | Health check |
| `GET /v1/models`, `GET /v1/models/{id}` | API key | List/inspect models |
| `POST /v1/chat/completions`, `POST /v1/completions` | API key | Inference (streaming supported) |
| `POST /api/auth/login` | — | Get a JWT |
| `GET/PUT /api/users/me`, `GET/POST/PUT /api/users*` | JWT (admin for user mgmt) | Self-service & user administration |
| `/api/projects*`, `/api/projects/{id}/members*`, `/api/projects/{id}/keys*` | JWT + project role | Projects, membership, API keys |
| `GET /api/keys/{id}/reveal`, `DELETE /api/keys/{id}` | JWT, project admin | Reveal a key that opted in at creation (audited) / revoke keys |
| `GET /api/config` | JWT | Deployment flags the UI needs, e.g. whether recoverable keys are allowed |
| `/api/ssh-keys*` | JWT | Manage & validate your SSH keys |
| `GET/PUT/DELETE /api/pricing*` | JWT, admin | Per-model pricing |
| `GET /api/logs/requests`, `/api/logs/stats` | JWT (scoped to your projects) | Request logs & usage stats |
| `GET /api/logs/audit` | JWT, admin | Audit trail |
| `GET /docs`, `/redoc`, `/openapi.json` | none (rate limited) | Interactive API docs |

## Production checklist

- Set a stable `UNILLM_JWT_SECRET` and a dedicated `UNILLM_ENCRYPTION_KEY` (via a secret manager).
- Decide on key recoverability: keys are show-once unless the creator opts in, so the default is already conservative. Set `UNILLM_RECOVERABLE_KEYS=false` if no key should ever be recoverable, which also disables reveal for keys created before the change.
- Use PostgreSQL (`DATABASE_URL`) — the SQLite default is for single-user/dev use.
- Terminate TLS at a reverse proxy; set `UNILLM_TRUST_PROXY_HEADERS=true` there and bind UniLLM to localhost (default bind is `0.0.0.0`).
- Security headers are on by default. If your reverse proxy also sets them, drop one of the two — UniLLM only fills in headers that are not already present, but a proxy that appends rather than replaces will produce duplicates.
- Review the built-in rate limits (`UNILLM_LOGIN_RATE_LIMIT`, `UNILLM_LOGIN_FAILURE_LIMIT`, `UNILLM_DOCS_RATE_LIMIT`). They are per-process, so with several replicas each enforces its own share — add a shared limiter at the reverse proxy if you run more than one.
- Set model pricing in the console so request costs are recorded.
- Back up the database — it holds users, hashed keys, and the audit trail.

## Development

```bash
pytest tests/                 # run tests
alembic revision --autogenerate -m "..."   # new migration (alembic.ini points at unillm.db)
```

Project layout:

```
unillm/
├── config.py            # secret resolution (JWT / Fernet)
├── types.py             # Pydantic request/response models
├── client/ssh_signer.py # client-side SSH key signing utility
├── db/                  # SQLAlchemy models, CRUD, session management
├── llm/                 # backend handlers: vertex_ai, vertex_ai_kms, vllm
└── proxy/
    ├── proxy_server.py  # FastAPI app, /v1 endpoints, request logging
    ├── api_routes.py    # /api management endpoints (JWT)
    ├── auth.py          # API-key auth + model access control
    ├── ssh_auth.py      # SSH signature verification
    └── proxy_cli.py     # CLI entry point
frontend/                # React admin console (Vite; builds into unillm/static)
alembic/                 # database migrations
tests/                   # pytest suite
```

The `litellm/` directory is a vendored upstream reference copy (gitignored, not part of this project).

## License

See [LICENSE](LICENSE).
