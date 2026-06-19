#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
weorder_shadow_cut.py  —  SHADOW (read-only) online stock-cut from weorder.

ใช้ทำ "ยอดเงา" สำหรับเทียบกับโพรเซสเดิมใน ecom_stock ว่าตรงไหม
ก่อนจะพัฒนาเป็นตัวเขียนจริง (connector). *ไม่เขียน DB ใด ๆ*

อ่าน 2 แหล่ง (read-only):
  - weorder DB  (192.168.0.41)  : ออเดอร์จริง -> แตกเซ็ต -> ยอดส่งออกรายวันราย SKU
  - stock-movement DB (localhost): รายชื่อรหัสสินค้า (products.code) ไว้ map ปลายทาง

วิธีนับ "ส่งออกวัน D" = order ที่ COALESCE(rts_time, shipped_at, collection_time)
ตกในวัน D (เวลา server) และ status_normalized ไม่ใช่ CANCELLED/RETURNED
(พิสูจน์แล้วว่าตรงยอดนับมือระดับสัปดาห์ ~0.5%).

การ map รหัส weorder.sku -> stock-movement.code:
  1) ตรงตัว  2) เติม 'JH' ข้างหน้า  3) ตัดท้ายราคา _<digits>  4) JH+ตัดราคา
  + ALIAS (ยืนยันโดยผู้ใช้ 2026-06-14)
SET ที่ไม่มีใน product_set_bom -> dead-letter (ไม่ตัด, แค่ report) ตามที่ตกลง.

usage:
  python scripts/weorder_shadow_cut.py [YYYY-MM-DD]   # default = เมื่อวาน
"""
import os, re, sys, io
from collections import defaultdict
from datetime import date, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
SM_ENV = os.path.join(HERE, "..", ".env")
WO_ENV = r"D:\AI_WORKSPACE\Production\weorder\.env"

# alias: weorder sku (upper) -> stock-movement code (upper). มีอยู่แล้ว อย่าเพิ่มซ้ำ
ALIAS = {
    "PHOTOCARD": "PHOTO CARD",
    "T6A-10G": "JHT6.1-10G",
    "T6A-5G": "JHT6.1-5G",
}


def load_env(path):
    e = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                e[k.strip()] = v.strip().strip('"')
    return e


def connect_sm(e):
    return psycopg2.connect(
        host=e.get("DB_HOST", "localhost"), port=e.get("DB_PORT", "5432"),
        dbname=e.get("DB_NAME", "postgres"), user=e.get("DB_USER", "postgres"),
        password=e.get("DB_PASSWORD", "postgres123"),
    )


def connect_wo(e):
    c = psycopg2.connect(
        host=e["POSTGRES_SERVER"], port=e.get("POSTGRES_PORT", "5432"),
        dbname=e["POSTGRES_DB"], user=e["POSTGRES_USER"],
        password=e["POSTGRES_PASSWORD"], connect_timeout=15,
    )
    c.set_session(readonly=True)
    return c


def build_mapper(sm_codes):
    # upper -> original code
    table = {x.strip().upper(): x for x in sm_codes if x}

    def to_code(sku):
        s = str(sku or "").upper().strip()
        if s in ALIAS:
            s = ALIAS[s]
        if s in table:
            return table[s]
        if "JH" + s in table:
            return table["JH" + s]
        st = re.sub(r"_\d{2,4}$", "", s)
        if st != s:
            if st in table:
                return table[st]
            if "JH" + st in table:
                return table["JH" + st]
        return None

    return to_code


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else (date.today() - timedelta(days=1)).isoformat()

    sm_env = load_env(SM_ENV)
    c = connect_sm(sm_env)
    cur = c.cursor()
    sch = sm_env.get("DB_SCHEMA", "stock_online")
    cur.execute(f"SELECT code FROM {sch}.products")
    sm_codes = [r[0] for r in cur.fetchall() if r[0]]
    c.close()
    to_code = build_mapper(sm_codes)

    wo_env = load_env(WO_ENV)
    c = connect_wo(wo_env)
    cur = c.cursor()
    cur.execute("SELECT id, sku FROM product")
    pid2sku = {r[0]: (r[1] or "") for r in cur.fetchall()}
    sku2pid = {(v or "").upper().strip(): k for k, v in pid2sku.items() if v}
    bom = defaultdict(list)
    cur.execute("SELECT set_product_id, component_product_id, quantity FROM product_set_bom")
    for sp, cp, q in cur.fetchall():
        bom[sp].append((cp, float(q or 1)))
    cur.execute(
        """SELECT oi.sku, oi.quantity, oh.channel_code
             FROM order_item oi JOIN order_header oh ON oh.id = oi.order_id
            WHERE COALESCE(oh.rts_time, oh.shipped_at, oh.collection_time)::date = %s
              AND COALESCE(oh.status_normalized,'') NOT IN ('CANCELLED','RETURNED')""",
        (target,),
    )
    rows = cur.fetchall()
    c.close()

    def resolve(raw, qty, depth=0):
        s = (raw or "").upper().strip()
        pid = sku2pid.get(s)
        if pid and pid in bom and depth < 4:
            out = []
            for cp, bq in bom[pid]:
                out += resolve(pid2sku.get(cp, ""), qty * bq, depth + 1)
            return out
        if pid:
            return [(pid2sku.get(pid, s), qty)]
        return [(s, qty)]

    cut = defaultdict(float)
    dead = defaultdict(float)
    bychan = defaultdict(float)
    for raw, q, ch in rows:
        for sku, cq in resolve(raw, float(q or 0)):
            code = to_code(sku)
            if code:
                cut[code] += cq
                bychan[ch or "?"] += cq
            else:
                dead[sku] += cq

    tot = sum(cut.values())
    dtot = sum(dead.values())
    print(f"===== weorder SHADOW online cut — {target} (read-only, ไม่เขียน DB) =====")
    print(f"order lines: {len(rows)} | ตัดได้ {tot:,.0f} ชิ้น -> {len(cut)} รหัส "
          f"| dead-letter {dtot:,.0f} ชิ้น / {len(dead)} sku ({(dtot/(tot+dtot)*100 if tot+dtot else 0):.1f}%)")
    print("แยกช่อง:", {k: int(v) for k, v in sorted(bychan.items(), key=lambda x: -x[1])})
    print("--- ยอดตัดราย code (เรียงรหัส, เทียบกับโพรเซสเดิมได้) ---")
    for code in sorted(cut):
        print(f"  {cut[code]:8,.0f}  {code}")
    if dead:
        print("--- dead-letter (SET ไม่มี BOM, ไม่ตัด) ---")
        for sku, v in sorted(dead.items(), key=lambda x: -x[1]):
            print(f"  {v:8,.0f}  {sku}")


if __name__ == "__main__":
    main()
