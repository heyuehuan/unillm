"""
Bringing Google Cloud credentials back when the ADC token expires.

UniLLM reaches Vertex AI with Application Default Credentials. On a workstation or
a shared box those credentials are a *user* login, and Google expires that login
roughly daily. When it lapses every Gemini call fails in the same place — the token
refresh, before a single byte reaches Vertex — and the fix is to run
`gcloud auth application-default login` on the machine the proxy runs on. Until now
that meant shell access to that machine, so the one person with it became the only
person who could end the outage.

This module puts the same recovery behind the console, for any signed-in user:

  * `status()` decides whether a refresh is warranted, from two independent signals.
  * `run_health_test()` settles the question with one real, minimal Gemini call.
  * `RefreshSession` drives `gcloud` as a child process, hands its sign-in URL to
    whatever browser the operator already has, and takes the authorization code back.

Two deliberate limits. Only one refresh runs at a time, because two gcloud processes
writing the same ADC file is a race whose loser is the credential file. And a refresh
is only offered when something actually looks broken, so this is not a standing
"run gcloud for me" button on a healthy deployment.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from unillm._logging import verbose_proxy_logger
from unillm.db.models import RequestLog

# The prompt the health test sends. Short on purpose: it has to prove the credential
# path end to end, and nothing more. Every token it spends is spent on a deployment
# somebody already suspects is broken.
HEALTH_PROMPT = "Health test, reply 'hi' only."

# The cheapest Gemini in the lineup. Overridable per deployment, because the alias
# has to exist in this config's model_list to be callable at all.
DEFAULT_HEALTH_MODEL = "gemini-2.5-flash-lite"

# "Recent" for both halves of the log signal: a reauth failure inside this window
# with no success inside it means the credentials have been down the whole time.
DEFAULT_STALE_AFTER_SECONDS = 3600

# --no-launch-browser is what makes this workable from a browser on another machine:
# gcloud prints the sign-in URL and then waits at "Enter authorization code" instead
# of trying to open a browser on the server, which has none.
DEFAULT_LOGIN_ARGS: Tuple[str, ...] = ("--no-launch-browser",)

# A sign-in the operator walked away from should not hold the lock forever.
SESSION_TIMEOUT_SECONDS = 900

# How much of gcloud's own output is handed back to the console. Enough to show the
# real error when the login fails; not the whole session.
_OUTPUT_TAIL_CHARS = 4000


def _utcnow() -> datetime:
    """Naive UTC, matching how the log tables store timestamps."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AdkConfig:
    """The `gcp_adk` block of `general_settings`, resolved with its defaults."""

    service_account: Optional[str] = None
    health_model: str = DEFAULT_HEALTH_MODEL
    login_args: Tuple[str, ...] = DEFAULT_LOGIN_ARGS
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS
    gcloud_path: str = "gcloud"
    # Off by default. Spawning gcloud is a real privilege, and a deployment that
    # runs on a service-account key file or a GCE metadata identity has no user
    # token to refresh — for those, this feature is noise at best.
    enabled: bool = False

    @property
    def available(self) -> bool:
        """Whether a refresh could actually be attempted on this machine."""
        return self.enabled and bool(shutil.which(self.gcloud_path))


def load_config() -> AdkConfig:
    """
    Read the `gcp_adk` block from the YAML `general_settings`.

    Imported late: this module is reached from the management router, which the
    proxy server imports at startup, so a module-level import would close the loop.
    """
    from unillm.proxy import proxy_server

    raw = (proxy_server.general_settings or {}).get("gcp_adk") or {}
    if not isinstance(raw, dict):
        verbose_proxy_logger.warning("general_settings.gcp_adk is not a mapping; ignoring it")
        raw = {}

    login_args = raw.get("login_args")
    if isinstance(login_args, str):
        login_args = login_args.split()
    if not login_args:
        login_args = list(DEFAULT_LOGIN_ARGS)

    try:
        stale_after = int(raw.get("stale_after_seconds", DEFAULT_STALE_AFTER_SECONDS))
    except (TypeError, ValueError):
        stale_after = DEFAULT_STALE_AFTER_SECONDS

    return AdkConfig(
        service_account=(raw.get("service_account") or None),
        health_model=(raw.get("health_model") or DEFAULT_HEALTH_MODEL),
        login_args=tuple(str(a) for a in login_args),
        stale_after_seconds=max(60, stale_after),
        gcloud_path=(raw.get("gcloud_path") or "gcloud"),
        enabled=bool(raw.get("enabled", False)),
    )


