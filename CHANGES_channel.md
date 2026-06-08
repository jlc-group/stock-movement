# CHANGES — รองรับ `channel` (server/app.py)

สรุปการแก้ `server/app.py` ให้รองรับมิติ **channel** ตามสัญญา API หลัง migration
`001_add_channel.sql` (เพิ่มคอลัมน์ `channel TEXT NOT NULL DEFAULT 'mixed'`,
เปลี่ยน UNIQUE เป็น `(product_id, movement_date, channel)`, ข้อมูลเก่า = `'mixed'`).

> ไฟล์เดียวที่แก้คือ `server/app.py` (byte-compile ผ่าน). ไม่แตะ schema/migration,
> frontend, config, scripts หรือไฟล์อื่น

---

## แก้อะไรบ้าง

### 1) เพิ่ม source of truth ของ channel + validator (ระดับโมดูล)
ใกล้ ๆ `VALID_CATEGORIES` / `num()`
- `CHANNELS` — ลิสต์เดียวที่ทุกที่ใช้ร่วมกัน: `online, offline, wholesale, redemption, kol, influencer, return(in), receive(in), adjust(both), mixed(both, legacy)` แต่ละตัวมี `key/label/dir`
- `VALID_CHANNELS = {c["key"] for c in CHANNELS}` (รวม `mixed`)
- `norm_channel(v)` — ว่าง/ไม่ส่ง -> `'mixed'`; key ไม่รู้จัก -> `raise ValueError` (ให้ call site คืน 400)
- กฎ "ไม่ส่ง channel = mixed" และ allow-list อยู่ที่เดียว → POST/PUT/GET-channels/online-import ใช้ตรงกันเสมอ

### 2) `GET /api/channels` (ใหม่, ไม่ต้อง auth)
อยู่ก่อน `export_product()` — คืน `jsonify(channels=CHANNELS)` (static, ไม่แตะ DB) ให้ frontend เอาไปทำ dropdown

### 3) `fetch_product()` — อ่าน tx ต่อสินค้า
- SELECT เพิ่ม `channel` ต่อท้าย
- แต่ละแถว tx เพิ่ม element index 6 = `r[6] or "mixed"` (ต่อท้าย note, **ไม่สลับลำดับ 0..5 เดิม**)
- โครงนี้ถูกใช้โดย `export_product()` และทุก response ของ create/POST/PUT → channel ไหลไปทุกที่อัตโนมัติ

### 4) `GET /api/products` (bulk path)
- SELECT เพิ่ม `channel` ต่อท้าย; `ORDER BY product_id, movement_date, id` (เพิ่ม `id` ให้แถวหลาย channel ในวันเดียวเรียงนิ่ง ตรงกับ recompute)
- append tx เพิ่ม element ที่ 7 = `r[7] or "mixed"` (index map ขยับ: r[0]=product_id … r[6]=note, r[7]=channel)

### 5) `recompute_product()` — running balance
- **ไม่เปลี่ยน logic การบวก** — loop เดิมรวมทุกแถวที่คืนมาอยู่แล้ว จึงรองรับ "หลายแถว/วัน (แยก channel)" โดยอัตโนมัติ
- `ORDER BY movement_date, id` เดิมมี tiebreaker `id` (ลำดับ insert) อยู่แล้ว → balance นิ่งทุกครั้งที่รัน เพิ่มแค่คอมเมนต์อธิบาย

### 6) `POST /api/movements` (`add_movement`)
- parse `channel = norm_channel(data.get("channel"))`; ผิด -> `400 {"error":"bad_request","detail":"channel ไม่ถูกต้อง"}`
- INSERT เพิ่มคอลัมน์ `channel`; `ON CONFLICT (product_id, movement_date, channel) DO UPDATE` — **body สะสมเหมือนเดิม** (qty_in/qty_out บวกเพิ่ม, doc_no/note COALESCE)
- Backward compat: ไม่ส่ง channel -> `mixed` (พฤติกรรมเดิม)

