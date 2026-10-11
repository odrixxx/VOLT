"""
handlers.py — «мозг» бота. Здесь лежит вся логика (пока одна: приветствие).

ЗАДАЧА БОТА (на текущем этапе):
  Поприветствовать пользователя при ПЕРВОМ взаимодействии — и всё.
  Больше бот ничего не делает: ни эхо, ни команды, ни кнопки.

Что считается "первым взаимодействием"?
  Пользователь впервые что-то отправил боту: нажал /start или написал любое
  текстовое сообщение. Бот отвечает приветствием и запоминает его ID.
  Все последующие сообщения бот молча игнорирует.

Как бот запоминает?
  В памяти Python лежит множество (set) с ID уже поприветствованных людей.
  Это простое учебное решение. Минус: при перезапуске скрипта память очищается,
  и знакомых придётся приветствовать заново. Позже заменим на SQLite.

ПОРЯДОК РАБОТЫ (Telegram проверяет хэндлеры СВЕРХУ ВНИЗ, первый подошедший — срабатывает):
  1. cmd_start      — реагирует ровно на команду /start
  2. any_text       — реагирует на любой другой текст
  3. unknown_type   — реагирует на нетекст (стикер, фото, голосовое…)
"""

import asyncio                              # чтобы запустить тяжёлую OCR-проверку «в фоне», не вешая бота
import glob                                 # поиск картинок с любым расширением (welcome.*, photo_1.jpg и т.п.)
import logging                              # писать в терминал, что происходит при проверке чека
import os                                   # чтобы собрать путь к файлу картинки

