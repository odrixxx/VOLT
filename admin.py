"""
admin.py — СЕКРЕТНАЯ панель администратора (админ-луч / admin mirror).

Что здесь происходит:
  1. Каждое входящее сообщение пользователя бот молча пересылает администраторам
     (их ID прописаны в .env: ADMIN_IDS). Пользователь ничего не замечает:
     бот НЕ отвечает на эти пересылки, НИКАК их не подписывает и не удаляет.
  2. Админ может в ЛЮБОЙ момент написать пользователю ОТ ИМЕНИ БОТА:
       /write <ID_пользователя> <текст>     — разовое сообщение
       /talk  <ID_пользователя>             — «режим диалога»: всё, что админ
                                              пишет дальше, летит этому человеку
       /stop                                — выйти из режима диалога
  3. Ответы пользователей во время диалога админ видит в этом же чате с ботом
     (с пометкой, от кого они).

ВАЖНО (почему пользователи ничего не узнают):
  - Хэндлеры ниже реагируют ТОЛЬКО на сообщения от ID из ADMIN_IDS.
    Чужие сообщения проходят мимо этого файла полностью.
  - Никаких подсказок/кнопок для обычных юзеров тут нет и не будет.

Этот роутер подключается в bot.py ПОСЛЕ основного router, но его хэндлеры
специфичны (фильтр по админ-ID), поэтому порядок для обычных юзеров не важен.
"""

from __future__ import annotations          # чтобы аннотации ботов (Bot) не вычислялись в рантайме

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import Message, TelegramObject

if TYPE_CHECKING:                      # импорт только для подсказок типизации, в рантайме не нужен
    from aiogram import Bot


def _load_admin_ids() -> set[int]:
    """Читаем ADMIN_IDS из окружения. Отдельная функция — чтобы можно было
    перечитать после load_dotenv() в bot.py (импорты-то идут раньше)."""
    raw = os.getenv("ADMIN_IDS", "")
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


# Заполняется при импорте (обычно пусто) и ОБНОВЛЯЕТСЯ функцией init_admins()
# уже после того, как bot.py загрузил .env.
ADMIN_IDS: set[int] = _load_admin_ids()


def init_admins() -> set[int]:
    """Перечитать ADMIN_IDS из .env. Вызывается из bot.py перед запуском поллинга."""
    ADMIN_IDS.clear()
    ADMIN_IDS.update(_load_admin_ids())
    return ADMIN_IDS


log = logging.getLogger("volt.admin")

# Отдельный роутер только для админских дел. Подключается в bot.py вторым.
admin_router = Router()


def is_admin(user_id: int | None) -> bool:
    """Проверка: этот человек — админ? (пустой ADMIN_IDS = админов нет, все молчат)."""
    return user_id is not None and user_id in ADMIN_IDS


class AdminOnly(BaseFilter):
    """Фильтр «это админ». Проверяет ADMIN_IDS В МОМЕНТ прихода сообщения,
    а не в момент регистрации хэндлеров — так панель работает даже если
    ADMIN_IDS стал известен позже (или поменялся через reload_dot_env)."""

    async def __call__(self, obj: TelegramObject) -> bool:
        user = getattr(obj, "from_user", None)
        return is_admin(getattr(user, "id", None))


def admin_only() -> AdminOnly:
    """Возвращает экземпляр фильтра-проверки админа (см. AdminOnly выше)."""
    return AdminOnly()


# ---------------------------------------------------------------------------
# ЖУРНАЛ ПОЛЬЗОВАТЕЛЕЙ + РЕЖИМ ДИАЛОГА
# ---------------------------------------------------------------------------

@dataclass
class UserInfo:
    """Минимум, который мы запоминаем о каждом, кто написал боту."""
    user_id: int
    username: str = ""          # @ник, если есть
    first_name: str = ""
    last_name: str = ""
    chat_id: int = 0            # куда писать этому человеку (его личный чат с ботом)
    messages: int = 0           # сколько всего сообщений прислал
    last_text: str = ""         # последняя текстовая фраза (для списка /users)
    blocked_admins: set[int] = field(default_factory=set)  # админ, которому юзер не ответил (не пишем снова)


# Кто есть кто: user_id -> UserInfo. Заполняется на лету из входящих сообщений.
USERS: dict[int, UserInfo] = {}

# Активный режим диалога: админ_id -> user_id, которому сейчас летят сообщения.
TALK_MODE: dict[int, int] = {}


