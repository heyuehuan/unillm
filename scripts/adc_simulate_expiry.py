#!/usr/bin/env python3
"""
Simulate an expired Application Default Credentials token — and put it back.

The daily ADC expiry is the thing the "Google Cloud auth" console page exists to
recover from, and it is awkward to wait for: it happens once a day, at a time
nobody picks. This script produces the same failure on demand by replacing the
refresh token in the ADC file with one Google will reject.

    python scripts/adc_simulate_expiry.py --status
    python scripts/adc_simulate_expiry.py --corrupt
    python scripts/adc_simulate_expiry.py --restore

The file stays valid JSON with every other field intact, so google-auth loads it
happily and fails exactly where a real expiry fails: at the token refresh, with
`invalid_grant`. A corrupted *file* would fail earlier and differently, and would
not exercise the code path under test.

`--corrupt` refuses to run without first writing a backup, and refuses to overwrite
a backup that already holds real credentials — the second corrupt in a row would
otherwise back up the fake token over the real one and make the damage permanent.
No token value is ever printed, including in `--status`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Any, Dict, Optional, Tuple

DEFAULT_ADC_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "gcloud", "application_default_credentials.json"
)
BACKUP_SUFFIX = ".unillm-backup"

# Recognizable on sight in a backup diff, and shaped enough like a refresh token
# that Google's token endpoint answers `invalid_grant` rather than a parse error.
SENTINEL = "1//0-unillm-simulated-expired-refresh-token"


def adc_path(explicit: Optional[str]) -> str:
    if explicit:
        return os.path.expanduser(explicit)
    # GOOGLE_APPLICATION_CREDENTIALS wins for the library too, so it has to win here:
    # corrupting the well-known file while the proxy reads another one proves nothing.
    return os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or DEFAULT_ADC_PATH


def load(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save(path: str, data: Dict[str, Any]) -> None:
    """Write in place, preserving the 0600 mode gcloud gives the file."""
    mode = os.stat(path).st_mode & 0o777
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def token_holder(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    The dict that actually carries `refresh_token`.

    A plain user login carries it at the top level. An impersonated one nests the
    user's own credentials under `source_credentials` and reaches the service
    account through them — so that is where the corruption has to land for the
    impersonated flow to break the same way.
    """
    if "refresh_token" in data:
        return data
    source = data.get("source_credentials")
    if isinstance(source, dict) and "refresh_token" in source:
        return source
    return None


def impersonated_account(data: Dict[str, Any]) -> Optional[str]:
    """
    The service account this ADC impersonates, if any.

    Worth reporting because it is what belongs in `gcp_adk.service_account` in the
    server config: the console's re-login has to impersonate the same identity, or
    the renewed credentials will not be the ones the proxy was using. It is an email
    address, not a credential.
    """
    url = data.get("service_account_impersonation_url")
    if not isinstance(url, str) or ":generateAccessToken" not in url:
        return None
    return url.rsplit("/", 1)[-1].split(":", 1)[0] or None


def describe(path: str) -> Tuple[bool, str]:
    """(exists, one-line description) — never includes token material."""
    if not os.path.exists(path):
        return False, "missing"
    try:
        data = load(path)
    except (OSError, json.JSONDecodeError) as exc:
        return True, f"unreadable ({exc})"
    holder = token_holder(data)
    kind = data.get("type", "unknown")
    if holder is None:
        return True, f"type={kind}, no refresh token (nothing to simulate)"
    state = "SIMULATED-EXPIRED" if holder.get("refresh_token") == SENTINEL else "live"
    return True, f"type={kind}, refresh token {state}"


def cmd_status(path: str) -> int:
    exists, description = describe(path)
    backup = path + BACKUP_SUFFIX
    print(f"ADC file : {path}")
    print(f"           {description}")
    if os.path.exists(backup):
        _, backup_description = describe(backup)
        print(f"Backup   : {backup}")
        print(f"           {backup_description}")
    else:
        print("Backup   : none")
    if exists:
        try:
            account = impersonated_account(load(path))
        except (OSError, json.JSONDecodeError):
            account = None
        if account:
            print(f"Impersonates: {account}")
            print("           Put this in general_settings.gcp_adk.service_account.")
    return 0 if exists else 1


def cmd_corrupt(path: str, force_backup: bool) -> int:
    if not os.path.exists(path):
        print(f"No ADC file at {path}. Run 'gcloud auth application-default login' first.",
              file=sys.stderr)
        return 1

    data = load(path)
    holder = token_holder(data)
    if holder is None:
        print("This ADC file has no refresh token — nothing to expire. A service-account "
              "key or metadata identity does not expire the way a user login does.",
              file=sys.stderr)
        return 1
    if holder["refresh_token"] == SENTINEL:
        print("Already simulated-expired. Use --restore to put the real token back.")
        return 0

    backup = path + BACKUP_SUFFIX
    if os.path.exists(backup) and not force_backup:
        _, backup_description = describe(backup)
        if "live" not in backup_description:
            print(f"Refusing to overwrite {backup}: it does not hold a live token, so "
                  f"replacing it would lose the only real copy. Restore first, or pass "
                  f"--force-backup if you are sure.", file=sys.stderr)
            return 1
    shutil.copy2(path, backup)
    print(f"Backed up to {backup}")

    holder["refresh_token"] = SENTINEL
    save(path, data)
    print("Refresh token replaced with a rejected sentinel. The next Vertex call will "
          "fail with invalid_grant, exactly as a daily expiry does.")
    return 0


def cmd_restore(path: str) -> int:
    backup = path + BACKUP_SUFFIX
    if not os.path.exists(backup):
        print(f"No backup at {backup}.", file=sys.stderr)
        return 1
    shutil.copy2(backup, path)
    _, description = describe(path)
    print(f"Restored from {backup} — {description}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true", help="report the current state")
    action.add_argument("--corrupt", action="store_true", help="simulate an expired token")
    action.add_argument("--restore", action="store_true", help="put the backed-up token back")
    parser.add_argument("--path", help="ADC file to act on (default: the well-known location)")
    parser.add_argument("--force-backup", action="store_true",
                        help="overwrite an existing backup even if it holds no live token")
    args = parser.parse_args()

    path = adc_path(args.path)
    if args.status:
        return cmd_status(path)
    if args.corrupt:
        return cmd_corrupt(path, args.force_backup)
    return cmd_restore(path)


if __name__ == "__main__":
    raise SystemExit(main())
