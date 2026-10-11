"""
receipt_check.py — РЕАЛЬНАЯ проверка чека (OCR + правила акции).

Что делает этот файл:
  1. Берёт картинку чека (байты, которые прислал пользователь в Telegram).
  2. Распознавает текст на картинке — это называется OCR
     (Optical Character Recognition = «оптическое распознавание символов»).
  3. Проверяет распознанный текст по правилам акции:
       - на чеке есть название бренда VOLT;
       - есть дата покупки;
       - есть сумма;
       - фото достаточно чёткое (мы дополнительно считаем «резкость» картинки);
       - дата не из будущего и не слишком старая;
       - такой чек ещё никто не сдавал (защита от повторной отправки одного и того же фото).
  4. Возвращает объект CheckResult: ok=True — чек принят, ok=False — отклонён (+ причина).

Какой OCR используется (по порядку попыток):
  A) PaddleOCR (lang="latin") — лучше всего читает русские буквы на кассовых чеках.
     Ставится так:  pip install paddlepaddle paddleocr
  B) RapidOCR — лёгкая бесплатная альтернатива без тяжёлых зависимостей.
     Ставится так:  pip install rapidocr-onnxruntime
     Минус: русский он читает «транслитом» (МАГАЗИН -> MATA3NH), поэтому для
     русских слов мы используем «размывку» (fuzzy-поиск), см. _find().
  C) Tesseract (pytesseract + системный tesseract-ocr с rus+eng) — если стоит у тебя.
  D) Если ничего не установлено — бот честно скажет «не могу проверить»,
     а НЕ будет притворяться, что проверил. Это важно: заглушки больше нет.

ВНИМАНИЕ: первый запуск распознавателя может качать модели OCR в интернет
(несколько секунд/минут). Дальше всё работает локально и быстро.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache

log = logging.getLogger("volt.receipt")

# ---------------------------------------------------------------------------
# НАСТРОЙКИ АКЦИИ — меняй цифры под себя, код трогать не нужно
# ---------------------------------------------------------------------------

BRAND_KEYWORDS = ("volt", "вольт", "woht")   # как OCR мог прочесть слово VOLT
MIN_SUM = 1.0                                 # минимальная сумма чека (в рублях)
MAX_AGE_DAYS = 14                             # сколько дней чек считается «свежим»
ALLOW_FUTURE_DAYS = 1                         # допуск на неверные часы в кассе
BLUR_THRESHOLD = 60.0                         # порог резкости (варьянс лапласиана): меньше = мутно
CONF_MIN_LINE = 0.35                          # строки OCR с уверенностью ниже этой — выкидываем
OCR_MAX_SIDE = 1600                           # картинку больше этого размера уменьшаем (быстрее + точнее)

# Папка, куда сохраняются принятые чеки (для ручной проверки администрацией).
CHECKS_DIR = os.path.join(os.path.dirname(__file__), "accepted_checks")

# ---------------------------------------------------------------------------
# РЕЗУЛЬТАТ ПРОВЕРКИ
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Итог проверки одного чека."""

    ok: bool
    reason: str = ""                       # человекочитаемая причина отказа
    brand_found: bool = False
    sum_value: float | None = None
    date_value: date | None = None
    sharpness: float | None = None
    ocr_lines: list[str] = field(default_factory=list)
    engine: str = ""                       # каким OCR распознавали
    receipt_id: str = ""                   # «отпечаток» чека (дата+сумма+бренд)
    image_id: str = ""                     # file_unique_id фото из Telegram

    @property
    def summary(self) -> str:
        """Короткая сводка для сообщения пользователю / админу."""
        parts = []
        if self.date_value:
            parts.append(f"дата {self.date_value.strftime('%d.%m.%Y')}")
        if self.sum_value is not None:
            parts.append(f"сумма {self.sum_value:.2f} ₽")
        if self.brand_found:
            parts.append("VOLT найден")
        return ", ".join(parts) if parts else "без деталей"


# ---------------------------------------------------------------------------
# УТИЛИТЫ ТЕКСТА
# ---------------------------------------------------------------------------