def _vertex_model_types() -> Sequence[str]:
    """Model types whose requests depend on Google credentials."""
    from unillm.proxy.proxy_server import MODEL_TYPE_VERTEX_AI, MODEL_TYPE_VERTEX_AI_KMS

    return (MODEL_TYPE_VERTEX_AI, MODEL_TYPE_VERTEX_AI_KMS)


# ---------------------------------------------------------------------------
# Signal (a): what the request log says
# ---------------------------------------------------------------------------

# Substrings that identify a failure as "the credentials are the problem", matched
# case-insensitively against the recorded error. They are the phrases google-auth
# and Vertex actually produce — an expired user login surfaces as RefreshError with
# `invalid_grant`, a revoked one adds "reauth", and a missing ADC file raises
# DefaultCredentialsError. Deliberately narrow: a 500 from a bad prompt must not
# look like an outage that a re-login would fix.
_AUTH_ERROR_MARKERS = (
    "invalid_grant",
    "refresherror",
    "reauth",
    "defaultcredentialserror",
    "default credentials were not found",
    "could not automatically determine credentials",
    "unauthenticated",
    "invalid authentication credentials",
    "token has been expired or revoked",
    "request is missing required authentication credential",
)


def is_auth_failure(status_code: Optional[int], error_message: Optional[str]) -> bool:
    """
    Whether a logged failure points at the credentials rather than the request.

    Two ways to qualify. Vertex answering 401 or 403 is unambiguous — the token was
    presented and rejected. Otherwise the exception text has to name a credential
    problem, because a refresh that fails never reaches Vertex at all and is logged
    as a plain 500 with google-auth's message attached.
    """
    if status_code in (401, 403):
        return True
    if not error_message:
        return False
    lowered = error_message.lower()
    return any(marker in lowered for marker in _AUTH_ERROR_MARKERS)


@dataclass
class LogSignal:
    """What the recent request log says about the Google credential path."""

    window_seconds: int
    last_auth_failure_at: Optional[datetime] = None
    last_auth_failure_message: Optional[str] = None
    last_success_at: Optional[datetime] = None

    @property
    def looks_expired(self) -> bool:
        """
        A reauth failure in the window, and nothing succeeded in that window.

        Both halves matter. A failure alone can be one unlucky call against a
        credential that is otherwise fine; a success anywhere in the window proves
        the token still refreshes, whatever else went wrong.
        """
        return self.last_auth_failure_at is not None and self.last_success_at is None


def log_signal(db: Session, window_seconds: int) -> LogSignal:
    """Read the recent Vertex traffic and summarize the credential picture."""
    since = _utcnow() - timedelta(seconds=window_seconds)
    scoped = db.query(RequestLog).filter(
        RequestLog.created_at >= since,
        RequestLog.model_type.in_(_vertex_model_types()),
    )

    last_success = (
        scoped.filter(RequestLog.status_code == 200)
        .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
        .first()
    )

    # Scanned newest-first and capped: the marker test is Python-side (SQLite has no
    # portable case-insensitive multi-substring match), and one busy hour of failures
    # should not turn a status poll into a full-table read.
    failures = (
        scoped.filter(or_(RequestLog.status_code.is_(None), RequestLog.status_code != 200))
        .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
        .limit(200)
        .all()
    )
    auth_failure = next(
        (f for f in failures if is_auth_failure(f.status_code, f.error_message)), None
    )

    return LogSignal(
        window_seconds=window_seconds,
        last_auth_failure_at=auth_failure.created_at if auth_failure else None,
        last_auth_failure_message=_truncate(auth_failure.error_message) if auth_failure else None,
        last_success_at=last_success.created_at if last_success else None,
    )


def _truncate(text: Optional[str], limit: int = 300) -> Optional[str]:
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Signal (b): the health test
# ---------------------------------------------------------------------------

@dataclass
class HealthResult:
    """The outcome of one real call to the configured health model."""

    healthy: bool
    checked_at: datetime
    model: str
    latency_ms: int
    reply: Optional[str] = None
    error: Optional[str] = None
    auth_related: bool = False
    checked_by: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "healthy": self.healthy,
            "checked_at": self.checked_at,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "reply": self.reply,
            "error": self.error,
            "auth_related": self.auth_related,
            "checked_by": self.checked_by,
        }


# The last health test anyone ran, kept in process so the console can show it to the
# next person to open the page without spending another call. It is a cache of an
# observation, not state the system depends on: losing it on restart costs one click.
_last_health: Optional[HealthResult] = None


