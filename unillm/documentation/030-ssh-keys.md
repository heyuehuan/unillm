---
title: Register an SSH key
group: Getting started
keywords: [ssh, signature, attribution, ed25519, signer, identity]
---

Project API keys are shared by everyone in the project, so a request logged against a key
does not say who sent it. SSH attribution closes that gap. You sign the API key with your
own SSH private key and send both together. The server verifies the signature against the
public key you registered, and records your username on the request.

## Server mode

{{#if ssh_mode=none}}
> **This server runs in `none` mode.** Signing is optional. Unsigned requests are
> accepted and logged against the API key alone, with no username attached. Register a
> key anyway if you want your own calls named in the logs, and so nothing changes for
> you if the server later moves to `warning` or `enforce`.
{{/if}}
{{#if ssh_mode=warning}}
> **This server runs in `warning` mode.** Unsigned requests still work, and the response
> body carries an extra `warning` field asking you to sign. Registering a key and signing
> removes the warning and puts your username on every call.
{{/if}}
{{#if ssh_mode=enforce}}
> **This server runs in `enforce` mode.** Unsigned requests are rejected with 401, so you
> have to register a key and sign your API key before any call succeeds. Work through the
> four steps below first.
{{/if}}

The mode comes from `ssh_required` in the server config, and an operator can change it.
All three modes behave like this:

| Mode | Unsigned request | Signed request |
| --- | --- | --- |
| `none` | Accepted. This is the default. | Accepted, and attributed. |
| `warning` | Accepted, with a `warning` field added to the response body. | Accepted, and attributed. |
| `enforce` | Rejected with 401. | Accepted, and attributed. |

## 1. Create or pick a key pair

Ed25519, RSA and ECDSA P-256 keys all work. Ed25519 is the smallest and fastest:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
```

An existing key is fine. UniLLM never sees the private half.

## 2. Register the public key

Print the public key with `cat ~/.ssh/id_ed25519.pub` and copy the whole line. In
[My SSH Keys](#/sshkeys), choose **Add key**, paste it, and give it a name.

Key names are fixed to the form `yourusername--suffix`. The console fills in the prefix
and you type the suffix, for example `laptop` or `1`. Each account holds up to three keys,
which covers a laptop, a workstation and a build machine.

## 3. Validate it

Use **Validate my key** on the same page. It asks you to sign the fixed test string
`sk-12345678` and paste the result. The page prints the exact command for your key name
and path, including an OpenSSL variant for hosts without the UniLLM client installed. A
green result means the registered public key matches the private key you signed with.

## 4. Sign your real API key

The bundled signer takes the API key and prints the signed form:

```bash
python -m unillm.client.ssh_signer sk-your-api-key
python -m unillm.client.ssh_signer sk-your-api-key --key-name alice--laptop
python -m unillm.client.ssh_signer --list-keys
```

The output is three fields joined by double pipes: the API key, the registered key name,
and the base64 signature.

```
sk-your-api-key||alice--laptop||MEUCIQD...
```

Pass that whole string wherever the API key goes:

```python
from unillm.client import sign_api_key
import openai

signed = sign_api_key("sk-your-api-key", key_name="alice--laptop")

client = openai.OpenAI(
    api_key=signed.full_key,
    base_url="http://your-server:4000/v1",
)
```

With no arguments the signer looks for `~/.ssh/id_ed25519`, `~/.ssh/id_ecdsa` and
`~/.ssh/id_rsa` in that order, and guesses the key name from your system username. Pass
`--key-name` when your UniLLM username differs from it.

## Limits

- The signature covers the static API key, so the same signed string is valid every time.
  Anyone who captures it can replay it. Treat it like the API key itself.
- It identifies who signed. The server does not cross-check that person against the
  project member list.
- The SSHSIG format produced by `ssh-keygen -Y sign` is a different encoding and is
  rejected. Use the bundled signer, or the OpenSSL command the console prints.
