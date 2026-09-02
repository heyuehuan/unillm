---
title: Google Cloud auth
group: Console guide
keywords: [google, gcloud, adc, credentials, expired, refresh, reauthenticate, vertex, gemini, token]
---

Every Gemini call this proxy makes is signed with Google Application Default Credentials
sitting on the server. When those credentials come from a person's `gcloud` login rather
than a service account key file, Google expires them roughly once a day. The proxy keeps
answering, but every Vertex request starts failing with a refresh error until someone logs
in again.

[Google Cloud auth](#/gcp-auth) is that login, moved into the console. Any user or admin
can check the credentials and renew them from a browser, without a shell on the proxy host.

{{#if gcp_adk=disabled}}
> **This server has the feature turned off.** Neither the page nor its sidebar entry
> appears. An operator turns it on by setting `ALLOW_GCP_ADC_TOKEN_REFRESH: true` under
> `general_settings` in the server config.
{{/if}}

## Is anything wrong?

The page answers that two ways, and either one is enough to unlock the renewal.

**From the request log.** A credential failure on a Vertex model within the last hour,
with no successful Vertex call in that same hour, means the credentials are dead. Failures
are matched on the shapes Google actually returns — `invalid_grant`, `RefreshError`,
`DefaultCredentialsError`, 401 and 403 — so an ordinary timeout or a rejected parameter
does not count. The window comes from `stale_after_seconds` in the config.

**From a health test.** A quiet deployment logs nothing, so the log cannot tell you
anything. Press **Run health test** and the server sends one short call to the cheapest
model in the deployment, `gemini-2.5-flash-lite` by default:

```
Health test, reply 'hi' only.
```

It comes back healthy with the model's reply and the round-trip time, or unhealthy with
the error. An unhealthy result caused by credentials also unlocks the renewal, so this is
the button to press when you suspect the credentials but nothing has been logged yet.

## Renewing the credentials

**Start re-authentication** runs `gcloud auth application-default login` on the proxy host
and shows you what it prints. Then:

1. Copy the sign-in URL the page shows, and open it in your own browser.
2. Sign in as a Google account with access to the project.
3. Google gives you an authorization code. Paste it back into the page.

Some sign-ins finish on their own, without ever showing a code — the browser flow
completed and `gcloud` simply exited. The page notices that and reports success; there is
nothing to paste.

If the config names a service account, the login impersonates it, so it does not matter
which operator ran the renewal — the credentials end up belonging to the same identity
either way.

When it succeeds the proxy drops its cached credentials and picks up the new ones on the
next request. **No restart is needed.**

## Who can do what

| | View status | Health test | Renew |
| --- | --- | --- | --- |
| Viewer | Yes | No | No |
| User | Yes | Yes | Yes |
| Admin | Yes | Yes | Yes, and can force |

Renewal is refused with an error while the credentials look healthy — this is a recovery
tool, not a button to press for fun. Admins can override that with **Force**, which is for
the case where you know the credentials are about to expire and would rather not wait for
the failures.

Only one sign-in runs at a time. If a second person starts one while the first is in
flight, they join the same session and see the same URL rather than starting a competing
`gcloud` that would fight over the same credentials file.

Everything is recorded in the audit log: who started a renewal, who submitted a code, who
cancelled. The code itself is never stored, and anything token-shaped in the `gcloud`
output is redacted before it reaches the browser.

## Trying it out

To see the whole flow without waiting a day for a real expiry, corrupt the stored
credentials on purpose. The script backs the file up first:

```bash
python scripts/adc_simulate_expiry.py --corrupt    # back up, then break the refresh token
python scripts/adc_simulate_expiry.py --status     # what state the file is in
python scripts/adc_simulate_expiry.py --restore    # put the backup back
```

After `--corrupt`, a health test fails with a credential error and the renewal unlocks.
`--restore` is the way back if you would rather not do a real sign-in.