from aiogram import F, Router               # Router — «коробка» для хэндлеров; F — фильтр «загляни в поле сообщения»
from aiogram.filters import CommandStart    # фильтр: срабатывать ровно на /start
from aiogram.types import (                 # типы сообщений и элементов интерфейса Telegram
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# Реальная проверка чеков (OCR + резкость + дубликаты) живёт в отдельном файле.
import receipt_check as rc

log = logging.getLogger("volt.bot")

# Создаём роутер. В bot.py мы передадим его диспетчеру (Dispatcher),
# а диспетчер будет пересылать сюда все входящие обновления от Telegram.
router = Router()

# ---------------- КАРТИНКА К ПРИВЕТСТВИЮ ----------------
# Положи свою картинку (постер акции / своё фото — неважно что) в корень проекта,
# рядом с bot.py. Бот сам найдёт её по любому из типичных имён и в любом формате,
# так что переименовывать файл НЕ нужно.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# Шаблоны имён файлов-кандидатов, которые бот считает «картинкой приветствия».
WELCOME_NAME_PATTERNS = (
    "welcome.*", "Welcome.*", "WELCOME.*",
    "poster.*", "Poster.*", "posters.*", "акция.*", "volt.*",
    "photo.*", "photo_*", "img.*", "image.*", "IMG_*", "IMG-*",
    "DSC_*", "DSCF*", "PXL_*", "Screenshot*", "screenshot*",
    "mmexport*", "1000*", "IMG[0-9]*",
)

# Форматы, которые принимает Telegram как фото
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif"}


def find_welcome_image() -> str | None:
    """Ищет картинку приветствия в корне проекта. Возвращает путь или None.

    Порядок поиска:
      1. Точное имя welcome.png / welcome.jpg / welcome.jpeg — всегда приоритет;
      2. Любой файл с «типичным» именем (фото_1.jpg, IMG_2024.PNG, Screenshot...);
      3. Если в корне лежит РОВНО одна картинка — считаем, что это она и есть
         (загрузил свою фотку без переименования — и она сразу работает).
    """
    # 1. Точные имена
    for ext in (".png", ".jpg", ".jpeg"):
        exact = os.path.join(PROJECT_ROOT, f"welcome{ext}")
        if os.path.isfile(exact):
            return exact

    # 2. Кандидаты по шаблонам имён
    candidates: list[str] = []
    for pattern in WELCOME_NAME_PATTERNS:
        for path in glob.glob(os.path.join(PROJECT_ROOT, pattern)):
            if os.path.isfile(path) and os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS:
                candidates.append(path)
    if candidates:
        # берём самый свежий по дате изменения — обычно именно его только что загрузили
        return max(candidates, key=os.path.getmtime)

    # 3. Любая единственная картинка в корне проекта
    all_images = [
        p for p in glob.glob(os.path.join(PROJECT_ROOT, "*"))
        if os.path.isfile(p) and os.path.splitext(p)[1].lower() in IMAGE_EXTENSIONS
    ]
    if len(all_images) == 1:
        return all_images[0]
    return None


def load_welcome_photo() -> BufferedInputFile | None:
    """Читает картинку с диска и готовит её к отправке. Возвращает None, если файла нет."""
    path = find_welcome_image()
    if path is None:
        return None                          # картинки нет — не падаем с ошибкой, просто пропустим фото
    with open(path, "rb") as f:  # "rb" = read binary, читаем как бинарные данные
        data = f.read()
    return BufferedInputFile(data, filename=os.path.basename(path))  # имя файла, которое увидит Telegram

# Множество ID пользователей, которых УЖЕ поприветствовали.
# set удобен тем, что проверка «есть ли число в множестве» работает быстро,
# а дубликаты в множество попасть физически не могут.
welcomed_users: set[int] = set()

# ---------------- КНОПКА «ЗАГРУЗИТЬ ЧЕК» ----------------
# data — это служебная метка, которую Telegram пришлёт боту при нажатии.
# Её не видит пользователь, видит только надпись text.
CALLBACK_UPLOAD_CHECK = "upload_check"

# ID того сообщения с инструкцией, которое надо удалить после того, как
# пользователь пришлёт фото чека. Храним в памяти словарём: user_id -> message_id.
# Минус учебного решения: при перезапуске бота эти ID забываются (тогда сообщение
# просто не удалится — бот не упадёт). Правильное решение — FSM (aiogram-filters),
# поставим позже.
pending_instruction_msg: dict[int, int] = {}


def upload_check_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с одной большой кнопкой под текстом."""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📤 ЗАГРУЗИТЬ ЧЕК", callback_data=CALLBACK_UPLOAD_CHECK)
    ]])
    return kb


INSTRUCTION_TEXT = (
    "КАК ПОЛУЧИТЬ РОБУКСЫ? 🤑\n"
    "\n"
    "1️⃣ Купи VOLT и сохрани чек.\n"
    "2️⃣ Сфоткай его чётко. Должно быть видно: название «VOLT», дату и сумму. "
    "(Мутные фото не пройдут!)\n"
    "3️⃣ 👇 Жми кнопку ниже и отправь фото сюда.\n"
    "\n"
    "Бот проверит чек за пару секунд и выдаст награду! 🎁\n"
    "⚠️ Один чек = одна выплата. Фотошоп не катит."
)


def instruction_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для сообщения-инструкции: кнопка ведёт сразу в диалог с ботом."""
    # TODO: замени username на реальный юзернейм своего бота (его даёт @BotFather).
    bot_username = os.getenv("BOT_USERNAME", "your_bot")
    url = f"https://t.me/{bot_username}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 ЗАГРУЗИТЬ ЧЕК", url=url)],
        [InlineKeyboardButton(text="↩️ Назад", callback_data=CALLBACK_UPLOAD_CHECK)],
    ])