def last_health_result() -> Optional[HealthResult]:
    return _last_health


class HealthModelUnavailable(RuntimeError):
    """The configured health model is not in this deployment's model_list."""


async def run_health_test(checked_by: Optional[str] = None) -> HealthResult:
    """
    Send the smallest useful request to the cheapest configured Gemini.

    It goes through the same handler a served request uses, so it exercises the
    real credential path — including the ADC refresh that is the thing under
    suspicion — rather than a separate code path that could pass while the proxy
    fails.
    """
    global _last_health
    from unillm.proxy import proxy_server

    cfg = load_config()
    alias = cfg.health_model
    if proxy_config_missing(alias):
        raise HealthModelUnavailable(
            f"Health model '{alias}' is not configured in model_list"
        )

    handler = proxy_server.vertex_handlers[alias]
    backend_model = proxy_server._get_actual_model_name(alias)
    params = proxy_server._get_model_params(alias)

    started = time.time()
    try:
        response = await handler.chat_completion(
            model=backend_model,
            messages=[{"role": "user", "content": HEALTH_PROMPT}],
            max_tokens=16,
            temperature=0,
            stream=False,
            project=params.get("project"),
            location=params.get("location"),
        )
        reply = None
        if response.choices and response.choices[0].message:
            reply = response.choices[0].message.content
        result = HealthResult(
            healthy=True,
            checked_at=_utcnow(),
            model=alias,
            latency_ms=int((time.time() - started) * 1000),
            reply=_truncate(reply, 200),
            checked_by=checked_by,
        )
    except Exception as exc:  # the failure is the answer here, whatever its shape
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        message = f"{type(exc).__name__}: {exc}"
        result = HealthResult(
            healthy=False,
            checked_at=_utcnow(),
            model=alias,
            latency_ms=int((time.time() - started) * 1000),
            error=_truncate(message),
            auth_related=is_auth_failure(status_code, message),
            checked_by=checked_by,
        )
        verbose_proxy_logger.warning("GCP ADK health test failed: %s", message)

    _last_health = result
    return result


def proxy_config_missing(alias: str) -> bool:
    """Whether the model alias has no handler in the running proxy."""
    from unillm.proxy import proxy_server

    return alias not in proxy_server.vertex_handlers


def reset_health_result() -> None:
    """Forget the cached health observation (used after a successful re-login)."""
    global _last_health
    _last_health = None


# ---------------------------------------------------------------------------
# The gcloud re-login session
# ---------------------------------------------------------------------------

# gcloud prints exactly one https URL in this flow, and it is the one to sign in at.
_URL_RE = re.compile(r"https://\S+")
# It asks for the code without a trailing newline, so the prompt is only ever seen
# as a partial line — matched on the phrase rather than on a complete line.
#
# The wording is not stable across gcloud versions. Older ones print "Enter
# authorization code:"; current ones print "Once finished, enter the verification
# code provided in your browser:". Matching one literal phrase left the session stuck
# showing the URL with no box to paste into, so both known phrasings are accepted and
# a trailing "...code:" prompt of any wording counts as a fallback.
_CODE_PROMPT_RE = re.compile(
    r"enter\s+(?:the\s+)?(?:verification|authorization|auth)\w*\s+code", re.IGNORECASE
)
_TRAILING_PROMPT_RE = re.compile(r"code[^\n]{0,80}:\s*$", re.IGNORECASE)

# Anything token-shaped is scrubbed before gcloud's output reaches a browser.
# ya29.* is an access token, 1//* a refresh token, and gcloud echoes neither on
# purpose — but the output is streamed verbatim from a process handling credentials,
# so it is filtered rather than trusted.
_SECRET_RE = re.compile(r"(ya29\.[\w\-\.]+|1//[\w\-\.]+|4/[\w\-]{20,})")

STATE_STARTING = "starting"
STATE_AWAITING_URL = "awaiting_url"
STATE_AWAITING_CODE = "awaiting_code"
STATE_FINISHING = "finishing"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

_TERMINAL_STATES = (STATE_SUCCEEDED, STATE_FAILED, STATE_CANCELLED)


def scrub(text: str) -> str:
    return _SECRET_RE.sub("[redacted]", text)


