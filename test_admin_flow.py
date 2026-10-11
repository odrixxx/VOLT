"""Тест секретной админ-панели (admin.py) БЕЗ Telegram.

Запуск:  python3 test_admin_flow.py

Проверяем три вещи:
  1. Каждое сообщение пользователя тихо копируется всем админам, а самому
     пользователю бот НЕ отвечает ничего нового (он ничего не замечает).
  2. Админ может написать юзеру от имени бота (/write и режим /talk),
     а команды админа юзеру НЕ пересылаются.
  3. Обычный пользователь не получает доступ к админ-командам — они для него
     просто не срабатывают (фильтр по ADMIN_IDS).
"""
import asyncio
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(__file__))

# Админов задаём ДО импорта admin.py — хэндлеры регистрируются с этим списком.
os.environ["ADMIN_IDS"] = "777"

import admin as a


sent: list[tuple] = []      # всё, что «бот отправил» за время теста


class FakeBot:
    """Кукла бота: пишет в журнал вместо сети."""

    async def send_message(self, chat_id=None, text=None, **kw):
        sent.append(("send", chat_id, text))


def make_message(user_id: int, text: str | None = None, chat_type: str = "private"):
    m = MagicMock()
    m.from_user.id = user_id
    m.from_user.first_name = f"Юзер{user_id}"
    m.from_user.last_name = None
    m.from_user.username = f"u{user_id}"
    m.chat.id = user_id if chat_type == "private" else -100
    m.chat.type = chat_type
    m.bot = FakeBot()
    m.text = text
    m.photo = None
    m.document = None
    m.voice = None
    m.sticker = None

    async def _copy_to(chat_id):
        sent.append(("copy", chat_id, text))
        return MagicMock()

    async def _answer(t=None, **kw):
        sent.append(("answer", m.chat.id, t))
        return MagicMock()

    m.copy_to = _copy_to
    m.answer = _answer
    return m


def cmd_msg(user_id: int, full_text: str):
    """Собес сообщение-команду: /write 123 привет -> command.args='123 привет'."""
    m = make_message(user_id, full_text)
    obj = MagicMock()
    parts = full_text.split(maxsplit=1)
    obj.args = parts[1] if len(parts) > 1 else None
    return m, obj


async def main() -> None:
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'} {name} {extra}")
        ok = ok and cond

    check("ADMIN_IDS загружен из env", a.ADMIN_IDS == {777}, str(a.ADMIN_IDS))

    # 1. Пользователь 111 пишет боту -> зеркалится админу 777, юзеру — молчим
    sent.clear()
    await a.mirror_to_admins(make_message(111, "привет, где мои робуксы?"))
    kinds = [s[0] for s in sent]
    check("mirror copied to admin", ("copy", 777, "привет, где мои робуксы?") in sent, str(kinds))
    check("mirror header to admin", any(s[0] == "send" and s[1] == 777 and "НОВОЕ СООБЩЕНИЕ" in s[2] for s in sent))
    check("user got NO reply", not any(s[1] == 111 for s in sent if s[0] in ("send", "answer")), str(sent))
    check("user registered", 111 in a.USERS and a.USERS[111].messages == 1)

    # 2. /write от админа -> сообщение улетит юзеру от имени бота
    sent.clear()
    m, c = cmd_msg(777, "/write 111 Чек принят, жди начисление ✅")
    await a.admin_write(m, c)
    check("admin write delivered", ("send", 111, "Чек принят, жди начисление ✅") in sent, str(sent))
    check("admin got receipt", any(s[0] == "answer" and "Отправлено" in (s[2] or "") for s in sent))

    # 3. /talk + обычный текст админа -> пересылается юзеру; /stop выключает
    sent.clear()
    m, c = cmd_msg(777, "/talk 111")
    await a.admin_talk(m, c)
    check("talk mode on", a.TALK_MODE.get(777) == 111)

    sent.clear()
    await a.admin_talk_forward(make_message(777, "Здарова! По твоему чеку всё ок"))
    check("talk forwarded to user", ("send", 111, "Здарова! По твоему чеку всё ок") in sent, str(sent))

    # команда админа в режиме диалога юзеру НЕ уходит
    sent.clear()
    m, c = cmd_msg(777, "/stop")
    await a.admin_stop(m)
    await a.admin_talk_forward(make_message(777, "/users"))   # не должно ничего отправить
    check("talk mode off", 777 not in a.TALK_MODE)
    check("commands not leaked to users", not any(s[0] == "send" and s[1] == 111 for s in sent), str(sent))

    # 4. Юзер не админ: админские функции его сообщений не касаются
    #    (фильтр admin_only не пустит его хэндлеры; напрямую дергаем mirror —
    #     он обязан только зеркалировать)
    sent.clear()
    await a.mirror_to_admins(make_message(222, "а можно два чека?"))
    check("second user mirrored", any(s[0] == "copy" and s[1] == 777 for s in sent))
    check("second user registered", 222 in a.USERS)

    # 5. Блокировка: если доставка упала с «blocked», режим диалога снимается,
    #    и в чат админа не летит никаких служебных сообщений (чтобы юзер ничего
    #    не увидел, если читает этот же диалог через зеркало).
    class BlockedBot(FakeBot):
        async def send_message(self, chat_id=None, text=None, **kw):
            sent.append(("send", chat_id, text))
            if chat_id == 333:
                raise Exception("Telegram server says: Bot was blocked by the user")

    a.TALK_MODE[777] = 333
    sent.clear()
    mb = make_message(777, "ты тут?")
    mb.bot = BlockedBot()
    await a.admin_talk_forward(mb)
    check("talk cleared after block", 333 not in a.TALK_MODE.values())
    check("no service noise in admin chat", not any(s[1] == 777 for s in sent), str(sent))

    # 6. Сообщения админа НЕ зеркалятся самим себе (иначе при /talk была бы каша)
    sent.clear()
    await a.mirror_to_admins(make_message(777, "проверка зеркала"))
    check("admin messages not mirrored", len(sent) == 0, str(sent))

    print("\nRESULT:", "ALL PASS" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
