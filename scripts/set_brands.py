"""Add a `brand` column to products (if missing) and backfill it from code.

Idempotent — safe to re-run, e.g. after a full ETL reload. Brand rules live in
the shared `brand_map.classify_brand`.

Run: python3 scripts/set_brands.py
"""
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from brand_map import classify_brand

SCHEMA = config.DB_SCHEMA


def main():
    conn = psycopg2.connect(**config.DB)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(f"ALTER TABLE {SCHEMA}.products ADD COLUMN IF NOT EXISTS brand VARCHAR(50)")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_products_brand ON {SCHEMA}.products(brand)")

    cur.execute(f"SELECT id, code, name, category_code FROM {SCHEMA}.products")
    rows = cur.fetchall()
    for pid, code, name, category in rows:
        cur.execute(f"UPDATE {SCHEMA}.products SET brand = %s WHERE id = %s",
                    (classify_brand(code, name, category), pid))

    conn.commit()
    cur.execute(f"SELECT brand, COUNT(*) FROM {SCHEMA}.products GROUP BY brand ORDER BY COUNT(*) DESC")
    print("by brand:", cur.fetchall())
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
