---
date: 2026-06-08
title: Export Excel รายการเคลื่อนไหว + badge ตามช่วงวันที่ + เปลี่ยน "จ่าย"→"ออก"
status: done
tags: [work-log, feature, frontend, backend, excel]
---

# Export Excel รายการเคลื่อนไหว + badge ตามช่วงวันที่ + เปลี่ยน "จ่าย"→"ออก"

> ⚠️ ยัง **UNCOMMITTED** (อยู่ใน working tree เท่านั้น ยังไม่ push ขึ้น git)
> ไฟล์ที่แก้: `frontend/index.html`, `server/app.py`

## เป้าหมาย (Goal)

1. เพิ่มปุ่ม **Export Excel** ในหน้ารายการสินค้า ให้ดึงรายการรับเข้า/จ่ายออก
   เป็นไฟล์ Excel ตาม filter ที่เลือก (โดยเฉพาะช่วงวันที่)
2. เปลี่ยนคำว่า "จ่าย" ที่ยืนเดี่ยวๆ เป็น "ออก" ให้สอดคล้องกับหัวตาราง
3. แก้ปัญหา: badge `▼ รับ` ไม่ขึ้นเมื่อดูยอดย้อนหลัง (badge ดูแค่วันนี้
   แต่ตารางแสดงยอดรวมตามช่วงวันที่ → ไม่ตรงกัน)

## สิ่งที่เปลี่ยน (Changes)

- `server/app.py` — เพิ่ม endpoint `POST /api/movements/export` (public, ไม่ต้อง admin)
  - รับ body `{codes:[...], from, to}` → คืนไฟล์ `.xlsx` (รายการเคลื่อนไหวแบบ flat)
  - กรอง `p.code = ANY(codes)` และ `(qty_in <> 0 OR qty_out <> 0)` (ตัดแถว 0/0 ออก)
    + ช่วงวันที่ (ถ้ามี); เรียงตาม วันที่ → รหัส → id
  - คอลัมน์: วันที่ | รหัสสินค้า | ชื่อสินค้า | แบรนด์ | หมวด | รับ | ออก | คงเหลือ |
    เลขที่เอกสาร | หมายเหตุ + แถว **รวม** (รวมรับ/รวมออก) ท้ายตาราง
  - validate วันที่รูปแบบ ISO (400) และ codes ว่าง (400); ชื่อไฟล์ `movements_<from>_<to>.xlsx`
- `frontend/index.html`
  - ปุ่มสีเขียว `#exportListBtn` "⬇️ Export Excel" ใน toolbar (ข้างช่วงวันที่)
  - ฟังก์ชัน `exportMovements()` — ส่ง `filtered` codes + `dateRange` ผ่าน fetch POST,
    ดาวน์โหลด blob, มี loading state + alert เมื่อ error
  - เปลี่ยน "จ่าย" → "ออก" 3 จุด: badge `▲ ออก` (+tooltip), sort options
    (out_desc/out_asc), tooltip ตาราง Dashboard
  - `todayMoveBadges(p)` ปรับให้ **range-aware**: ถ้า filter ช่วงวันที่ →
    badge ▼ รับ / ▲ ออก ใช้ยอดรวมในช่วง (`p._range` / `computeRangeStats`)
    ให้ตรงกับคอลัมน์ในตาราง; ถ้าไม่ filter → คงเดิม (ดูเฉพาะวันนี้)

## เหตุผล / การตัดสินใจ (Decisions)

- **POST + blob download** แทน GET เพราะ codes ที่ผ่าน filter อาจมี 100+ รหัส (URL ยาวเกิน)
- **กรองแถว 0/0 ออก** — รายงานชื่อ "การเคลื่อนไหว" จึงเอาเฉพาะวันที่มีรับหรือออกจริง
- เปลี่ยนเฉพาะ "จ่าย" ที่ยืนเดี่ยว — คำประสม "จ่ายออก" / "รับ-จ่าย" **คงไว้**
  เพราะอ่านเป็นชื่อฟิลด์อยู่แล้ว
- badge เดิม hardcode วันนี้ ทำให้ไม่ตรงกับตารางที่เป็น range → ให้ badge อิง range เดียวกัน

## ทดสอบ (Testing)

- `POST /api/movements/export` — มีช่วงวันที่ (200), ไม่มีช่วงวันที่ (200), codes ว่าง (400)
- ตรวจไฟล์ xlsx: header, แถวข้อมูล, แถวรวม, การกรอง 0/0 ถูกต้อง
- badge: ช่วง 1–8 มิ.ย. JH905-70G (รับ 2,400 เกิดวันก่อนหน้า) เดิมไม่มี badge รับ →
  ตอนนี้แสดง `▼ รับ`; กรณีไม่ filter ยังแสดงเฉพาะ movement วันนี้
- console ไม่มี error

## ขั้นถัดไป (Next)

- รอคำสั่ง user เพื่อ `git push` (ยังไม่ commit)

## Related

- [[CHANGELOG]]
