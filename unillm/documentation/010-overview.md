---
title: What UniLLM is
group: Getting started
keywords: [overview, architecture, backends, glossary, proxy]
---

UniLLM is a self-hosted proxy that sits between your code and one or more LLM backends.
Your client speaks the OpenAI API. UniLLM checks the API key, checks that the key may use
the requested model, forwards the request to the backend that serves that model, and
records what happened.

Backends supported today:

- Vertex AI Gemini, using Google Application Default Credentials.
- Vertex AI Gemini with a customer-managed encryption key (Cloud KMS).
- vLLM, or any other server exposing an OpenAI-compatible API.

## Why a proxy

- One base URL and one key format for every backend.
- API keys belong to a project and can be limited to specific models.
- Every request is logged with tokens, latency, status, cost and caller.
- Every management action is written to an append-only audit trail.
- Cost is computed from a pricing table an admin maintains.

## Request flow

```
OpenAI SDK / curl ──► UniLLM /v1  ──►  Vertex AI Gemini
                        │          ──►  Vertex AI + KMS
This console ─► /api ───┤          ──►  vLLM or OpenAI-compatible
                        ▼
              SQLite or PostgreSQL
      users · projects · api_keys · ssh_keys
      request_logs · audit_logs · model_pricing
```

## Credentials

They are separate. An API key cannot read the console, and a console session cannot call
the inference endpoints.

| Credential | Used for | Where it comes from |
| --- | --- | --- |
| `sk-…` | Inference endpoints under `/v1` | Created inside a project. See [Projects and API keys](#/documentation/projects). |
| JWT | This console and the `/api` endpoints | Issued by `POST /api/auth/login`, valid 24 hours. |

## Vocabulary

| Term | Meaning |
| --- | --- |
| Project | A container for API keys, members and usage. Every request belongs to one. |
| Personal project | A single-owner project created for each user account. It takes no members. |
| Model alias | The model name clients send, for example `gemini-2.5-flash-lite`. The server config maps it to a backend model. |
| Backend | The service that runs the model: Vertex AI, Vertex AI with KMS, or vLLM. |
| SSH attribution | An optional signature that names the individual behind a shared project key. |
| Request log | One row per inference request, successful or failed. |
| Audit log | One row per management action, for example a login, a key reveal or a role change. |

## Next steps

- [Quickstart](#/documentation/quickstart) for a first response in five minutes.
- [Inference API](#/documentation/api-inference) for the endpoint contract.
- [Run and configure the server](#/documentation/dev-run) to deploy your own instance.