# «похожие» замены: OCR часто путает такие символы
_LOOKALIKE = str.maketrans({
    "0": "o", "O": "o", "Q": "o", "D": "o",
    "1": "l", "I": "l", "|": "l", "!": "l", "7": "l",
    "5": "s", "S": "s", "$": "s",
    "8": "b", "B": "b",
    "3": "z", "Z": "z",
    "2": "r",
    "4": "a", "A": "a", "@": "a",
    "6": "g", "G": "g", "9": "g",
    "V": "v", "W": "w",
})

# транслит: RapidOCR читает русские буквы латиницей — превратим обратно
_TRANSLIT = {
    "a": "а", "b": "ь", "c": "с", "e": "е", "f": "ф", "g": "9", "h": "н",
    "i": "и", "j": "у", "k": "к", "l": "л", "m": "м", "n": "и", "o": "о",
    "p": "р", "q": "ч", "r": "г", "s": "s", "t": "т", "u": "v", "v": "в",
    "w": "ш", "x": "х", "y": "у", "z": "з", "{": "ж", "|": "ј", "}" : "э",
    "~": "ъ", "'": "ь", ";": "я", "<": "б", ">": "ю", "@": "@",
    "ch": "ч", "sh": "ш", "yu": "ю", "ya": "я", "zh": "ж",
}


def normalize(text: str) -> str:
    """Приводит строку к виду, в котором легко искать слова и цифры."""
    t = unicodedata.normalize("NFKC", text or "")
    t = t.lower()
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return t


def squeeze(text: str) -> str:
    """Оставляет только буквы/цифры (пробелы, точки, двоеточия — мусор для поиска слова)."""
    return re.sub(r"[^a-z0-9а-яё]", "", normalize(text))


def lookalike(text: str) -> str:
    """Заменяет визуально похожие символы, чтобы 'VOL7' стало 'volt', 'V0LT' -> 'volt'."""
    s = squeeze(text)
    return s.translate(_LOOKALIKE)


def translit_to_cyrillic(text: str) -> str:
    """RapidOCR читает кириллицу латиницей. Делаем обратное превращение на всякий случай."""
    s = normalize(text)
    out = []
    i = 0
    while i < len(s):
        two = s[i:i + 2]
        if two in _TRANSLIT and len(two) == 2:
            out.append(_TRANSLIT[two])
            i += 2
            continue
        ch = s[i]
        out.append(_TRANSLIT.get(ch, ch))
        i += 1
    return "".join(out)


def fuzzy_contains(word: str, haystack: str, max_dist: int = 1) -> bool:
    """Есть ли в тексте слово, отличающееся от word не более чем на max_dist символа."""
    if not word or not haystack:
        return False
    n = len(word)
    win = range(max(3, n - max_dist), n + max_dist + 1)
    for size in win:
        for start in range(0, len(haystack) - size + 1):
            cand = haystack[start:start + size]
            if _lev_le(cand, word, max_dist):
                return True
    return False


def _lev_le(a: str, b: str, limit: int) -> bool:
    """Быстрая проверка: расстояние Левенштейна между a и b <= limit?"""
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return False
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = cur[0]
        ai = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if cur[j] < best:
                best = cur[j]
        if best > limit:
            return False
        prev = cur
    return prev[lb] <= limit


# ---------------------------------------------------------------------------
# ПОИСК ДАТЫ И СУММЫ
# ---------------------------------------------------------------------------

_DATE_PAT = re.compile(
    r"(?:(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4}))"          # 11.10.2026 / 11/10/26
    r"|(?:(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2}))"           # 2026-10-11
    r"|((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}))",
    re.IGNORECASE,
)

_MONTHS_RU = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "июн": 6, "июл": 7,
    "авг": 8, "сен": 9, "сент": 9, "окт": 10, "ноя": 11, "дек": 12,
}
_MONTHS_EN = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_SUM_PAT = re.compile(
    r"(?:(?:\d{1,3}(?:[ ,.]?\d{3})*|\d+)(?:[.,]\d{1,2})?)",
)
_CURRENCY_HINT = re.compile(r"(руб|₽|рос|rossiya|rub|rur|byn|грн|тенге|₸|сум|UZS|KZT|UAH)", re.IGNORECASE)
_TOTAL_HINT = re.compile(r"(итог|itoг|total|sum|amount|nal|налич|к\s?оплате|оплачен|nano|cymm)", re.IGNORECASE)


