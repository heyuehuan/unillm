---
title: Log probabilities
group: API reference
keywords: [logprobs, top_logprobs, compact, min_p, last_n, max_bytes, classification]
---

Log probabilities are opt-in per model, because backend support varies by model and
changes between releases. Add `supports_logprobs: true` to a model in the server config,
and clients can request them the usual OpenAI way.

```bash
curl http://your-server:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model": "gemini-2.5-flash-lite",
       "messages": [{"role": "user", "content": "hi"}],
       "logprobs": true, "top_logprobs": 5}'
```

Responses use the OpenAI shape on every backend:
`choices[].logprobs.content[].{token, logprob, top_logprobs}`. Gemini's `logprobsResult` is
translated into it, and vLLM's is passed through.

Asking a model that lacks the flag returns 400 naming the model and the rejected
parameters. Set `drop_params: true` under `general_settings` to strip unsupported
parameters and complete the request instead.

Coverage: chat completions on all three backends. `/v1/completions` serves logprobs on
vLLM only, because the Vertex handlers build text completions from a chat call and Gemini
output cannot be reshaped into the legacy `{tokens, token_logprobs, text_offset}` format
without inventing byte offsets.

## Size controls

`top_logprobs` is a fixed count applied at every generated position, so a long answer at
`top_logprobs: 20` can be a megabyte of JSON behind two kilobytes of text. These four
parameters exist to cut that down. All are applied to the response, and none of them
change what the backend generates or what inference costs.

| Parameter | Effect | Typical use |
| --- | --- | --- |
| `logprobs_min_p` | Drops alternatives below a probability floor. | `0.01`. Confident positions keep 5 to 10 alternatives instead of 20. |
| `logprobs_format` | `compact` collapses each position to a `{token: logprob}` map. | Roughly a third of the bytes, when you parse the response yourself. |
| `logprobs_last_n` | Returns only the final N positions. | `1` for a classification or yes-or-no token. |
| `logprobs_max_bytes` | Server-side ceiling on the whole payload. | Set by an admin, not by the caller. |

### logprobs_min_p

```json
{"logprobs": true, "top_logprobs": 20, "logprobs_min_p": 0.01}
```

Pick the floor around `0.01`. The top 20 entries of a real next-token distribution almost
always sit above `1e-4`, so a floor of `0.0001` usually removes nothing. The chosen token's
own logprob is never filtered, because a sampled token can legitimately sit far down the
tail. A floor requires a non-zero `top_logprobs`, otherwise the request is rejected with
422 rather than silently ignored.

This filters what the backend already returned. It cannot ask for more: the OpenAI wire
format only accepts a top-k count.

### logprobs_format

```json
{"logprobs": true, "top_logprobs": 20, "logprobs_format": "compact"}
```

```json
"logprobs": {
  "format": "compact",
  "tokens": ["Yes"],
  "token_logprobs": [-0.05],
  "top_logprobs": [{"Yes": -0.05, "No": -3.2}]
}
```

Measured against the standard shape, that is 61% smaller at `top_logprobs: 5` and 65%
smaller at `top_logprobs: 20`. Three things to know before switching:

- An OpenAI SDK will not parse it. Use it where you read the response yourself.
- The per-token `bytes` field is gone. Stay on the default format if you need the raw
  UTF-8 of a token.
- Two token ids can decode to the same string. The map keeps the higher logprob.

The default is `openai`, so existing clients see byte-identical responses. On
`/v1/completions` the parameter is accepted and has no effect, since that endpoint already
returns the flat shape.

### logprobs_last_n

```json
{"logprobs": true, "top_logprobs": 20, "logprobs_last_n": 1}
```

Returns logprobs for the final N generated positions only. Combined with
`logprobs_format: "compact"` this is a few hundred bytes where the default shape would be
tens of kilobytes.

Under streaming the logprobs cannot be sent as they arrive, because which positions are
the last N is unknown until the stream ends. Content deltas still stream live, and the
logprobs arrive in a final chunk with an empty delta, just before `data: [DONE]`.

### logprobs_max_bytes

A server-side cap on how many bytes of logprobs one request may return. The default is
1 MB, which is about 540 generated tokens at `top_logprobs: 20` in the OpenAI shape, or
about 1500 in the compact one.

Set it in the config file:

```yaml
general_settings:
  logprobs_max_bytes: 2097152   # 2 MB
```

A global admin can also change it at runtime under Admin, Server settings. Resolution runs
database, then config file, then built-in default, so clearing the console value falls back
to the file. Any signed-in user can read the effective value from `GET /api/config`.

The cap is enforced twice:

- Before inference, when the request is provably too big. This needs a bound on the number
  of positions, which either `max_tokens` or `logprobs_last_n` supplies. The check uses a
  lower bound, so it never refuses a request that would have fit. Rejection is 413.
- After generation, exactly. Anything still over budget is truncated to a prefix and marked
  with `"truncated": true` and `"truncated_at": <positions returned>`.

The budget covers the whole request, so `n: 4` shares one allowance across the four
choices, and a streamed response spends it across chunks rather than per chunk.
