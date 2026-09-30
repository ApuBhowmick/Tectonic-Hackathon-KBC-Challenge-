"""API settings from the environment / .env. Secrets are never defaulted and never logged."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from engine.config import demo_as_of_default, load_demo
from engine.db import REPO_ROOT, db_path

MIN_SECRET_LEN = 16


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    jwt_secret: str
    admin_secret: str
    db_path: Path
    demo_today: date                # customers can never look past the demo clock
    token_ttl_minutes: int = 60
    login_max_failures: int = 5
    login_window_seconds: int = 300
    max_body_bytes: int = 10_000
    demo_autologin: bool = True     # POST /auth/demo-login issues a token for the hero only; DEMO_AUTOLOGIN=0 disables
    hero_id: str = "C0001"
    public_dashboard: bool = True   # read-only aggregates without login; PUBLIC_DASHBOARD=0 switches it off


def load_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env", override=False)
    missing = [k for k in ("JWT_SECRET", "ADMIN_SECRET") if len(os.environ.get(k, "").strip()) < MIN_SECRET_LEN]
    if missing:
        raise SettingsError(f"{', '.join(missing)} must be set in .env (at least {MIN_SECRET_LEN} characters)")
    return Settings(os.environ["JWT_SECRET"].strip(), os.environ["ADMIN_SECRET"].strip(), db_path(),
                    demo_as_of_default(), public_dashboard=os.environ.get("PUBLIC_DASHBOARD", "1").strip() != "0",
                    demo_autologin=os.environ.get("DEMO_AUTOLOGIN", "1").strip() != "0",
                    hero_id=str(load_demo()["hero"]))
