"""Brand classification by product-code prefix — single source of truth.

Used by the Flask backend (new-product insert + API output) and the ETL
(`scripts/setup_database.py`, `scripts/set_brands.py`) so the brand stored in
PostgreSQL always matches what the frontend filters on.
"""
import re

# The four real brands (order = how they appear in the UI dropdown).
BRANDS = ["Jula's Herb", "Jdent", "Beauterry", "JNIS"]
# Premium / giveaway merchandise (category PM) — its own filter group.
PREMIUM_BRAND = "สินค้าพรีเมี่ยม"
# Everything else (boxes, misc merchandise).
OTHER_BRAND = "อื่นๆ"


def classify_brand(code: str, name: str = "", category: str = "") -> str:
    c = (code or "").strip().upper()
    n = name or ""
    # Name wins for Jdent: every "เจเด้นท์/เจเด้น" product is Jdent, even when its
    # code is coded under the JH (Jula's Herb) prefix, e.g. JHD1/JHD2.
    if "เจเด้น" in n:
        return "Jdent"
    # Category PM = premium merchandise (PM0005, ร่มแตงโม, Photo card, ...).
    # Any PM-prefixed code is premium too, even if its sheet landed in another
    # category (e.g. เสื้อปุ๋ย=PM0060, กระเป๋านุ่มนิ่ม=PM0066 came in as OTHER).
    if (category or "").strip().upper() == "PM" or c.startswith("PM"):
        return PREMIUM_BRAND
    if c.startswith("JH"):
        return "Jula's Herb"
    if re.match(r"^[LCSR]\d", c):          # L/C/S/R lines: sunflower, melon, soap, retinol
        return "Jula's Herb"
    if re.match(r"^D\d", c):               # D# = Jdent toothpaste
        return "Jdent"
    if c.startswith("BTA"):
        return "Beauterry"
    if c.startswith("JN"):
        return "JNIS"
    return OTHER_BRAND
