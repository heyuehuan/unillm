---
title: Run and configure the server
group: Developer guide
keywords: [install, deploy, config, yaml, environment variables, production, tls]
---

Requires Python 3.10 or newer.

## Install

```bash
pip install -r requirements_unillm.txt
# or, as a package
pip install -e .
```

## Configure the models

Copy `unillm_config.yaml_example` to `unillm_config.yaml` and describe each model:

```yaml
model_list:
  # Vertex AI Gemini (default model_type: vertex-ai)
  - model_name: gemini-2.5-flash-lite
    unillm_params:
      model: gemini-2.5-flash-lite
      project: your-gcp-project-id
      location: us-central1
      supports_logprobs: true

  # Vertex AI with a customer-managed encryption key
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
      api_key: ""

general_settings:
  port: 4000
  # ssh_required: none | warning | enforce
  # drop_params: false
  # logprobs_max_bytes: 1048576
```

Vertex AI backends use Application Default Credentials. Run
`gcloud auth application-default login` once on the host.

## Secrets and the first admin

```bash
export UNILLM_JWT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export UNILLM_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export UNILLM_ADMIN_USERNAME=admin
export UNILLM_ADMIN_PASSWORD='a-strong-password'
```

Without `UNILLM_JWT_SECRET` the server generates an ephemeral one, and every session ends
at the next restart. The admin account is seeded on startup when it does not already exist,
and its API key is printed once to stdout.

## Run

```bash
python -m unillm.proxy.proxy_cli --config unillm_config.yaml --port 4000
```

Alembic migrations run on startup. The default database is `sqlite:///./unillm.db`. Set
`DATABASE_URL` to a PostgreSQL URL for anything with more than one user.

The console is served at `/`, this documentation at `/documentation`, and the OpenAPI page
at `/docs`.

## Environment variables

| Variable | Description |
| --- | --- |
| `UNILLM_JWT_SECRET` | Signs console tokens. Required in production. |
| `UNILLM_ENCRYPTION_KEY` | Fernet key for API keys at rest. Derived from the JWT secret when unset. |
| `UNILLM_RECOVERABLE_KEYS` | Default `true`. `false` stops storing a decryptable copy, which makes every key show-once and disables reveal for existing keys too. |
| `UNILLM_ADMIN_USERNAME`, `UNILLM_ADMIN_PASSWORD` | Seed the first admin on startup. |
| `UNILLM_ADMIN_SYNC` | `true` resets the seeded admin password and role on every restart. |
| `DATABASE_URL` | SQLAlchemy URL. Default `sqlite:///./unillm.db`. |
| `UNILLM_CONFIG` | Path to the YAML config, same as `--config`. |
| `UNILLM_MASTER_KEY`, `UNILLM_API_KEYS` | Static API keys, used when a key is not in the database. |
| `UNILLM_CORS_ORIGINS` | Comma-separated cross-origin allowlist. Same-origin only by default. |
| `UNILLM_TRUST_PROXY_HEADERS` | `true` trusts `X-Forwarded-For`. Only set this behind a proxy you control. |
| `UNILLM_TRUSTED_PROXY_HOPS` | How many proxies sit in front. Decides which `X-Forwarded-For` entry is the client. Default `1`. |
| `UNILLM_LOGIN_RATE_LIMIT` | Login attempts per IP and username pair, as `count/seconds`. Default `30/60`. |
| `UNILLM_LOGIN_IP_RATE_LIMIT` | Login attempts per IP regardless of username. Default `100/60`. |
| `UNILLM_LOGIN_FAILURE_LIMIT` | Failed logins per username across all addresses. Default `5/900`. |
| `UNILLM_DOCS_RATE_LIMIT` | Requests per IP to `/docs`, `/redoc` and `/openapi.json`. Default `30/60`. |
| `UNILLM_LOGIN_AUDIT_LIMIT` | How often a throttled login writes an audit row. Default `1/300`. |
| `UNILLM_SECURITY_HEADERS` | Default `true`. Sends CSP, frame, sniffing, referrer, permissions and HSTS headers. |
| `UNILLM_CSP` | Replaces the app Content-Security-Policy. `off` sends none. |
| `UNILLM_HSTS_MAX_AGE` | HSTS max-age in seconds, default `31536000`. Only sent over HTTPS. |
| `UNILLM_DEV_MODE` | `true` allows unauthenticated requests. Ignored whenever a database is configured. |
| `UNILLM_SSH_KEYS` | Fallback SSH public keys as `name:pubkey:user,...`. The database is preferred. |

Each rate limit accepts `off` to disable it.

## Production checklist

- Set a stable `UNILLM_JWT_SECRET` and a dedicated `UNILLM_ENCRYPTION_KEY` from a secret
  manager.
- Decide on key recoverability. The default keeps a decryptable copy of every key, which is
  what makes Reveal work. It also means whoever holds the database and the environment holds
  the keys.
- Use PostgreSQL. The SQLite default is for single-user and development use.
- Terminate TLS at a reverse proxy, bind UniLLM to localhost, and set
  `UNILLM_TRUST_PROXY_HEADERS=true` with the right hop count. Leaving the trust flag off
  behind a proxy makes every client share the proxy address, so one caller's failed logins
  throttle everybody.
- Review the login and docs rate limits. They are per process, so add a shared limiter at
  the proxy if you run several replicas.
- Set model pricing so requests are costed.
- Back up the database. It holds users, hashed keys and the audit trail.