def parse_date(text: str) -> date | None:
    """Вытаскивает первую осмысленную дату из текста."""
    low = normalize(text)
    for m in _DATE_PAT.finditer(low):
        d1, m1, y1, y2, m2, d2, mon_en, day_en = m.groups()
        try:
            if d1:
                d, mo, y = int(d1), int(m1), int(y1)
            elif y2:
                y, mo, d = int(y2), int(m2), int(d2)
            else:
                mo = _MONTHS_EN.get((mon_en or "")[:3])
                d, y = int(day_en), None
                if not mo:
                    continue
                yy = re.search(r"(20\d{2})", low[m.end(): m.end() + 12])
                y = int(yy.group(1)) if yy else date.today().year
            if y < 100:
                y += 2000
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return date(y, mo, d)
        except ValueError:
            continue
    # русские названия месяцев («октября»)
    for name, mo in _MONTHS_RU.items():
        if name in low:
            dd = re.search(r"(\d{1,2})\s*" + name, low) or re.search(name + r"\s*(\d{1,2})", low)
            yy = re.search(r"(20\d{2})", low)
            if dd:
                try:
                    return date(int(yy.group(1)) if yy else date.today().year, mo, int(dd.group(1)))
                except ValueError:
                    pass
    return None


_KOPECK_RE = re.compile(r"(\d+)\s*(?:руб|р\.|rub)\w*\s*[.,]?\s*(\d{1,2})\s*(?:коп|к\.|kop)\w*", re.IGNORECASE)


def numbers_in(line: str) -> list[float]:
    """Все денежные числа строки. Специально обрабатывает формат «89 руб. 90 коп.»."""
    vals = []
    norm = normalize(line)

    # особый формат русских чеков: «89 руб. 90 коп.» -> 89.90 (иначе получились бы 89 и 90 по отдельности)
    consumed_spans = []
    rest = norm
    offset = 0
    while True:
        m = _KOPECK_RE.search(rest)
        if not m:
            break
        vals.append(int(m.group(1)) + int(m.group(2)) / 100.0)
        consumed_spans.append((offset + m.start(), offset + m.end()))
        offset += m.end()
        rest = rest[m.end():]

    for mm in _SUM_PAT.finditer(norm):
        if any(mm.start() >= a and mm.end() <= b for a, b in consumed_spans):
            continue   # это число уже вошло в «руб. коп.» — не считаем его отдельно
        s = mm.group(0).replace(" ", "").replace("\u00a0", "")
        # 1 234,56 / 1.234,56 / 1234.56
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        elif re.fullmatch(r"\d{1,3}([ .]\d{3})+", s):
            s = s.replace(" ", "").replace(".", "")
        try:
            v = float(s)
        except ValueError:
            continue
        if 0 < v < 1_000_000:
            vals.append(v)
    return vals


def parse_sum(text: str) -> float | None:
    """Вытаскивает сумму. Приоритет — строки со словом «итого/total» или с валютой."""
    lines = [ln for ln in text.splitlines() if ln.strip()]

    candidates: list[float] = []
    for line in lines:
        nums = numbers_in(line)
        if not nums:
            continue
        norm = normalize(line)
        if _TOTAL_HINT.search(norm) or _CURRENCY_HINT.search(norm):
            candidates.extend(nums)
    if candidates:
        return max(candidates)

    all_nums: list[float] = []
    for line in lines:
        all_nums.extend(numbers_in(line))
    if not all_nums:
        return None
    # сумма чека обычно самая крупная из чисел (даты уже отфильтрованы длиной > 4 цифр с точкой)
    money = [v for v in all_nums if v >= MIN_SUM]
    return max(money) if money else max(all_nums)


# ---------------------------------------------------------------------------
# РЕЗКОСТЬ ФОТО (защита от «мутных» снимков)
# ---------------------------------------------------------------------------


