"""Flask backend: serves the frontend and a JSON API backed by PostgreSQL.

Read endpoints:
    GET  /                  -> frontend
    GET  /api/health        -> DB connectivity
    GET  /api/products      -> full dataset (compact shape)

Admin (write) endpoints — require Authorization: Bearer <token>:
    POST /api/admin/login   -> exchange PIN for a session token
    POST /api/products      -> create a new product
    POST /api/movements     -> record/accumulate a daily in/out movement
    PUT  /api/movements     -> edit (overwrite) an existing day's movement
"""
import os
import sys
import uuid
from functools import wraps
from datetime import datetime, date
from collections import defaultdict

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

app = Flask(__name__, static_folder=None)

# In-memory set of valid admin session tokens (cleared on restart).
ADMIN_TOKENS = set()

VALID_CATEGORIES = {"FG", "BTA", "PM", "BOX"}


def get_conn():
    return psycopg2.connect(**config.DB)


def num(x):
    """Decimal/None -> JSON-friendly int or float."""
    if x is None:
        return 0
    f = float(x)
    return int(f) if f.is_integer() else f


# ---- Admin auth ---------------------------------------------------------
def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token or token not in ADMIN_TOKENS:
            return jsonify(error="unauthorized", detail="ต้องเข้าสู่โหมด Admin ก่อน"), 401
        return fn(*args, **kwargs)
    return wrapper


# ---- Shared helpers -----------------------------------------------------
def fetch_product(cur, product_id):
    """Return one product in the compact frontend shape (with tx list)."""
    cur.execute(f"""
        SELECT id, sheet_name, code, name, category_code,
               opening_balance, total_in, total_out, closing_balance
        FROM {config.DB_SCHEMA}.products
        WHERE id = %s
    """, (product_id,))
    p = cur.fetchone()
    if not p:
        return None
    cur.execute(f"""
        SELECT movement_date, qty_in, qty_out, balance, doc_no, note
        FROM {config.DB_SCHEMA}.stock_movements
        WHERE product_id = %s
        ORDER BY movement_date, id
    """, (product_id,))
    tx = [[
        r[0].isoformat() if r[0] else "",
        num(r[1]), num(r[2]), num(r[3]),
        r[4] or "", r[5] or "",
    ] for r in cur.fetchall()]
    return {
        "sheet": p[1], "code": p[2], "name": p[3] or "", "category": p[4],
        "opening": num(p[5]), "total_in": num(p[6]), "total_out": num(p[7]),
        "closing": num(p[8]), "tx": tx,
    }