async def try_greet(message: Message) -> None:
    """Общая логика: поздороваться, если человек пишет впервые. Иначе — молчать.

    Шаги:
      1. Берём уникальный ID автора сообщения.
      2. Если такого ID нет в welcomed_users — это первое взаимодействие:
         добавляем ID в множество и отправляем приветствие.
      3. Если ID уже есть — просто выходим (бот больше ничего не умеет).
    """
    user_id = message.from_user.id                 # числовой ID пользователя (одинаков у него во всех чатах с этим ботом)

    if user_id in welcomed_users:
        return                                     # уже здоровались — молча игнорируем

    welcomed_users.add(user_id)                    # запоминаем: теперь этот человек «не новичок»

    name = message.from_user.first_name or "народ"  # имя может быть не задано — тогда обращаемся «заряженный народ»

    # ТЕКСТ ПРИВЕТСТВИЯ (акция VOLT). Обратные косые черты \ в тексте НЕ экранируем —
    # в Python-строке они означают перевод строки (\n), всё остальное пишется как есть.
    greeting = (
        f"ДОБРО ПОЖАЛОВАТЬ В КОМАНДУ VOLT! ⚡️💛\n"
        f"Привет, {name}! Мы — твои любимые энергетики VOLT, и теперь мы тут, "
        f"в Telegram, чтобы делиться крутыми акциями и дарить тебе халяву! 🎁\n"
        f"🎮 ХОЧЕШЬ РОБУКСЫ? ЛЕГКО!\n"
        f"Мы запускаем акцию, от которой заряжает сильнее, чем банка VOLT утром:\n"
        f"🛒 КАК УЧАСТВОВАТЬ:\n"
        f"✅ Купи любой энергетик VOLT\n"
        f"✅ Сфоткай чек покупки (чётко, чтобы всё было видно)\n"
        f"✅ Загрузи чек сюда в чат / отправь боту\n"
        f"✅ Получи робуксы на свой аккаунт Roblox! 🤑\n"
        f"💡 Важно: чеки принимаются с [дата] по [дата]. Один чек = одна порция робуксов!\n"
        f"Готов? Тогда жми, участвуй и делись с друзьями! 💪\n"
        f"🔥 VOLT — ЗАРЯЖАЙСЯ ПО МАКСИМУМУ! 🔥\n"
        f"#VOLTenergy #АкцияVOLT #Robux #ЧекиЗаРобуксы"
    )

    await send_greeting(message, greeting)         # картинка + подпись (или только текст, если картинки нет)


