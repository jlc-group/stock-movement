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
import io
import json
import urllib.request
import urllib.error
from functools import wraps
from datetime import datetime, date
from collections import defaultdict

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request, send_from_directory, send_file

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from brand_map import classify_brand

# URL ของ Script-Ecom launcher (ใช้ในหน้า "ออนไลน์" ดึงยอดออนไลน์มาลงช่อง online)
SCRIPT_ECOM_URL = os.getenv("SCRIPT_ECOM_URL", "http://127.0.0.1:4321")

app = Flask(__name__, static_folder=None)

# In-memory set of valid admin session tokens (cleared on restart).
ADMIN_TOKENS = set()

VALID_CATEGORIES = {"FG", "BTA", "PM", "BOX", "OTHER"}

# ---- Channels -----------------------------------------------------------
# Single source of truth for the movement "channel" dimension (added in the
# 001_add_channel migration). The DB column is `channel TEXT NOT NULL DEFAULT
# 'mixed'` with UNIQUE(product_id, movement_date, channel). 'mixed' is the
# legacy lane that pre-migration rows landed in, so a request that omits a
# channel keeps behaving exactly as before by defaulting to 'mixed'.
# `dir` is advisory metadata for the frontend dropdown only (in/out/both).
CHANNELS = [
    {"key": "online",     "label": "ออนไลน์",          "dir": "out"},
    {"key": "offline",    "label": "ออฟไลน์/หน้าร้าน", "dir": "out"},
    {"key": "wholesale",  "label": "ขายส่ง",           "dir": "out"},
    {"key": "redemption", "label": "แลกของรางวัล",     "dir": "out"},
    {"key": "kol",        "label": "KOL",              "dir": "out"},
    {"key": "influencer", "label": "Influencer",       "dir": "out"},
    {"key": "return",     "label": "รับคืน",           "dir": "in"},
    {"key": "receive",    "label": "รับเข้า",          "dir": "in"},
    {"key": "adjust",     "label": "ปรับยอด",          "dir": "both"},
    {"key": "mixed",      "label": "รวม (เดิม)",       "dir": "both"},
]
VALID_CHANNELS = {c["key"] for c in CHANNELS}


