"""Synthetic, schema-identical copy of `thelook_ecommerce` for offline runs.

Includes *fake* PII (names, emails, addresses, coordinates) on purpose so the masking layers are
tested against data that looks real. Deterministic (seeded). A few business "stories" are baked in
so offline answers are interesting:
  * seasonality (Nov/Dec peaks) and year-over-year growth;
  * Texas customers spend less per order than California customers;
  * last full month has a cancellation/return spike (the "churn spike" question);
  * one denim brand has a much higher return rate than another.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from retail_agent.warehouse.schema import DUCKDB_TYPES, TABLES

FIRST = ["James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda", "William", "Elizabeth",
         "David", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah", "Wei", "Mei", "Lucas",
         "Ana", "Pedro", "Camila", "Min-jun", "Ji-woo", "Louis", "Chloe", "Oliver", "Emma", "Lukas", "Mia"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
        "Wang", "Li", "Zhang", "Silva", "Santos", "Kim", "Lee", "Park", "Martin", "Bernard", "Taylor", "Muller"]
STREETS = ["Oak", "Maple", "Pine", "Cedar", "Elm", "Washington", "Lake", "Hill", "Park", "Sunset", "River"]
SUFFIX = ["St", "Ave", "Rd", "Blvd", "Ln", "Dr", "Way", "Ct"]

GEO = {
    "United States": (0.42, {
        "California": ["Los Angeles", "San Francisco", "San Diego", "San Jose"],
        "Texas": ["Houston", "Dallas", "Austin", "San Antonio"],
        "New York": ["New York", "Buffalo", "Albany"],
        "Florida": ["Miami", "Orlando", "Tampa"],
        "Illinois": ["Chicago", "Springfield"],
        "Washington": ["Seattle", "Spokane"],
        "Vermont": ["Burlington"],
    }),
    "China": (0.25, {"Guangdong": ["Guangzhou", "Shenzhen"], "Shanghai": ["Shanghai"], "Beijing": ["Beijing"]}),
    "Brasil": (0.13, {"Sao Paulo": ["Sao Paulo", "Campinas"], "Rio de Janeiro": ["Rio de Janeiro"]}),
    "South Korea": (0.06, {"Seoul": ["Seoul"], "Busan": ["Busan"]}),
    "France": (0.05, {"Ile-de-France": ["Paris"], "Provence": ["Marseille"]}),
    "United Kingdom": (0.05, {"England": ["London", "Manchester"]}),
    "Germany": (0.04, {"Berlin": ["Berlin"], "Bavaria": ["Munich"]}),
}
TRAFFIC = (["Search", "Organic", "Facebook", "Email", "Display"], [0.70, 0.15, 0.06, 0.05, 0.04])

CATEGORIES = {
    "Women": ["Intimates", "Dresses", "Tops & Tees", "Sweaters", "Jeans", "Swim", "Outerwear & Coats",
              "Pants & Capris", "Skirts", "Active", "Accessories", "Sleep & Lounge"],
    "Men": ["Jeans", "Outerwear & Coats", "Tops & Tees", "Sweaters", "Shorts", "Underwear", "Swim", "Active",
            "Suits & Sport Coats", "Pants", "Socks", "Accessories"],
}
PRICE = {"Outerwear & Coats": 140, "Suits & Sport Coats": 160, "Jeans": 80, "Sweaters": 70, "Dresses": 75,
         "Active": 45, "Swim": 50, "Pants": 60, "Pants & Capris": 55, "Skirts": 45, "Tops & Tees": 30,
         "Intimates": 25, "Underwear": 20, "Socks": 12, "Shorts": 35, "Accessories": 40, "Sleep & Lounge": 38}
BRANDS = ["Levi's", "Calvin Klein", "Tommy Hilfiger", "Carhartt", "Columbia", "Nike", "Hanes", "Allegra K",
          "Diesel", "Wrangler", "The North Face", "Ralph Lauren", "Champion", "Speedo", "Patagonia", "Volcom"]
ADJ = ["Classic", "Slim Fit", "Relaxed", "Premium", "Essential", "Vintage", "Performance", "Organic"]
STATUS = ["Complete", "Shipped", "Processing", "Cancelled", "Returned"]


def _months_back(now: datetime, n: int) -> datetime:
    y, m = now.year, now.month - n
    while m <= 0:
        m += 12
        y -= 1
    return datetime(y, m, 1)


def generate(n_users: int = 15000, n_products: int = 1200, seed: int = 7, now: datetime | None = None) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    now = (now or datetime.now()).replace(microsecond=0)
    start = datetime(2022, 1, 1)
    last_month_start = _months_back(now, 1)
    this_month_start = _months_back(now, 0)
    span_days = (now - start).days

    # ---------------- products ----------------
    prod_rows = []
    for pid in range(1, n_products + 1):
        dept = "Women" if rng.random() < 0.55 else "Men"
        cat = rng.choice(CATEGORIES[dept])
        brand = rng.choice(BRANDS)
        if cat == "Jeans" and rng.random() < 0.5:
            brand = rng.choice(["Levi's", "Wrangler", "Diesel"])
        price = round(float(PRICE[cat] * rng.lognormal(0, 0.35)), 2)
        prod_rows.append({
            "id": pid,
            "cost": round(price * float(rng.uniform(0.35, 0.6)), 2),
            "category": cat,
            "name": f"{brand} {rng.choice(ADJ)} {cat.split(' & ')[0].rstrip('s')} {pid}",
            "brand": brand,
            "retail_price": price,
            "department": dept,
            "sku": f"SKU{pid:07d}{rng.integers(100, 999)}",
            "distribution_center_id": int(rng.integers(1, 11)),
        })
    products = pd.DataFrame(prod_rows)

    # ---------------- users (with FAKE PII) ----------------
    countries = list(GEO)
    cprobs = np.array([GEO[c][0] for c in countries])
    cprobs = cprobs / cprobs.sum()
    user_rows = []
    for uid in range(1, n_users + 1):
        country = countries[rng.choice(len(countries), p=cprobs)]
        states = GEO[country][1]
        state = rng.choice(list(states))
        city = rng.choice(states[state])
        gender = "F" if rng.random() < 0.5 else "M"
        fn, ln = rng.choice(FIRST), rng.choice(LAST)
        created = start + timedelta(days=float(rng.uniform(0, span_days)) ** 1.0)
        user_rows.append({
            "id": uid,
            "first_name": fn,
            "last_name": ln,
            "email": f"{fn.lower()}.{ln.lower()}{uid}@example.com",
            "age": int(rng.integers(12, 71)),
            "gender": gender,
            "state": state,
            "street_address": f"{rng.integers(10, 9999)} {rng.choice(STREETS)} {rng.choice(SUFFIX)}",
            "postal_code": f"{rng.integers(10000, 99999)}",
            "city": city,
            "country": country,
            "latitude": round(float(rng.uniform(-35, 60)), 6),
            "longitude": round(float(rng.uniform(-120, 140)), 6),
            "traffic_source": rng.choice(TRAFFIC[0], p=TRAFFIC[1]),
            "created_at": created,
            "user_geom": None,
        })
    users = pd.DataFrame(user_rows)
    users["user_geom"] = "POINT(" + users["longitude"].astype(str) + " " + users["latitude"].astype(str) + ")"

    # ---------------- orders + items ----------------
    pid_by_cat_brand_return = {"Diesel": 0.25, "Levi's": 0.06}
    prod_price = products.set_index("id")["retail_price"].to_dict()
    prod_brand = products.set_index("id")["brand"].to_dict()
    order_rows, item_rows = [], []
    oid = iid = 0
    for u in user_rows:
        n_orders = int(rng.geometric(0.42))
        for _ in range(n_orders):
            # orders skew towards recent dates (growth) and Nov/Dec (seasonality)
            for _try in range(4):
                lo = max((u["created_at"] - start).days, 0)
                d = lo + (span_days - lo) * (rng.random() ** 0.7)
                created = start + timedelta(days=float(d), seconds=float(rng.integers(0, 86400)))
                if created.month in (11, 12) or rng.random() < 0.8:
                    break
            if created > now:
                created = now - timedelta(hours=float(rng.uniform(1, 72)))
            in_last_month = last_month_start <= created < this_month_start
            p_status = np.array([0.50, 0.22, 0.10, 0.10, 0.08])
            if in_last_month:  # churn spike story
                p_status = np.array([0.36, 0.18, 0.08, 0.22, 0.16])
            status = STATUS[rng.choice(5, p=p_status / p_status.sum())]
            n_items = int(rng.choice([1, 1, 1, 2, 2, 3, 4]))
            if u["state"] == "Texas" and n_items > 1 and rng.random() < 0.5:
                n_items -= 1  # Texas underspending story: smaller baskets
            oid += 1
            shipped = created + timedelta(days=float(rng.uniform(0.2, 3))) if status != "Processing" and status != "Cancelled" else None
            delivered = shipped + timedelta(days=float(rng.uniform(1, 5))) if shipped and status in ("Complete", "Returned") else None
            returned = delivered + timedelta(days=float(rng.uniform(1, 10))) if delivered and status == "Returned" else None
            for _ in range(n_items):
                iid += 1
                pid = int(rng.integers(1, n_products + 1))
                item_status = status
                brand_ret = pid_by_cat_brand_return.get(prod_brand[pid])
                if brand_ret and status == "Complete" and rng.random() < brand_ret:
                    item_status = "Returned"
                price = prod_price[pid]
                if u["state"] == "Texas":
                    price = round(price * 0.85, 2)  # Texas buys cheaper items / more discounts
                item_rows.append({
                    "id": iid, "order_id": oid, "user_id": u["id"], "product_id": pid,
                    "inventory_item_id": iid * 3 + int(rng.integers(0, 3)), "status": item_status,
                    "created_at": created, "shipped_at": shipped, "delivered_at": delivered,
                    "returned_at": returned if item_status == "Returned" else None,
                    "sale_price": price,
                })
            order_rows.append({
                "order_id": oid, "user_id": u["id"], "status": status, "gender": u["gender"],
                "created_at": created, "returned_at": returned, "shipped_at": shipped,
                "delivered_at": delivered, "num_of_item": n_items,
            })
    orders = pd.DataFrame(order_rows)
    order_items = pd.DataFrame(item_rows)
    return {"orders": orders, "order_items": order_items, "products": products, "users": users}


def write_duckdb(frames: dict[str, pd.DataFrame], path: Path) -> None:
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = duckdb.connect(str(path))
    try:
        for name, table in TABLES.items():
            cols = ", ".join(f'"{c.name}" {DUCKDB_TYPES[c.type]}' for c in table.columns)
            con.execute(f'CREATE TABLE "{name}" ({cols})')
            df = frames[name][table.column_names()]
            con.register("_df", df)
            con.execute(f'INSERT INTO "{name}" SELECT * FROM _df')
            con.unregister("_df")
    finally:
        con.close()
