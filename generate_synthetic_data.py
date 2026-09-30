"""
Synthetic data generator for the KBC "Life-Event Signal Engine" hackathon project.

Produces 5 separate tables, matching the schema:

  customers.csv       -> id, name, age, city, income_band, products_held (json)
  transactions.csv    -> customer_id, date, amount, merchant, category, city
  app_events.csv      -> customer_id, timestamp, event_type, detail
  kate_messages.csv   -> customer_id, timestamp, text
  ground_truth.csv    -> customer_id, planted_event_type, start_date

Design notes:
  - The engine must NEVER read ground_truth.csv. It exists only so you can score
    your detection accuracy for the demo/pitch.
  - Customer C0001 ("Sophie Van Damme") is a fully hand-scripted moving-house case,
    matching the day-by-day signal timeline used in the demo script:
      Day 1  notary/rent-deposit payment
      Day 3  moving company payment
      Day 5  in-app search "change address"
      Day 6  message to Kate asking about changing domicile
      Day 8  IKEA + paint shop purchases
      Day 9  card transactions start happening in the new city
      Day 12 first payment to a new energy supplier
  - The rest of the population (default 400) gets ~12% planted life events spread
    across 5 event types (moving_house, new_baby, new_job, starting_business,
    retirement_approaching), each expressed as a *combination* of weak/strong
    signals across transactions, app_events, and kate_messages -- never a single
    dead giveaway. The remaining ~88% are pure noise: everyday transactions with
    no event at all, which is what makes the "engine ignores IKEA-alone" story
    credible.

Usage:
  python generate_synthetic_data.py --n 400 --event-rate 0.12 --seed 42
"""

import argparse
import json
import random
import csv
from datetime import datetime, timedelta

FIRST_NAMES = [
    "Emma",
    "Liam",
    "Olivia",
    "Noah",
    "Sien",
    "Arne",
    "Fien",
    "Lars",
    "Marie",
    "Tom",
    "Elise",
    "Jonas",
    "Amber",
    "Wout",
    "Nora",
    "Bram",
    "Julie",
    "Dries",
    "Lotte",
    "Milan",
    "Femke",
    "Ruben",
    "Anke",
    "Stijn",
]
LAST_NAMES = [
    "Peeters",
    "Janssens",
    "Maes",
    "Jacobs",
    "Mertens",
    "Willems",
    "Claes",
    "Goossens",
    "Wouters",
    "De Smet",
    "Vermeulen",
    "Verhoeven",
    "Aerts",
    "Hermans",
    "Michiels",
    "Van Damme",
    "De Clercq",
    "Pauwels",
]
CITIES = [
    "Hasselt",
    "Leuven",
    "Antwerpen",
    "Gent",
    "Brugge",
    "Mechelen",
    "Kortrijk",
    "Genk",
    "Sint-Truiden",
    "Diest",
]
INCOME_BANDS = ["low", "mid", "high"]
PRODUCTS = [
    "Current Account",
    "Savings Account",
    "Credit Card",
    "Home Insurance",
    "Car Insurance",
    "Investment Account",
    "Mortgage",
    "Personal Loan",
    "Pension Savings",
]

EVENT_TYPES = [
    "moving_house",
    "new_baby",
    "new_job",
    "starting_business",
    "retirement_approaching",
]

TODAY = datetime(2026, 9, 30)


# ---------------------------------------------------------------------------
# Baseline (noise) activity -- every customer gets this regardless of event
# ---------------------------------------------------------------------------


