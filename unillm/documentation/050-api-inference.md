---
title: Inference API
group: API reference
keywords: [v1, chat completions, completions, models, headers, parameters, openapi]
---

Base URL is `/v1` on the UniLLM host. Authentication is a project API key. The wire
format is OpenAI's, so an OpenAI SDK works without changes.

An interactive OpenAPI page is served at `/docs` on the same host, and the raw schema at
`/openapi.json`. Both are rate limited per client IP.

## Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/health` | none | Liveness check. Returns status and version. |
| GET | `/v1/models` | API key | List models this key may use. |
| GET | `/v1/models/{id}` | API key | Inspect one model. |
| POST | `/v1/chat/completions` | API key | Chat completion, streaming or not. |
| POST | `/v1/completions` | API key | Legacy text completion. |

Each `/v1/...` path is also served without the prefix, so `/chat/completions` behaves the
same as `/v1/chat/completions`.

## Authentication

Send the key in either header:

```
Authorization: Bearer sk-your-key
x-api-key: sk-your-key
```

Key resolution order is database key, then keys supplied through the `UNILLM_MASTER_KEY`
and `UNILLM_API_KEYS` environment variables, then reject.

When the deployment uses SSH attribution, the key value is the three-part signed string
described in [Register an SSH key](#/documentation/ssh-keys).

## Request fields

Forwarded to the backend:

| Field | Notes |
| --- | --- |
| `model` | The alias from the server config. Required. |
| `messages` | Chat messages. Required for `/v1/chat/completions`. |
| `prompt` | Required for `/v1/completions`. |
| `temperature` | 0 to 2. |
| `top_p` | 0 to 1. |
| `max_tokens` | Upper bound on generated tokens. |
| `stop` | String or list of strings. |
| `stream` | `true` switches to server-sent events. |
| `logprobs`, `top_logprobs` | Only on models configured with `supports_logprobs`. See [Log probabilities](#/documentation/api-logprobs). |

Handled by UniLLM and never forwarded:

| Field | Notes |
| --- | --- |
| `labels` | Flat string map stored on the request log row. |
| `logprobs_min_p`, `logprobs_last_n`, `logprobs_format` | Shape the logprobs in the response. |

Accepted and validated, then ignored: `n`, `presence_penalty`, `frequency_penalty`,
`user`, and the other OpenAI fields. They are accepted so an existing client does not
break. They do not change the result.

## Responses

The response body is the OpenAI shape: `id`, `object`, `created`, `model`, `choices` and
`usage`. Two additions appear only in the cases that produce them:

- `user` carries the SSH username when the request was signed.
- `warning` explains an unsigned request when the server runs `ssh_required: warning`.

Streamed responses are `text/event-stream` chunks in the OpenAI delta format, ending with
`data: [DONE]`.

## Status codes

| Status | Cause |
| --- | --- |
| 400 | A parameter this model cannot serve. The message names the model and the parameter. |
| 401 | Missing key, unknown key, or a failed SSH signature check. |
| 403 | The key may not use this model, the project is archived, or the account is disabled. |
| 404 | Unknown model alias. |
| 413 | The logprobs payload the request asks for is provably over the server limit. |
| 422 | The body failed validation, for example `top_logprobs` without `logprobs`. |
| 5xx | The backend failed. The upstream message is recorded in the request log, and a sanitized message is returned. |

Failed requests are logged too, with the status code and the error message, so a 4xx is
visible on the [Logs](#/logs) page rather than lost.

## Logging

Every request writes one row: timestamp, request id, project, key name and key prefix, SSH
username, client IP, model alias, backend model, backend type, stream flag, labels, status
code, error message, prompt and completion tokens, cost, and latency in milliseconds.

Prompts and completions are not stored.
