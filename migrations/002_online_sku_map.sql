-- 002_online_sku_map.sql — ตารางจับคู่ SKU ออนไลน์ที่ Script-Ecom map ไม่ได้ (unmatched)
-- ให้ผู้ใช้กำหนดเองว่า SKU นี้ตัดเป็นรหัสสินค้าไหน × กี่ชิ้นต่อหน่วยขาย (เช่นเซ็ต SET_C4X3 = JHC4-35G ×3).
--
-- ความปลอดภัย: ตารางนี้เป็น "fallback" ที่ใช้เฉพาะรายการที่ upstream ตอบเป็น unmatched เท่านั้น
-- ยอดที่ตัดจาก mapping นี้จะถูกเขียนลง channel='online_manual' (แยกจาก 'online'/'mixed')
-- จึงไม่ทับ ไม่ชนกับยอดของสินค้าตัวอื่น. คำนวณใหม่แบบ derived ทุกครั้ง → idempotent.
--   scope='always'  -> ใช้ทุกวัน (จำถาวร)
--   scope='once'    -> ใช้เฉพาะ once_date (ตัดครั้งเดียว ไม่จำ)
CREATE TABLE IF NOT EXISTS stock_online.online_sku_map (
    id         SERIAL PRIMARY KEY,
    sku        TEXT UNIQUE NOT NULL,            -- SKU ต้นทาง (normalize เป็นตัวพิมพ์ใหญ่)
    code       TEXT NOT NULL,                   -- รหัสปลายทางใน stock_online.products
    multiplier NUMERIC NOT NULL DEFAULT 1,      -- จำนวนชิ้นที่ตัดต่อ 1 หน่วยขาย (เซ็ต = >1)
    scope      TEXT NOT NULL DEFAULT 'always',  -- 'always' | 'once'
    once_date  DATE,                            -- ใช้เมื่อ scope='once'
    note       TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
