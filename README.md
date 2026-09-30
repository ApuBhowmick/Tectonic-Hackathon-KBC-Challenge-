# KBC Life Moments (hackathon prototype) — DRAFT

A **life-moment engine**. It notices what is happening in a customer's life (first: *moving house*, plus four more
events) from everyday signals, works out the whole bundle of needs behind it, and offers the smallest useful next
step at the right time, in the right channel, with a clear "why". Customers stay in control ("Not now",
"This isn't me"). Cost scales with real life moments, not with the number of customers: code triages, the LLM
reasons only about flagged customers, and code enforces the bank's rules.

It is not a chatbot feature and not a new product. All data is synthetic.

## Architecture

```
 synthetic CSVs ──► SQLite (data/kbc.db)
                        │
   [1] signals   ── engine/signals.py     named signals with row references, as of a date (replayable)
   [2] triage    ── engine/triage.py      code only: any strong signal, or 2+ distinct weak/medium signals
   [3] LLM judge ── engine/llm_judge.py   OpenAI Structured Outputs, disk cache, only for flagged customers,
        │                                 re-asked only when the signal fingerprint changes
   [4] verify    ── engine/verify.py      evidence must exist, needs must be in the catalog
   [5] guardrails── engine/guardrails.py  code decides: threshold, caps, back-off, stress, sensitive, channel
        ▼
   decisions table ──► FastAPI (api/) ──► one static page (web/index.html): phone, replay, admin
        ▲                                        │
        └────────── feedback (accept / not now / not me)
```

Rules that hold everywhere: the LLM suggests and code decides; all free text (merchant names, chat) is untrusted
data inside a delimited block; the engine and the API never read `ground_truth`; time is always an `as_of`
parameter (customers can never look past the demo date, 2026-09-30).

## Run it

Python 3.11+ (the system `python3` may be older). From the repository root:

```
/opt/homebrew/bin/python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` (never commit it):

| Variable | Meaning |
|---|---|
| `OPENAI_API_KEY` | your key |
| `OPENAI_MODEL` | the model to use (there is no default on purpose) |
| `JWT_SECRET` | at least 16 random characters, signs login tokens |
| `ADMIN_SECRET` | at least 16 characters, the password for the admin dashboard |

A quick way to generate the two secrets: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.

```
.venv/bin/python scripts/import_data.py            # rebuilds data/kbc.db from "synthetic data/" (~40 s: bcrypt)
.venv/bin/python scripts/add_demo_customers.py     # hard cases D001-D007

# With a configured model (real LLM, cached on disk in data/llm_cache/):
.venv/bin/python scripts/run_pipeline.py --from 2026-09-05 --to 2026-09-30 --reset
# Without a model (deterministic stand-in rules, NOT an LLM; every screen says so):
.venv/bin/python scripts/run_pipeline.py --from 2026-09-05 --to 2026-09-30 --standin --reset

.venv/bin/python scripts/run_pipeline.py --demo    # D001-D007: expected vs actual
.venv/bin/python scripts/evaluate.py               # precision/recall/time-to-detect -> data/eval_report.json
.venv/bin/python scripts/record_fixtures.py        # record live LLM fixtures for the tests (needs the model)
.venv/bin/uvicorn api.main:app --port 8000         # then open http://127.0.0.1:8000/
.venv/bin/pytest -q
```

`run_pipeline.py --from ... --to ... --dry-run` counts the LLM calls a replay needs without calling the API.
If `JWT_SECRET` or `ADMIN_SECRET` is missing the API starts but answers 503 to everything.

## Demo logins (synthetic PINs)

| Customer | PIN | Story |
|---|---|---|
| C0001 Olivier Van Damme | 1146 | the hero: moving Gent → Antwerpen |
| D001 Marc Declercq | 4011 | furniture and paint only: not a move |
| D002 Lena Hermans | 4022 | helping her mother with an address: not her move |
| D003 Tom Peeters | 4033 | full move, then "Not now" |
| D004 Anna Janssens | 4044 | full move, money is tight: budget check, no sales |
| D005 Jan Verbeke | 4055 | bereavement: a human, not an offer |
| D006 Pieter Claes | 4066 | only a rent deposit: wait |
| D007 Injection Test | 4077 | merchant name tries to hijack the model |

The **Phone** and **Replay** tabs open straight away as the demo customer (C0001, Olivier) through a
no-credentials `/auth/demo-login` that can only ever issue a token for that one customer (set `DEMO_AUTOLOGIN=0` in
`.env` to turn it off). "Switch customer" shows the normal login form for the other demo customers.

The **Dashboard** tab needs no login: it reads the read-only `/public/dashboard` (synthetic aggregates only; set
`PUBLIC_DASHBOARD=0` in `.env` to switch it off and require the admin login again). The `ADMIN_SECRET` is only
needed for the Replay view. All other customers have random PINs.

## Security notes

JWT with expiry (60 min), token subject must equal the customer id in the path on every `/customers/{id}/...`
route (a router-level dependency, tested against the real route list), admin routes need the admin role, decision
ids are always looked up together with the owning customer (404 otherwise), strict request models that forbid
extra fields, parameterised SQL, generic error messages, in-memory login rate limit, request size limit,
security headers, no API docs and no debug mode. The token lives in page memory only.

## Limitations (honest list)

- **Synthetic data, clean signals.** The generator plants a fixed pattern per event, so near-perfect detection
  scores on it are expected and prove little. The hard cases D001-D007 are the real test.
- **Decisions made without a model are not LLM results.** When `OPENAI_MODEL` is not set the pipeline can use a
  deterministic stand-in (clearly labelled in the UI and dashboard). Accuracy and cost numbers from it are not
  evidence about an LLM.
- **Actions are simulated.** "Update everything" only shows what would happen; nothing is changed anywhere.
- **Address = city.** The data has no street address, no balance and no transaction descriptions.
- **Stress is approximated:** the LLM's flag, or negative 30-day cashflow (excluding the event's own spending)
  for low-income customers.
- Login rate limiting is in memory (single process); a real deployment needs a shared store.
- Cost projections need the price of your model in `config/pricing.yaml`; until then the dashboard shows call
  counts and "n/a" for dollars.

## Status

Built: import, hard cases, config-driven signals and triage, LLM judge with verification, guardrails, pipeline,
API with auth, single-page UI (phone, replay, admin), offline evaluation. Not built: feedback effects beyond
suppression windows in the next pipeline run, Aikido scan before/after, voice, deployment.
