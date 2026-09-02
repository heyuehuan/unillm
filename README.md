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
- API keys stored as SHA-256 hashes. By default the server also keeps an encrypted-at-rest copy so a project's developers and admins can reveal a key again later (audited); set `UNILLM_RECOVERABLE_KEYS=false` and no copy is kept, making every key show-once
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
      supports_logprobs: true             # opt in to logprobs (see below)

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
  # drop_params: false   # strip unsupported params instead of returning 400
```

### Log probabilities

`logprobs` / `top_logprobs` are opt-in per model. Add `supports_logprobs: true` to a
model's `unillm_params` and clients can request them the usual OpenAI way:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-..." \
  -d '{"model": "gemini-2.5-flash-lite", "messages": [{"role":"user","content":"hi"}],
       "logprobs": true, "top_logprobs": 5}'
```

Responses carry OpenAI's shape (`choices[].logprobs.content[].{token,logprob,top_logprobs}`)
regardless of backend — UniLLM translates Gemini's `logprobsResult` into it, and passes
vLLM's through unchanged.

It's opt-in because backend support is per-model and shifts between releases: on Vertex AI
`gemini-2.0/2.5-flash` serve logprobs, while `gemini-3.x` reject them with *"Logprobs is not
supported for this model"*. Enabling it on a model that can't do it turns a clear 400 from
UniLLM into an opaque upstream error, so confirm the model serves logprobs before flipping
the flag.

Requesting logprobs from a model without the flag returns **400** naming the model and the
rejected params; the full explanation (which flag to set, on which backend model) goes to
the request log rather than to the API caller. Set `drop_params: true` under
`general_settings` to strip unsupported params and complete the request instead — same
semantics as litellm's `litellm_settings: drop_params`.

Coverage: chat completions on all three backends. `/v1/completions` supports logprobs only
on vLLM — the Vertex handlers synthesize text completions from a chat call, and Gemini's
per-token output can't be reshaped into the legacy `{tokens, token_logprobs, text_offset}`
format without inventing byte offsets.

#### Trimming the payload with `logprobs_min_p`

`top_logprobs` is a fixed count, so asking for 20 pays for 20 at *every* generated token —
including positions where the model was almost certain and ranks 3-20 are noise. A
500-token answer at `top_logprobs: 20` is roughly 1 MB of JSON against 2 KB of text.

`logprobs_min_p` drops returned alternatives whose probability is below a floor:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-..." \
  -d '{"model": "gemini-2.5-flash-lite", "messages": [{"role":"user","content":"hi"}],
       "logprobs": true, "top_logprobs": 20, "logprobs_min_p": 0.01}'
```

It is a UniLLM-only parameter applied to the response, not forwarded to the backend, and it
works on both completion endpoints, streamed and not. The chosen token's own `logprob` is
never filtered — a sampled token can legitimately sit far down the tail, and dropping it
would discard the one value every caller needs. Only the `top_logprobs` alternatives are
thinned, so it requires a non-zero `top_logprobs` (a floor with nothing to filter is
rejected with a 422 rather than silently ignored).

**Pick the floor around `0.01`, not lower.** The saving comes from cutting *into* the top-k,
and the top 20 of a real next-token distribution nearly always sit above `1e-4` — so a floor
of `0.0001` typically removes nothing at all. At `0.01` a confident position keeps around 5-10
alternatives instead of 20; genuinely uncertain positions keep all 20, which is the point.

Note this is a filter over what the backend already returned, never a request for more. It
cannot be used to ask for "everything above p": the OpenAI wire format only accepts a top-k
count, and a bare threshold is unbounded anyway — probabilities sum to 1, so a floor of
`0.0001` permits up to 10,000 alternatives at a single position where `top_logprobs` caps
at 20.

#### Shrinking the payload with `logprobs_format: "compact"`

The OpenAI shape spends four JSON keys and a nested object on every single alternative.
`logprobs_format: "compact"` collapses each position to a plain `{token: logprob}` map with
the token already decoded, which is roughly a third of the bytes:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-..." \
  -d '{"model": "gemini-2.5-flash-lite", "messages": [{"role":"user","content":"Yes or no?"}],
       "logprobs": true, "top_logprobs": 20, "logprobs_format": "compact"}'
```

```jsonc
"logprobs": {
  "format": "compact",
  "tokens": ["Yes"],
  "token_logprobs": [-0.05],
  "top_logprobs": [{"Yes": -0.05, "No": -3.2}]   // decoded tokens, straight to logprobs
}
```

