---
title: Make an LLM call
group: Getting started
keywords: [openai sdk, python, curl, streaming, labels, errors]
---

Any OpenAI client works. Point it at `/v1` on the UniLLM host and use a project API key.
The model name is the alias configured on the server, which you can list with
`GET /v1/models`.

## Python

```python
import openai

client = openai.OpenAI(
    api_key="sk-your-key",
    base_url="http://your-server:4000/v1",
)

response = client.chat.completions.create(
    model="gemini-2.5-flash-lite",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

## Streaming

```python
stream = client.chat.completions.create(
    model="gemini-2.5-flash-lite",
    messages=[{"role": "user", "content": "Count to five"}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

Streamed responses are server-sent events in the OpenAI format and end with `data: [DONE]`.
The request log row is written when the stream finishes, so token counts and latency cover
the whole response.

## curl

```bash
curl http://your-server:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-flash-lite",
    "messages": [{"role": "user", "content": "Hello"}],
    "temperature": 0.2,
    "max_tokens": 256
  }'
```

## Labels

Add a `labels` object to the request body. It is stored on the request log row and never
forwarded to the backend, so it is a safe place for your own routing information such as a
team, a feature or an environment.

```json
{
  "model": "gemini-2.5-flash-lite",
  "messages": [{"role": "user", "content": "Hello"}],
  "labels": {"team": "search", "env": "staging"}
}
```

Labels appear in the detail panel on the [Logs](#/logs) page.

## Common errors

| Status | Meaning | Fix |
| --- | --- | --- |
| 401 | Missing or invalid API key, or a required SSH signature failed. | Check the key. If the deployment enforces signing, see [Register an SSH key](#/documentation/ssh-keys). |
| 403 | The key may not use that model, the project is archived, or the account is disabled. | Check the key allowed models on the project Keys tab. |
| 404 | Unknown model alias. | List valid names with `GET /v1/models`. |
| 400 | A parameter the model cannot serve, for example logprobs on a model without the flag. | See [Log probabilities](#/documentation/api-logprobs). |
| 413 | The requested logprobs payload is larger than the server allows. | Lower `top_logprobs`, or set `logprobs_last_n`. |
