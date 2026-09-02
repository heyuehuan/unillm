---
title: Management API
group: API reference
keywords: [api, jwt, login, endpoints, roles, rate limit, token]
---

Everything the console does goes through `/api`. The console is one client of this API,
so scripts can do the same work.

## Getting a token

```bash
curl -X POST http://your-server:4000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "..."}'
```

The response carries a JWT valid for 24 hours. Send it as `Authorization: Bearer <jwt>` on
every other `/api` call.

A password change, an admin password reset, a role change or a deactivation invalidates
every outstanding token for that user.

## Endpoints

| Endpoint | Required access | Purpose |
| --- | --- | --- |
| `POST /api/auth/login` | none | Exchange username and password for a JWT. |
| `GET /api/config` | any signed-in user | Deployment flags the console needs, for example whether keys are recoverable. |
| `GET,PUT /api/users/me` | self | Read or update your own profile and password. |
| `GET,POST /api/users`, `PUT /api/users/{id}` | global admin | List, create and edit accounts. |
| `GET,POST /api/projects`, `GET,PUT /api/projects/{id}` | member, or global admin | Projects, including archive and unarchive. |
| `/api/projects/{id}/members`, `/api/projects/{id}/member-candidates` | project admin | Membership. Personal projects reject these. |
| `GET,POST /api/projects/{id}/keys` | project developer or admin to read, admin to create | Project API keys. |
| `PUT /api/keys/{id}`, `DELETE /api/keys/{id}` | project admin | Rename, restrict or revoke a key. |
| `GET /api/keys/{id}/reveal` | project developer or admin | Return a key in plaintext. Audited, and only when the deployment keeps recoverable keys. |
| `/api/ssh-keys`, `POST /api/ssh-keys/validate` | self | Register, edit, delete and test your SSH keys. |
| `GET /api/models` | any signed-in user | Model list with health, traffic and pricing merged. |
| `GET,PUT,DELETE /api/pricing` | global admin | Per-model prices. |
| `GET,PUT,DELETE /api/settings` | global admin | Runtime server settings. |
| `GET /api/logs/requests`, `GET /api/logs/stats` | any signed-in user, scoped to their projects | Request log and aggregates. |
| `GET /api/logs/audit` | global admin | Audit trail. |

## Scoping

A global admin sees every project. Everyone else sees the projects they belong to, and log
queries are filtered to those projects on the server. Passing `mine=true` narrows a log or
stats query to your own SSH username. Filtering by another person's `ssh_username` is
allowed for global admins only.

Archived projects still appear in log queries. Archiving stops the keys working and hides
the project from the active list. It does not erase past usage.

## Errors

Errors use the FastAPI shape, `{"detail": ...}`, where `detail` is a string, or a list of
field errors for a validation failure. A 401 on any endpoint other than login means the
token expired or was invalidated, and the console signs you out when it sees one.

## Rate limits

Login and the OpenAPI pages are rate limited per client IP. Defaults are 30 attempts per
minute per IP and username pair, 100 per minute per IP, 5 failed logins per 15 minutes per
username, and 30 requests per minute to `/docs`. A successful login clears the failure
counter. Each limit is configurable through an environment variable, listed in
[Run and configure the server](#/documentation/dev-run).

Limits are counted per process. Several replicas each enforce their own share, so put a
shared limiter at the reverse proxy if you run more than one.
