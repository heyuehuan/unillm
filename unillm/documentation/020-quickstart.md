---
title: Quickstart
group: Getting started
keywords: [start, first request, api key, sign in, login]
---

From a fresh account to a first response. This assumes someone has already deployed the
server and given you an account. To run your own instance, read
[Run and configure the server](#/documentation/dev-run).

## 1. Sign in

Open the server URL in a browser. Sign in with the username and password your admin gave
you. A session lasts 24 hours, then you sign in again.

## 2. Open your project

Go to [Projects](#/projects). Every account with the `user` role gets a personal project
at creation time. Team projects are created by an admin, and you see one once you are
added as a member.

## 3. Get an API key

Open the project and stay on the Keys tab. A key is displayed in full once, at the moment
it is created. What you can do later depends on the deployment:

- If the key row has a **Reveal** button, the server keeps an encrypted copy and can show
  you the key again. Each reveal is written to the audit log.
- If it does not, create a new key with **New key** and copy the value immediately.
  Closing the dialog discards it.

Creating and revoking keys needs the project `admin` role. Revealing needs `developer` or
`admin`.

## 4. Check which models the key allows

Each key lists its allowed models. The value `all` means every model in the server config.
A request for a model outside that list is rejected with 403. The names a key can use are
also available at runtime:

```bash
curl http://your-server:4000/v1/models \
  -H "Authorization: Bearer sk-your-key"
```

## 5. Send a request

```bash
curl http://your-server:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "gemini-2.5-flash-lite",
       "messages": [{"role": "user", "content": "Hello"}]}'
```

A response means you are done. Open [Logs](#/logs) and your request is the top row, with
token counts, latency and cost.

> If the server answers 401 with a message about SSH, the deployment requires signed keys.
> Register a key first, then sign your API key before sending it. See
> [Register an SSH key](#/documentation/ssh-keys).
