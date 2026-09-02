---
title: Models and pricing
group: Console guide
keywords: [models, alias, health, pricing, cost, vertex, vllm, kms]
---

The [Models](#/models) page lists every model the deployment knows about: models in the
server config, models that appear in the request log, and models with a price set. Each
card shows the backend it maps to, recent health, total requests, and the price used to
cost them.

## Aliases and backends

Clients send an alias. The server config maps that alias to a backend model:

```yaml
model_list:
  - model_name: gemini-2.5-flash-lite      # the alias clients send
    unillm_params:
      model: gemini-2.5-flash-lite         # the backend model
      project: your-gcp-project-id
      location: us-central1
```

`model_type` selects the backend: `vertex-ai` (the default), `vertex-ai-kms` for Vertex AI
with a customer-managed key, and `vllm` for vLLM or any OpenAI-compatible server. Adding a
model needs a config edit and a restart.

Aliases are how you decouple client code from backend naming. Several aliases can point at
the same backend model with different settings.

## Health

Health is read from the last ten requests for that alias.

| Status | Meaning |
| --- | --- |
| Healthy | The most recent request returned 2xx. |
| Issues | The most recent request returned 4xx or 5xx. |
| No data | No requests yet, so nothing to judge. |

This is a traffic signal rather than a probe. A model nobody calls stays at "No data", and
a model whose last call was a client mistake shows "Issues".

## Pricing

Cost is computed by UniLLM from a price table a global admin maintains under Admin,
Pricing. Prices are per one million tokens, priced separately for input and output. There
is no lookup of provider list prices, so a model with no price records zero cost.

An alias with no price of its own falls back to the price on its backend model, and the
console labels that as inherited. This keeps costs flowing when you add an alias, and the
inherited label is a prompt to confirm the variant really bills at the same rate.

Changing a price applies to future requests. Rows already written keep the cost they were
recorded with, so historical totals stay stable.