Measured against the standard shape, that is 61% smaller at `top_logprobs: 5` and 65% smaller
at `top_logprobs: 20` — a bigger saving than any `logprobs_min_p` floor can produce, because
it removes per-entry structure rather than entries.

Three things to know before switching:

- **It is not OpenAI-compatible.** An OpenAI SDK will not parse it. Use it when you read the
  response yourself, which is the normal case for classification and scoring.
- **The `bytes` field is gone.** If you need the raw UTF-8 of a token — for tokens that are
  partial multi-byte sequences — stay on the default format.
- **Duplicate decoded tokens collapse.** Two token ids can decode to the same string; the map
  has room for one, and keeps the higher logprob.

It is opt-in and defaults to `"openai"`, so existing clients see byte-identical responses. It
works streamed and not, and combines with `logprobs_min_p`. On `/v1/completions` it is accepted
but has no effect: that endpoint already returns the flat shape.

#### Returning only the tail with `logprobs_last_n`

Most logprobs requests care about one position, not all of them: the classification token,
the final word, the yes or no. `logprobs_last_n` returns logprobs for only the final N
generated positions and drops the rest:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-..." \
  -d '{"model": "gemini-2.5-flash-lite", "messages": [{"role":"user","content":"Yes or no?"}],
       "logprobs": true, "top_logprobs": 20, "logprobs_last_n": 1}'
```

That request returns one position instead of every position the model produced. Combined with
`logprobs_format: "compact"` it is a few hundred bytes where the default shape would be tens of
kilobytes.

No provider offers this. OpenAI, Gemini, vLLM, TGI, llama.cpp and Together all take a top-k
count and apply it to every position; the nearest thing anywhere is Fireworks' `echo_last`,
which trims the *prompt* suffix rather than the generation. So this is a UniLLM-only parameter
applied to the response, like `logprobs_min_p` and `logprobs_format` — the model still generates
every token and the backend still returns every logprob. What changes is how much crosses the
wire to you, not what you pay for inference. Use `max_tokens` to bound the generation itself.

It works on both endpoints and requires `logprobs` (on `/v1/completions`, `logprobs: 0` counts —
that plus `logprobs_last_n: 1` is the smallest useful logprobs request there is). It composes
with `logprobs_min_p` and `logprobs_format`, and it makes a request estimable for the size cap
below even without `max_tokens`, because however long the generation runs only N positions come
back.

**Streaming defers the logprobs to one chunk at the end.** Which positions are the last N is not
knowable until the stream finishes, so they cannot be sent as they arrive. Content deltas still
stream live and unchanged; the logprobs arrive in a final chunk with an empty delta, emitted just
before `data: [DONE]`.

#### Capping the total size with `logprobs_max_bytes`

Logprobs are the one part of a response whose size the caller controls and the model does not.
`logprobs_max_bytes` caps how many bytes of them a single request may return. It defaults to
**1 MB**, which is about 540 generated tokens at `top_logprobs: 20` in the OpenAI shape, or
about 1,500 in the compact one.

Set it in the config file:

```yaml
general_settings:
  logprobs_max_bytes: 2097152   # 2 MB