class RefreshSession:
    """
    One `gcloud auth application-default login`, driven from the console.

    The command is interactive by design, and this class is the two-way adapter for
    it. gcloud writes a URL and then blocks on a prompt; the console shows the URL,
    the operator signs in wherever their browser is, and the authorization code comes
    back through `submit_code`.

    The flow that needs no code is supported by the same machinery: if gcloud
    completes on its own — a browser did open, or the local redirect was caught —
    the process simply exits, and the reader sees that instead of a prompt. That is
    what "detect if the sign-in happened automatically" means here: the console keeps
    polling and the session reaches `succeeded` without anyone pasting anything.
    """

    def __init__(self, config: AdkConfig, started_by: Optional[str] = None):
        self.id = uuid.uuid4().hex[:16]
        self.config = config
        self.started_by = started_by
        self.started_at = _utcnow()
        self.state = STATE_STARTING
        self.url: Optional[str] = None
        self.error: Optional[str] = None
        self.output: str = ""
        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------

    def command(self) -> List[str]:
        argv = [self.config.gcloud_path, "auth", "application-default", "login"]
        argv.extend(self.config.login_args)
        if self.config.service_account:
            argv.append(f"--impersonate-service-account={self.config.service_account}")
        return argv

    async def start(self) -> None:
        """Spawn gcloud and wait for it to announce a URL (or finish on its own)."""
        argv = self.command()
        verbose_proxy_logger.info("Starting ADC re-login: %s", " ".join(argv))
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Merged: gcloud splits this flow across both streams, and reading
                # them separately reorders the prompt relative to the URL.
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            self.state = STATE_FAILED
            self.error = f"'{self.config.gcloud_path}' is not installed on the proxy host"
            return
        except OSError as exc:
            self.state = STATE_FAILED
            self.error = f"Could not start gcloud: {exc}"
            return

        self.state = STATE_AWAITING_URL
        self._reader = asyncio.create_task(self._pump())

        # Give gcloud a moment to print the URL so the first response can carry it.
        # If it needs longer the console polls; this only avoids a guaranteed
        # round trip in the common case.
        for _ in range(100):
            if self.url or self.state in _TERMINAL_STATES:
                break
            await asyncio.sleep(0.05)

    async def _pump(self) -> None:
        """
        Read gcloud's merged output until the process exits.

        Read in chunks, not lines: the authorization-code prompt has no newline, so
        a line-oriented reader blocks on it forever and the session never leaves
        `awaiting_url`.
        """
        assert self._process is not None and self._process.stdout is not None
        deadline = time.monotonic() + SESSION_TIMEOUT_SECONDS
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await self._terminate()
                    self.state = STATE_FAILED
                    self.error = "Timed out waiting for the sign-in to complete"
                    return
                try:
                    chunk = await asyncio.wait_for(
                        self._process.stdout.read(1024), timeout=min(remaining, 5.0)
                    )
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    break
                self._absorb(chunk.decode("utf-8", "replace"))

            code = await self._process.wait()
            if self.state in _TERMINAL_STATES:
                return
            if code == 0:
                self.state = STATE_SUCCEEDED
                await _on_credentials_refreshed()
            else:
                self.state = STATE_FAILED
                self.error = self.error or f"gcloud exited with status {code}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a reader crash must not wedge the session
            self.state = STATE_FAILED
            self.error = f"Reading gcloud output failed: {exc}"
            verbose_proxy_logger.exception("ADC re-login reader failed")

    def _absorb(self, text: str) -> None:
        self.output = (self.output + scrub(text))[-_OUTPUT_TAIL_CHARS:]
        if self.url is None:
            match = _URL_RE.search(self.output)
            if match:
                self.url = match.group(0).rstrip(".,)")
        if self.state == STATE_AWAITING_URL and self._asks_for_code():
            self.state = STATE_AWAITING_CODE

    def _asks_for_code(self) -> bool:
        """Whether gcloud is sitting at its "paste the code" prompt right now."""
        tail = self.output[-500:]
        return bool(_CODE_PROMPT_RE.search(tail) or _TRAILING_PROMPT_RE.search(tail))

    async def submit_code(self, code: str) -> None:
        """Hand the pasted authorization code to gcloud's waiting prompt."""
        async with self._lock:
            if self.state not in (STATE_AWAITING_CODE, STATE_AWAITING_URL):
                raise RuntimeError(f"This sign-in is not waiting for a code (state: {self.state})")
            if self._process is None or self._process.stdin is None:
                raise RuntimeError("The sign-in process is no longer running")
            self.state = STATE_FINISHING
            try:
                self._process.stdin.write((code.strip() + "\n").encode())
                await self._process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                self.state = STATE_FAILED
                self.error = f"gcloud stopped listening before the code arrived: {exc}"

    async def cancel(self) -> None:
        await self._terminate()
        if self.state not in (STATE_SUCCEEDED, STATE_FAILED):
            self.state = STATE_CANCELLED

    async def _terminate(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
        except ProcessLookupError:
            pass

    @property
    def finished(self) -> bool:
        return self.state in _TERMINAL_STATES

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.id,
            "state": self.state,
            "url": self.url,
            "error": self.error,
            "output": self.output,
            "started_at": self.started_at,
            "started_by": self.started_by,
            "service_account": self.config.service_account,
            "command": " ".join(self.command()),
        }


