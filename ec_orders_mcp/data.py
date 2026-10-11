"""Create a deterministic, synthetic SQLite database of e-commerce orders.

All data is fictional. Customer e-mail and phone columns exist on purpose so the
server can demonstrate that personal data never leaves it unmasked.

Usage: python -m ec_orders_mcp.data --db data/orders.db
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE customers (
    customer_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    email        TEXT NOT NULL,
    phone        TEXT NOT NULL,
    prefecture   TEXT NOT NULL,
    signed_up_on TEXT NOT NULL
);
CREATE TABLE products (
    product_id   TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    category     TEXT NOT NULL,
    unit_price   INTEGER NOT NULL
);
CREATE TABLE orders (
    order_id     TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES customers(customer_id),
    channel      TEXT NOT NULL,
    status       TEXT NOT NULL,
    ordered_at   TEXT NOT NULL,
    shipping_fee INTEGER NOT NULL
);
CREATE TABLE order_items (
    order_item_id TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL REFERENCES orders(order_id),
    product_id    TEXT NOT NULL REFERENCES products(product_id),
    quantity      INTEGER NOT NULL,
    unit_price    INTEGER NOT NULL
);
CREATE INDEX idx_orders_ordered_at ON orders(ordered_at);
CREATE INDEX idx_items_order ON order_items(order_id);
"""

CHANNELS = ["marketplace_a", "marketplace_b", "marketplace_c", "own_store"]
CATEGORIES = {"Accessories": (800, 4000), "Home": (1500, 9000), "Apparel": (2000, 12000), "Outdoor": (3000, 20000)}
PREFECTURES = ["Tokyo", "Kanagawa", "Osaka", "Aichi", "Fukuoka", "Hokkaido"]
FIRST = ["Aoi", "Haruto", "Yui", "Sota", "Mio", "Ren", "Hina", "Yuto"]
LAST = ["Sato", "Suzuki", "Takahashi", "Tanaka", "Ito", "Watanabe", "Yamamoto", "Nakamura"]


def build(db_path: str | Path, customers: int = 300, products: int = 40, orders: int = 2000, seed: int = 7) -> Path:
    rng = random.Random(seed)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)

    cust_rows = []
    for i in range(1, customers + 1):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        cust_rows.append(
            (
                f"C{i:05d}",
                name,
                f"customer{i:05d}@example.com",
                f"090-{rng.randint(1000, 9999)}-{rng.randint(1000, 9999)}",
                rng.choice(PREFECTURES),
                (date(2024, 1, 1) + timedelta(days=rng.randint(0, 365))).isoformat(),
            )
        )
    con.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?)", cust_rows)

    prod_rows = []
    for i in range(1, products + 1):
        category = rng.choice(list(CATEGORIES))
        low, high = CATEGORIES[category]
        prod_rows.append((f"P{i:04d}", f"{category} item {i:03d}", category, round(rng.randint(low, high), -1)))
    con.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", prod_rows)

    order_rows, item_rows = [], []
    item_seq = 1
    start = datetime(2025, 1, 1)
    for i in range(1, orders + 1):
        ordered_at = start + timedelta(minutes=rng.randint(0, 365 * 24 * 60 - 1))
        order_id = f"ORD-{10000 + i}"
        order_rows.append(
            (
                order_id,
                rng.choice(cust_rows)[0],
                rng.choices(CHANNELS, [0.4, 0.25, 0.15, 0.2])[0],
                rng.choices(["completed", "shipped", "cancelled", "returned"], [0.82, 0.08, 0.06, 0.04])[0],
                ordered_at.isoformat(sep=" ", timespec="minutes"),
                rng.choice([0, 500, 700]),
            )
        )
        for _ in range(rng.choices([1, 2, 3], [0.65, 0.25, 0.10])[0]):
            product = rng.choice(prod_rows)
            item_rows.append((f"L{item_seq:07d}", order_id, product[0], rng.choice([1, 1, 1, 2, 3]), product[3]))
            item_seq += 1
    con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", order_rows)
    con.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)", item_rows)
    con.commit()
    con.close()
    return db_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic orders database.")
    parser.add_argument("--db", default="data/orders.db")
    args = parser.parse_args()
    print(f"wrote {build(args.db)}")


if __name__ == "__main__":
    main()