def recompute_product(cur, product_id):
    """Recalculate running balances + product aggregates from opening balance.

    Must be called after any insert/update of that product's movements,
    because `balance` is cumulative and every later row shifts.
    """
    cur.execute(f"""
        SELECT opening_balance FROM {config.DB_SCHEMA}.products WHERE id = %s
    """, (product_id,))
    row = cur.fetchone()
    if not row:
        return
    opening = float(row[0] or 0)

    cur.execute(f"""
        SELECT id, qty_in, qty_out
        FROM {config.DB_SCHEMA}.stock_movements
        WHERE product_id = %s
        ORDER BY movement_date, id
    """, (product_id,))
    rows = cur.fetchall()

    running = opening
    total_in = 0.0
    total_out = 0.0
    for mid, qin, qout in rows:
        qin = float(qin or 0)
        qout = float(qout or 0)
        brought_forward = running
        running += qin - qout
        total_in += qin
        total_out += qout
        cur.execute(f"""
            UPDATE {config.DB_SCHEMA}.stock_movements
            SET balance = %s, brought_forward = %s
            WHERE id = %s
        """, (running, brought_forward, mid))

    cur.execute(f"""
        UPDATE {config.DB_SCHEMA}.products
        SET total_in = %s, total_out = %s, closing_balance = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (total_in, total_out, running, product_id))


# ---- Read endpoints -----------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(config.FRONTEND_DIR, "index.html")


@app.route("/api/health")
def health():
    try:
        conn = get_conn()
        conn.close()
        return jsonify(status="ok", database="connected",
                       admin_enabled=bool(config.ADMIN_PIN))
    except Exception as e:
        return jsonify(status="error", database="unreachable", detail=str(e)), 503


@app.route("/api/products")
def products():
    """Full dataset in the compact shape the frontend expects."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cur.execute(f"""
        SELECT id, sheet_name, code, name, category_code,
               opening_balance, total_in, total_out, closing_balance
        FROM {config.DB_SCHEMA}.products
        ORDER BY id
    """)
    prod_rows = cur.fetchall()

    cur.execute(f"""
        SELECT product_id, movement_date, qty_in, qty_out, balance, doc_no, note
        FROM {config.DB_SCHEMA}.stock_movements
        ORDER BY product_id, movement_date
    """)
    tx_by_product = defaultdict(list)
    for r in cur.fetchall():
        tx_by_product[r[0]].append([
            r[1].isoformat() if r[1] else "",
            num(r[2]), num(r[3]), num(r[4]),
            r[5] or "", r[6] or "",
        ])

    cur.close()
    conn.close()

    products_out = [{
        "sheet": p["sheet_name"],
        "code": p["code"],
        "name": p["name"] or "",
        "category": p["category_code"],
        "opening": num(p["opening_balance"]),
        "total_in": num(p["total_in"]),
        "total_out": num(p["total_out"]),
        "closing": num(p["closing_balance"]),
        "tx": tx_by_product.get(p["id"], []),
    } for p in prod_rows]

    return jsonify(
        generated_at=datetime.now().isoformat(),
        product_count=len(products_out),
        products=products_out,
    )


# ---- Admin endpoints ----------------------------------------------------
@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    if not config.ADMIN_PIN:
        return jsonify(error="admin_disabled",
                       detail="ยังไม่ได้ตั้งค่า ADMIN_PIN ใน .env"), 403
    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin", "")).strip()
    if pin and pin == str(config.ADMIN_PIN):
        token = uuid.uuid4().hex
        ADMIN_TOKENS.add(token)
        return jsonify(status="ok", token=token)
    return jsonify(error="invalid_pin", detail="PIN ไม่ถูกต้อง"), 401


@app.route("/api/admin/logout", methods=["POST"])
@require_admin
def admin_logout():
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    ADMIN_TOKENS.discard(token)
    return jsonify(status="ok")


@app.route("/api/products", methods=["POST"])
@require_admin
def create_product():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip()
    name = str(data.get("name", "")).strip()
    category = str(data.get("category", "")).strip().upper()
    sheet_name = str(data.get("sheet_name", "")).strip() or code
    try:
        opening = float(data.get("opening_balance", 0) or 0)
    except (TypeError, ValueError):
        return jsonify(error="bad_request", detail="ยอดยกมาต้องเป็นตัวเลข"), 400

    if not code:
        return jsonify(error="bad_request", detail="ต้องระบุรหัสสินค้า"), 400
    if category not in VALID_CATEGORIES:
        return jsonify(error="bad_request",
                       detail=f"หมวดต้องเป็น {', '.join(sorted(VALID_CATEGORIES))}"), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        # Duplicate guards
        cur.execute(f"SELECT 1 FROM {config.DB_SCHEMA}.products WHERE code = %s", (code,))
        if cur.fetchone():
            return jsonify(error="conflict", detail=f"มีรหัส {code} อยู่แล้ว"), 409
        cur.execute(f"SELECT 1 FROM {config.DB_SCHEMA}.products WHERE sheet_name = %s", (sheet_name,))
        if cur.fetchone():
            return jsonify(error="conflict", detail=f"มี sheet '{sheet_name}' อยู่แล้ว"), 409

        cur.execute(f"""
            INSERT INTO {config.DB_SCHEMA}.products
                (sheet_name, code, name, category_code,
                 opening_balance, total_in, total_out, closing_balance)
            VALUES (%s, %s, %s, %s, %s, 0, 0, %s)
            RETURNING id
        """, (sheet_name, code, name, category, opening, opening))
        product_id = cur.fetchone()[0]
        product = fetch_product(cur, product_id)
        conn.commit()
        return jsonify(status="ok", product=product), 201
    except Exception as e:
        conn.rollback()
        return jsonify(error="server_error", detail=str(e)), 500
    finally:
        conn.close()


@app.route("/api/movements", methods=["POST"])
@require_admin
def add_movement():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip()
    mv_date = str(data.get("date", "")).strip()
    doc_no = (str(data.get("doc_no", "")).strip() or None)
    note = (str(data.get("note", "")).strip() or None)
    try:
        qty_in = float(data.get("qty_in", 0) or 0)
        qty_out = float(data.get("qty_out", 0) or 0)
    except (TypeError, ValueError):
        return jsonify(error="bad_request", detail="จำนวนต้องเป็นตัวเลข"), 400

    if not code:
        return jsonify(error="bad_request", detail="ต้องระบุรหัสสินค้า"), 400
    if not mv_date:
        return jsonify(error="bad_request", detail="ต้องระบุวันที่"), 400
    try:
        date.fromisoformat(mv_date)
    except ValueError:
        return jsonify(error="bad_request", detail="วันที่ต้องอยู่ในรูปแบบ YYYY-MM-DD"), 400
    if qty_in < 0 or qty_out < 0:
        return jsonify(error="bad_request", detail="จำนวนห้ามติดลบ"), 400
    if qty_in == 0 and qty_out == 0:
        return jsonify(error="bad_request", detail="ต้องกรอกรับเข้าหรือจ่ายออกอย่างน้อย 1 ช่อง"), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM {config.DB_SCHEMA}.products WHERE code = %s", (code,))
        row = cur.fetchone()
        if not row:
            return jsonify(error="not_found", detail=f"ไม่พบรหัสสินค้า {code}"), 404
        product_id = row[0]

        # Same-day -> accumulate; different day -> new row.
        cur.execute(f"""
            INSERT INTO {config.DB_SCHEMA}.stock_movements
                (product_id, movement_date, doc_no, qty_in, qty_out, balance, note)
            VALUES (%s, %s, %s, %s, %s, 0, %s)
            ON CONFLICT (product_id, movement_date) DO UPDATE SET
                qty_in  = {config.DB_SCHEMA}.stock_movements.qty_in  + EXCLUDED.qty_in,
                qty_out = {config.DB_SCHEMA}.stock_movements.qty_out + EXCLUDED.qty_out,
                doc_no  = COALESCE(EXCLUDED.doc_no, {config.DB_SCHEMA}.stock_movements.doc_no),
                note    = COALESCE(EXCLUDED.note,  {config.DB_SCHEMA}.stock_movements.note)
        """, (product_id, mv_date, doc_no, qty_in, qty_out, note))

        recompute_product(cur, product_id)
        product = fetch_product(cur, product_id)
        conn.commit()
        return jsonify(status="ok", product=product)
    except Exception as e:
        conn.rollback()
        return jsonify(error="server_error", detail=str(e)), 500
    finally:
        conn.close()


@app.route("/api/movements", methods=["PUT"])
@require_admin
def edit_movement():
    """Edit an existing daily movement: set (overwrite) qty/doc/note for a date,
    then recompute the running balance for the whole product."""
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip()
    mv_date = str(data.get("date", "")).strip()
    doc_no = (str(data.get("doc_no", "")).strip() or None)
    note = (str(data.get("note", "")).strip() or None)
    try:
        qty_in = float(data.get("qty_in", 0) or 0)
        qty_out = float(data.get("qty_out", 0) or 0)
    except (TypeError, ValueError):
        return jsonify(error="bad_request", detail="จำนวนต้องเป็นตัวเลข"), 400

    if not code:
        return jsonify(error="bad_request", detail="ต้องระบุรหัสสินค้า"), 400
    if not mv_date:
        return jsonify(error="bad_request", detail="ต้องระบุวันที่"), 400
    try:
        date.fromisoformat(mv_date)
    except ValueError:
        return jsonify(error="bad_request", detail="วันที่ต้องอยู่ในรูปแบบ YYYY-MM-DD"), 400
    if qty_in < 0 or qty_out < 0:
        return jsonify(error="bad_request", detail="จำนวนห้ามติดลบ"), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM {config.DB_SCHEMA}.products WHERE code = %s", (code,))
        row = cur.fetchone()
        if not row:
            return jsonify(error="not_found", detail=f"ไม่พบรหัสสินค้า {code}"), 404
        product_id = row[0]

        cur.execute(f"""
            UPDATE {config.DB_SCHEMA}.stock_movements
            SET qty_in = %s, qty_out = %s, doc_no = %s, note = %s
            WHERE product_id = %s AND movement_date = %s
        """, (qty_in, qty_out, doc_no, note, product_id, mv_date))
        if cur.rowcount == 0:
            conn.rollback()
            return jsonify(error="not_found",
                           detail=f"ไม่พบรายการของวันที่ {mv_date}"), 404

        recompute_product(cur, product_id)
        product = fetch_product(cur, product_id)
        conn.commit()
        return jsonify(status="ok", product=product)
    except Exception as e:
        conn.rollback()
        return jsonify(error="server_error", detail=str(e)), 500
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(host=config.SERVER_HOST, port=config.SERVER_PORT, debug=True)