async def _on_credentials_refreshed() -> None:
    """
    Make the new credentials take effect without a restart.

    Every handler caches what it built from the old ADC — the REST one holds the
    Credentials object itself, the CMEK one holds SDK models whose clients captured
    credentials when they were constructed. Left alone they would keep presenting
    the dead token until the process was bounced, which is exactly the restart this
    feature exists to avoid.
    """
    from unillm.proxy import proxy_server

    reset_health_result()
    for name, handler in list(proxy_server.vertex_handlers.items()):
        invalidate = getattr(handler, "invalidate_credentials", None)
        if invalidate is None:
            continue
        try:
            invalidate()
        except Exception as exc:
            verbose_proxy_logger.warning("Could not reset credentials for '%s': %s", name, exc)


# One at a time, process-wide. Two gcloud logins racing on the same ADC file is how
# you end up with neither credential.
_session_lock = asyncio.Lock()
_active_session: Optional[RefreshSession] = None


def active_session() -> Optional[RefreshSession]:
    return _active_session


def get_session(session_id: str) -> Optional[RefreshSession]:
    if _active_session is not None and _active_session.id == session_id:
        return _active_session
    return None


async def start_session(config: AdkConfig, started_by: Optional[str] = None) -> RefreshSession:
    """
    Begin a re-login, or return the one already in flight.

    Returning the running session rather than refusing is the useful behaviour: two
    people reacting to the same outage should land on the same URL, not deadlock
    each other out of fixing it.
    """
    global _active_session
    async with _session_lock:
        if _active_session is not None and not _active_session.finished:
            return _active_session
        session = RefreshSession(config, started_by=started_by)
        _active_session = session
    await session.start()
    return session


async def clear_session() -> None:
    """Drop the active session, stopping it first if it is still running (tests)."""
    global _active_session
    if _active_session is not None:
        await _active_session.cancel()
    _active_session = None


# ---------------------------------------------------------------------------
# The combined picture
# ---------------------------------------------------------------------------

def status(db: Session, config: Optional[AdkConfig] = None) -> Dict[str, Any]:
    """
    Everything the console needs to decide whether to offer a re-login.

    Two signals, either of which opens the door:

      (a) the request log shows a credential failure in the last hour with no
          successful Vertex call in that hour, and
      (b) the last health test came back unhealthy for a credential reason.

    They answer different questions. (a) is free and passive, and notices an outage
    nobody has looked at yet. (b) costs one tiny Gemini call and is definitive,
    which is what makes it the button rather than the default.
    """
    config = config or load_config()
    signal = log_signal(db, config.stale_after_seconds)
    health = last_health_result()

    health_says_expired = bool(health and not health.healthy and health.auth_related)
    needs_refresh = signal.looks_expired or health_says_expired

    session = active_session()
    return {
        "enabled": config.enabled,
        "available": config.available,
        "service_account": config.service_account,
        "health_model": config.health_model,
        "health_prompt": HEALTH_PROMPT,
        "window_seconds": config.stale_after_seconds,
        "needs_refresh": needs_refresh,
        "reasons": _reasons(signal, health_says_expired),
        "last_auth_failure_at": signal.last_auth_failure_at,
        "last_auth_failure_message": signal.last_auth_failure_message,
        "last_success_at": signal.last_success_at,
        "last_health": health.as_dict() if health else None,
        "active_session": session.as_dict() if session and not session.finished else None,
    }


def _reasons(signal: LogSignal, health_says_expired: bool) -> List[str]:
    reasons = []
    if signal.looks_expired:
        minutes = signal.window_seconds // 60
        reasons.append(
            f"A credential failure was logged in the last {minutes} minutes "
            f"and no Vertex request has succeeded since."
        )
    if health_says_expired:
        reasons.append("The last health test failed with a credential error.")
    return reasons
