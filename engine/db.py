"""SQLite schema (TECH_SPEC section 2) and connection helper."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCHEMA = """
CREATE TABLE customers(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, age INTEGER, city TEXT, income_band TEXT,
    products_held TEXT NOT NULL,          -- json list, real product names
    address_on_file TEXT,                 -- = city (the CSVs have no address)
    pin_hash TEXT,                        -- bcrypt hash of a generated demo PIN
    is_demo INTEGER NOT NULL DEFAULT 0    -- 1 for D001-D007
);
CREATE TABLE transactions(
    id TEXT PRIMARY KEY,                  -- tx:<n>, file order
    customer_id TEXT NOT NULL, date TEXT NOT NULL, amount REAL NOT NULL,
    merchant TEXT, category TEXT, city TEXT
);
CREATE TABLE app_events(
    id TEXT PRIMARY KEY,                  -- ev:<n>
    customer_id TEXT NOT NULL, timestamp TEXT NOT NULL, event_type TEXT, detail TEXT
);
CREATE TABLE kate_messages(
    id TEXT PRIMARY KEY,                  -- kate:<n>
    customer_id TEXT NOT NULL, timestamp TEXT NOT NULL, text TEXT
);
-- Offline tooling ONLY (evaluate.py, add_demo_customers.py). Never read by engine/api.
CREATE TABLE ground_truth(customer_id TEXT NOT NULL, planted_event_type TEXT NOT NULL, start_date TEXT);

CREATE TABLE decisions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, as_of TEXT NOT NULL,
    event_type TEXT, stage TEXT, confidence REAL, triage_flagged INTEGER NOT NULL DEFAULT 0,
    llm_called INTEGER NOT NULL DEFAULT 0, llm_output TEXT,
    final_action TEXT, suppress_reason TEXT, channel TEXT, top_need TEXT,
    message TEXT, why TEXT, created_at TEXT,
    fingerprint TEXT,                     -- signal+feedback fingerprint: re-judge only when it changes
    judge_model TEXT,                     -- model (or 'stand-in-rules-v1') that produced llm_output
    verify_notes TEXT                     -- json list from verify.py / guardrails
);
CREATE TABLE feedback(
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, decision_id INTEGER,
    event_type TEXT, response TEXT NOT NULL, created_at TEXT
);
CREATE TABLE contacts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, decision_id INTEGER,
    channel TEXT, sent_at TEXT
);
CREATE TABLE llm_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT, prompt_hash TEXT, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER, cached INTEGER, created_at TEXT
);
CREATE TABLE privacy_prefs(
    customer_id TEXT NOT NULL, category TEXT NOT NULL, enabled INTEGER NOT NULL,
    PRIMARY KEY(customer_id, category)
);

CREATE INDEX idx_tx_cust_date ON transactions(customer_id, date);
CREATE INDEX idx_ev_cust_ts ON app_events(customer_id, timestamp);
CREATE INDEX idx_kate_cust_ts ON kate_messages(customer_id, timestamp);
CREATE INDEX idx_dec_cust_asof ON decisions(customer_id, as_of);
CREATE UNIQUE INDEX idx_dec_unique ON decisions(customer_id, as_of);   -- idempotent per (customer, as_of)
"""


def db_path() -> Path:
    p = Path(os.environ.get("DATABASE_PATH", "data/kbc.db"))
    return p if p.is_absolute() else REPO_ROOT / p


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    return conn
