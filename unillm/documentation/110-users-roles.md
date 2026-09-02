---
title: Users and roles
group: Console guide
keywords: [users, admin, viewer, accounts, password, disable, settings, profile]
---

## Global roles

Set on the account itself, under Admin, Users.

| Role | Gets |
| --- | --- |
| `admin` | Full access to every project, user, price and setting, plus the audit log. |
| `user` | A personal project and a first API key. Sees the projects they belong to. |
| `viewer` | Read-only. No personal project and no API key. |

Global role and project role are separate. A `user` can be an admin of one project and a
viewer of another. A global `admin` has project admin rights everywhere without being a
member.

## Creating an account

A global admin creates accounts under Admin, Users. Creation returns the account and, for
`user` accounts, the first API key in plaintext. Copy it then, because that is the only
time it is shown unless the deployment keeps recoverable keys.

Promoting a `viewer` to `user` provisions the personal project and first key at that
moment, and shows the key once in the same way.

## Editing an account

An admin can change the display name, the global role and the password, disable password
login, and deactivate the account. Admins cannot edit their own account on this page, which
keeps a single admin from locking themselves out.

Each of these bumps the account's token version, so every session that account still has
open stops working immediately.

A disabled account keeps its project memberships. Requests using its keys are refused with
403 and the reason.

## Your own account

[Settings](#/settings) covers what you can change yourself: display name, email, theme, and
your password. Changing your password reissues your session token, so you stay signed in on
the tab you did it from and every other session ends.

## Server settings

Under Admin, Server settings, for global admins. Values here take effect without a restart
and are written to the audit log. Each setting resolves in three steps: a database row set
from this page, then `general_settings` in the config file, then the built-in default. The
Reset button deletes the row so the file value applies again.
