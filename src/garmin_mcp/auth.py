"""Shared failed-attempt rate limiting. Used by oauth.py to guard the login
form -- the actual authentication now lives there (see oauth.py's module
docstring for why this server needs a real OAuth authorization server
instead of a plain bearer-header check).
"""

import time
from collections import defaultdict

from starlette.requests import Request

FAILED_ATTEMPT_WINDOW_SECONDS = 60
MAX_FAILED_ATTEMPTS = 10
LOCKOUT_SECONDS = 300


class FailedAttemptLimiter:
    """In-memory, single-process."""

    def __init__(self, window_seconds: float, max_attempts: int, lockout_seconds: float):
        self.window_seconds = window_seconds
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._attempts: dict[str, list[float]] = defaultdict(list)
        self._locked_until: dict[str, float] = {}

    def is_locked_out(self, key: str) -> bool:
        until = self._locked_until.get(key)
        return until is not None and time.monotonic() < until

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        attempts = self._attempts[key]
        attempts.append(now)
        cutoff = now - self.window_seconds
        while attempts and attempts[0] < cutoff:
            attempts.pop(0)
        if len(attempts) >= self.max_attempts:
            self._locked_until[key] = now + self.lockout_seconds
            attempts.clear()

    def record_success(self, key: str) -> None:
        self._attempts.pop(key, None)


limiter = FailedAttemptLimiter(FAILED_ATTEMPT_WINDOW_SECONDS, MAX_FAILED_ATTEMPTS, LOCKOUT_SECONDS)


def client_key(request: Request) -> str:
    """Identify the client to rate-limit against.

    The container is only reachable via the loopback-bound Docker port
    publish (127.0.0.1:18080) -- nothing on the public internet can connect
    to that port directly, only a locally-running process (Tailscale Funnel,
    Cloudflare Tunnel, or our own tests) can. Docker's bridge networking
    means request.client.host is *always* the Docker gateway address
    (172.20.0.1) regardless of true origin, which would make every real
    client share one lockout bucket. Because only a trusted local process
    can reach this port at all, an X-Forwarded-For header here can only have
    been set by that process (a public attacker can't bypass it to spoof the
    header directly), so it's safe to trust for real per-client separation.
    Falls back to the raw peer address if the header is absent (e.g. local
    testing without a proxy in front).
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
