"""Central configuration: loads .env and defines paths + DB settings."""
import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = directory containing this file
ROOT = Path(__file__).resolve().parent

# Load environment variables from .env (if present)
load_dotenv(ROOT / ".env")

# ---- Paths --------------------------------------------------------------
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
# PROCESSED_DIR เก็บ runtime state (ยอดยืนยันสะสมรายวัน + สถานะปริ้น) ที่
# "ต้องอยู่รอด deploy". deploy.ps1 ทำ robocopy /PURGE บนโฟลเดอร์โปรเจกต์ →
# ถ้าเก็บใน ROOT/data จะถูกลบทิ้งทุก deploy. จึง default ไปโฟลเดอร์ "พี่น้อง"
# นอก ROOT (เช่น Production/stock-movement-data/processed) ที่ robocopy ไม่แตะ.
# override ผ่าน env PROCESSED_DIR ได้ถ้าต้องการกำหนดเอง.
PROCESSED_DIR = Path(os.getenv("PROCESSED_DIR") or (ROOT.parent / f"{ROOT.name}-data" / "processed"))
FRONTEND_DIR = ROOT / "frontend"

EXCEL_PATH = RAW_DIR / os.getenv("EXCEL_FILE", "Stock Online_JLC GROUP 2026.xlsx")
JSON_PATH = PROCESSED_DIR / "stock_data.json"
COMPACT_JSON_PATH = PROCESSED_DIR / "stock_data_compact.json"

# ---- Database -----------------------------------------------------------
DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "5432")),
    dbname=os.getenv("DB_NAME", "postgres"),
    user=os.getenv("DB_USER", "postgres"),
    password=os.getenv("DB_PASSWORD", ""),
)
DB_SCHEMA = os.getenv("DB_SCHEMA", "stock_online")

# ---- Server -------------------------------------------------------------
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

# ---- Admin --------------------------------------------------------------
# PIN required to enter admin mode (write operations). Empty = admin disabled.
ADMIN_PIN = os.getenv("ADMIN_PIN", "")
