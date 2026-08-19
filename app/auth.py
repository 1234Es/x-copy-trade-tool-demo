"""Operator session auth for the public deployment. A single shared
operator password (not a multi-user system -- see DESIGN.md), an in-memory
session store, and a small in-memory per-IP lockout on repeated failed
logins. All in-memory and fine for a single-process app; there is no
multi-worker/multi-instance deployment of this service.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

SESSION_COOKIE_NAME = "operator_session"
SESSION_TTL_SECONDS = 60 * 60 * 12  # 12 hours

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 15 * 60


@dataclass
class AuthState:
    sessions: dict[str, float] = field(default_factory=dict)  # token -> expires_at
    failed_attempts: dict[str, list[float]] = field(default_factory=dict)  # ip -> attempt timestamps

    def is_locked_out(self, client_ip: str, now: float) -> bool:
        attempts = [t for t in self.failed_attempts.get(client_ip, []) if now - t < LOCKOUT_WINDOW_SECONDS]
        self.failed_attempts[client_ip] = attempts
        return len(attempts) >= MAX_FAILED_ATTEMPTS

    def record_failed_attempt(self, client_ip: str, now: float) -> None:
        self.failed_attempts.setdefault(client_ip, []).append(now)

    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions[token] = time.time() + SESSION_TTL_SECONDS
        return token

    def is_valid(self, token: str | None) -> bool:
        if not token:
            return False
        expires_at = self.sessions.get(token)
        if expires_at is None:
            return False
        if expires_at < time.time():
            del self.sessions[token]
            return False
        return True

    def invalidate(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)
