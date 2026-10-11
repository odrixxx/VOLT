"""
Телеграм-бот: приветствие при первом взаимодействии с пользователем.

Запуск:
    1. pip install -r requirements.txt
    2. Получи токен у @BotFather в Telegram
    3. python bot.py            (или задай токен: TELEGRAM_BOT_TOKEN=... python bot.py)
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

# ---------------- Настройки ----------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")

# Память о том, кого уже поприветствовали (в памяти бота; сброс при перезапуске)
welcomed_users: set[int] = set()

router = Router()

# ---------------- Хэндлеры ----------------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Команда /start — всегда показывает приветствие."""
    welcomed_users.add(message.from_user.id)
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Это твой новый бот. Я работаю.\n"
        "Напиши мне чтонибудь, и я отвечу 😉"
    )


@router.message(F.text)
async def handle_first_message(message: Message) -> None:
    """Любое текстовое сообщение: если пользователь впервые — приветствуем."""
    user_id = message.from_user.id

    if user_id not in welcomed_users:
        welcomed_users.add(user_id)
        await message.answer(
            f"👋 Привет, {message.from_user.first_name}! Рад тебя видеть впервые.\n"
            "Нажми /start, чтобы увидеть полное приветствие, "
            "или просто напиши мне сообщение."
        )
        return

    await message.answer(f"Ты написал: «{message.text}»")


# ---------------- Запуск ----------------


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    if "ВСТАВЬ_СЮДА" in BOT_TOKEN:
        raise SystemExit(
            "❌ Не задан токен! Получи токен у @BotFather и вставь его в bot.py "
            "или задай переменную окружения TELEGRAM_BOT_TOKEN."
        )

    bot = Bot(token=BOT_TOKEN)
    try:
        dp = Dispatcher()
        dp.include_router(router)

        # Регистрируем меню команд в Telegram (красивая кнопка слева внизу)
        from aiogram.types import BotCommand

        await bot.set_my_commands([BotCommand(command="start", description="Начать / Приветствие")])

        print("✅ Бот запущен. Нажми Ctrl+C, чтобы остановить.")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен.")
    except SystemExit as e:
        if e.code:
            print(e.code)
    except Exception as e:
        # Понятная подсказка вместо страшного трейсбека
        if "Unauthorized" in str(e):
            print("❌ Telegram не принимает токен. Проверь, что токен скопирован полностью "
                  "и не был отозван (можно создать новый у @BotFather).")
        else:
            raise