def sharpness_score(image_bytes: bytes) -> float | None:
    """Варьянс фильтра Лапласа: чем больше — тем резче картинка. None, если Pillow нет."""
    try:
        import io

        from PIL import Image, ImageOps
        import numpy as np
    except Exception:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        img = ImageOps.exif_transpose(img)
        w, h = img.size
        scale = max(w, h) / 800.0
        if scale > 1:
            img = img.resize((int(w / scale), int(h / scale)))
        arr = np.asarray(img, dtype=np.float64)
        lap = (
            arr[:-2, 1:-1] + arr[2:, 1:-1] + arr[1:-1, :-2] + arr[1:-1, 2:]
            - 4 * arr[1:-1, 1:-1]
        )
        return float(lap.var())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# OCR-ДВИЖКИ
# ---------------------------------------------------------------------------

_engine_lock = threading.Lock()
_current_engine: str | None = None


@lru_cache(maxsize=1)
def _get_paddle():
    try:
        from paddleocr import PaddleOCR
    except Exception:
        return None
    for langs in (("latin",), ("en",), ("ch",), (None,)):
        try:
            kwargs = dict(use_angle_cls=True, show_log=False, use_gpu=False)
            if langs[0]:
                kwargs["lang"] = langs[0]
            return PaddleOCR(**kwargs)
        except Exception as e:
            log.debug("PaddleOCR(%s) не поднялась: %s", langs, e)
    return None


@lru_cache(maxsize=1)
def _get_rapid():
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        return None
    try:
        return RapidOCR()
    except Exception as e:
        log.debug("RapidOCR не поднялась: %s", e)
    return None


@lru_cache(maxsize=1)
def _get_tesseract():
    try:
        import pytesseract
        from PIL import Image  # noqa: F401
    except Exception:
        return None
    exe = pytesseract.pytesseract.tesseract_cmd
    if not (shutil.which(exe) or os.path.exists(exe)):
        return None
    langs = []
    try:
        avail = set(pytesseract.get_languages(config="").split())
        for want in ("rus", "eng", "rus+eng"):
            parts = want.split("+")
            if all(p in avail for p in parts):
                langs.append(want)
    except Exception:
        langs = ["eng"]
    return pytesseract, (langs[-1] if langs else "eng")


def available_engines() -> list[str]:
    out = []
    if _get_paddle() is not None:
        out.append("paddleocr")
    if _get_rapid() is not None:
        out.append("rapidocr")
    if _get_tesseract() is not None:
        out.append("tesseract")
    return out


def _prep_image(image_bytes: bytes):
    """Возвращает (numpy-картинка BGR, путь во временном файле) — то, что любят OCR-библиотеки."""
    import io
    import tempfile

    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > OCR_MAX_SIDE:
        k = OCR_MAX_SIDE / max(w, h)
        img = img.resize((max(1, int(w * k)), max(1, int(h * k))))
    # лёгкий контраст — кассовая лента часто бледная
    try:
        from PIL import ImageEnhance
        img = ImageEnhance.Contrast(img).enhance(1.35)
    except Exception:
        pass
    import numpy as np
    arr = np.asarray(img)[:, :, ::-1]  # RGB -> BGR
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    img.save(tmp.name, format="PNG")
    tmp.close()
    return arr, tmp.name


