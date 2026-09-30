import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.security import create_token
from api.settings import Settings
from engine.config import load_config
from engine.pipeline import run_range
from engine.standin_judge import standin_judge

CFG = load_config()
TODAY = date(2026, 9, 30)
SECRET, ADMIN = "j" * 40, "a" * 40
PINS = {"C0001": "1146", "D004": "4044", "D003": "4033", "D005": "4055", "D002": "4022"}


@pytest.fixture(scope="session")
def api_db(db_file, tmp_path_factory):
    """Full DB with decisions produced by the stand-in judge (the API never needs a live LLM)."""
    path = tmp_path_factory.mktemp("api") / "api.db"
    shutil.copy(db_file, path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    run_range(conn, date(2026, 9, 5), TODAY, CFG, lambda ctx: standin_judge(ctx, CFG), workers=1,
              judge_model="stand-in-rules-v1")
    conn.close()
    return path


@pytest.fixture()
def settings(api_db, tmp_path):
    p = tmp_path / "work.db"
    shutil.copy(api_db, p)
    return Settings(SECRET, ADMIN, p, TODAY)


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    c = TestClient(app)
    c.fastapi_app = app
    return c


def login(client, cid):
    r = client.post("/auth/login", json={"customer_id": cid, "pin": PINS[cid]})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def admin(client):
    r = client.post("/auth/admin-login", json={"admin_secret": ADMIN})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def contact_decision(settings, cid):
    c = sqlite3.connect(settings.db_path)
    row = c.execute("SELECT id FROM decisions WHERE customer_id=? AND final_action='contact' "
                    "ORDER BY as_of DESC LIMIT 1", (cid,)).fetchone()
    c.close()
    return row[0]


# ---------------------------------------------------------------- authentication

def test_login_ok_and_token_claims(client):
    r = client.post("/auth/login", json={"customer_id": "C0001", "pin": "1146"})
    body = r.json()
    claims = jwt.decode(body["access_token"], SECRET, algorithms=["HS256"])
    assert claims["sub"] == "C0001" and claims["role"] == "customer" and claims["exp"] > claims["iat"]
    assert body["expires_in"] == 3600


def test_login_failures_are_generic_and_identical(client):
    wrong = client.post("/auth/login", json={"customer_id": "C0001", "pin": "0000"})
    unknown = client.post("/auth/login", json={"customer_id": "C9999", "pin": "1146"})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json() == unknown.json()


def test_login_rate_limit(client):
    for _ in range(5):
        assert client.post("/auth/login", json={"customer_id": "C0001", "pin": "bad"}).status_code == 401
    r = client.post("/auth/login", json={"customer_id": "C0001", "pin": "1146"})   # even the right PIN is blocked
    assert r.status_code == 429


def test_admin_login_and_rate_limit(client):
    assert client.post("/auth/admin-login", json={"admin_secret": "nope"}).status_code == 401
    assert admin(client)
    for _ in range(5):
        client.post("/auth/admin-login", json={"admin_secret": "nope"})
    assert client.post("/auth/admin-login", json={"admin_secret": ADMIN}).status_code == 429


def test_extra_fields_and_bad_ids_are_rejected(client):
    assert client.post("/auth/login", json={"customer_id": "C0001", "pin": "1146", "role": "admin"}
                       ).status_code == 422
    assert client.post("/auth/login", json={"customer_id": "C0001' OR 1=1--", "pin": "1"}).status_code == 422
    h = login(client, "C0001")
    assert client.post("/customers/C0001/feedback", headers=h,
                       json={"decision_id": 1, "response": "not_now", "customer_id": "C0002"}).status_code == 422
    r = client.get("/customers/C0001' OR 1=1--/suggestion", headers=h)
    assert r.status_code in (403, 422) and "OR 1=1" not in r.text


def test_expired_tampered_and_alg_none_tokens_are_rejected(client, settings):
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    expired, _ = create_token(settings, "C0001", "customer", now=old)
    forged = jwt.encode({"sub": "C0001", "role": "customer", "exp": 9999999999}, "wrong" * 10, algorithm="HS256")
    none_alg = jwt.encode({"sub": "C0001", "role": "customer", "exp": 9999999999}, None, algorithm="none")
    for tok in (expired, forged, none_alg, "garbage"):
        r = client.get("/customers/C0001/suggestion", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401, tok[:20]


# ---------------------------------------------------------------- authorisation: every route

def _routes(app, prefix):
    """(method, path) for every route declared on the customer/admin routers. Never empty (asserted)."""
    from api.routes import admin as admin_router
    from api.routes import customer as customer_router
    router = {"/customers/": customer_router, "/admin": admin_router}[prefix]
    out = sorted((m, r.path) for r in router.routes for m in r.methods - {"HEAD", "OPTIONS"})
    assert out, "route list is empty: the authorisation tests would pass vacuously"
    return out


def test_routers_carry_the_auth_dependencies():
    from api.routes import admin as admin_router
    from api.routes import customer as customer_router
    from api.security import require_admin, require_self
    assert [d.dependency for d in customer_router.dependencies] == [require_self]
    assert [d.dependency for d in admin_router.dependencies] == [require_admin]


def _fill(path, cid="C0001"):
    return path.replace("{customer_id}", cid).replace("{decision_id}", "1")


def test_route_tables_are_what_we_expect(client):
    paths = {p for _, p in _routes(client.fastapi_app, "/customers/")}
    assert paths == {"/customers/{customer_id}/suggestion", "/customers/{customer_id}/why/{decision_id}",
                     "/customers/{customer_id}/timeline", "/customers/{customer_id}/feedback",
                     "/customers/{customer_id}/actions/{decision_id}/accept"}


def test_every_customer_route_needs_a_token_and_the_right_customer(client):
    mine, other, adm = login(client, "C0001"), login(client, "D004"), admin(client)
    body = {"decision_id": 1, "response": "not_now"}
    for method, path in _routes(client.fastapi_app, "/customers/"):
        url = _fill(path)
        kw = {"json": body} if method == "POST" and path.endswith("feedback") else {}
        assert client.request(method, url, **kw).status_code == 401, (method, path)            # no token
        assert client.request(method, url, headers=other, **kw).status_code == 403, (method, path)   # other customer
        assert client.request(method, url, headers=adm, **kw).status_code == 403, (method, path)     # admin token
        assert client.request(method, url, headers=mine, **kw).status_code != 403, (method, path)    # owner passes auth


def test_every_admin_route_needs_the_admin_role(client):
    cust, adm = login(client, "C0001"), admin(client)
    routes = _routes(client.fastapi_app, "/admin")
    assert {p for _, p in routes} == {"/admin/dashboard", "/admin/customers/{customer_id}/timeline"}
    for method, path in routes:
        url = _fill(path)
        assert client.request(method, url).status_code == 401, path
        assert client.request(method, url, headers=cust).status_code == 403, path
        assert client.request(method, url, headers=adm).status_code == 200, path


# ---------------------------------------------------------------- cross-customer decision ids (IDOR)

def test_cannot_use_another_customers_decision_id(client, settings):
    olivier_decision = contact_decision(settings, "C0001")
    anna = login(client, "D004")
    assert client.get(f"/customers/D004/why/{olivier_decision}", headers=anna).status_code == 404
    assert client.post("/customers/D004/actions/%d/accept" % olivier_decision, headers=anna).status_code == 404
    r = client.post("/customers/D004/feedback", headers=anna,
                    json={"decision_id": olivier_decision, "response": "not_me"})
    assert r.status_code == 404
    c = sqlite3.connect(settings.db_path)
    assert c.execute("SELECT COUNT(*) FROM feedback WHERE decision_id=?", (olivier_decision,)).fetchone()[0] == 0
    c.close()
    # Olivier's own decision is unaffected and still open
    olivier = login(client, "C0001")
    assert client.get("/customers/C0001/suggestion", headers=olivier).json()["status"] == "open"


def test_non_contact_and_unknown_decision_ids_are_404(client, settings):
    h = login(client, "C0001")
    c = sqlite3.connect(settings.db_path)
    wait_id = c.execute("SELECT id FROM decisions WHERE customer_id='C0001' AND final_action!='contact' LIMIT 1"
                        ).fetchone()[0]
    c.close()
    assert client.get(f"/customers/C0001/why/{wait_id}", headers=h).status_code == 404
    assert client.get("/customers/C0001/why/99999999", headers=h).status_code == 404


# ---------------------------------------------------------------- behaviour

def test_olivier_suggestion_and_why(client):
    h = login(client, "C0001")
    s = client.get("/customers/C0001/suggestion", headers=h).json()
    assert s["available"] and s["event_type"] == "moving_house" and s["as_of"] == "2026-09-14"
    assert s["top_need"] == "home_insurance_transfer" and s["status"] == "open" and s["message"]
    w = client.get(f"/customers/C0001/why/{s['decision_id']}", headers=h).json()
    assert w["evidence"] and w["confidence"] >= 0.75 and w["why"].startswith("We noticed")
    assert "Card and account transactions" in w["data_categories_used"]
    assert w["analysis_source"].startswith("demo rules")
    assert all(e["text"] for e in w["evidence"])


def test_human_support_suggestion_for_sensitive_case(client):
    s = client.get("/customers/D005/suggestion", headers=login(client, "D005")).json()
    assert s["available"] and s["channel"] == "human_support" and s["top_need"] is None


def test_no_suggestion_for_decoy(client):
    assert client.get("/customers/D002/suggestion", headers=login(client, "D002")).json() == {
        "available": False, "first_name": "Lena", **{k: None for k in ("decision_id", "as_of", "event_type", "channel", "top_need",
                                                 "top_need_title", "message", "why", "status")}}


def test_suggestion_as_of_is_capped_at_the_demo_clock(client):
    h = login(client, "C0001")
    early = client.get("/customers/C0001/suggestion?as_of=2026-09-13", headers=h).json()
    assert early["available"] is False
    future = client.get("/customers/C0001/suggestion?as_of=2031-01-01", headers=h).json()
    assert future["available"] and future["as_of"] == "2026-09-14"


def test_timeline_for_replay_and_cap(client):
    h = login(client, "C0001")
    t = client.get("/customers/C0001/timeline?as_of=2026-09-14", headers=h).json()
    assert [s["name"] for s in t["signals"]] == ["rent_deposit", "moving_company", "address_search"]
    assert t["days"][-1]["as_of"] == "2026-09-14" and t["days"][-1]["action"] == "contact"
    assert t["home_city"] == "Gent"
    late = client.get("/customers/C0001/timeline?as_of=2040-01-01", headers=h).json()
    assert late["as_of"] == "2026-09-30" and late["days"][-1]["as_of"] == "2026-09-30"
    assert "kate_address_question" in [s["name"] for s in late["signals"]]


def test_accept_then_double_accept_rejected(client):
    h = login(client, "C0001")
    did = client.get("/customers/C0001/suggestion", headers=h).json()["decision_id"]
    r = client.post(f"/customers/C0001/actions/{did}/accept", headers=h)
    assert r.status_code == 200 and r.json()["simulated"] and r.json()["items"]
    assert any("insurance" in i.lower() for i in r.json()["items"])
    assert client.post(f"/customers/C0001/actions/{did}/accept", headers=h).status_code == 409
    assert client.get("/customers/C0001/suggestion", headers=h).json()["status"] == "accepted"
    assert client.post("/customers/C0001/feedback", headers=h,
                       json={"decision_id": did, "response": "not_me"}).status_code == 409


def test_cannot_accept_a_dismissed_decision(client):
    h = login(client, "C0001")
    did = client.get("/customers/C0001/suggestion", headers=h).json()["decision_id"]
    assert client.post("/customers/C0001/feedback", headers=h,
                       json={"decision_id": did, "response": "not_now"}).status_code == 201
    assert client.post(f"/customers/C0001/actions/{did}/accept", headers=h).status_code == 409
    assert client.get("/customers/C0001/suggestion", headers=h).json()["status"] == "not_now"


def test_feedback_response_must_be_valid(client):
    h = login(client, "C0001")
    assert client.post("/customers/C0001/feedback", headers=h,
                       json={"decision_id": 1, "response": "maybe"}).status_code == 422


def test_stressed_customer_gets_budget_check_only(client):
    h = login(client, "D004")
    s = client.get("/customers/D004/suggestion", headers=h).json()
    assert s["top_need"] == "budget_check"
    items = client.post(f"/customers/D004/actions/{s['decision_id']}/accept", headers=h).json()["items"]
    assert len(items) == 1 and "budget" in items[0].lower()


# ---------------------------------------------------------------- dashboard

def test_dashboard_numbers(client):
    d = client.get("/admin/dashboard", headers=admin(client)).json()
    f = d["funnel"]
    assert f["customers"] == 407 and f["flagged"] == 56 and f["judged"] == 56
    assert f["contacted"] >= 50 and f["confident"] >= f["contacted"]
    assert sum(d["outcome_by_customer"].values()) == f["flagged"]          # each flagged customer counted once
    assert d["llm"]["standin_in_use"] is True
    assert set(d["events_contacted"]) >= {"moving_house", "new_baby", "new_job", "starting_business",
                                          "retirement_approaching"}
    assert d["projection"]["daily_llm_calls"]["no_triage"] == 2_300_000
    assert d["projection"]["daily_llm_calls"]["with_triage_and_fingerprint_reuse"] < \
        d["projection"]["daily_llm_calls"]["with_triage"] < 2_300_000
    assert {h["customer_id"] for h in d["hard_cases"]} == {f"D00{i}" for i in range(1, 8)}
    assert "synthetic data" in d["label"]


# ---------------------------------------------------------------- hardening

def test_security_headers_generic_errors_and_no_docs(client):
    r = client.get("/")
    assert r.headers["X-Content-Type-Options"] == "nosniff" and "frame-ancestors 'none'" in r.headers[
        "Content-Security-Policy"]
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404
    assert client.get("/customers/C0001/suggestion").json() == {"detail": "Not authenticated"}
    assert client.post("/auth/login", content=b"x" * 20000, headers={"Content-Type": "application/json"}
                       ).status_code == 413


def test_unconfigured_server_answers_503_not_defaults(monkeypatch):
    import importlib
    import api.main as m
    monkeypatch.setenv("JWT_SECRET", "")
    monkeypatch.setenv("ADMIN_SECRET", "")
    monkeypatch.setattr("api.settings.load_dotenv", lambda *a, **k: None)
    importlib.reload(m)
    try:
        assert TestClient(m.app).post("/auth/admin-login", json={"admin_secret": "x"}).status_code == 503
    finally:
        monkeypatch.undo()
        importlib.reload(m)


def test_hard_case_journeys_with_standin_judge(client):
    """Stand-in results (NOT LLM evidence): all seven hard cases reach their expected outcome."""
    cases = {c["customer_id"]: c for c in client.get("/admin/dashboard", headers=admin(client)).json()["hard_cases"]}
    assert all(c["ok"] for c in cases.values()), {k: v["actual"] for k, v in cases.items() if not v["ok"]}
    assert cases["D004"]["actual"]["top_need"] == "budget_check"
    assert cases["D005"]["actual"]["channel"] == "human_support"
    assert cases["D003"]["actual"]["reason"] == "recent_dismissal"


# ---------------------------------------------------------------- public dashboard (no login, read-only)

def test_public_dashboard_needs_no_login_and_matches_admin(client):
    pub = client.get("/public/dashboard")
    assert pub.status_code == 200
    adm = client.get("/admin/dashboard", headers=admin(client)).json()
    assert pub.json() == adm and pub.json()["funnel"]["customers"] == 407


def test_public_router_exposes_only_the_dashboard():
    from api.routes import public as public_router
    assert sorted((m, r.path) for r in public_router.routes for m in r.methods) == [("GET", "/public/dashboard")]


def test_public_dashboard_can_be_switched_off(settings):
    from dataclasses import replace
    c = TestClient(create_app(replace(settings, public_dashboard=False)))
    assert c.get("/public/dashboard").status_code == 404


def test_admin_and_customer_routes_stay_protected_with_public_dashboard_on(client):
    assert client.get("/admin/dashboard").status_code == 401
    assert client.get("/admin/customers/C0001/timeline").status_code == 401
    assert client.get("/customers/C0001/suggestion").status_code == 401
    assert client.get("/public/customers/C0001/timeline").status_code == 404


# ---------------------------------------------------------------- demo login (hero only, no credentials)

def test_demo_login_issues_a_token_for_the_hero_only(client):
    r = client.post("/auth/demo-login")
    assert r.status_code == 200
    claims = jwt.decode(r.json()["access_token"], SECRET, algorithms=["HS256"])
    assert claims["sub"] == "C0001" and claims["role"] == "customer" and claims["exp"] > claims["iat"]
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    s = client.get("/customers/C0001/suggestion", headers=h).json()
    assert s["available"] and s["first_name"] == "Olivier"
    # the demo token grants nothing else: other customers and admin routes stay closed
    assert client.get("/customers/D004/suggestion", headers=h).status_code == 403
    assert client.get("/admin/dashboard", headers=h).status_code == 403
    assert client.get("/admin/customers/C0001/timeline", headers=h).status_code == 403


def test_demo_login_takes_no_input_and_cannot_pick_a_customer(client):
    r = client.post("/auth/demo-login", json={"customer_id": "D004"})
    claims = jwt.decode(r.json()["access_token"], SECRET, algorithms=["HS256"])
    assert claims["sub"] == "C0001"                                   # body is ignored
    assert client.get("/auth/demo-login").status_code == 405


def test_demo_login_can_be_switched_off(settings):
    from dataclasses import replace
    c = TestClient(create_app(replace(settings, demo_autologin=False)))
    assert c.post("/auth/demo-login").status_code == 404
    assert c.post("/auth/login", json={"customer_id": "C0001", "pin": "1146"}).status_code == 200   # normal login still works
