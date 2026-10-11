"""Смоук-тест всей цепочки бота БЕЗ Telegram: кнопка -> инструкция -> фото -> вердикт.

Запуск:  python3 test_bot_flow.py

Как это работает: вместо настоящего Telegram мы подсовываем хэндлерам «куклу»
(Message/Bot), которая записывает все, что бот хотел отправить. Так можно
проверить логику, не запуская бота и не тратя интернет-трафик.
"""
import asyncio
import io
import os
import sys
import tempfile
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(__file__))

# Журнал принятых чеков пишем во временную папку, чтобы не гадить в проект.
import receipt_check as rc
rc.CHECKS_DIR = tempfile.mkdtemp()

import handlers as h

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

sent: list[tuple] = []          # всё, что «бот отправил»


class FakeFile:
    def __init__(self, data):
        self.data = data

    async def download_to_memory_bytes(self):
        return self.data


class FakeBot:
    data = b""

    async def get_file(self, file_id):
        return FakeFile(FakeBot.data)

    async def delete_message(self, chat_id=None, message_id=None):
        sent.append(("delete", message_id))


def make_message(user_id: int):
    m = MagicMock()
    m.from_user.id = user_id
    m.from_user.first_name = "Тест"
    m.chat.id = 555
    m.bot = FakeBot()

    p = MagicMock()
    p.file_id = "f1"
    p.file_unique_id = f"uniq_{user_id}"
    m.photo = [p, p, p]
    m.document = None

    async def answer(text=None, reply_markup=None, **kw):
        msg = MagicMock()
        msg.message_id = 999

        async def _delete():
            sent.append(("delete_self",))

        async def _edit_text(t):
            sent.append(("edit", t))

        msg.delete = _delete
        msg.edit_text = _edit_text
        sent.append(("answer", text, bool(reply_markup)))
        return msg

    async def answer_photo(photo=None, caption=None, reply_markup=None, **kw):
        msg = MagicMock()
        msg.message_id = 998

        async def _delete():
            sent.append(("delete_self",))

        async def _edit_text(t):
            sent.append(("edit", t))

        msg.delete = _delete
        msg.edit_text = _edit_text
        sent.append(("photo", caption, bool(reply_markup)))
        return msg

    async def delete():
        sent.append(("delete_self",))

    async def edit_text(text):
        sent.append(("edit", text))

    m.answer = answer
    m.answer_photo = answer_photo
    return m


def draw_receipt(lines, blur=0):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (760, 90 + 60 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype(FONT, 30)
    except Exception:
        f = ImageFont.load_default()
    y = 20
    for ln in lines:
        d.text((20, y), ln, fill="black", font=f)
        y += 60
    if blur:
        from PIL import ImageFilter
        img = img.filter(ImageFilter.GaussianBlur(blur))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


GOOD_LINES = ["МАГАЗИН У ПЕТИ", "VOLT ENERGY 0.5L 89.99", "ДАТА 11.10.2026 14:32", "ИТОГО: 89.99 руб"]


async def main() -> None:
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'} {name} {extra}")
        ok = ok and cond

    # 1. Приветствие должно быть с кнопкой
    FakeBot.data = draw_receipt(GOOD_LINES)
    await h.try_greet(make_message(111))
    last = sent[-1]
    check("greeting has button", last[-1] is True)

    # 2. Нажатие кнопки -> текст-инструкция про робуксы
    cb = MagicMock()
    cb.from_user.id = 111
    cb.message = make_message(111)

    async def noop(*a, **k):
        return None
    cb.answer = noop
    await h.cb_upload_check(cb)
    instr = [s[1] for s in sent if s[0] == "answer"][-1]
    check("instruction text", "КАК ПОЛУЧИТЬ РОБУКСЫ" in instr and "Один чек = одна выплата" in instr)
    check("instruction id remembered", 111 in h.pending_instruction_msg)

    # 3. Пользователь шлёт фото: инструкция удаляется, есть «Проверка чека», есть вердикт
    sent.clear()
    await h.handle_receipt_photo(make_message(111))
    kinds = [s[0] for s in sent]
    texts = [s[1] for s in sent if s[0] == "answer"]
    check("instruction deleted", ("delete", 999) in sent, str(kinds))
    check("checking notice", any(t and "Проверка чека" in t for t in texts))
    check("receipt accepted", any(t and "ЧЕК ПРИНЯТ" in t for t in texts),
          repr([t[:60] for t in texts if t][-1:]))

    # 4. Повтор того же чека -> дубликат отклоняется
    sent.clear()
    await h.handle_receipt_photo(make_message(111))
    texts = [s[1] for s in sent if s[0] == "answer"]
    check("duplicate rejected",
          any(t and "НЕ ПРИНЯТ" in t and "уже был принят" in t for t in texts))

    # 5. Мутное фото -> отказ
    FakeBot.data = draw_receipt(GOOD_LINES, blur=6)
    sent.clear()
    await h.handle_receipt_photo(make_message(333))
    texts = [s[1] for s in sent if s[0] == "answer"]
    check("blurry rejected", any(t and "НЕ ПРИНЯТ" in t for t in texts),
          repr([t[:50] for t in texts if t][-1:]))

    # 6. Чек без VOLT -> отказ
    FakeBot.data = draw_receipt(["МАГАЗИН", "COCA COLA 0.5 89.99", "ДАТА 11.10.2026", "ИТОГО: 89.99 руб"])
    sent.clear()
    await h.handle_receipt_photo(make_message(444))
    texts = [s[1] for s in sent if s[0] == "answer"]
    check("no-brand rejected", any(t and "VOLT" in t and "НЕ ПРИНЯТ" in t for t in texts))

    print("\nRESULT:", "ALL PASS" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
