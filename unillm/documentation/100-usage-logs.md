---
title: Usage, logs and audit
group: Console guide
keywords: [dashboard, usage, stats, request log, audit, filters, labels, scope, time, timezone]
---

Four pages read the same two tables. The [Dashboard](#/dashboard) is the summary,
[Usage](#/usage) is the breakdown, [Logs](#/logs) is the per-request detail, and the Audit
tab under Admin is the management trail.

## Filters

The filter bar is shared by Dashboard, Usage and Logs, and your selection follows you
between them.

- **Time range.** Last 24 hours, 3 days, 7 days, all time, or a custom window.
- **Projects.** One, several, or all the projects you can see.
- **Scope.** "All" shows everything in range. "Mine" narrows to requests attributed to
  your own SSH username, which only has content in deployments that use SSH attribution.

What you can see is decided on the server. A global admin sees every project. Everyone
else sees the projects they belong to, including archived ones.

## Request logs

One row per inference request, written after the response finishes, including for failed
requests. Selecting a row opens the detail panel with the full record: request id, project,
key name and prefix, SSH username, client IP, alias and backend model, stream flag, labels,
status, error message, token counts, cost and latency.

Prompts and completions are never stored. If you need to group requests by something the
server cannot see, send it in `labels` on the request. See
[Make an LLM call](#/documentation/first-call).

## Usage

Aggregates over the same rows: request counts, token totals, spend, error rate, and
breakdowns by model and by project. The error rate counts anything at or above status 400,
so a 304 is not an error.

## Audit log

Under Admin, Audit, for global admins. One row per management action: logins, failed and
rate-limited login attempts, user creation, role changes, project changes, key creation,
key reveals, key revocations, pricing edits and settings edits. Each row records who, when,
from which address, and a detail object with what changed. Rows are appended and never
edited.

Read the audit log when you need to answer who did this and when. Read the request log when
you need to answer what did this cost and how did it perform.

## What time is it?

Every timestamp in the console — request logs, the audit trail, "last used", "last
success" — is shown in one timezone chosen by the deployment, not by your browser. This
server uses **{{display_timezone}}**. Two people in different places reading the same log
row see the same wall-clock time, and a row can be compared against a chat message or a
colleague's screenshot without anyone doing the arithmetic.

The custom date range in the filter bar reads in that same zone, so the rows it selects
agree with the times printed beside them. The zone's short name is shown next to the
inputs.

Timestamps are stored in UTC and the API always sends them with a `Z`, so scripts reading
`/api/logs/requests` get unambiguous instants regardless of this setting. An operator
changes the display zone with `display_timezone` in the server config; any IANA zone name
works, and the region form (`America/Toronto`) is preferred over `EST` because it follows
daylight saving on its own.
