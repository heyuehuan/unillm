---
title: Projects and API keys
group: Console guide
keywords: [project, members, roles, keys, reveal, revoke, archive]
---

A project is the unit everything else hangs off. Keys belong to a project, usage is
counted per project, and access is granted per project.

## Project types

- **Personal project.** Created automatically for each account with the `user` role, along
  with a first API key. It has one owner and takes no members. Promoting a `viewer` to
  `user` creates it at that moment, and the new key is shown once.
- **Team project.** Created by a global admin, then given members. Use these for anything
  shared, since membership is what lets other people see the usage and the keys.

## Project roles

| Role | Can do |
| --- | --- |
| `admin` | Everything below, plus manage members, create and revoke keys, rename and archive the project. |
| `developer` | See the project, see and reveal its keys, see its usage. |
| `viewer` | See the project and its usage. No key access at all. |

A global admin has project `admin` rights everywhere without being a member.

## API keys

Open a project and stay on the Keys tab.

**Create.** Give the key a name, then choose the models it may use. Leaving the model list
empty means every configured model. Naming a subset means a request for anything else
returns 403. The plaintext key is displayed once, at creation.

**Reveal.** By default the server also stores a Fernet-encrypted copy of each key, so a
project developer or admin can read it again later. Each reveal is written to the audit
log with the reader's name. If the deployment sets `UNILLM_RECOVERABLE_KEYS=false`, no
copy is kept, the Reveal button is absent, and a key that was missed at creation is gone
for good.

**Edit.** A key's name and allowed models can be changed while it is active. Editing a
revoked key is refused.

**Revoke.** Deletion is immediate and permanent. Requests with that key start failing with
401. The request rows it already produced stay in the log.

Keys carry a prefix that is safe to quote in a ticket or a log line, for example `sk-abc1`.
The full value only ever appears at creation, or through Reveal.

## Members

The Members tab lists everyone with access, their project role, and whether their account
is still active. A project admin adds a member by picking a username and a role. The
candidate list holds usernames only.

Personal projects reject membership calls, since they belong to one account by definition.

## Archiving

Archiving takes a project out of the active list and stops its keys working. It does not
remove members and does not delete usage. The project's past requests remain visible in
logs and stats, both to its members and to admins. Unarchive puts it back.

Use archive for a finished piece of work whose numbers you still want. Revoke individual
keys instead when the project itself continues.
