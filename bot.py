"""
bot.py — ГЛАВНЫЙ файл проекта. Здесь бот создаётся и запускается.
Сами ответы бота («хэндлеры») лежат в отдельном файле handlers.py.

Запуск (по шагам):
    1. pip install -r requirements.txt      # установить библиотеки
    2. Создай файл .env и впиши туда токен от @BotFather (см. README.md)
    3. python bot.py                        # запустить бота

Остановка: нажми Ctrl+C в терминале.
"""

import asyncio                                # встроенная библиотека Python для «асинхронных» программ
import logging                                # чтобы видеть в терминале, что происходит
import os                                     # чтобы читать переменные окружения (наш токен из .env)

from dotenv import load_dotenv                # читает файл .env и превращает его в переменные окружения
from aiogram import Bot, Dispatcher           # Bot = сам бот; Dispatcher = «диспетчер сообщений»

from handlers import router                   # наш «роутер» с хэндлерами из файла handlers.py

# ---------------- Загрузка токена ----------------
# load_dotenv() берёт файл .env рядом с этим скриптом и подгружает его содержимое.
# После этого os.getenv("TELEGRAM_BOT_TOKEN") вернёт строку с токеном.
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")   # если переменной нет — вернётся пустая строка ""


async def main() -> None:
    """Функция-сборка: создаёт бота, подключает хэндлеры и запускает их."""

    # Логирование — чтобы в терминале было видно служебные сообщения
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    # Проверка: если токена нет — объясняем новичку, что сделать, и выходим
    if not BOT_TOKEN or "ВСТАВЬ" in BOT_TOKEN or "твой_токен" in BOT_TOKEN:
        raise SystemExit(
            "❌ Токен не найден!\n"
            "   1. Напиши @BotFather в Telegram, отправь /newbot и получи токен.\n"
            "   2. Создай файл .env в папке проекта (можно скопировать .env.example).\n"
            "   3. Впиши в него строку: TELEGRAM_BOT_TOKEN=твой_токен\n"
            "   4. Запусти снова: python bot.py"
        )

    bot = Bot(token=BOT_TOKEN)      # «лицо» бота: умеет читать и писать сообщения в Telegram
    dp = Dispatcher()               # диспетчер: получает обновления от Telegram и раздаёт их хэндлерам

    # Подключаем наш роутер из handlers.py.
    # Теперь все @router.message(...) из того файла начинают работать.
    dp.include_router(router)

    print("✅ Бот запущен и ждёт сообщений. Нажми Ctrl+C, чтобы остановить.")
    try:
        # start_polling — бот сам периодически спрашивает Telegram: «есть новые сообщения?»
        await dp.start_polling(bot)
    finally:
        # Всегда закрываем сетевое соединение аккуратно, даже при ошибке
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())         # точка входа: запускаем нашу асинхронную функцию main()
    except KeyboardInterrupt:
        # Пользователь нажал Ctrl+C — это нормальная остановка, не ошибка
        print("\n🛑 Бот остановлен. Пока!")
    except Exception as e:
        # Понятная подсказка вместо страшного текста ошибки
        if "Unauthorized" in str(e):
            print("❌ Telegram не принимает токен. Проверь: токен скопирован целиком? "
                  "Не был ли он отозван? Можно создать новый у @BotFather (/revoke).")
        else:
            raise