```

...or change it at runtime under **Admin → Server Settings**, which needs a global admin and is
written to the audit log. Resolution runs database → config file → built-in default, so clearing
the value in the console falls back to whatever the file says. Every authenticated user can read
the effective value from `GET /api/config`.

The cap is enforced twice:

- **Before inference**, when the request is provably too big. A request whose *smallest
  possible* logprobs payload already exceeds the cap is rejected with **413** and a message
  naming the limit. This needs a bound on the number of positions, which either `max_tokens`
  or `logprobs_last_n` supplies. The check uses a lower bound, not an average, so it never
  refuses a request that would have fit.
- **After generation**, exactly. Anything still over budget is truncated to a prefix and marked
  with `"truncated": true` and `"truncated_at": <positions returned>`. Truncation is never
  silent — without the marker a caller could not tell a withheld tail from a short answer.

The budget covers the whole request, so `n: 4` shares one allowance across the four choices, and
a streamed response spends it across chunks rather than per chunk.

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
| `UNILLM_RECOVERABLE_KEYS` | No | Default `true`: every new key keeps a Fernet-encrypted copy, so `/api/keys/{id}/reveal` can return it to a project developer or admin. This is a deployment-wide decision, not a per-key one. Set it to `false` and new keys store nothing at all — they are shown once at creation — and reveal returns 404 for every key, including ones encrypted while it was on. Turning it back on does not recover keys minted while it was off. |
| `UNILLM_ADMIN_USERNAME` / `UNILLM_ADMIN_PASSWORD` | Bootstrap | Seed the first admin on startup (no-op if the user exists). |
| `UNILLM_ADMIN_SYNC` | No | `true` → reset the seeded admin's password/role on every restart (off by default). |
| `DATABASE_URL` | No | SQLAlchemy URL; default `sqlite:///./unillm.db`. Use PostgreSQL for teams. |
| `UNILLM_CONFIG` | No | Path to the YAML config (also settable via `--config`). |
| `UNILLM_MASTER_KEY` / `UNILLM_API_KEYS` | No | Static env-var API keys (fallback when not in DB). |
| `UNILLM_CORS_ORIGINS` | No | Comma-separated cross-origin allowlist. Default: same-origin only. |
| `UNILLM_TRUST_PROXY_HEADERS` | No | `true` → trust `X-Forwarded-For` for client IPs (only behind a trusted reverse proxy). |
| `UNILLM_TRUSTED_PROXY_HOPS` | No | Number of reverse proxies in front of UniLLM (default `1`). Decides which `X-Forwarded-For` entry is the client. |
| `UNILLM_LOGIN_RATE_LIMIT` | No | Login attempts allowed per client IP **and username**, as `<count>/<seconds>`. Default `30/60`. `off` disables. |
| `UNILLM_LOGIN_IP_RATE_LIMIT` | No | Ceiling on login attempts per client IP regardless of username. Default `100/60`. `off` disables. |
| `UNILLM_LOGIN_FAILURE_LIMIT` | No | Failed logins allowed per username across all IPs. Default `5/900`. Cleared on a successful login; `off` disables. |
| `UNILLM_DOCS_RATE_LIMIT` | No | Requests per client IP to `/docs`, `/redoc` and `/openapi.json`. Default `30/60`. `off` disables. |
| `UNILLM_LOGIN_AUDIT_LIMIT` | No | How often a throttled login adds an audit row, per key. Default `1/300`. `off` records every rejection. |
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
| `GET /api/keys/{id}/reveal` | JWT, project developer or admin | Reveal a key's plaintext (audited). Viewers are refused, as they are for the key list |
| `DELETE /api/keys/{id}` | JWT, project admin | Revoke a key |
| `GET /api/config` | JWT | Deployment flags the UI needs, e.g. whether recoverable keys are allowed |
| `/api/ssh-keys*` | JWT | Manage & validate your SSH keys |
| `GET/PUT/DELETE /api/pricing*` | JWT, admin | Per-model pricing |
| `GET /api/logs/requests`, `/api/logs/stats` | JWT (scoped to your projects) | Request logs & usage stats |
| `GET /api/logs/audit` | JWT, admin | Audit trail |
| `GET /docs`, `/redoc`, `/openapi.json` | none (rate limited) | Interactive API docs |

## Production checklist

- Set a stable `UNILLM_JWT_SECRET` and a dedicated `UNILLM_ENCRYPTION_KEY` (via a secret manager).
- Decide on key recoverability. The default keeps a decryptable copy of every key, which is what makes Reveal work; it also means whoever holds the database and the environment holds the keys. Set `UNILLM_RECOVERABLE_KEYS=false` if that trade is wrong for your deployment — keys then exist in plaintext only in the response that creates them, and reveal is disabled for keys created before the change too.
- Use PostgreSQL (`DATABASE_URL`) — the SQLite default is for single-user/dev use.
- Terminate TLS at a reverse proxy; set `UNILLM_TRUST_PROXY_HEADERS=true` there and bind UniLLM to localhost (default bind is `0.0.0.0`).
  Set `UNILLM_TRUSTED_PROXY_HOPS` to the number of proxies in the chain — UniLLM reads that many entries back from the end of
  `X-Forwarded-For`, because the leading entries of that header are written by the client. Leaving the trust flag off behind a
  proxy is also wrong: every client then shares the proxy's address, so one caller's failed logins throttle everyone.
- Security headers are on by default. If your reverse proxy also sets them, drop one of the two — UniLLM only fills in headers that are not already present, but a proxy that appends rather than replaces will produce duplicates.
- Review the built-in rate limits (`UNILLM_LOGIN_RATE_LIMIT`, `UNILLM_LOGIN_IP_RATE_LIMIT`, `UNILLM_LOGIN_FAILURE_LIMIT`, `UNILLM_DOCS_RATE_LIMIT`). They are per-process, so with several replicas each enforces its own share — add a shared limiter at the reverse proxy if you run more than one.
- Logins are budgeted three ways: per (IP, username) pair, per IP, and per username across all addresses. The pair budget is what keeps one attacker's guesses from throttling everybody who shares an address; the per-IP ceiling still bounds how much password-hashing work a single address can demand.
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
