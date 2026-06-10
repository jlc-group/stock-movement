---
date: 2026-06-09
title: Reconcile pgSQL ให้ตรงกับ Excel (ทีละสินค้า) — รอบแรก 7 ตัว
status: in-progress
tags: [work-log, data, reconciliation, pgsql, excel]
---

# Reconcile pgSQL ↔ Excel (ทีละสินค้า)

> ⚠️ เป็นการ **เขียนข้อมูลลง remote pgSQL จริง** (ไม่ใช่ ETL reload) — แก้รายสินค้า
> ตามที่ user สั่งทีละตัว มี backup ราย-สินค้าเก็บไว้ที่ `/tmp/backup_<CODE>_<ts>.json`

## สถานการณ์ (Context)
- pgSQL = ฐานข้อมูลหลักของแอป, Excel = ข้อมูลเก่า/อ้างอิง
- ตั้งแต่ 4 มิ.ย. user บันทึกผ่านหน้าเว็บลง pgSQL โดยตรง บางส่วนไม่ได้เขียนใน Excel
- เป้าหมายสุดท้าย: ข้อมูลเก่าจาก Excel ต้องตรงกับ pgSQL
- ข้อมูล **เหมือนกันเป๊ะ ม.ค.–2 มิ.ย.** ความต่างกระจุกที่ **3–9 มิ.ย.** (+ บางตัวมี date-shift 13/05)

## กฎที่ user กำหนด (Decision rules)
1. **Date-shift รับ 03/06 (Excel) vs 04/06 (web)** → ยึด Excel = ย้ายรับมา 03/06
2. **ห้ามแก้รหัสสินค้าใน pgSQL** — user เปลี่ยนรหัสบางตัวเองแล้ว (เช่น JHT5.1→JHT5A,
   L8A-2.5G ฯลฯ) ให้ยึดรหัส pgSQL จับคู่ Excel ด้วย "ชื่อ" แทนเมื่อรหัสต่าง
3. 2 สินค้าที่มีใน Excel แต่ไม่มีใน pgSQL (`JHL5A-90G`, `JHD2-12G`) → **ข้ามไปก่อน** (ยังไม่สร้าง)

## รูปแบบความต่างที่เจอซ้ำ (Patterns)
- (ก) รับเลื่อนวัน 03→04 มิ.ย. (ยอดรวมเท่ากัน) — บางตัวเลื่อนข้ามเดือน 13/05→13/06
- (ข) บางตัว รับ 05–06 มิ.ย. ยังไม่ลง pgSQL (pgSQL ขาดรับ)
- (ค) pgSQL ตัดออก (qty_out) เกิน Excel ช่วง 8–9 มิ.ย.

## สินค้าที่ COMMITTED แล้ววันนี้ (7 ตัว)
| รหัส | ชื่อ | วิธี sync | คงเหลือ เดิม→ใหม่ |
|---|---|---|---|
| JHA1-40G | วอเตอร์เมลอน บีบี บอดี้โลชั่น 40G | full (in+out) ยึด Excel | 5,857 → 7,294 |
| JHL3-8G  | ดีดีครีมวอเตอร์เมล่อนซันสกีน 8G | full ยึด Excel | 1,811 → 38,240 |
| JHL9-48G | อโวคาโด ไฮโดร ล็อก มอยส์เจอร์ ครีม 48G | full ยึด Excel | 532 → 593 |
| JHL3-40G | ดีดีครีมวอเตอร์เมล่อนซันสกีน 40G | **in-only** (คง qty_out pgSQL) | 1,732 → 11,812 |
| PM0009 | ถุงผ้าเขียวจุฬาฯ ใหญ่ | full ยึด Excel | 2,510 → 2,490 |
| PM0031 | กระเป๋าผ้าเขียวฯ ใบเล็ก | full ยึด Excel | 4,965 → 4,855 |
| PM0059 | ผ้าโพกหัวดอกทานตะวัน | full ยึด Excel | 7,472 → 6,481 |

หมายเหตุ JHL3-40G: user สั่งให้ sync เฉพาะ "รับ" (5,670 ย้าย 03/06 + 10,080 ของ 05/06)
แต่ **คง "ออก" pgSQL เดิมไว้ไม่ตัด** → คงเหลือต่าง Excel อยู่ 1,781 ตามตั้งใจ

## วิธี/สคริปต์ (อยู่ใน /tmp ไม่เข้า repo)
- `/tmp/sync_product.py CODE [CODE...]` = full sync (qty_in+qty_out=Excel ทุกวัน, zero แถว
  ที่ไม่มีใน Excel), backup ก่อน, recompute balance (มิเรอร์ `app.recompute_product`)
- `/tmp/sync_in_only.py CODE` = sync เฉพาะ qty_in, คง qty_out pgSQL
- `/tmp/diff_one_code.py CODE` = diff รายวัน Excel vs pgSQL (read-only)
- `/tmp/diff_names_full.py`, `/tmp/diff_before0306.py`, `/tmp/check_renamed.py` = เช็คเป็นชุดตามชื่อ
- การจับคู่: codekey (strip space/-/:'  upper) → fallback namekey (lower, normalize space)
- Excel: รหัสที่ A6/B10, ชื่อที่ B6, แถว movement row7+, col(1-based) 1=date 4=ยกมา 5=รับ 6=ออก 7=คงเหลือ

## ยังค้าง (TODO ครั้งหน้า)
- ยังเหลือสินค้าอีกหลายตัวที่ต่าง (รอบก่อนเจอ ~70 ตัว) ยังไม่ sync — user ทยอยสั่งทีละกลุ่ม
- 2 สินค้า missing (JHL5A-90G, JHD2-12G) รอตัดสินใจสร้าง/หรือเป็นรหัสที่เปลี่ยนไป
- "กระติกน้ำ JH (คละสี)" หาไม่เจอทั้ง Excel และ pgSQL
- JHL9-48G เคยดูเหมือนต่างทั้งปี แต่จริงๆ แก้ 2 แถวก็ตรง (ที่เหลือเป็นคาสเคด)
