"""Быстрые проверки логики receipt_check.py (без Telegram).

Запуск:  python3 test_receipt_check.py
"""
import datetime as dt
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from PIL import Image, ImageDraw, ImageFont

import receipt_check as rc

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def make_receipt(lines, size=None, blur=False):
    if size is None:
        size = (760, 90 + 60 * len(lines))
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype(FONT, 30)
    except Exception:
        f = ImageFont.load_default()
    y = 20
    for ln in lines:
        d.text((30, y), ln, fill="black", font=f)
        y += 60
    if blur:
        from PIL import ImageFilter
        img = img.filter(ImageFilter.GaussianBlur(6))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


ok = True


def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        ok = False


# --- парсинг даты / суммы ---
today = dt.date.today()
check("date dd.mm.yyyy", rc.parse_date("ДАТА: 11.10.2026 14:32") == dt.date(2026, 10, 11))
check("date iso", rc.parse_date("DATE: 2026-10-11 14:32") == dt.date(2026, 10, 11))
check("date short year", rc.parse_date("11/10/26") == dt.date(2026, 10, 11))
check("sum itogo", rc.parse_sum("ИТОГО: 89.90") == 89.90)
check("sum rubles", abs(rc.parse_sum("89 руб. 90 коп.") - 89.90) < 0.01)
check("sum thousands", rc.parse_sum("TOTAL 1 250,00 RUB") == 1250.0)
check("sum picks max on total line", rc.parse_sum("VOLT 0.5L\n2 шт.\nИТОГО: 179.80") == 179.80)

# --- поиск бренда через шум OCR ---
check("brand exact", rc.squeeze("VOLTMARKET").find("volt") >= 0)
text_ru = "MATA3NHnATEPOYKA\nHEK N:000123\nAATA: 11.10.2026 14:32\nOHEPTETNKVOLTO.5\n89 py6. 90 kon.\nMT0FO: 89.90"
sq = rc.squeeze(text_ru)
tr = rc.squeeze(rc.translit_to_cyrillic(text_ru))
lk = rc.lookalike(text_ru)
found = any(s in sq or s in tr or rc.fuzzy_contains(s, lk, 1) or rc.fuzzy_contains(s, sq, 1)
            for s in [rc.squeeze(k) for k in rc.BRAND_KEYWORDS])
check("brand found in translit OCR text", found)
noise = "МАГАЗИН РОСТОВЕР\nЧЕК 55\n10.10.2026\nИТОГО: 250.00"
sq2, tr2, lk2 = rc.squeeze(noise), rc.squeeze(rc.translit_to_cyrillic(noise)), rc.lookalike(noise)
found2 = any(s in sq2 or s in tr2 or rc.fuzzy_contains(s, lk2, 1) or rc.fuzzy_contains(s, sq2, 1)
             for s in [rc.squeeze(k) for k in rc.BRAND_KEYWORDS])
check("brand NOT found in random store receipt", not found2)

# --- резкость ---
sharp = rc.sharpness_score(make_receipt(["VOLT MARKET", "TOTAL 89.99"]))
blurred = rc.sharpness_score(make_receipt(["VOLT MARKET", "TOTAL 89.99"], blur=True))
print(f"   sharpness: normal={sharp} blurred={blurred}")
check("blur detection works", sharp is not None and blurred is not None and sharp > blurred)

# --- сквозная проверка с реальным OCR (если движок установлен) ---
engines = rc.available_engines()
print("   доступные OCR-движки:", engines or "нет")
if engines:
    tmpdir = tempfile.mkdtemp()
    old_dir = rc.CHECKS_DIR
    rc.CHECKS_DIR = os.path.join(tmpdir, "checks")
    try:
        fresh = make_receipt([
            "VOLT MARKET",
            f"DATE: {today.strftime('%d.%m.%Y')} 14:32",
            "ENERGETIC VOLT 0.5L",
            "PRICE 89.99 RUB",
            "TOTAL: 89.99",
        ])
        r1 = rc.check_receipt(fresh, user_id=111, telegram_file_id="uniq_1")
        print("   verdict:", r1.ok, "|", r1.reason or r1.summary, "| engine:", r1.engine)
        check("valid receipt accepted", r1.ok)
        check("ocr read the date", r1.date_value == today)
        check("ocr read the sum", r1.sum_value is not None and r1.sum_value > 0)
        check("brand detected", r1.brand_found)

        r2 = rc.check_receipt(fresh, user_id=222, telegram_file_id="uniq_1")
        print("   repeat verdict:", r2.ok, "|", r2.reason)
        check("duplicate photo rejected", (not r2.ok) and "уже был принят" in r2.reason)

        r3 = rc.check_receipt(fresh, user_id=333, telegram_file_id="uniq_2")
        check("same receipt by other user rejected", not r3.ok)

        old = make_receipt([
            "VOLT MARKET", "DATE: 01.01.2020 09:00", "ENERGETIC VOLT 0.5L", "TOTAL: 89.99",
        ])
        r4 = rc.check_receipt(old, user_id=444, telegram_file_id="uniq_4")
        print("   old receipt:", r4.ok, "|", r4.reason)
        check("expired receipt rejected", not r4.ok and "срок акции" in r4.reason)

        future = make_receipt([
            "VOLT MARKET", "DATE: 31.12.2030 09:00", "ENERGETIC VOLT 0.5L", "TOTAL: 89.99",
        ])
        r5 = rc.check_receipt(future, user_id=555, telegram_file_id="uniq_5")
        check("future date rejected", not r5.ok and "будущего" in r5.reason)

        noBrand = make_receipt(["PYATEROCHKA MARKET", f"DATE: {today.strftime('%d.%m.%Y')}", "TOTAL: 250.00"])
        r6 = rc.check_receipt(noBrand, user_id=666, telegram_file_id="uniq_6")
        print("   no brand:", r6.ok, "|", r6.reason)
        check("receipt without VOLT rejected", not r6.ok and "VOLT" in r6.reason)

        blurry = make_receipt(["VOLT MARKET", f"DATE: {today.strftime('%d.%m.%Y')}", "TOTAL: 89.99"], blur=True)
        r7 = rc.check_receipt(blurry, user_id=777, telegram_file_id="uniq_7")
        print("   blurry:", r7.ok, "|", r7.reason)
        check("blurry photo rejected", not r7.ok)
    finally:
        rc.CHECKS_DIR = old_dir
        shutil.rmtree(tmpdir, ignore_errors=True)
else:
    print("   OCR-движки не установлены — сквозной тест пропущен")

print("\nRESULT:", "ALL PASS" if ok else "THERE WERE FAILURES")
sys.exit(0 if ok else 1)
