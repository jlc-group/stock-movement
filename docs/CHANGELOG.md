---
title: Changelog
created: 2026-05-29
tags: [changelog]
---

# Changelog

ทุก change สำคัญของแอป — รวบยอด เรียงจากใหม่ → เก่า

รูปแบบ: [Keep a Changelog](https://keepachangelog.com/) + [SemVer](https://semver.org/)

---

## [Unreleased]

(งานที่ยังไม่ release / ยังทดสอบอยู่)

### 2026-06-08 — ยังไม่ commit (working tree)

#### Added
- **Export Excel รายการเคลื่อนไหว** (หน้ารายการสินค้า) — ปุ่ม "⬇️ Export Excel" ใน toolbar
  → `POST /api/movements/export` คืนไฟล์ `.xlsx` รายการรับเข้า/จ่ายออกแบบ flat
  ตาม filter ทั้งหมด (แบรนด์/หมวด/ค้นหา/ช่วงวันที่); กรองแถว 0/0 ออก, มีแถวรวมท้ายตาราง

#### Changed
- เปลี่ยนคำว่า "จ่าย" (ที่ยืนเดี่ยว) → "ออก" 3 จุด: badge, sort dropdown, dashboard tooltip
  (คำประสม "จ่ายออก"/"รับ-จ่าย" คงไว้)
- `todayMoveBadges()` ปรับให้ badge ▼ รับ / ▲ ออก อิงช่วงวันที่ที่ filter (เดิมดูแค่วันนี้)

#### Fixed
- badge `▼ รับ` ไม่ขึ้นเมื่อดูยอดย้อนหลัง (badge ผูกวันนี้ แต่ตารางเป็นยอดรวมตามช่วง → ไม่ตรงกัน)

ดูรายละเอียดที่ [[work-log/2026-06-08-movement-export-and-badges]]

---

## [0.3.0] — 2026-05-29

### Added
- **Admin mode** (PIN-based, `ADMIN_PIN` ใน `.env`)
  - ปุ่ม 🔑 Admin + login modal → token เก็บใน sessionStorage
  - **เพิ่มสินค้าใหม่** (รหัส, หมวด, ชื่อ, ยอดยกมา)
  - **บันทึกรับ-จ่ายรายวัน** พร้อม **search + autocomplete** (ไฮไลต์คำ, โชว์คงเหลือ, keyboard nav) + preview คงเหลือใหม่สดๆ
  - วันเดียวกันบวกเพิ่ม / คนละวันสร้างแถวใหม่ + คำนวณคงเหลือใหม่อัตโนมัติ
  - toast แจ้งผลสำเร็จ/ผิดพลาด
- Backend write API: `POST /api/admin/login`, `/api/admin/logout`, `/api/products`, `/api/movements`
- helper `recompute_product()` (running balance + aggregate) และ `fetch_product()`
- `/api/health` เพิ่มฟิลด์ `admin_enabled`
- `.claude/launch.json` (dev preview config)

ดูรายละเอียดที่ [[work-log/2026-05-29-admin-mode]]

---

## [0.2.0] — 2026-05-29

### Added
- **Date range filter** ในหน้าหลัก — เลือกช่วงวันที่เพื่อดู in/out/closing ของช่วงนั้น
- **Sticky table header (`<thead>`)** — ตรึงหัวคอลัมน์ตอน scroll
- **Header click sort** ที่คอลัมน์ "จ่าย" — คลิกครั้งแรก desc, คลิกอีกครั้ง asc, คลิกอีกครั้ง default
- Sort options เพิ่ม: `รับเข้าน้อย→มาก`, `จ่ายน้อย→มาก`, `⚠️ คงเหลือติดลบก่อน`
- Modal summary คำนวณ **dynamic ตามเดือนที่เลือก** (opening = balance ก่อนเดือนนี้, closing = balance สุดท้ายในเดือน)
- KPI ด้านบน **dynamic ตาม filter** + แสดง `<count> / <total>` ถ้ามี filter
- `docs/` (Obsidian vault) — เอกสารโครงสร้าง, work-log, changelog

### Changed
- **Top bar ตรึงตาย** — ลบ scroll shrink animation
- Table `border-collapse: collapse` → `separate` (เพื่อให้ sticky `<th>` ทำงาน)
- ใช้ `pStats(p)` wrapper สำหรับเลือก totals/range stats — ลด code duplication ใน render

ดูรายละเอียดที่ [[work-log/2026-05-29-frontend-ux-improvements]]

---

## [0.1.0] — 2026-05-29

### Added
- **Full-stack restructure** จาก flat directory
  - `config.py` ศูนย์กลาง config โหลด `.env`
  - `server/app.py` Flask backend
  - `scripts/` รวม ETL/audit
  - `frontend/` รวม HTML
  - `data/raw/` + `data/processed/`
- `.env` + `.env.example` — DB credentials/server config
- `.gitignore` — ignore secrets + generated data
- `requirements.txt`
- `README.md`
- API endpoints
  - `GET /` — frontend
  - `GET /api/health` — DB connectivity
  - `GET /api/products` — products + tx จาก PostgreSQL

### Changed
- Frontend อ่านข้อมูลผ่าน `fetch('/api/products')` แทน embed JSON
- Scripts ทั้งหมดใช้ `import config` แทน hardcode DB credentials
- `full_audit.py` อ่าน JSON compact แทน parse HTML
- HTML ขนาด: 1.7 MB → 17 KB (ลบ embed JSON)

### Removed
- Hardcoded DB credentials ในไฟล์ Python
- Embedded `<script id="data" type="application/json">` ใน HTML

ดูรายละเอียดที่ [[work-log/2026-05-29-fullstack-restructure]]

---

## Convention

- **[Major.Minor.Patch]** — Major: breaking, Minor: feature, Patch: bug
- **Added** — feature ใหม่
- **Changed** — เปลี่ยน behavior ของของเดิม
- **Deprecated** — กำลังจะเลิกใช้
- **Removed** — เอาออกแล้ว
- **Fixed** — bug fix
- **Security** — security patch
