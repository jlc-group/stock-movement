"""Create schema, tables, and load all data into PostgreSQL."""
import os
import sys
import psycopg2
import psycopg2.extras
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from brand_map import classify_brand

DB = config.DB

DDL = """
CREATE SCHEMA IF NOT EXISTS stock_online;

-- ===== categories =====
CREATE TABLE IF NOT EXISTS stock_online.categories (
    code        VARCHAR(10) PRIMARY KEY,
    name_th     VARCHAR(100) NOT NULL,
    description VARCHAR(255)
);

-- ===== products (master) =====
CREATE TABLE IF NOT EXISTS stock_online.products (
    id                  SERIAL PRIMARY KEY,
    sheet_name          VARCHAR(100) NOT NULL UNIQUE,
    code                VARCHAR(50)  NOT NULL,
    name                VARCHAR(255),
    category_code       VARCHAR(10)  NOT NULL REFERENCES stock_online.categories(code),
    brand               VARCHAR(50),
    opening_balance     NUMERIC(15,2) DEFAULT 0,
    total_in            NUMERIC(15,2) DEFAULT 0,
    total_out           NUMERIC(15,2) DEFAULT 0,
    closing_balance     NUMERIC(15,2) DEFAULT 0,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_products_code      ON stock_online.products(code);
CREATE INDEX IF NOT EXISTS idx_products_category  ON stock_online.products(category_code);
CREATE INDEX IF NOT EXISTS idx_products_brand     ON stock_online.products(brand);

-- ===== stock_movements (รายวัน) =====
CREATE TABLE IF NOT EXISTS stock_online.stock_movements (
    id              BIGSERIAL PRIMARY KEY,
    product_id      INTEGER NOT NULL REFERENCES stock_online.products(id) ON DELETE CASCADE,
    movement_date   DATE    NOT NULL,
    doc_no          VARCHAR(100),
    brought_forward NUMERIC(15,2) DEFAULT 0,
    qty_in          NUMERIC(15,2) DEFAULT 0,
    qty_out         NUMERIC(15,2) DEFAULT 0,
    balance         NUMERIC(15,2) DEFAULT 0,
    note            TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (product_id, movement_date)
);
CREATE INDEX IF NOT EXISTS idx_movements_product  ON stock_online.stock_movements(product_id);
CREATE INDEX IF NOT EXISTS idx_movements_date     ON stock_online.stock_movements(movement_date);
CREATE INDEX IF NOT EXISTS idx_movements_has_qty  ON stock_online.stock_movements(product_id, movement_date)
    WHERE qty_in <> 0 OR qty_out <> 0;

-- ===== view: สรุปสินค้าคงเหลือ =====
CREATE OR REPLACE VIEW stock_online.v_stock_summary AS
SELECT
    p.id,
    p.code,
    p.name,
    p.category_code,
    c.name_th AS category_name,
    p.opening_balance,
    p.total_in,
    p.total_out,
    p.closing_balance,
    CASE
        WHEN p.closing_balance <= 0 THEN 'OUT_OF_STOCK'
        WHEN p.closing_balance <= 50 THEN 'LOW'
        ELSE 'OK'
    END AS stock_status
FROM stock_online.products p
JOIN stock_online.categories c ON c.code = p.category_code
ORDER BY p.id;

-- ===== view: รายการที่มีการเคลื่อนไหวจริง =====
CREATE OR REPLACE VIEW stock_online.v_actual_movements AS
SELECT
    p.code,
    p.name,
    p.category_code,
    m.movement_date,
    m.doc_no,
    m.qty_in,
    m.qty_out,
    m.balance,
    m.note
FROM stock_online.stock_movements m
JOIN stock_online.products p ON p.id = m.product_id
WHERE m.qty_in <> 0 OR m.qty_out <> 0
ORDER BY p.code, m.movement_date;

-- ===== view: สรุปรายเดือนต่อสินค้า =====
CREATE OR REPLACE VIEW stock_online.v_monthly_summary AS
SELECT
    p.code,
    p.name,
    p.category_code,
    DATE_TRUNC('month', m.movement_date)::DATE AS month,
    SUM(m.qty_in)  AS month_in,
    SUM(m.qty_out) AS month_out,
    MAX(m.balance) FILTER (WHERE m.movement_date = (
        SELECT MAX(movement_date) FROM stock_online.stock_movements
        WHERE product_id = m.product_id
          AND DATE_TRUNC('month', movement_date) = DATE_TRUNC('month', m.movement_date)
    )) AS month_end_balance
FROM stock_online.stock_movements m
JOIN stock_online.products p ON p.id = m.product_id
GROUP BY p.code, p.name, p.category_code, DATE_TRUNC('month', m.movement_date), m.product_id
ORDER BY p.code, month;
"""