async def send_greeting(message: Message, text: str) -> None:
    """Отправляет приветствие КАРТИНКОЙ с подписью (текстом под ней).

    Если файла welcome.png нет — бот не упадёт, а пришлёт просто текст.
    Так проект можно запускать даже до того, как ты положишь свою картинку.
    """
    photo = load_welcome_photo()                   # пробуем прочитать картинку с диска

    if photo is not None:
        # send_photo = «отправь фото в этот же чат»; caption = подпись под фото (наш текст акции)
        await message.answer_photo(photo=photo, caption=text, reply_markup=upload_check_keyboard())
    else:
        # картинки нет — ограничиваемся текстом + кнопкой, чтобы бот продолжал работать
        await message.answer(text, reply_markup=upload_check_keyboard())


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Срабатывает, когда пользователь пишет /start (или нажимает большую кнопку Start).

    Эта команда перехватывается ПЕРВОЙ, поэтому до хэндлера any_text ниже
    она не дойдёт — двойного приветствия не будет.
    """
    await try_greet(message)


@router.message(F.text)
async def any_text(message: Message) -> None:
    """Срабатывает на ЛЮБОЕ текстовое сообщение (кроме /start — он перехвачен выше).

    Так работает «первое взаимодействие без /start»: человек просто написал
    «привет» — и получил приветствие. Повторные сообщения — игнор.
    """
    await try_greet(message)


@router.message()
async def unknown_type(message: Message) -> None:
    """Срабатывает на всё остальное: стикеры, голосовые, видео…

    Логика та же: при первом взаимодействии приветствуем, дальше молчим.
    Фото чеков обрабатываются хэндлерами НИЖЕ — до сюда они не доходят.
    """
    await try_greet(message)


# =========================================================================
# КНОПКА «ЗАГРУЗИТЬ ЧЕК» + ПРИЁМ И ПРОВЕРКА ЧЕКА
# =========================================================================

@router.callback_query(F.data == CALLBACK_UPLOAD_CHECK)
async def cb_upload_check(callback: CallbackQuery) -> None:
    """Реакция нажатие на кнопку в приветствии: показать инструкцию."""
    # answer() обязателен: без него у пользователя сверху крутится часики «загрузка».
    await callback.answer()

    msg = await callback.message.answer(
        INSTRUCTION_TEXT,
        reply_markup=instruction_keyboard(),
    )
    # запоминаем ID этого сообщения, чтобы удалить его, когда придёт фото
    pending_instruction_msg[callback.from_user.id] = msg.message_id


async def _download_photo(bot_message: Message) -> tuple[bytes, str] | None:
    """Скачивает самое большое фото из сообщения. Возвращает (байты, file_unique_id)."""
    photo = bot_message.photo[-1]            # Telegram присылает 3 размера, берём максимальный
    file_path = await bot_message.bot.get_file(photo.file_id)
    data = await file_path.download_to_memory_bytes()
    # file_unique_id одинаков для одного и того же загруженного файла у всех
    unique_id = getattr(photo, "file_unique_id", "") or ""
    return data, unique_id


async def _drop_instruction(user_id: int, chat_id: int, bot) -> None:
    """Удаляет сообщение-инструкцию, если помним его ID."""
    msg_id = pending_instruction_msg.pop(user_id, None)
    if msg_id is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as e:                       # сообщение уже удалили / слишком старое
        log.debug("не удалось удалить инструкцию %s: %s", msg_id, e)


@router.message(F.photo)
async def handle_receipt_photo(message: Message) -> None:
    """Главный хэндлер: пользователь прислал фото — проверяем чек по-настоящему."""
    user_id = message.from_user.id

    # Если человек ещё не здоровался — сначала приветствие, потом разберём фото.
    if user_id not in welcomed_users:
        await try_greet(message)

    await _drop_instruction(user_id, message.chat.id, message.bot)

    # Скачиваем картинку из Telegram (она лежит на серверах TG, а не у нас).
    try:
        downloaded = await _download_photo(message)
    except Exception as e:
        log.exception("ошибка скачивания фото")
        await message.answer(f"Не смог скачать твоё фото: {type(e).__name__}. Попробуй ещё раз.")
        return
    if downloaded is None:
        await message.answer("Что-то с файлом — попробуй отправить фото заново.")
        return
    image_bytes, unique_id = downloaded

    checking = await message.answer("⏳ Проверка чека...")

    # check_receipt — синхронная «тяжёлая» функция (OCR может идти секунды).
    # to_thread запускает её в отдельном потоке, чтобы бот не завис для других людей.
    try:
        res = await asyncio.to_thread(rc.check_receipt, image_bytes, user_id, unique_id)
    except Exception as e:
        log.exception("краш проверки чека")
        await checking.edit_text(
            f"💥 Внутри проверки произошла ошибка ({type(e).__name__}).\n"
            "Напиши администратору, мы посмотрим логи."
        )
        return

    await checking.delete()                      # убираем «Проверка чека...»

    if res.ok:
        await message.answer(
            "✅ ЧЕК ПРИНЯТ!\n\n"
            f"Я прочитал: {res.summary}.\n"
            f"🎁 Награда начисляется вручную администратором в течение 24 часов.\n"
            "Скинь, пожалуйста, свой ник в Roblox — передам его вместе с наградой.\n\n"
            f"(идентификатор чека: {res.receipt_id})"
        )
    else:
        await message.answer(
            f"❌ ЧЕК НЕ ПРИНЯТ.\n\n{res.reason}\n\n"
            "Если уверен, что с фото всё в порядке — отправь другое, более чёткое."
        )


@router.message(F.document)
async def handle_document(message: Message) -> None:
    """Чек часто приходит как «файл» (document), а не как фото. Обрабатываем и так."""
    mime = (message.document.mime_type or "")
    if not mime.startswith("image/"):
        await message.answer("Пришли чек картинкой 📷 (jpg или png), а не файлом документа.")
        return
    # Притворяемся фото: переиспользуем ту же логику, подменив скачивание.
    user_id = message.from_user.id
    if user_id not in welcomed_users:
        await try_greet(message)
    await _drop_instruction(user_id, message.chat.id, message.bot)

    try:
        file = await message.bot.get_file(message.document.file_id)
        image_bytes = await file.download_to_memory_bytes()
    except Exception as e:
        await message.answer(f"Не смог скачать файл: {type(e).__name__}")
        return

    checking = await message.answer("⏳ Проверка чека...")
    try:
        res = await asyncio.to_thread(
            rc.check_receipt, image_bytes, user_id,
            getattr(message.document, "file_unique_id", "") or "",
        )
    except Exception as e:
        log.exception("краш проверки чека")
        await checking.edit_text(f"💥 Ошибка проверки ({type(e).__name__}). Попробуй позже.")
        return

    await checking.delete()
    if res.ok:
        await message.answer(f"✅ ЧЕК ПРИНЯТ!\n\nЯ прочитал: {res.summary}.\n"
                             f"(идентификатор чека: {res.receipt_id})")
    else:
        await message.answer(f"❌ ЧЕК НЕ ПРИНЯТ.\n\n{res.reason}")