def baseline_transactions(rng, customer_id, home_city, salary, days=90):
    """Ordinary, unremarkable transaction history."""
    txns = []
    start = TODAY - timedelta(days=days)
    d = start
    while d <= TODAY:
        if d.day == 25:
            txns.append(
                [
                    customer_id,
                    d.strftime("%Y-%m-%d"),
                    round(salary, 2),
                    "Salary - Employer",
                    "income",
                    home_city,
                ]
            )
        if d.day == 3:
            txns.append(
                [
                    customer_id,
                    d.strftime("%Y-%m-%d"),
                    -round(rng.uniform(650, 1100), 2),
                    rng.choice(
                        [
                            "Rent Payment - Immo Janssens",
                            "Mortgage Payment - KBC",
                        ]
                    ),
                    "housing",
                    home_city,
                ]
            )
        if d.weekday() in (1, 5) and rng.random() < 0.7:
            txns.append(
                [
                    customer_id,
                    d.strftime("%Y-%m-%d"),
                    -round(rng.uniform(15, 90), 2),
                    rng.choice(["Colruyt", "Delhaize", "Aldi", "Carrefour"]),
                    "groceries",
                    home_city,
                ]
            )
        if d.day in (5, 18) and rng.random() < 0.5:
            txns.append(
                [
                    customer_id,
                    d.strftime("%Y-%m-%d"),
                    -round(rng.uniform(40, 120), 2),
                    rng.choice(["Fluvius Energy", "Telenet", "Proximus"]),
                    "utilities",
                    home_city,
                ]
            )
        if rng.random() < 0.15:
            txns.append(
                [
                    customer_id,
                    d.strftime("%Y-%m-%d"),
                    -round(rng.uniform(20, 65), 2),
                    rng.choice(
                        [
                            "Restaurant De Kroon",
                            "Take-away Sushi",
                            "Cafe Central",
                        ]
                    ),
                    "dining",
                    home_city,
                ]
            )
        if rng.random() < 0.2:
            txns.append(
                [
                    customer_id,
                    d.strftime("%Y-%m-%d"),
                    -round(rng.uniform(10, 60), 2),
                    rng.choice(
                        ["NMBS", "Shell", "Q8 Fuel", "Cambio Car Sharing"]
                    ),
                    "transport",
                    home_city,
                ]
            )
        d += timedelta(days=1)
    return txns


# ---------------------------------------------------------------------------
# Sophie: the fully hand-scripted star case
# ---------------------------------------------------------------------------


def build_sophie():
    cid = "C0001"
    customer = [
        cid,
        "Sophie Van Damme",
        29,
        "Gent",
        "mid",
        json.dumps(["Current Account", "Savings Account", "Home Insurance"]),
    ]

    d0 = TODAY - timedelta(days=20)  # "Day 1" of the timeline, 20 days ago
    txns, app_events, kate_msgs = [], [], []

    # steady baseline before the event starts
    txns += baseline_transactions(random.Random(1), cid, "Gent", 2650, days=70)
    # trim baseline so it doesn't overlap/contradict the scripted days below
    txns = [t for t in txns if t[1] < d0.strftime("%Y-%m-%d")]

    def day(n):
        return (d0 + timedelta(days=n - 1)).strftime("%Y-%m-%d")

    def ts(n, hour=14):
        return (d0 + timedelta(days=n - 1, hours=hour)).strftime(
            "%Y-%m-%d %H:%M"
        )

    # Day 1: rent deposit to new landlord
    txns.append(
        [
            cid,
            day(1),
            -1800.00,
            "Rent Deposit - Vastgoed Antwerpen",
            "housing",
            "Gent",
        ]
    )
    # Day 3: moving company
    txns.append(
        [cid, day(3), -540.00, "Verhuizingen Peeters", "moving", "Gent"]
    )
    # Day 5: in-app search
    app_events.append(
        [
            cid,
            ts(
                5,
                20,
            ),
            "search",
            'searched "change address"',
        ]
    )
    # Day 6: message to Kate
    kate_msgs.append(
        [cid, ts(6, 19), "How do I change my domicile / address with KBC?"]
    )
    # Day 8: IKEA + paint shop
    txns.append([cid, day(8), -640.00, "IKEA", "furniture", "Antwerpen"])
    txns.append(
        [
            cid,
            day(8),
            -120.00,
            "Paint Shop - Colora",
            "home improvement",
            "Antwerpen",
        ]
    )
    # Day 9: card use shifts to Antwerp (location signal) -- a few days of normal spend, new city
    for offset, merchant, cat, amt in [
        (9, "Carrefour", "groceries", -42.30),
        (10, "Cafe Central Antwerpen", "dining", -18.50),
        (11, "Delhaize", "groceries", -55.10),
    ]:
        txns.append([cid, day(offset), amt, merchant, cat, "Antwerpen"])
    # Day 12: new energy supplier at the new address
    txns.append(
        [
            cid,
            day(12),
            -95.00,
            "Luminus Energy - New Contract",
            "utilities",
            "Antwerpen",
        ]
    )

    # a little more recent normal life in the new city, up to today
    for offset in range(13, 21):
        if random.random() < 0.4:
            txns.append(
                [
                    cid,
                    day(offset),
                    -round(random.uniform(15, 60), 2),
                    random.choice(["Colruyt Antwerpen", "Delhaize", "NMBS"]),
                    "groceries",
                    "Antwerpen",
                ]
            )

    ground_truth = [cid, "moving_house", day(1)]
    return customer, txns, app_events, kate_msgs, ground_truth