### 7) `PUT /api/movements` (`edit_movement`)
- parse `channel` แบบเดียวกัน (ว่าง -> `mixed`)
- UPDATE เพิ่มเงื่อนไข `AND channel = %s` (กันไปโดนหลายแถวของวันเดียวกัน); params เพิ่ม `channel` ท้ายสุด
- ข้อความ 404 ระบุ channel: `ไม่พบรายการของวันที่ {date} ช่อง {channel}`
- Backward compat: ไม่ส่ง channel = แก้แถว `mixed` (ตรงพฤติกรรมก่อน migration)

### 8) `import_movements()` (นำเข้า Excel) — แก้บังคับ
- เดิมใช้ `ON CONFLICT (product_id, movement_date)` ซึ่ง **constraint นี้ถูกลบหลัง migration** → ถ้าไม่แก้จะ 500
- INSERT เพิ่มคอลัมน์ `channel` ค่าคงที่ `'mixed'`; `ON CONFLICT (product_id, movement_date, channel) DO UPDATE` body สะสมเหมือนเดิม
- params ของ loop เท่าเดิม (เพราะ `'mixed'` hardcode ใน VALUES) — template/parser ของ Excel ไม่ต้องเพิ่มคอลัมน์ channel (นอกสโคป)

### 9) `POST /api/online/import` (ใหม่, `@require_admin`)
อยู่ใน Admin section ต่อจาก `import_movements()`
- Body: `{ "date": "YYYY-MM-DD", "items": [ {"code","qty"} ] }`
- validate วันที่ (`date.fromisoformat`, 400), items ต้องเป็น list ไม่ว่าง (400), qty เป็นเลข >= 0 (400)
- code ที่ไม่พบ -> นับ `skipped` (ไม่ auto-create)
- upsert ช่อง online แบบ **เซ็ตทับ (ไม่สะสม)**: `INSERT ... channel='online' ... ON CONFLICT (product_id, movement_date, channel) DO UPDATE SET qty_out = EXCLUDED.qty_out` (qty_in คง 0) — idempotent, รันซ้ำวันเดิมปลอดภัย
- เก็บ product_id ที่กระทบ → `recompute_product` ทุกตัว → commit
- คืน `{"status":"ok","written":<จำนวนที่เขียน>,"skipped":<จำนวนที่ข้าม>}`
- try/except + rollback แบบเดียวกับ admin endpoints อื่น
- แตะเฉพาะช่อง `online` ช่องที่กรอกมือ (offline/redemption/…) ไม่ถูกแตะ

---

## ไม่แตะอะไร (รักษา backward compat)

- `/`, `/api/health`, `/api/admin/login`, `/api/admin/logout` — เหมือนเดิม
- `create_product` (`POST /api/products`) — ไม่เกี่ยวกับ channel, ไม่แก้
- `export_product` (`/api/products/export`) — ได้ channel ฟรีผ่าน `fetch_product` แต่ไม่บังคับใช้ในไฟล์ Excel ที่ export (คอลัมน์เท่าเดิม)
- template/parser นำเข้า Excel (`IMPORT_HEADERS`, `_parse_import_*`, dry-run/preview, needs_products) — เหมือนเดิม
- `require_admin`, ADMIN_TOKENS, logic การคิด balance/aggregate ใน recompute — ไม่เปลี่ยน
- รูปทรง tx เดิม index 0..5 (date, qty_in, qty_out, balance, doc_no, note) ไม่สลับ — channel ต่อท้ายเป็น index 6 เท่านั้น

## ความเข้ากันได้ย้อนหลัง (สรุป)
- request เดิมที่ "ไม่ส่ง channel" → ลงเลน `mixed` ทุกกรณี (POST สะสม, PUT แก้แถว mixed, import = mixed) → ยอดรวมตรงกับก่อน migration
- response เดิมยังอ่าน index 0..5 ได้ปกติ; client ที่รองรับ channel อ่าน index 6 เพิ่ม

## ตรวจแล้ว
- `python -m py_compile server/app.py` → ผ่าน
- ทุก `ON CONFLICT` ใช้คีย์ 3 คอลัมน์ `(product_id, movement_date, channel)` ตรงกับ migration
- ใช้ชื่อ decorator `@require_admin` (มีอยู่เดิมในไฟล์)
