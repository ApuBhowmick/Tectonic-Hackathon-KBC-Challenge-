"""Routes. The customer router carries `require_self` as a router-level dependency, the admin router
`require_admin`: a route cannot be added to either without being protected."""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from api import services
from api.models import (AcceptOut, AdminLoginIn, FeedbackIn, LoginIn, SuggestionOut, TimelineOut, TokenOut,
                        WhyOut)
from api.security import (CUSTOMER_ID_PATTERN, Identity, RateLimiter, create_token, get_settings,
                          require_admin, require_self, secrets_equal, verify_pin)
from api.settings import Settings
from engine.config import Config


def get_conn(settings: Settings = Depends(get_settings)) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_config(request: Request) -> Config:
    return request.app.state.config


def get_limiter(request: Request) -> RateLimiter:
    return request.app.state.limiter


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------------ auth (public)
auth = APIRouter(prefix="/auth", tags=["auth"])


@auth.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, conn: sqlite3.Connection = Depends(get_conn),
          settings: Settings = Depends(get_settings), limiter: RateLimiter = Depends(get_limiter)) -> TokenOut:
    keys = (f"cust:{body.customer_id}", f"ip:{_client_ip(request)}")
    if limiter.blocked(*keys):
        raise HTTPException(429, "Too many attempts, try again later", headers={"Retry-After": "300"})
    row = conn.execute("SELECT pin_hash FROM customers WHERE id=?", (body.customer_id,)).fetchone()
    if not verify_pin(body.pin, row["pin_hash"] if row else None):
        limiter.fail(*keys)
        raise HTTPException(401, "Invalid credentials")
    limiter.reset(keys[0])
    token, ttl = create_token(settings, body.customer_id, "customer")
    return TokenOut(access_token=token, expires_in=ttl, role="customer")


@auth.post("/demo-login", response_model=TokenOut)
def demo_login(settings: Settings = Depends(get_settings)) -> TokenOut:
    """No-credentials login for the ONE demo customer (the hero). It can never issue a token for anyone else,
    and the token is still subject to the same ownership checks on every route."""
    if not settings.demo_autologin:
        raise HTTPException(404, "Not found")
    token, ttl = create_token(settings, settings.hero_id, "customer")
    return TokenOut(access_token=token, expires_in=ttl, role="customer")


@auth.post("/admin-login", response_model=TokenOut)
def admin_login(body: AdminLoginIn, request: Request, settings: Settings = Depends(get_settings),
                limiter: RateLimiter = Depends(get_limiter)) -> TokenOut:
    key = f"admin:{_client_ip(request)}"
    if limiter.blocked(key):
        raise HTTPException(429, "Too many attempts, try again later", headers={"Retry-After": "300"})
    if not secrets_equal(body.admin_secret, settings.admin_secret):
        limiter.fail(key)
        raise HTTPException(401, "Invalid credentials")
    token, ttl = create_token(settings, "admin", "admin")
    return TokenOut(access_token=token, expires_in=ttl, role="admin")


# ------------------------------------------------------------------ customer (self only)
customer = APIRouter(prefix="/customers/{customer_id}", tags=["customer"], dependencies=[Depends(require_self)])


@customer.get("/suggestion", response_model=SuggestionOut)
def suggestion(customer_id: str = Path(pattern=CUSTOMER_ID_PATTERN), as_of: date | None = Query(None),
               conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
               config: Config = Depends(get_config)) -> SuggestionOut:
    return services.get_suggestion(conn, customer_id, services.cap_as_of(as_of, settings), config)


@customer.get("/why/{decision_id}", response_model=WhyOut)
def why(decision_id: int = Path(ge=1), customer_id: str = Path(pattern=CUSTOMER_ID_PATTERN),
        conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
        config: Config = Depends(get_config)) -> WhyOut:
    return services.get_why(conn, customer_id, decision_id, settings, config)


@customer.get("/timeline", response_model=TimelineOut)
def timeline(customer_id: str = Path(pattern=CUSTOMER_ID_PATTERN), as_of: date | None = Query(None),
             conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
             config: Config = Depends(get_config)) -> TimelineOut:
    return services.get_timeline(conn, customer_id, services.cap_as_of(as_of, settings), config)


@customer.post("/feedback", status_code=201)
def feedback(body: FeedbackIn, customer_id: str = Path(pattern=CUSTOMER_ID_PATTERN),
             conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings)) -> dict:
    services.record_response(conn, customer_id, body.decision_id, body.response, settings)
    return {"decision_id": body.decision_id, "response": body.response}


@customer.post("/actions/{decision_id}/accept", response_model=AcceptOut)
def accept(decision_id: int = Path(ge=1), customer_id: str = Path(pattern=CUSTOMER_ID_PATTERN),
           conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
           config: Config = Depends(get_config)) -> AcceptOut:
    return services.accept(conn, customer_id, decision_id, settings, config)


# ------------------------------------------------------------------ admin
admin = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@admin.get("/dashboard")
def dashboard(conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
              config: Config = Depends(get_config)) -> dict:
    return services.get_dashboard(conn, settings, config)


@admin.get("/customers/{customer_id}/timeline", response_model=TimelineOut)
def admin_timeline(customer_id: str = Path(pattern=CUSTOMER_ID_PATTERN), as_of: date | None = Query(None),
                   conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
                   config: Config = Depends(get_config)) -> TimelineOut:
    """Presenter view of the replay; same data as the customer's own timeline."""
    return services.get_timeline(conn, customer_id, services.cap_as_of(as_of, settings), config)


# ------------------------------------------------------------------ public (read-only aggregates, no login)
def require_public_dashboard(settings: Settings = Depends(get_settings)) -> None:
    if not settings.public_dashboard:
        raise HTTPException(404, "Not found")


public = APIRouter(prefix="/public", tags=["public"], dependencies=[Depends(require_public_dashboard)])


@public.get("/dashboard")
def public_dashboard(conn: sqlite3.Connection = Depends(get_conn), settings: Settings = Depends(get_settings),
                     config: Config = Depends(get_config)) -> dict:
    """Same synthetic aggregates as /admin/dashboard. Nothing customer-specific beyond the seven demo cases."""
    return services.get_dashboard(conn, settings, config)