def ocr_lines(image_bytes: bytes) -> tuple[list[tuple[str, float]], str]:
    """Распознаёт текст. Возвращает ([(строка, уверенность)], имя движка)."""
    global _current_engine
    arr, path = _prep_image(image_bytes)
    try:
        with _engine_lock:
            eng = _current_engine

            if eng in (None, "paddleocr"):
                pd = _get_paddle()
                if pd is not None:
                    try:
                        raw = pd.ocr(path, cls=True)
                        lines = _parse_paddle(raw)
                        if lines:
                            _current_engine = "paddleocr"
                            return lines, "paddleocr"
                    except Exception as e:
                        log.warning("PaddleOCR упала: %s", e)

            if eng in (None, "rapidocr"):
                rp = _get_rapid()
                if rp is not None:
                    try:
                        out = rp(arr)
                        # RapidOCR возвращает (результат, время). Результат — список
                        # [бокс, текст, уверенность]. Берём только тексты с оценками.
                        res = out[0] if isinstance(out, tuple) and out else out
                        lines: list[tuple[str, float]] = []
                        for item in (res or []):
                            try:
                                if isinstance(item, (list, tuple)) and len(item) >= 3:
                                    lines.append((str(item[1]), float(item[2])))
                                elif isinstance(item, (list, tuple)) and len(item) == 2:
                                    lines.append((str(item[0]), float(item[1])))
                            except Exception:
                                continue
                        if lines:
                            _current_engine = "rapidocr"
                            return lines, "rapidocr"
                    except Exception as e:
                        log.warning("RapidOCR упала: %s", e)

            if eng in (None, "tesseract"):
                tess = _get_tesseract()
                if tess is not None:
                    pyt, lang = tess
                    try:
                        import io
                        from PIL import Image
                        data = pyt.image_to_data(Image.open(io.BytesIO(image_bytes)), lang=lang,
                                                 output_type=pyt.OutputType.DICT)
                        lines = []
                        for txt, conf in zip(data["text"], data["conf"]):
                            txt = (txt or "").strip()
                            if not txt:
                                continue
                            try:
                                c = float(conf) / 100.0
                            except Exception:
                                c = 0.5
                            lines.append((txt, max(0.0, min(1.0, c))))
                        if lines:
                            _current_engine = "tesseract"
                            return lines, "tesseract"
                    except Exception as e:
                        log.warning("Tesseract упал: %s", e)
        raise RuntimeError("no_engine")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _parse_paddle(raw) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    if not raw:
        return out
    pages = raw if isinstance(raw, list) else [raw]
    for page in pages:
        items = page if isinstance(page, list) else [page]
        for it in items:
            try:
                if isinstance(it, (list, tuple)) and len(it) >= 2:
                    payload = it[1]
                    if isinstance(payload, (list, tuple)) and len(payload) >= 2:
                        out.append((str(payload[0]), float(payload[1])))
            except Exception:
                continue
    return out


# ---------------------------------------------------------------------------
# ЖУРНАЛ ПРИНЯТЫХ ЧЕКОВ (один чек = одна выплата)
# ---------------------------------------------------------------------------

_LEDGER_NAME = "accepted_checks.txt"


def _ledger_path() -> str:
    return os.path.join(CHECKS_DIR, _LEDGER_NAME)


_ledger_lock = threading.Lock()