def norm_channel(v):
    """Normalize a request channel value.

    Missing/blank -> 'mixed' (backward compatible with pre-migration callers).
    Unknown key -> ValueError, so call sites can return a 400.
    """
    s = str(v or "").strip().lower() or "mixed"
    if s not in VALID_CHANNELS:
        raise ValueError(f"channel ไม่ถูกต้อง: {s}")
    return s


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
               opening_balance, total_in, total_out, closing_balance, brand
        FROM {config.DB_SCHEMA}.products
        WHERE id = %s
    """, (product_id,))
    p = cur.fetchone()
    if not p:
        return None
    cur.execute(f"""
        SELECT movement_date, qty_in, qty_out, balance, doc_no, note, channel
        FROM {config.DB_SCHEMA}.stock_movements
        WHERE product_id = %s
        ORDER BY movement_date, id
    """, (product_id,))
    # channel is appended LAST (index 6) so the existing 0..5 fields keep their
    # positions for every export/create/POST/PUT consumer of this shape.
    tx = [[
        r[0].isoformat() if r[0] else "",
        num(r[1]), num(r[2]), num(r[3]),
        r[4] or "", r[5] or "", r[6] or "mixed",
    ] for r in cur.fetchall()]
    return {
        "sheet": p[1], "code": p[2], "name": p[3] or "", "category": p[4],
        "opening": num(p[5]), "total_in": num(p[6]), "total_out": num(p[7]),
        "closing": num(p[8]), "brand": p[9] or classify_brand(p[2], p[3], p[4]), "tx": tx,
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

    # After the channel migration there can be MANY rows per movement_date
    # (one per channel). Running balance folds every channel of a day; the
    # loop below already sums all returned rows, so the only requirement is a
    # deterministic order. We order by (movement_date, id) — i.e. insertion
    # order within a day — to keep cumulative balances stable across reruns.
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


@app.route("/campaign")
def campaign():
    return send_from_directory(config.FRONTEND_DIR, "campaign.html")


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
               opening_balance, total_in, total_out, closing_balance, brand
        FROM {config.DB_SCHEMA}.products
        ORDER BY id
    """)
    prod_rows = cur.fetchall()

    cur.execute(f"""
        SELECT product_id, movement_date, qty_in, qty_out, balance, doc_no, note, channel
        FROM {config.DB_SCHEMA}.stock_movements
        ORDER BY product_id, movement_date, id
    """)
    # channel appended LAST (index 6 of each tx row), mirroring fetch_product.
    # ORDER BY adds `id` so same-day multi-channel rows have a stable order
    # matching recompute_product's running-balance order.
    tx_by_product = defaultdict(list)
    for r in cur.fetchall():
        tx_by_product[r[0]].append([
            r[1].isoformat() if r[1] else "",
            num(r[2]), num(r[3]), num(r[4]),
            r[5] or "", r[6] or "", r[7] or "mixed",
        ])

    cur.close()
    conn.close()

    products_out = [{
        "sheet": p["sheet_name"],
        "code": p["code"],
        "name": p["name"] or "",
        "category": p["category_code"],
        "brand": p["brand"] or classify_brand(p["code"], p["name"], p["category_code"]),
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


@app.route("/api/channels")
def channels():
    """Canonical channel metadata for the frontend dropdown (static, no DB).

    Derived from the CHANNELS module constant so the allow-list used by the
    POST/PUT validators and the keys advertised here can never drift apart.
    """
    return jsonify(channels=CHANNELS)


@app.route("/api/products/export")
def export_product():
    """Stream one product's full history as a real .xlsx file."""
    code = str(request.args.get("code", "")).strip()
    if not code:
        return jsonify(error="bad_request", detail="ต้องระบุรหัสสินค้า"), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM {config.DB_SCHEMA}.products WHERE code = %s", (code,))
        row = cur.fetchone()
        if not row:
            return jsonify(error="not_found", detail=f"ไม่พบรหัสสินค้า {code}"), 404
        product = fetch_product(cur, row[0])
    finally:
        conn.close()

    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="F3F4F6")
    right = Alignment(horizontal="right")

    ws["A1"] = f"{product['code']} · {product['name']}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"หมวด: {product['category']}  ·  Sheet: {product['sheet']}  ·  {len(product['tx'])} วัน"
    ws["A4"] = "ยอดยกมา"; ws["B4"] = product["opening"]
    ws["A5"] = "รับเข้าทั้งหมด"; ws["B5"] = product["total_in"]
    ws["A6"] = "จ่ายออกทั้งหมด"; ws["B6"] = product["total_out"]
    ws["A7"] = "คงเหลือปัจจุบัน"; ws["B7"] = product["closing"]
    for r in range(4, 8):
        ws[f"A{r}"].font = bold
        ws[f"B{r}"].alignment = right

    hdr = ["วันที่", "เลขที่", "รับ", "จ่าย", "คงเหลือ", "หมายเหตุ"]
    hrow = 9
    for i, h in enumerate(hdr, start=1):
        c = ws.cell(row=hrow, column=i, value=h)
        c.font = bold
        c.fill = head_fill
    for j, t in enumerate(product["tx"], start=hrow + 1):
        d = t[0]
        ws.cell(row=j, column=1, value=("/".join(reversed(d.split("-"))) if d else ""))
        ws.cell(row=j, column=2, value=t[4])
        ws.cell(row=j, column=3, value=t[1])
        ws.cell(row=j, column=4, value=t[2])
        ws.cell(row=j, column=5, value=t[3])
        ws.cell(row=j, column=6, value=t[5])

    widths = [14, 16, 12, 12, 12, 40]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"stock_{code}.xlsx".replace("/", "-")
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=fname,
    )