def register_user(message: Message) -> UserInfo:
    """Запоминает/обновляет карточку пользователя по входящему сообщению."""
    u = message.from_user
    info = USERS.get(u.id)
    if info is None:
        info = USERS[u.id] = UserInfo(user_id=u.id)
    info.username = u.username or ""
    info.first_name = u.first_name or ""
    info.last_name = u.last_name or ""
    # Личный чат с ботом = chat.id приватного диалога (в группах не запоминаем)
    if message.chat.type == "private":
        info.chat_id = message.chat.id
    info.messages += 1
    if message.text:
        info.last_text = message.text[:80]
    return info


def user_label(info: UserInfo) -> str:
    """Человекочитаемая подпись: [123456789] Иван @ivan."""
    name = " ".join(x for x in (info.first_name, info.last_name) if x) or "без имени"
    nick = f" @{info.username}" if info.username else ""
    return f"[{info.user_id}] {name}{nick}"


async def safe_send(bot: Bot, chat_id: int, text: str) -> bool:
    """Отправить сообщение и не упасть, если пользователь заблокировал бота.

    Возвращает True, если дошло. При блокировке чистим TALK_MODE, чтобы админ
    не стрелял в пустоту, и пишем ему об этом отдельным служебным сообщением.
    """
    try:
        await bot.send_message(chat_id=chat_id, text=text)
        return True
    except Exception as e:
        err = str(e)
        if "blocked" in err.lower() or "Bot was blocked" in err:
            log.info("пользователь %s заблокировал бота", chat_id)
            # снимаем режим диалога с этим адресатом у всех админов
            for adm, target in list(TALK_MODE.items()):
                if target == chat_id:
                    TALK_MODE.pop(adm, None)
            return False
        log.exception("не удалось отправить сообщение в %s", chat_id)
        return False


async def notify_admins(bot: Bot, text: str) -> None:
    """Разослать служебное сообщение всем админам из ADMIN_IDS."""
    for adm in sorted(ADMIN_IDS):
        try:
            await bot.send_message(chat_id=adm, text=text)
        except Exception as e:
            log.debug("админ %s не принял рассылку: %s", adm, e)


# ---------------------------------------------------------------------------
# ПЕРЕСЫЛКА ВСЕХ ВХОДЯЩИХ СООБЩЕНИЙ АДМИНАМ (невидимо для пользователей)
# ---------------------------------------------------------------------------