CATEGORIES = [
    ('FG',  'สินค้าสำเร็จรูป',  'Finished Goods - Jula\'s Herb / Online SKUs'),
    ('BTA', 'Beauterry',         'Beauterry brand products'),
    ('PM',  'วัสดุ/ของแถม',      'Premium materials, packaging, freebies'),
    ('BOX', 'กล่อง',              'Carton boxes'),
]


def main():
    data = json.load(open(config.JSON_PATH, encoding='utf-8'))
    products = data['products']
    print(f"Loaded {len(products)} products from JSON")

    conn = psycopg2.connect(**DB)
    conn.autocommit = False
    cur = conn.cursor()

    print("→ Creating schema + tables...")
    cur.execute(DDL)
    print("✓ Schema created")

    print("→ Loading categories...")
    psycopg2.extras.execute_values(cur,
        "INSERT INTO stock_online.categories (code, name_th, description) VALUES %s "
        "ON CONFLICT (code) DO UPDATE SET name_th = EXCLUDED.name_th, description = EXCLUDED.description",
        CATEGORIES)
    print(f"✓ {len(CATEGORIES)} categories")

    print("→ Clearing existing products/movements...")
    cur.execute("TRUNCATE stock_online.stock_movements, stock_online.products RESTART IDENTITY CASCADE")

    print("→ Loading products...")
    prod_rows = [
        (p['sheet'], p['code'], p['name'], p['category'], classify_brand(p['code'], p['name'], p['category']),
         p['opening'], p['total_in'], p['total_out'], p['closing'])
        for p in products
    ]
    inserted = psycopg2.extras.execute_values(cur,
        "INSERT INTO stock_online.products "
        "(sheet_name, code, name, category_code, brand, opening_balance, total_in, total_out, closing_balance) "
        "VALUES %s RETURNING id, sheet_name",
        prod_rows, fetch=True)
    sheet_to_id = {sheet: pid for pid, sheet in inserted}
    print(f"✓ {len(inserted)} products")

    print("→ Loading stock movements...")
    mv_rows = []
    for p in products:
        pid = sheet_to_id[p['sheet']]
        for t in p['transactions']:
            mv_rows.append((
                pid,
                t['date'],
                t['doc_no'] or None,
                t['brought_forward'] or 0,
                t['in'] or 0,
                t['out'] or 0,
                t['balance'] or 0,
                t['note'] or None,
            ))
    print(f"   Total movement rows to insert: {len(mv_rows):,}")
    psycopg2.extras.execute_values(cur,
        "INSERT INTO stock_online.stock_movements "
        "(product_id, movement_date, doc_no, brought_forward, qty_in, qty_out, balance, note) "
        "VALUES %s",
        mv_rows, page_size=2000)
    print(f"✓ {len(mv_rows):,} stock movements")

    conn.commit()
    print("\n=== Verification ===")
    cur.execute("SELECT COUNT(*) FROM stock_online.products")
    print(f"products: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM stock_online.stock_movements")
    print(f"stock_movements: {cur.fetchone()[0]}")
    cur.execute("SELECT category_code, COUNT(*) FROM stock_online.products GROUP BY category_code ORDER BY category_code")
    print("by category:", cur.fetchall())
    cur.execute("""
        SELECT category_code,
               SUM(total_in)::INT AS total_in,
               SUM(total_out)::INT AS total_out,
               SUM(closing_balance)::INT AS closing
        FROM stock_online.products GROUP BY category_code ORDER BY category_code
    """)
    print("Totals:")
    for r in cur.fetchall():
        print(f"  {r[0]:4s}  in={r[1]:>10,}  out={r[2]:>10,}  closing={r[3]:>10,}")

    cur.execute("""
        SELECT COUNT(*) FROM stock_online.stock_movements
        WHERE qty_in <> 0 OR qty_out <> 0
    """)
    print(f"Movements with actual qty: {cur.fetchone()[0]:,}")

    cur.close(); conn.close()
    print("\n✅ Done!")


if __name__ == '__main__':
    main()
