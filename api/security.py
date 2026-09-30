"""JWT, login rate limiting and the auth dependencies (ownership is enforced here, not in route bodies)."""
from __future__ import annotations

import hmac
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Path, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.settings import Settings

from api.models import CUSTOMER_ID_PATTERN  # noqa: E402
ALGORITHM = "HS256"
_bearer = HTTPBearer(auto_error=False)
# compared against when the customer does not exist, so timing does not reveal valid ids
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-pin", bcrypt.gensalt(rounds=10))


@dataclass(frozen=True)
class Identity:
    sub: str
    role: str        # customer | admin


def create_token(settings: Settings, sub: str, role: str, now: datetime | None = None) -> tuple[str, int]:
    now = now or datetime.now(timezone.utc)      # real wall clock: token expiry is an API concern, not engine
    ttl = timedelta(minutes=settings.token_ttl_minutes)
    token = jwt.encode({"sub": sub, "role": role, "iat": now, "exp": now + ttl}, settings.jwt_secret,
                       algorithm=ALGORITHM)
    return token, int(ttl.total_seconds())


def decode_token(settings: Settings, token: str) -> Identity:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM],
                            options={"require": ["exp", "sub", "role"]})
    except jwt.PyJWTError:
        raise HTTPException(401, "Not authenticated")
    if claims.get("role") not in ("customer", "admin") or not isinstance(claims.get("sub"), str):
        raise HTTPException(401, "Not authenticated")
    return Identity(claims["sub"], claims["role"])


def verify_pin(pin: str, pin_hash: str | None) -> bool:
    hashed = pin_hash.encode() if pin_hash else _DUMMY_HASH
    ok = bcrypt.checkpw(pin.encode()[:72], hashed)
    return ok and pin_hash is not None


def secrets_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class RateLimiter:
    """In-memory failure counter per key over a sliding window (demo-grade; use a shared store in production)."""

    def __init__(self, max_failures: int, window_seconds: int, clock: Callable[[], float] = time.monotonic):
        self.max, self.window, self.clock = max_failures, window_seconds, clock
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        q, now = self._fails[key], self.clock()
        while q and now - q[0] > self.window:
            q.popleft()
        if not q:
            self._fails.pop(key, None)
            return deque()
        return q

    def blocked(self, *keys: str) -> bool:
        return any(len(self._prune(k)) >= self.max for k in keys)

    def fail(self, *keys: str) -> None:
        for k in keys:
            self._fails[k].append(self.clock())

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def current_identity(creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                     settings: Settings = Depends(get_settings)) -> Identity:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(401, "Not authenticated")
    return decode_token(settings, creds.credentials)


def require_self(customer_id: str = Path(pattern=CUSTOMER_ID_PATTERN),
                 identity: Identity = Depends(current_identity)) -> Identity:
    """Token subject must equal the customer id in the path. Admins have their own /admin routes."""
    if identity.role != "customer" or not hmac.compare_digest(identity.sub, customer_id):
        raise HTTPException(403, "Forbidden")
    return identity


def require_admin(identity: Identity = Depends(current_identity)) -> Identity:
    if identity.role != "admin":
        raise HTTPException(403, "Forbidden")
    return identity