# ---------------------------------------------------------------------------
# Generic planted-event signal generator (for the wider population)
# ---------------------------------------------------------------------------


def inject_planted_event(rng, cid, event_type, home_city, salary, base_txns):
    """Returns (extra_txns, app_events, kate_messages, start_date) for a planted event,
    expressed as a *combination* of weak + strong signals across all 3 tables,
    spread over roughly a 2-3 week window."""
    start = TODAY - timedelta(days=rng.randint(18, 25))
    new_city = rng.choice([c for c in CITIES if c != home_city])
    txns, app_events, kate_msgs = [], [], []

    def d(offset):
        return (start + timedelta(days=offset)).strftime("%Y-%m-%d")

    def t(offset, hour=None):
        h = hour if hour is not None else rng.randint(8, 21)
        return (start + timedelta(days=offset, hours=h)).strftime(
            "%Y-%m-%d %H:%M"
        )

    if event_type == "moving_house":
        txns.append(
            [
                cid,
                d(0),
                -round(rng.uniform(1200, 2200), 2),
                "Rent Deposit - Vastgoed Kantoor",
                "housing",
                home_city,
            ]
        )
        txns.append(
            [
                cid,
                d(2),
                -round(rng.uniform(300, 700), 2),
                "Moving Company - Verhuis Snel",
                "moving",
                home_city,
            ]
        )
        app_events.append([cid, t(4), "search", 'searched "change address"'])
        kate_msgs.append(
            [cid, t(5), "Can you help me update my address with the bank?"]
        )
        txns.append(
            [
                cid,
                d(7),
                -round(rng.uniform(200, 700), 2),
                "IKEA",
                "furniture",
                new_city,
            ]
        )
        for i in range(3):
            txns.append(
                [
                    cid,
                    d(8 + i),
                    -round(rng.uniform(15, 60), 2),
                    rng.choice(["Colruyt", "Delhaize"]),
                    "groceries",
                    new_city,
                ]
            )
        txns.append(
            [
                cid,
                d(11),
                -round(rng.uniform(60, 120), 2),
                "New Energy Supplier Contract",
                "utilities",
                new_city,
            ]
        )

    elif event_type == "new_baby":
        app_events.append([cid, t(1), "search", 'searched "maternity leave"'])
        for i in range(4):
            txns.append(
                [
                    cid,
                    d(2 + i * 2),
                    -round(rng.uniform(20, 150), 2),
                    rng.choice(["Babypark", "Dreambaby", "Pharmacy Co-pay"]),
                    "baby",
                    home_city,
                ]
            )
        kate_msgs.append(
            [cid, t(6), "What benefits am I entitled to after having a baby?"]
        )
        txns.append(
            [
                cid,
                d(14),
                round(rng.uniform(150, 250), 2),
                "Child Benefit - Groeipakket",
                "income",
                home_city,
            ]
        )

    elif event_type == "new_job":
        # remove one recent salary line to simulate a gap, then add a bigger one from a new employer
        salary_txns = [x for x in base_txns if x[4] == "income"]
        if salary_txns:
            base_txns.remove(rng.choice(salary_txns))
        app_events.append(
            [cid, t(2), "search", 'searched "change employer details"']
        )
        txns.append(
            [
                cid,
                d(5),
                round(salary * rng.uniform(1.2, 1.5), 2),
                "Salary - New Employer",
                "income",
                home_city,
            ]
        )
        txns.append(
            [
                cid,
                d(6),
                -round(rng.uniform(60, 180), 2),
                "Business Attire - Zeeman",
                "shopping",
                home_city,
            ]
        )
        kate_msgs.append(
            [cid, t(7), "I started a new job, do I need to update anything?"]
        )

    elif event_type == "starting_business":
        app_events.append(
            [cid, t(1), "search", 'searched "self-employed bank account"']
        )
        txns.append(
            [
                cid,
                d(3),
                -89.5,
                "Company Registration - Ondernemingsloket",
                "business",
                home_city,
            ]
        )
        txns.append(
            [
                cid,
                d(4),
                -round(rng.uniform(2000, 6000), 2),
                "Large Transfer - Business Setup",
                "withdrawal",
                home_city,
            ]
        )
        kate_msgs.append(
            [cid, t(9), "What's the best account setup for a freelancer?"]
        )
        for i in range(2):
            txns.append(
                [
                    cid,
                    d(12 + i * 3),
                    -round(rng.uniform(20, 80), 2),
                    "Business Software Subscription",
                    "business",
                    home_city,
                ]
            )

    elif event_type == "retirement_approaching":
        app_events.append(
            [cid, t(2), "search", 'searched "pension simulation"']
        )
        kate_msgs.append(
            [
                cid,
                t(3),
                "Can you check how much pension I'll get if I retire at 65?",
            ]
        )
        txns.append(
            [
                cid,
                d(6),
                0.0,
                "Pension Simulation Request - MyPension.be",
                "admin",
                home_city,
            ]
        )
        txns.append(
            [
                cid,
                d(9),
                -round(rng.uniform(500, 1200), 2),
                "Transfer to Pension Savings",
                "savings",
                home_city,
            ]
        )

    return txns, app_events, kate_msgs, d(0)