@app.route("/api/movements/export", methods=["POST"])
def export_movements():
    """Stream a flat daily-movement log (.xlsx) for the given product codes
    within an optional date range. Used by the product-list page "Export"
    button so the file honours whatever filters the user has applied: the
    client sends the already-filtered codes plus the active date range.
    """
    data = request.get_json(silent=True) or {}
    codes = data.get("codes") or []
    codes = [str(c).strip() for c in codes if str(c).strip()]
    date_from = str(data.get("from", "")).strip()
    date_to = str(data.get("to", "")).strip()
    for d in (date_from, date_to):
        if d:
            try:
                date.fromisoformat(d)
            except ValueError:
                return jsonify(error="bad_request", detail="วันที่ต้องอยู่ในรูปแบบ YYYY-MM-DD"), 400
    if not codes:
        return jsonify(error="bad_request", detail="ไม่มีรายการสินค้าให้ส่งออก"), 400

    where = ["p.code = ANY(%s)", "(m.qty_in <> 0 OR m.qty_out <> 0)"]
    params = [codes]
    if date_from:
        where.append("m.movement_date >= %s"); params.append(date_from)
    if date_to:
        where.append("m.movement_date <= %s"); params.append(date_to)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT m.movement_date, p.code, p.name, p.brand, p.category_code,
                   m.qty_in, m.qty_out, m.balance, m.doc_no, m.note
            FROM {config.DB_SCHEMA}.stock_movements m
            JOIN {config.DB_SCHEMA}.products p ON p.id = m.product_id
            WHERE {' AND '.join(where)}
            ORDER BY m.movement_date, p.code, m.id
        """, params)
        rows = cur.fetchall()
    finally:
        conn.close()

    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Movements"
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="2563EB")
    right = Alignment(horizontal="right")

    rng = (f"{'/'.join(reversed(date_from.split('-')))}" if date_from else "เริ่มต้น") + \
          " – " + (f"{'/'.join(reversed(date_to.split('-')))}" if date_to else "ปัจจุบัน")
    ws["A1"] = "รายงานการเคลื่อนไหวสินค้า (รับเข้า / จ่ายออก)"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"ช่วงวันที่: {rng}  ·  {len(rows)} รายการ"

    hdr = ["วันที่", "รหัสสินค้า", "ชื่อสินค้า", "แบรนด์", "หมวด",
           "รับ", "ออก", "คงเหลือ", "เลขที่เอกสาร", "หมายเหตุ"]
    hrow = 4
    for i, h in enumerate(hdr, start=1):
        c = ws.cell(row=hrow, column=i, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
    total_in = total_out = 0.0
    j = hrow
    for r in rows:
        j += 1
        d = r[0].isoformat() if r[0] else ""
        ws.cell(row=j, column=1, value=("/".join(reversed(d.split("-"))) if d else ""))
        ws.cell(row=j, column=2, value=r[1])
        ws.cell(row=j, column=3, value=r[2] or "")
        ws.cell(row=j, column=4, value=r[3] or "")
        ws.cell(row=j, column=5, value=r[4] or "")
        ws.cell(row=j, column=6, value=num(r[5]))
        ws.cell(row=j, column=7, value=num(r[6]))
        ws.cell(row=j, column=8, value=num(r[7]))
        ws.cell(row=j, column=9, value=r[8] or "")
        ws.cell(row=j, column=10, value=r[9] or "")
        total_in += float(r[5] or 0)
        total_out += float(r[6] or 0)

    j += 1
    ws.cell(row=j, column=5, value="รวม").font = bold
    tin = ws.cell(row=j, column=6, value=num(total_in)); tin.font = bold; tin.alignment = right
    tout = ws.cell(row=j, column=7, value=num(total_out)); tout.font = bold; tout.alignment = right

    widths = [14, 16, 34, 16, 8, 10, 10, 12, 16, 30]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    tag = (date_from or "all") + "_" + (date_to or "all")
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"movements_{tag}.xlsx",
    )


@app.route("/api/report/compare-export", methods=["POST"])
def export_report_compare():
    """Stream the Report-page 1/3/6-month comparison as a real .xlsx.
    The client posts the already-computed table (periods + per-product cells)
    so the file matches the on-screen numbers exactly — no server recompute.
    """
    data = request.get_json(silent=True) or {}
    periods = [str(p).strip() for p in (data.get("periods") or []) if str(p).strip()]
    rows = data.get("rows") or []
    if not periods or not rows:
        return jsonify(error="bad_request", detail="ไม่มีข้อมูลให้ส่งออก"), 400

    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Comparison"
    bold = Font(bold=True)
    white = Font(bold=True, color="FFFFFF")
    grp_fill = PatternFill("solid", fgColor="2563EB")
    sub_fill = PatternFill("solid", fgColor="EFF6FF")
    center = Alignment(horizontal="center", vertical="center")
    right = Alignment(horizontal="right")

    ws["A1"] = "เปรียบเทียบสถิติย้อนหลัง " + " / ".join(periods)
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (f"สร้างเมื่อ {datetime.now().strftime('%d/%m/%Y %H:%M')}"
                f"  ·  {len(rows)} สินค้า  ·  ออกรวม · เฉลี่ย/วัน · วันออกมากสุด")

    g, s = 4, 5  # group-header row, sub-header row
    ws.cell(row=g, column=1, value="รหัส"); ws.merge_cells(start_row=g, start_column=1, end_row=s, end_column=1)
    ws.cell(row=g, column=2, value="ชื่อสินค้า"); ws.merge_cells(start_row=g, start_column=2, end_row=s, end_column=2)
    for col in (1, 2):
        hc = ws.cell(row=g, column=col); hc.font = bold; hc.fill = sub_fill; hc.alignment = center
    for pi, plabel in enumerate(periods):
        c0 = 3 + pi * 3
        gc = ws.cell(row=g, column=c0, value=plabel)
        ws.merge_cells(start_row=g, start_column=c0, end_row=g, end_column=c0 + 2)
        gc.font = white; gc.fill = grp_fill; gc.alignment = center
        for k, sublabel in enumerate(["ออกรวม", "เฉลี่ย/วัน", "วันออกมากสุด"]):
            sc = ws.cell(row=s, column=c0 + k, value=sublabel)
            sc.font = bold; sc.fill = sub_fill

    j = s
    for r in rows:
        j += 1
        ws.cell(row=j, column=1, value=str(r.get("code", "")))
        ws.cell(row=j, column=2, value=str(r.get("name", "")))
        cells = r.get("cells") or []
        for pi in range(len(periods)):
            c0 = 3 + pi * 3
            cell = cells[pi] if pi < len(cells) else {}
            oc = ws.cell(row=j, column=c0, value=num(cell.get("out", 0))); oc.alignment = right
            ac = ws.cell(row=j, column=c0 + 1, value=num(cell.get("avg", 0))); ac.alignment = right
            ws.cell(row=j, column=c0 + 2, value=str(cell.get("peak", "") or ""))

    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 34
    for pi in range(len(periods)):
        c0 = 3 + pi * 3
        ws.column_dimensions[get_column_letter(c0)].width = 11
        ws.column_dimensions[get_column_letter(c0 + 1)].width = 11
        ws.column_dimensions[get_column_letter(c0 + 2)].width = 20

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="report_compare.xlsx",
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
    brand = str(data.get("brand", "")).strip()
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
                (sheet_name, code, name, category_code, brand,
                 opening_balance, total_in, total_out, closing_balance)
            VALUES (%s, %s, %s, %s, %s, %s, 0, 0, %s)
            RETURNING id
        """, (sheet_name, code, name, category, brand or classify_brand(code, name, category), opening, opening))
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
    try:
        channel = norm_channel(data.get("channel"))  # missing/blank -> 'mixed'
    except ValueError:
        return jsonify(error="bad_request", detail="channel ไม่ถูกต้อง"), 400

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

        # Same (day, channel) -> accumulate; otherwise a new row. Conflict
        # target is the post-migration key (product_id, movement_date, channel)
        # so each channel keeps its own lane for the same day.
        cur.execute(f"""
            INSERT INTO {config.DB_SCHEMA}.stock_movements
                (product_id, movement_date, doc_no, qty_in, qty_out, balance, note, channel)
            VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
            ON CONFLICT (product_id, movement_date, channel) DO UPDATE SET
                qty_in  = {config.DB_SCHEMA}.stock_movements.qty_in  + EXCLUDED.qty_in,
                qty_out = {config.DB_SCHEMA}.stock_movements.qty_out + EXCLUDED.qty_out,
                doc_no  = COALESCE(EXCLUDED.doc_no, {config.DB_SCHEMA}.stock_movements.doc_no),
                note    = COALESCE(EXCLUDED.note,  {config.DB_SCHEMA}.stock_movements.note)
        """, (product_id, mv_date, doc_no, qty_in, qty_out, note, channel))

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
    try:
        channel = norm_channel(data.get("channel"))  # missing/blank -> 'mixed'
    except ValueError:
        return jsonify(error="bad_request", detail="channel ไม่ถูกต้อง"), 400

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

        # Target one (day, channel) lane — without the channel predicate this
        # would hit every channel row of the day after the migration.
        cur.execute(f"""
            UPDATE {config.DB_SCHEMA}.stock_movements
            SET qty_in = %s, qty_out = %s, doc_no = %s, note = %s
            WHERE product_id = %s AND movement_date = %s AND channel = %s
        """, (qty_in, qty_out, doc_no, note, product_id, mv_date, channel))
        if cur.rowcount == 0:
            conn.rollback()
            return jsonify(error="not_found",
                           detail=f"ไม่พบรายการของวันที่ {mv_date} ช่อง {channel}"), 404

        recompute_product(cur, product_id)
        product = fetch_product(cur, product_id)
        conn.commit()
        return jsonify(status="ok", product=product)
    except Exception as e:
        conn.rollback()
        return jsonify(error="server_error", detail=str(e)), 500
    finally:
        conn.close()


