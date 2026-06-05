# Stock Movement — Project Memory

JLC Group 2026 inventory tracking — Flask + PostgreSQL + vanilla JS.
Repo: `https://github.com/jlc-group/stock-movement`

---

## 1. Source Excel file

**File:** `data/raw/Stock Online_JLC GROUP 2026.xlsx` (~8 MB)
- Path env var: `EXCEL_FILE` in `.env`
- Loader: `scripts/extract_data.py` → produces JSON consumed by importer
- Setup: `scripts/setup_database.py` (creates schema + loads to PostgreSQL)
- Verify: `scripts/verify_data.py`, `scripts/full_audit.py`

### Workbook structure (141 sheets total)

| # | Sheet kind | Naming pattern | Count | Notes |
|---|---|---|---|---|
| 0 | Summary | `สรุปสินค้าคงเหลือ` | 1 | KPI overview, skip on ETL |
| 1..N | Product detail (FG / BTA) | `<seq>.<CODE>` e.g. `1.JH703-40G`, `'72.L20-30G'` | 104 | Numbered, dot-separated |
| - | Packaging / boxes | `BOX*`, `PM*`, `Photo card`, `UMBRELLA`, `PMHANDKERCHIEF`, `PMMIRROR` | ~36 | Non-numeric prefix |
| - | Skip list | `สรุปสินค้าคงเหลือ`, `สรุปวัสดุคงเหลือ`, `Stock`, `รอรหัส` | - | Hardcoded in `SKIP` set |

### Category classification (in `extract_data.py::classify`)
- `<seq>.<CODE>` where `CODE` starts with `BTA` → category **`BTA`**
- `<seq>.<CODE>` for everything else → category **`FG`** (finished goods)
- `BOX*` → `BOX`
- `PM*`, `Photo card`, `UMBRELLA`, `PMHANDKERCHIEF`, `PMMIRROR` → `PM`
- Anything else → `OTHER`

### Summary sheet layout (`สรุปสินค้าคงเหลือ`, 1015 rows × 22 cols)
```
r1   col K : "บริษัท เจแอลซี กรุ๊ป จำกัด"
r3   col A : "รายงานสินค้าคงเหลือ ..."
r4   cols G,H : "ณ วันที่" + date
r5   col A : section header e.g. "สินค้าสำเร็จรูป"
r6   header row: ลำดับ | รหัสสินค้า | ชื่อสินค้า | ยอดยกมา | รับ | จ่าย | จำนวน | หมายเหตุ
r7   unit row : ชิ้น | ชิ้น | ชิ้น | (ชิ้น)
r8+  data    : 1.0 | JH703-40G | name… | brought | in | out | balance | note
```

### Per-product sheet layout (e.g. `1.JH703-40G`, 1033 rows × 26 cols)
```
r1   "Stock - Online"
r2-4 company address block; cols J holds channel labels (Lazada/tiktok/shopee)
r6   A=CODE, B=product name
r7   A="Jan 2026" / "Feb 2026" ... ← MONTH HEADER (loops every ~30 rows)
r8   header: วัน/เดือน/ปี | รหัสสินค้า | เลขที่ | ยอดยกมา | รับ | จ่าย | คงเหลือ | หมายเหตุ
r9   unit row: ชิ้น (etc.)
r10+ data rows; date in col A as datetime
```

**Column index (1-based) for product data rows:**
1. `วัน/เดือน/ปี` (datetime)
2. `รหัสสินค้า`
3. `เลขที่` (doc_no)
4. `ยอดยกมา` (brought / opening for the row)
5. `รับ` (qty_in)
6. `จ่าย` (qty_out)
7. `คงเหลือ` (running balance)
8. `หมายเหตุ` (note)

### ETL gotchas (already handled in `extract_data.py`)
- Some date cells have wrong month vs the latest "Mon 2026" header → **rewritten to match `current_month`** (Excel data-entry errors)
- Duplicate dates within a sheet → first kept, rest skipped (`seen_dates`)
- Cell value `'รวม'` in date column → ignored
- Numeric coercion via `num()` (None/empty/text → 0)
- Code/name fallback: read A6/B6, then B10, then sheet name suffix

---

## 2. Database (PostgreSQL)

**Connection** (in `.env`):
```
DB_HOST=localhost   DB_PORT=5432   DB_NAME=postgres
DB_USER=postgres    DB_PASSWORD=postgres123
DB_SCHEMA=stock_online
```

### Tables
```sql
stock_online.products (
  id SERIAL PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT,
  category TEXT,           -- FG | BTA | BOX | PM | OTHER
  opening_balance NUMERIC DEFAULT 0
)

stock_online.stock_movements (
  id SERIAL PRIMARY KEY,
  product_id INT REFERENCES products(id) ON DELETE CASCADE,
  movement_date DATE NOT NULL,
  qty_in NUMERIC DEFAULT 0,
  qty_out NUMERIC DEFAULT 0,
  balance NUMERIC DEFAULT 0,   -- running balance (recomputed on edits)
  doc_no TEXT,
  note TEXT,
  UNIQUE(product_id, movement_date)
)
```

`UNIQUE(product_id, movement_date)` → only ONE movement row per day; admin POST uses `ON CONFLICT … DO UPDATE` to accumulate same-day in/out; admin PUT overwrites.

---

## 3. Web app

### Backend — `server/app.py` (Flask, port 8000)
- `GET /api/products` → list with `tx[]` per product
- `POST /api/admin/login` → returns bearer token (PIN = `ADMIN_PIN` env, default 2026)
- `POST /api/products` (admin) → create product
- `POST /api/movements` (admin) → upsert daily movement (accumulate same-day)
- `PUT  /api/movements` (admin) → **overwrite** daily movement, then recompute running balance for whole product
- `recompute_product(cur, product_id)` recomputes balance after any edit
- Run: `python -m server.app` (or `server/run.sh`)

### Frontend — `frontend/index.html` (single file, vanilla JS)
- Sticky top bar (`#stickyTop`) + sticky table header pinned via `--sticky-h` CSS var (auto-synced by `ResizeObserver`)
- Year + month dropdowns filter `currentProduct.tx`
  - Helpers: `periodTx()`, `openingBalance()`, `periodLabel()`
- Admin mode (PIN-gated):
  - Add product (`#addProductModal`)
  - Record daily movement (`#movementModal`) — same-day accumulates
  - Edit movement (`#editMovementModal`) — overwrites + recomputes downstream
  - All admin number inputs: `onfocus="this.select()"` + `inputmode="decimal"`
  - Admin modals close ONLY via ปิด / ✕ buttons (no click-outside, no Escape)
- Modal product table thead: `position: sticky; top: 0; z-index: 100`; `.modal-body { isolation: isolate }`

---

## 4. Style / language conventions
- All UI text Thai; code identifiers English
- Currency-less numbers; pieces (`ชิ้น`)
- Dates stored as `YYYY-MM-DD` (ISO) in DB & JSON; displayed as `DD/MM/YYYY` in UI
- Hard refresh (Ctrl+Shift+R) needed when CSS changes (browser caches aggressively)

---

## 5. House rules (from user)
- **Do not modify code beyond what the user asked.** When fixing one bug, keep changes minimal and surgical.
- After implementation, summarize what was changed AND what was deliberately NOT touched.
- When the user says "push" — `git add` only modified files, commit with a descriptive Thai-friendly English message, then `git push origin main`.
- Tests live in `/tmp/check_*.py` (admin: 16 cases, edit: 11 cases) — kept out of repo, run against live server on port 8000.