def _is_admin_message(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in ADMIN_IDS


@admin_router.message()
async def mirror_to_admins(message: Message) -> None:
    """Тихо копируем каждое пользовательское сообщение всем администраторам.

    Сообщения самих админов НЕ зеркалим — иначе в личном чате «админ + бот»
    начнётся каша из копий собственных реплик во время режима /talk.
    Пользователь ничего не замечает: мы ему ничего не отвечаем,
    ничего не удаляем и не редактируем.
    """
    info = register_user(message)

    if not ADMIN_IDS or _is_admin_message(message):
        return                       # админов нет или это сам админ — просто запоминаем юзера

    # Подпись для админа: кто, что, когда. Для медиа — тип вложения.
    kind = "текст"
    if message.photo:
        kind = "фото (чек?)"
    elif message.document:
        kind = "файл"
    elif message.voice:
        kind = "голосовое"
    elif message.sticker:
        kind = "стикер"

    header = (
        f"👤 НОВОЕ СООБЩЕНИЕ\n"
        f"{user_label(info)}\n"
        f"Тип: {kind} | Всего сообщений: {info.messages}\n"
        f"──────────────"
    )

    for adm in sorted(ADMIN_IDS):
        if adm in info.blocked_admins:
            continue                 # этому админу юзер не отвечает — не спамим
        try:
            await message.copy_to(chat_id=adm)      # точная копия сообщения (фото остаётся фото)
            await message.bot.send_message(chat_id=adm, text=header)
        except Exception as e:
            if "blocked" in str(e).lower():
                info.blocked_admins.add(adm)
            else:
                log.debug("пересылка админу %s не удалась: %s", adm, e)


# ---------------------------------------------------------------------------
# КОМАНДЫ АДМИНА
# ---------------------------------------------------------------------------

@admin_router.message(Command("help"), admin_only())
async def admin_help(message: Message) -> None:
    """Шпаргалка по админ-командам (только для админов)."""
    await message.answer(
        "🛠 ПАНЕЛЬ АДМИНА\n"
        "\n"
        "/users — список всех, кто писал боту\n"
        "/last <ID> — последние реплики конкретного человека\n"
        "/write <ID> <текст> — написать пользователю от имени бота\n"
        "/talk <ID> — режим диалога: всё, что пишешь дальше, летит этому юзеру\n"
        "/stop — выключить режим диалога\n"
        "\n"
        "Все входящие от пользователей приходят сюда автоматически, незаметно для них."
    )


@admin_router.message(Command("users"), admin_only())
async def admin_users(message: Message) -> None:
    """Показывает всех известных пользователей: ID, имя, @ник, число сообщений."""
    if not USERS:
        await message.answer("Пока никто не писал боту.")
        return
    lines = []
    for info in sorted(USERS.values(), key=lambda i: i.messages, reverse=True)[:50]:
        lines.append(f"{user_label(info)} — {info.messages} сообщ.\n   🗨 «{info.last_text}»")
    await message.answer("📒 ПОЛЬЗОВАТЕЛИ:\n\n" + "\n\n".join(lines))


@admin_router.message(Command("last"), admin_only())
async def admin_last(message: Message, command: CommandObject) -> None:
    """Быстрая справка по одному человеку: /last 123456789."""
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        await message.answer("Формат: /last <ID_пользователя>")
        return
    uid = int(command.args.strip())
    info = USERS.get(uid)
    if info is None:
        await message.answer(f"Пользователь {uid} мне неизвестен.")
        return
    await message.answer(
        f"📇 ПРОФИЛЬ\n"
        f"{user_label(info)}\n"
        f"Сообщений: {info.messages}\n"
        f"Последняя реплика: «{info.last_text}»\n"
        f"Чат для связи: {info.chat_id}"
    )


@admin_router.message(Command("write"), admin_only())
async def admin_write(message: Message, command: CommandObject) -> None:
    """Разовое сообщение пользователю от имени бота: /write <ID> <текст>."""
    if not command.args:
        await message.answer("Формат: /write <ID_пользователя> <текст сообщения>")
        return
    parts = command.args.split(maxsplit=1)
    if len(parts) < 2 or not parts[0].lstrip("-").isdigit():
        await message.answer("Не понял. Пример: /write 123456789 Привет! Твой чек принят ✅")
        return
    uid, text = int(parts[0]), parts[1]
    ok = await safe_send(message.bot, uid, text)
    await message.answer("✅ Отправлено." if ok else "❌ Не отправлено (см. причину выше/логи).")


@admin_router.message(Command("talk"), admin_only())
async def admin_talk(message: Message, command: CommandObject) -> None:
    """Включает режим диалога: следующие обычные сообщения админа пойдут юзеру."""
    if not command.args or not command.args.strip().lstrip("-").isdigit():
        await message.answer("Формат: /talk <ID_пользователя>")
        return
    uid = int(command.args.strip())
    if uid not in USERS and uid == message.from_user.id:
        await message.answer("Себе через бота писать нельзя 🙂")
        return
    TALK_MODE[message.from_user.id] = uid
    note = "" if uid in USERS else "\n(этот ID ещё не писал боту — сообщение уйдёт «вслепую»)"
    await message.answer(f"🎙 Режим диалога включён: пишу {uid}.{note}\nДля выхода: /stop")


@admin_router.message(Command("stop"), admin_only())
async def admin_stop(message: Message) -> None:
    """Выключает режим диалога."""
    if TALK_MODE.pop(message.from_user.id, None) is not None:
        await message.answer("⏹ Режим диалога выключен.")
    else:
        await message.answer("Режим диалога и так был выключен.")


# ---------------------------------------------------------------------------
# СООБЩЕНИЯ АДМИНА В РЕЖИМЕ ДИАЛОГА -> УЛЕТЮТ ПОЛЬЗОВАТЕЛЮ
# ---------------------------------------------------------------------------

@admin_router.message(
    admin_only(),
    ~F.text.startswith("/"),        # команды не пересылаем
    F.text,
)
async def admin_talk_forward(message: Message) -> None:
    """Обычный текст админа во время /talk пересылается выбранному юзеру от бота."""
    target = TALK_MODE.get(message.from_user.id)
    if target is None:
        # Админ пишет текст вне диалога — молча игнорируем (чтобы не мешать зеркалу).
        return
    await safe_send(message.bot, target, message.text)
    # Космический «отправлено» НЕ пишем в чат: это служебная реплика,
    # а любое сообщение в этом чате видит и пользователь. О результатах
    # админ судит по зеркалу (ответ юзера придёт копией) и по логам.


# ---------------------------------------------------------------------------
# ЗАПУСК БЕЗ ОСНОВНОГО БОТА (не обязательно, но удобно для проверки прав доступа)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raise SystemExit(
        "Этот файл — часть бота. Запускай главный файл:  python bot.py\n"
        "(admin_router уже подключён внутри него)."
    )