def ledger_lookup(key: str) -> tuple[bool, str]:
    """(найден?, кто сдал)."""
    p = _ledger_path()
    if not os.path.exists(p):
        return False, ""
    with _ledger_lock:
        with open(p, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("|")
                if len(parts) >= 2 and parts[0] == key:
                    return True, parts[1] if len(parts) > 1 else ""
    return False, ""


def ledger_add(key: str, user_id: int, extra: str = "") -> None:
    os.makedirs(CHECKS_DIR, exist_ok=True)
    with _ledger_lock:
        with open(_ledger_path(), "a", encoding="utf-8") as f:
            f.write(f"{key}|{user_id}|{extra}\n")


def make_receipt_id(res: CheckResult) -> str:
    """Отпечаток чека: дата + сумма + бренд. Плюс отдельная защита по file_unique_id."""
    d = res.date_value.strftime("%Y%m%d") if res.date_value else "nodate"
    s = f"{res.sum_value:.2f}" if res.sum_value is not None else "nosum"
    base = f"{d}_{s}_VOLT"
    return hashlib.sha1(base.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# ГЛАВНАЯ ФУНКЦИЯ
# ---------------------------------------------------------------------------


def check_receipt(image_bytes: bytes, user_id: int, telegram_file_id: str = "") -> CheckResult:
    """Полная реальная проверка чека. Ничего не «придумывает»: если OCR недоступен — честный отказ."""
    if not image_bytes:
        return CheckResult(False, "Пустое изображение.")

    # Сначала оцениваем резкость. Мутное фото отсекаем сразу: и OCR по нему
    # обычно ничего не читает, и тратьть время на распознавание незачем.
    sharp = sharpness_score(image_bytes)
    if sharp is not None and sharp < BLUR_THRESHOLD:
        return CheckResult(
            False,
            "Фото слишком мутное — я не смог разобрать ни одной буквы.\n"
            "Пересними чек крупно, ровно и при хорошем освещении.",
            sharpness=sharp,
            engine="skipped (blur)",
        )

    try:
        lines, engine = ocr_lines(image_bytes)
    except RuntimeError:
        return CheckResult(
            False,
            "На сервере не установлен модуль распознавания текста (OCR). "
            "Проверка невозможна — напиши администратору.",
            engine="none",
        )
    except Exception as e:
        log.exception("OCR crash")
        return CheckResult(False, f"Ошибка распознавания текста: {type(e).__name__}", engine="error")

    good = [(t, c) for t, c in lines if c >= CONF_MIN_LINE and t.strip()]
    if not good:
        good = lines
    text_all = "\n".join(t for t, _ in good)
    low_all = normalize(text_all)
    sq_all = squeeze(text_all)
    tr_all = squeeze(translit_to_cyrillic(text_all))
    lk_all = lookalike(text_all)

    brand = False
    for kw in BRAND_KEYWORDS:
        k_squeezed = squeeze(kw)
        if k_squeezed and (k_squeezed in sq_all or k_squeezed in tr_all):
            brand = True
            break
        if len(k_squeezed) >= 4 and (fuzzy_contains(k_squeezed, lk_all, 1)
                                     or fuzzy_contains(k_squeezed, sq_all, 1)
                                     or fuzzy_contains(k_squeezed, tr_all, 1)):
            brand = True
            break

    dt = parse_date(text_all)
    sm = parse_sum(text_all)
    sharp = sharpness_score(image_bytes)

    res = CheckResult(
        ok=False,
        brand_found=brand,
        date_value=dt,
        sum_value=sm,
        sharpness=sharp,
        ocr_lines=[t for t, _ in good],
        engine=engine,
    )

    # --- вердикт по правилам ---
    problems: list[str] = []

    if not brand:
        problems.append("не нашёл на чеке название «VOLT»")
    if dt is None:
        problems.append("не смог прочитать дату покупки")
    if sm is None:
        problems.append("не смог прочитать сумму")
    elif sm < MIN_SUM:
        problems.append(f"сумма чека слишком маленькая ({sm:.2f} ₽)")

    # резкость проверяем ПОСЛЕ текстовых условий: если OCR вообще ничего не увидел,
    # причина почти всегда в мутном фото — об этом и скажем.
    if sharp is not None and sharp < BLUR_THRESHOLD:
        if problems:
            problems = ["фото слишком мутное — OCR не смог ничего прочитать. Пересними чек крупнее и при хорошем свете."]
        else:
            problems.append("фото слишком мутное — пересними чек крупнее и резче")

    today = date.today()
    if dt is not None:
        if dt > today + timedelta(days=ALLOW_FUTURE_DAYS):
            problems.append(f"дата чека из будущего ({dt.strftime('%d.%m.%Y')})")
        elif (today - dt).days > MAX_AGE_DAYS:
            problems.append(f"чеку уже {(today - dt).days} дней — срок акции вышел (принимаются не старше {MAX_AGE_DAYS} дней)")

    if problems:
        res.reason = "Не вижу на чеке: " + "; ".join(problems) + ".\nПопробуй переснять чек при хорошем свете."
        return res

    res.receipt_id = make_receipt_id(res)
    res.image_id = telegram_file_id

    # дубликаты: сначала по конкретному фото, потом по «отпечатку» (дата+сумма)
    found_img, owner_img = ledger_lookup("IMG_" + res.image_id) if res.image_id else (False, "")
    found_sig, owner_sig = ledger_lookup(res.receipt_id)
    if found_img or found_sig:
        res.ok = False
        res.reason = (
            "⚠️ Такой чек уже был принят ранее. Один чек = одна выплата.\n"
            "Если это твоя первая отправка этого фото — напиши администратору."
        )
        return res

    # чек проходит все проверки — регистрируем его и сохраняем фото
    ledger_add(res.receipt_id, user_id, f"date={dt} sum={sm}")
    if res.image_id:
        ledger_add("IMG_" + res.image_id, user_id, f"sig={res.receipt_id}")
    try:
        os.makedirs(CHECKS_DIR, exist_ok=True)
        stamp = f"{user_id}_{dt.strftime('%Y%m%d')}_{sm:.2f}".replace(".", "_")
        with open(os.path.join(CHECKS_DIR, f"{stamp}.jpg"), "wb") as f:
            f.write(image_bytes)
    except OSError:
        log.warning("не удалось сохранить копию чека в %s", CHECKS_DIR)

    res.ok = True
    return res
