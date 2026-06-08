-- 001_add_channel.sql — เพิ่มมิติ "channel" ให้ stock_movements
-- ทดสอบแล้วบนสำเนา stock_online_test (51,098 แถว) เมื่อ 2026-06-08:
--   ข้อมูลเก่าทั้งหมด -> channel='mixed' (ยอดรวมแบบเดิม ไม่หาย)
--   เปลี่ยนกฎ UNIQUE เป็น (product_id, movement_date, channel) -> ใส่ online ซ้อนวันเดิมได้
-- รันบน prod (schema stock_online) ตอน cutover เท่านั้น (หลัง backup + ช่วง downtime)
ALTER TABLE stock_online.stock_movements ADD COLUMN IF NOT EXISTS channel TEXT NOT NULL DEFAULT 'mixed';
ALTER TABLE stock_online.stock_movements DROP CONSTRAINT IF EXISTS stock_movements_product_id_movement_date_key;
ALTER TABLE stock_online.stock_movements ADD CONSTRAINT stock_movements_pdc_key UNIQUE (product_id, movement_date, channel);