# ---------------------------------------------------------------------------
# Main population builder
# ---------------------------------------------------------------------------


def build_population(n, event_rate, rng):
    customers, transactions, app_events, kate_messages, ground_truth = (
        [],
        [],
        [],
        [],
        [],
    )

    # customer C0001 is always the hand-scripted Sophie
    c, t_, a_, k_, g_ = build_sophie()
    customers.append(c)
    transactions += t_
    app_events += a_
    kate_messages += k_
    ground_truth.append(g_)

    n_events = max(0, round((n - 1) * event_rate))
    event_customer_indices = (
        set(rng.sample(range(2, n + 1), n_events)) if n_events else set()
    )

    for i in range(2, n + 1):
        cid = f"C{i:04d}"
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        age = rng.randint(21, 70)
        home_city = rng.choice(CITIES)
        income_band = rng.choices(INCOME_BANDS, weights=[0.35, 0.45, 0.20])[0]
        salary = {
            "low": rng.uniform(1500, 2200),
            "mid": rng.uniform(2200, 3400),
            "high": rng.uniform(3400, 5500),
        }[income_band]
        products_held = rng.sample(PRODUCTS, k=rng.randint(2, 4))

        customers.append(
            [
                cid,
                f"{first} {last}",
                age,
                home_city,
                income_band,
                json.dumps(products_held),
            ]
        )

        base_txns = baseline_transactions(rng, cid, home_city, salary)

        if i in event_customer_indices:
            event_type = rng.choice(EVENT_TYPES)
            extra_txns, extra_app, extra_kate, start_date = (
                inject_planted_event(
                    rng, cid, event_type, home_city, salary, base_txns
                )
            )
            transactions += base_txns + extra_txns
            app_events += extra_app
            kate_messages += extra_kate
            ground_truth.append([cid, event_type, start_date])
        else:
            transactions += base_txns
            ground_truth.append([cid, "none", ""])

    return customers, transactions, app_events, kate_messages, ground_truth


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--n",
        type=int,
        default=400,
        help="total number of customers (Sophie counts as 1)",
    )
    parser.add_argument(
        "--event-rate",
        type=float,
        default=0.12,
        help="fraction of non-Sophie customers with a planted event",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--outdir", type=str, default=".")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    customers, transactions, app_events, kate_messages, ground_truth = (
        build_population(args.n, args.event_rate, rng)
    )

    write_csv(
        f"{args.outdir}/customers.csv",
        ["id", "name", "age", "city", "income_band", "products_held"],
        customers,
    )
    write_csv(
        f"{args.outdir}/transactions.csv",
        ["customer_id", "date", "amount", "merchant", "category", "city"],
        sorted(transactions, key=lambda r: (r[0], r[1])),
    )
    write_csv(
        f"{args.outdir}/app_events.csv",
        ["customer_id", "timestamp", "event_type", "detail"],
        sorted(app_events, key=lambda r: (r[0], r[1])),
    )
    write_csv(
        f"{args.outdir}/kate_messages.csv",
        ["customer_id", "timestamp", "text"],
        sorted(kate_messages, key=lambda r: (r[0], r[1])),
    )
    write_csv(
        f"{args.outdir}/ground_truth.csv",
        ["customer_id", "planted_event_type", "start_date"],
        ground_truth,
    )

    n_events = sum(1 for g in ground_truth if g[1] != "none")
    print(
        f"Generated {args.n} customers, {n_events} with a planted life event "
        f"({n_events / args.n:.1%})."
    )
    print(
        "Files: customers.csv, transactions.csv, app_events.csv, kate_messages.csv, ground_truth.csv"
    )
    print(
        "Reminder: ground_truth.csv is for YOUR accuracy scoring only -- never feed it to the engine."
    )


if __name__ == "__main__":
    main()
