import sqlite3  # noqa: F401


def test_row_counts(base_conn):
    def n(t):
        return base_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    assert (n("customers"), n("transactions"), n("app_events"), n("kate_messages"), n("ground_truth")) == (
        400, 24008, 49, 49, 400)


def test_ref_ids_stable_and_in_file_order(base_conn):
    q = base_conn.execute
    assert q("SELECT id FROM transactions ORDER BY rowid LIMIT 1").fetchone()[0] == "tx:1"
    assert q("SELECT id FROM transactions ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "tx:24008"
    assert q("SELECT COUNT(DISTINCT id) FROM transactions").fetchone()[0] == 24008
    assert q("SELECT id FROM app_events ORDER BY rowid LIMIT 1").fetchone()[0] == "ev:1"
    assert q("SELECT id FROM kate_messages ORDER BY rowid LIMIT 1").fetchone()[0] == "kate:1"


def test_ground_truth_counts(base_conn):
    got = dict(base_conn.execute("SELECT planted_event_type, COUNT(*) FROM ground_truth GROUP BY 1").fetchall())
    assert got == {"none": 351, "retirement_approaching": 14, "moving_house": 11,
                   "new_job": 11, "starting_business": 7, "new_baby": 6}


def test_pins_hashed_and_address_is_city(base_conn):
    rows = base_conn.execute("SELECT pin_hash, address_on_file, city FROM customers").fetchall()
    assert all(r["pin_hash"].startswith("$2") for r in rows)
    assert all(r["address_on_file"] == r["city"] for r in rows)


def test_fixed_demo_pin_for_olivier(base_conn):
    import bcrypt
    h = base_conn.execute("SELECT pin_hash FROM customers WHERE id='C0001'").fetchone()[0]
    assert bcrypt.checkpw(b"1146", h.encode())


def test_olivier(base_conn):
    r = base_conn.execute("SELECT name, city FROM customers WHERE id='C0001'").fetchone()
    assert (r["name"], r["city"]) == ("Olivier Van Damme", "Gent")