# ---- Bulk import (Excel) -----------------------------------------------
IMPORT_HEADERS = ["รหัสสินค้า", "วันที่", "รับเข้า", "จ่ายออก", "เลขที่เอกสาร", "หมายเหตุ"]


def _parse_import_date(v):
    """Excel cell -> 'YYYY-MM-DD' (or None if blank/invalid)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return False  # present but unparseable


def _parse_import_num(v):
    """Excel cell -> float (0 for blank). Raises ValueError if non-numeric."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return 0.0
    return float(str(v).replace(",", "").strip())


@app.route("/api/movements/import-template")
def import_template():
    """Download a blank .xlsx template with the expected header row."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "movements"
    head_fill = PatternFill("solid", fgColor="2563EB")
    for i, h in enumerate(IMPORT_HEADERS, start=1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
    # Example rows (illustrative only).
    ws.append(["JH703-40G", "05/06/2026", 100, 0, "PO-001", "รับเข้าตัวอย่าง"])
    ws.append(["JH703-40G", "06/06/2026", 0, 30, "SO-001", "จ่ายออกตัวอย่าง"])
    for i, w in enumerate([16, 14, 10, 10, 16, 30], start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="movement_import_template.xlsx",
    )


@app.route("/api/movements/import", methods=["POST"])
@require_admin
def import_movements():
    """Bulk-import daily in/out movements from an uploaded .xlsx.

    Single sheet, header row 1: รหัสสินค้า | วันที่ | รับเข้า | จ่ายออก |
    เลขที่เอกสาร | หมายเหตุ. Same-day rows accumulate (like POST /api/movements).
    Writes NOTHING unless every row is valid AND every code already exists —
    so the client can create missing products then re-upload safely.
    """
    from openpyxl import load_workbook

    # commit=1 actually writes; otherwise it's a dry-run preview (no writes).
    commit = str(request.form.get("commit", "")).strip() == "1"

    f = request.files.get("file")
    if not f:
        return jsonify(error="bad_request", detail="ไม่พบไฟล์ที่อัปโหลด"), 400
    try:
        wb = load_workbook(io.BytesIO(f.read()), data_only=True, read_only=True)
    except Exception:
        return jsonify(error="bad_request", detail="อ่านไฟล์ Excel ไม่สำเร็จ (.xlsx เท่านั้น)"), 400
    ws = wb.active

    # Map columns by header text (flexible to column order).
    header = [str(c.value).strip() if c.value is not None else "" for c in next(ws.iter_rows(min_row=1, max_row=1))]
    def find(*keys):
        for idx, h in enumerate(header):
            hl = h.lower()
            if any(k in hl for k in keys):
                return idx
        return -1
    ci_code = find("รหัส", "code")
    ci_date = find("วันที่", "date")
    ci_in   = find("รับ", "in")
    ci_out  = find("จ่าย", "ออก", "out")
    ci_doc  = find("เอกสาร", "เลขที่", "doc")
    ci_note = find("หมายเหตุ", "note")
    if ci_code < 0 or ci_date < 0:
        return jsonify(error="bad_request",
                       detail="ไม่พบคอลัมน์ 'รหัสสินค้า' หรือ 'วันที่' ในไฟล์"), 400

    def cell(row, idx):
        return row[idx] if 0 <= idx < len(row) else None

    rows, errors = [], []
    for rno, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if row is None or all(v is None or (isinstance(v, str) and not v.strip()) for v in row):
            continue  # skip blank line
        code = cell(row, ci_code)
        code = str(code).strip() if code is not None else ""
        d = _parse_import_date(cell(row, ci_date))
        try:
            qin = _parse_import_num(cell(row, ci_in))
            qout = _parse_import_num(cell(row, ci_out))
        except ValueError:
            errors.append({"row": rno, "detail": "จำนวนรับ/จ่ายต้องเป็นตัวเลข"})
            continue
        doc = cell(row, ci_doc); doc = str(doc).strip() if doc not in (None, "") else None
        note = cell(row, ci_note); note = str(note).strip() if note not in (None, "") else None

        if not code:
            errors.append({"row": rno, "detail": "ไม่มีรหัสสินค้า"}); continue
        if d is None:
            errors.append({"row": rno, "detail": "ไม่มีวันที่"}); continue
        if d is False:
            errors.append({"row": rno, "detail": "วันที่ไม่ถูกต้อง (ใช้ YYYY-MM-DD หรือ DD/MM/YYYY)"}); continue
        if qin < 0 or qout < 0:
            errors.append({"row": rno, "detail": "จำนวนห้ามติดลบ"}); continue
        if qin == 0 and qout == 0:
            errors.append({"row": rno, "detail": "ต้องมีรับเข้าหรือจ่ายออกอย่างน้อย 1 ช่อง"}); continue
        rows.append({"code": code, "date": d, "qty_in": qin, "qty_out": qout,
                     "doc_no": doc, "note": note})

    if errors:
        return jsonify(error="invalid_rows",
                       detail=f"พบข้อมูลผิดพลาด {len(errors)} แถว",
                       errors=errors[:50]), 400
    if not rows:
        return jsonify(error="empty", detail="ไม่พบข้อมูลในไฟล์"), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT code, id, name FROM {config.DB_SCHEMA}.products")
        code_to_id, code_to_name = {}, {}
        for c, i, nm in cur.fetchall():
            code_to_id[c] = i
            code_to_name[c] = nm or ""
        file_codes = {r["code"] for r in rows}
        unknown_set = {c for c in file_codes if c not in code_to_id}
        unknown = sorted(unknown_set)
        rows_per = defaultdict(int)
        for r in rows:
            if r["code"] in unknown_set:
                rows_per[r["code"]] += 1

        # Preview (dry-run): return the parsed rows for the user to review.
        if not commit:
            return jsonify(
                status="preview",
                total_rows=len(rows),
                unknown_codes=[{"code": c, "rows": rows_per[c]} for c in unknown],
                rows=[{
                    "code": r["code"],
                    "name": code_to_name.get(r["code"], ""),
                    "exists": r["code"] not in unknown_set,
                    "date": r["date"],
                    "qty_in": r["qty_in"],
                    "qty_out": r["qty_out"],
                    "doc_no": r["doc_no"] or "",
                    "note": r["note"] or "",
                } for r in rows],
            )

        if unknown:
            return jsonify(
                status="needs_products",
                detail=f"พบรหัสสินค้าที่ยังไม่มีในระบบ {len(unknown)} รายการ",
                unknown_codes=[{"code": c, "rows": rows_per[c]} for c in unknown],
                total_rows=len(rows),
            )

        affected = set()
        for r in rows:
            pid = code_to_id[r["code"]]
            # Legacy Excel import has no per-row channel -> lands in 'mixed' so
            # totals match pre-migration behavior. Conflict target updated to
            # the new 3-col key (the old 2-col constraint no longer exists).
            cur.execute(f"""
                INSERT INTO {config.DB_SCHEMA}.stock_movements
                    (product_id, movement_date, doc_no, qty_in, qty_out, balance, note, channel)
                VALUES (%s, %s, %s, %s, %s, 0, %s, 'mixed')
                ON CONFLICT (product_id, movement_date, channel) DO UPDATE SET
                    qty_in  = {config.DB_SCHEMA}.stock_movements.qty_in  + EXCLUDED.qty_in,
                    qty_out = {config.DB_SCHEMA}.stock_movements.qty_out + EXCLUDED.qty_out,
                    doc_no  = COALESCE(EXCLUDED.doc_no, {config.DB_SCHEMA}.stock_movements.doc_no),
                    note    = COALESCE(EXCLUDED.note,  {config.DB_SCHEMA}.stock_movements.note)
            """, (pid, r["date"], r["doc_no"], r["qty_in"], r["qty_out"], r["note"]))
            affected.add(pid)

        for pid in affected:
            recompute_product(cur, pid)
        conn.commit()
        return jsonify(status="ok",
                       imported_rows=len(rows),
                       products_affected=len(affected))
    except Exception as e:
        conn.rollback()
        return jsonify(error="server_error", detail=str(e)), 500
    finally:
        conn.close()


@app.route("/api/online/import", methods=["POST"])
@require_admin
def online_import():
    """Idempotent SET-overwrite of the 'online' channel per (product, date).

    Body: { "date": "YYYY-MM-DD", "items": [ {"code": ..., "qty": ...}, ... ] }

    Designed for an automated feed (e.g. Script-Ecom): it OVERWRITES qty_out in
    the online lane (DO UPDATE SET qty_out = EXCLUDED.qty_out) instead of
    accumulating, so re-running the same day's import is safe. It only ever
    touches channel='online' — manually entered lanes (offline, redemption, …)
    are untouched. Unknown codes are skipped (never auto-created).
    """
    data = request.get_json(silent=True) or {}
    mv_date = str(data.get("date", "")).strip()
    items = data.get("items")

    if not mv_date:
        return jsonify(error="bad_request", detail="ต้องระบุวันที่"), 400
    try:
        date.fromisoformat(mv_date)
    except ValueError:
        return jsonify(error="bad_request", detail="วันที่ต้องอยู่ในรูปแบบ YYYY-MM-DD"), 400
    if not isinstance(items, list) or not items:
        return jsonify(error="bad_request", detail="ต้องมี items[] อย่างน้อย 1 รายการ"), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT code, id FROM {config.DB_SCHEMA}.products")
        code_to_id = {c: i for c, i in cur.fetchall()}

        affected = set()
        written = 0
        skipped = 0
        for it in items:
            code = str((it or {}).get("code", "")).strip()
            try:
                qty = float((it or {}).get("qty", 0) or 0)
            except (TypeError, ValueError):
                return jsonify(error="bad_request",
                               detail=f"จำนวนของ {code or '(ไม่มีรหัส)'} ต้องเป็นตัวเลข"), 400
            if qty < 0:
                return jsonify(error="bad_request",
                               detail=f"จำนวนของ {code} ห้ามติดลบ"), 400

            pid = code_to_id.get(code)
            if pid is None:  # unknown code -> skip (do not auto-create)
                skipped += 1
                continue

            # SET-overwrite the online lane (contrast: add_movement accumulates).
            cur.execute(f"""
                INSERT INTO {config.DB_SCHEMA}.stock_movements
                    (product_id, movement_date, doc_no, qty_in, qty_out, balance, note, channel)
                VALUES (%s, %s, NULL, 0, %s, 0, NULL, 'online')
                ON CONFLICT (product_id, movement_date, channel) DO UPDATE SET
                    qty_out = EXCLUDED.qty_out
            """, (pid, mv_date, qty))
            affected.add(pid)
            written += 1

        for pid in affected:
            recompute_product(cur, pid)
        conn.commit()
        return jsonify(status="ok", written=written, skipped=skipped)
    except Exception as e:
        conn.rollback()
        return jsonify(error="server_error", detail=str(e)), 500
    finally:
        conn.close()


@app.route("/api/online/sync", methods=["POST"])
@require_admin
def online_sync():
    """หน้า 'ออนไลน์' กดปุ่มเดียว: ดึงยอดขายออนไลน์ล่าสุดจาก Script-Ecom แล้วลงช่อง 'online' ให้อัตโนมัติ.

    body (ไม่บังคับ): { "date": "YYYY-MM-DD" } — ไม่ส่ง = วันล่าสุดที่ Script-Ecom มี.
    เขียนเฉพาะช่อง online (SET-overwrite, ยิงซ้ำได้) — ไม่แตะช่องที่กรอกมือ.
    """
    data = request.get_json(silent=True) or {}
    mv_date = str(data.get("date", "")).strip()

    # 1) ดึงยอดออนไลน์ (คำนวณ+แปลงรหัสแล้ว) จาก Script-Ecom launcher
    url = SCRIPT_ECOM_URL.rstrip("/") + "/api/stock/propose"
    if mv_date:
        url += "?date=" + mv_date
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as he:
        # Script-Ecom เชื่อมได้ แต่ตอบ error (ที่พบบ่อย: ยังไม่มีข้อมูลออเดอร์ของวันนั้น)
        reason = ""
        try:
            reason = (json.loads(he.read().decode("utf-8")) or {}).get("error", "")
        except Exception:
            reason = ""
        if not reason:
            reason = f"HTTP {he.code}"
        return jsonify(error="no_data",
                       detail=f"Script-Ecom: {reason} — ลองเลือกวันที่ที่ดึงออเดอร์ไว้แล้ว หรือกดดึงออเดอร์ของวันนั้นใน Script-Ecom ก่อน"), 502
    except Exception as e:
        return jsonify(error="upstream_unreachable",
                       detail=f"เชื่อม Script-Ecom ไม่ได้ ({url}) — เปิด start_ui.bat ของ Script-Ecom ค้างไว้ก่อน [{e}]"), 502

    p_date = (payload.get("date") or mv_date or "").strip()
    proposed = payload.get("proposed") or []
    try:
        date.fromisoformat(p_date)
    except ValueError:
        return jsonify(error="bad_request", detail=f"วันที่จาก Script-Ecom ไม่ถูกต้อง: {p_date}"), 400

    # 2) ลงช่อง online (idempotent) — เฉพาะรหัสที่มีใน ecom_stock
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT code, id FROM {config.DB_SCHEMA}.products")
        code_to_id = {c: i for c, i in cur.fetchall()}
        affected, applied, skipped = set(), [], []
        doc_no = "ONLINE-" + p_date.replace("-", "")
        for it in proposed:
            code = str((it or {}).get("code", "")).strip()
            try:
                qty = float((it or {}).get("qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0
            pid = code_to_id.get(code)
            if pid is None:
                skipped.append(code)
                continue
            cur.execute(f"""
                INSERT INTO {config.DB_SCHEMA}.stock_movements
                    (product_id, movement_date, doc_no, qty_in, qty_out, balance, note, channel)
                VALUES (%s, %s, %s, 0, %s, 0, %s, 'online')
                ON CONFLICT (product_id, movement_date, channel) DO UPDATE SET
                    qty_out = EXCLUDED.qty_out, doc_no = EXCLUDED.doc_no, note = EXCLUDED.note
            """, (pid, p_date, doc_no, qty, "ตัดสต็อกออนไลน์ (auto)"))
            affected.add(pid)
            applied.append({"code": code, "qty": qty, "name": (it or {}).get("name", "")})
        for pid in affected:
            recompute_product(cur, pid)
        conn.commit()
        return jsonify(status="ok", date=p_date, written=len(applied),
                       skipped=len(skipped), skipped_codes=skipped, items=applied)
    except Exception as e:
        conn.rollback()
        return jsonify(error="server_error", detail=str(e)), 500
    finally:
        conn.close()


@app.route("/api/online/preview", methods=["POST"])
@require_admin
def online_preview():
    """โหมดพิสูจน์ (read-only): ดึง 'ยอดออนไลน์ที่ควรตัด' จาก Script-Ecom มาแสดงเฉยๆ — ไม่เขียนลง DB เลย.
    ให้ admin เอาไปเทียบกับยอดที่กรอกมือ เพื่อพิสูจน์ความถูกต้องก่อนเปิดโหมดอัตโนมัติ."""
    data = request.get_json(silent=True) or {}
    mv_date = str(data.get("date", "")).strip()
    url = SCRIPT_ECOM_URL.rstrip("/") + "/api/stock/propose"
    if mv_date:
        url += "?date=" + mv_date
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as he:
        reason = ""
        try:
            reason = (json.loads(he.read().decode("utf-8")) or {}).get("error", "")
        except Exception:
            reason = ""
        if not reason:
            reason = f"HTTP {he.code}"
        return jsonify(error="no_data",
                       detail=f"Script-Ecom: {reason} — ลองเลือกวันที่ที่ดึงออเดอร์ไว้แล้ว"), 502
    except Exception as e:
        return jsonify(error="upstream_unreachable",
                       detail=f"เชื่อม Script-Ecom ไม่ได้ ({url}) [{e}]"), 502

    p_date = (payload.get("date") or mv_date or "").strip()
    proposed = payload.get("proposed") or []
    try:
        date.fromisoformat(p_date)
    except ValueError:
        return jsonify(error="bad_request", detail=f"วันที่จาก Script-Ecom ไม่ถูกต้อง: {p_date}"), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT code, id, name FROM {config.DB_SCHEMA}.products")
        code_to = {c: {"id": i, "name": n} for c, i, n in cur.fetchall()}
        # ยอดช่อง online ที่ "เคยลงไว้แล้ว" ของวันนั้น (ถ้ามี) เพื่อเทียบ
        cur.execute(f"""
            SELECT product_id, qty_out FROM {config.DB_SCHEMA}.stock_movements
            WHERE movement_date = %s AND channel = 'online'
        """, (p_date,))
        existing = {pid: q for pid, q in cur.fetchall()}

        items, skipped, total = [], [], 0
        for it in proposed:
            code = str((it or {}).get("code", "")).strip()
            try:
                qty = float((it or {}).get("qty", 0) or 0)
            except (TypeError, ValueError):
                qty = 0
            info = code_to.get(code)
            if info is None:
                skipped.append({"code": code, "qty": qty, "name": (it or {}).get("name", "")})
                continue
            total += qty
            items.append({
                "code": code, "name": info["name"], "qty": qty,
                "already_online": existing.get(info["id"], 0),
            })
        items.sort(key=lambda x: x["code"])
        return jsonify(status="ok", date=p_date, mode="preview",
                       total_qty=total, matched=len(items), skipped=len(skipped),
                       items=items, skipped_items=skipped)
    except Exception as e:
        return jsonify(error="server_error", detail=str(e)), 500
    finally:
        conn.close()


@app.route("/api/online/pull", methods=["POST"])
@require_admin
def online_pull():
    """สั่ง Script-Ecom ดึงออเดอร์ (ค่าเริ่มต้น set2_all = Shopee+Lazada+TikTok) — fire แล้วให้หน้าเว็บ poll สถานะ."""
    data = request.get_json(silent=True) or {}
    job = str(data.get("job", "set2_all")).strip() or "set2_all"
    url = SCRIPT_ECOM_URL.rstrip("/") + "/api/run"
    body = json.dumps({"job": job}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode("utf-8"))
        return jsonify(status="ok", started=True, job=job, launcher=payload)
    except urllib.error.HTTPError as he:
        try:
            reason = (json.loads(he.read().decode("utf-8")) or {}).get("error", f"HTTP {he.code}")
        except Exception:
            reason = f"HTTP {he.code}"
        return jsonify(error="pull_failed", detail=f"Script-Ecom: {reason}"), 502
    except Exception as e:
        return jsonify(error="upstream_unreachable",
                       detail=f"เชื่อม Script-Ecom ไม่ได้ — เปิด start_ui.bat ของ Script-Ecom ค้างไว้ก่อน [{e}]"), 502


@app.route("/api/online/pull/status")
@require_admin
def online_pull_status():
    """อ่านสถานะงานดึงของ Script-Ecom (proxy /api/state) ให้หน้าเว็บ poll."""
    url = SCRIPT_ECOM_URL.rstrip("/") + "/api/state"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            payload = json.loads(r.read().decode("utf-8"))
        return jsonify(status="ok", job=payload.get("job", {}))
    except Exception as e:
        return jsonify(error="upstream_unreachable", detail=str(e)), 502


if __name__ == "__main__":
    app.run(
        host=config.SERVER_HOST,
        port=config.SERVER_PORT,
        debug=os.getenv("FLASK_DEBUG") == "1",
        threaded=True,
    )
