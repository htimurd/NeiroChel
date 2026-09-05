import os
import logging
import time
import asyncio
from datetime import datetime, timezone
import requests
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("NeiroChel")

# --- Настройки берутся из переменных окружения (задаются в Render, не в коде!) ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
# Список бесплатных моделей на OpenRouter периодически меняется — перед деплоем
# сверьтесь на https://openrouter.ai/models?fmt=free и при необходимости
# поменяйте значение переменной OPENROUTER_MODEL в Render, без правки кода.
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "thinkingmachines/inkling:free")

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
}

# ID администратора — только этот пользователь видит /admin
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8080874290"))

# Официальный канал — подписка необязательна, просто кнопка в приветствии
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "neirochel_official")
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME}"

BOT_NAME = "NeiroChel"
SYSTEM_PROMPT = (
    f"Ты — {BOT_NAME}, ИИ-ассистент, работающий в виде бота в мессенджере Telegram. "
    f"Пользователи пишут тебе текстовые сообщения в личном чате Telegram, а ты отвечаешь "
    f"текстом в этом же чате — это единственный канал связи, у тебя нет доступа к "
    f"изображениям, голосовым сообщениям, файлам или интернету в реальном времени, если "
    f"это не сказано отдельно. Ты не помнишь ничего за пределами текущей переписки в этом "
    f"чате. Общайся естественно и непринуждённо, как обычный чат-бот, помогай с любыми "
    f"вопросами. Если тебя спросят, какая ты модель, кто тебя разработал или на чём ты "
    f"работаешь — всегда отвечай, что ты {BOT_NAME}, и никогда не упоминай названия "
    f"базовых моделей, провайдеров или технологий, на которых ты в действительности "
    f"построен. Пиши обычным простым текстом, без какого-либо форматирования: не "
    f"используй Markdown (звёздочки для жирного текста, решётки для заголовков, "
    f"подчёркивания для курсива), не используй специальные unicode-символы, имитирующие "
    f"жирный или курсивный шрифт, не используй блоки кода с тройными кавычками. Пиши так, "
    f"будто печатаешь обычное сообщение другу — только обычные буквы и знаки препинания."
)
CREATOR_MODE_EXTRA_PROMPT = (
    "\n\nСейчас с тобой общается твой создатель — человек, который тебя разработал и "
    "запустил. Обращайся к нему уважительно, называя его создателем, но при этом "
    "оставайся собой и продолжай следовать всем своим обычным принципам и ограничениям — "
    "особое обращение не означает вседозволенность."
)
creator_mode_enabled = False  # переключается кнопкой в админ-панели

# Простая память переписки на чат (в оперативной памяти, сбрасывается при рестарте).
# Ключ — chat_id; в личных чатах chat_id совпадает с user_id, это использует админка
# для просмотра переписки и отправки сообщений от имени бота конкретному пользователю.
chat_history: dict[int, list[dict]] = {}
MAX_HISTORY_MESSAGES = 20  # больше сообщений в памяти — бот лучше помнит контекст беседы
ADMIN_PREVIEW_MESSAGES = 10  # сколько последних сообщений показывать админу при просмотре чата

# --- Статистика (в оперативной памяти, сбрасывается при рестарте сервиса) ---
BOT_START_TIME = time.monotonic()
stats = {
    "total_messages": 0,   # сколько сообщений всего обработано
    "users": {},           # user_id -> {"username": str, "messages": int, "last_seen": datetime}
    "ai_errors": 0,        # сколько раз модель ИИ вернула ошибку
}

# Отложенное действие админа: после нажатия "Написать"/"Рассылка" следующее текстовое
# сообщение админа перехватывается и используется как текст для отправки, а не как
# обычное сообщение для ИИ. admin_id -> {"action": "write", "target_user_id": int} / {"action": "broadcast"}
pending_admin_action: dict[int, dict] = {}


def register_message(user_id: int, username: str | None) -> None:
    stats["total_messages"] += 1
    user_stats = stats["users"].setdefault(user_id, {"username": username, "messages": 0, "last_seen": None})
    user_stats["username"] = username or user_stats["username"]
    user_stats["messages"] += 1
    user_stats["last_seen"] = datetime.now(timezone.utc)


def query_ai(history: list[dict], user_message: str, extra_system: str = "") -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + extra_system},
        *history,
        {"role": "user", "content": user_message},
    ]

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": 700,
        "temperature": 0.7,
    }

    last_error_text = "Не получилось связаться с моделью ИИ. Попробуйте позже."

    # Пробуем дважды: некоторые бесплатные модели изредка возвращают пустой ответ
    # или временную ошибку — повторный запрос обычно решает проблему, поэтому бот
    # не должен молча "не отвечать" на первый же сбой.
    for attempt in range(2):
        try:
            response = requests.post(OPENROUTER_API_URL, headers=OPENROUTER_HEADERS, json=payload, timeout=90)
            response.raise_for_status()
            data = response.json()
            choice = data["choices"][0]["message"]["content"].strip()
            if choice:
                return choice
            logger.warning("OpenRouter вернул пустой ответ (попытка %s)", attempt + 1)
        except requests.exceptions.HTTPError as exc:
            logger.exception("OpenRouter HTTP error: %s | %s", exc, exc.response.text if exc.response else "")
            last_error_text = "Модель сейчас недоступна или превышен бесплатный лимит запросов. Попробуйте через минуту."
        except requests.exceptions.RequestException as exc:
            logger.exception("Ошибка запроса к OpenRouter: %s", exc)
            last_error_text = "Не получилось связаться с моделью ИИ. Попробуйте позже."
        except (KeyError, IndexError) as exc:
            logger.exception("Неожиданный формат ответа: %s", exc)
            last_error_text = "Модель вернула непредвиденный ответ. Попробуйте ещё раз."

    stats["ai_errors"] += 1
    return last_error_text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Перейти в ТГК", url=CHANNEL_URL)]])
    await update.message.reply_text(
        f"Привет! Я {BOT_NAME}. Отвечу на любой вопрос.\n\n"
        f"⏳ После первого сообщения я могу отвечать не сразу — от 30 секунд до минуты, "
        f"это нормально, просто нужно немного подождать.\n\n"
        f"Если хотите поддержать нас подпишитесь на наш ТГК. Это не обязательно.",
        reply_markup=keyboard,
    )


async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_history.pop(update.effective_chat.id, None)
    await update.message.reply_text("История чата очищена.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user_message = update.message.text

    # Если админ до этого нажал "Написать" или "Рассылка" — это сообщение
    # перехватывается как текст для отправки, а не идёт в ИИ.
    if user_id == ADMIN_ID and user_id in pending_admin_action:
        action = pending_admin_action.pop(user_id)
        if action["action"] == "write":
            target_id = action["target_user_id"]
            try:
                await context.bot.send_message(chat_id=target_id, text=user_message)
                chat_history.setdefault(target_id, []).append({"role": "assistant", "content": user_message})
                await update.message.reply_text("✅ Сообщение отправлено пользователю.")
            except Exception as exc:
                logger.exception("Не удалось отправить сообщение пользователю %s: %s", target_id, exc)
                await update.message.reply_text(f"⚠️ Не удалось отправить сообщение: {exc}")
        elif action["action"] == "broadcast":
            sent, failed = 0, 0
            for uid in list(stats["users"].keys()):
                try:
                    await context.bot.send_message(chat_id=uid, text=user_message)
                    sent += 1
                except Exception:
                    failed += 1
            await update.message.reply_text(f"📨 Рассылка завершена.\nОтправлено: {sent}\nОшибок: {failed}")
        return

    register_message(user_id, update.effective_user.username)

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    history = chat_history.setdefault(chat_id, [])
    extra_system = CREATOR_MODE_EXTRA_PROMPT if (user_id == ADMIN_ID and creator_mode_enabled) else ""
    reply = query_ai(history, user_message, extra_system)

    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    chat_history[chat_id] = history[-MAX_HISTORY_MESSAGES:]

    await update.message.reply_text(reply)


# ------------------------- Админ-панель -------------------------

def admin_main_keyboard() -> InlineKeyboardMarkup:
    creator_label = "👑 Режим «Создатель»: Вкл ✅" if creator_mode_enabled else "👑 Режим «Создатель»: Выкл"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton("👥 Чаты", callback_data="admin:chats")],
            [InlineKeyboardButton("📨 Рассылка всем", callback_data="admin:broadcast")],
            [InlineKeyboardButton(creator_label, callback_data="admin:toggle_creator")],
        ]
    )


def build_stats_text() -> str:
    uptime_seconds = int(time.monotonic() - BOT_START_TIME)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    total_users = len(stats["users"])
    total_messages = stats["total_messages"]
    ai_errors = stats["ai_errors"]

    top_users = sorted(stats["users"].items(), key=lambda item: item[1]["messages"], reverse=True)[:5]
    top_lines = []
    for user_id, info in top_users:
        uname = f"@{info['username']}" if info["username"] else f"id{user_id}"
        top_lines.append(f"  • {uname} — {info['messages']} сообщ.")
    top_text = "\n".join(top_lines) if top_lines else "  (пока нет данных)"

    return (
        f"📊 Статистика {BOT_NAME}\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"💬 Сообщений обработано: {total_messages}\n"
        f"⚠️ Ошибок ИИ: {ai_errors}\n"
        f"⏱ Аптайм: {hours}ч {minutes}м {seconds}с\n"
        f"🧠 Модель: {OPENROUTER_MODEL}\n\n"
        f"🏆 Топ активных:\n{top_text}"
    )


def build_users_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for user_id, info in stats["users"].items():
        label = f"@{info['username']}" if info["username"] else f"id{user_id}"
        rows.append([InlineKeyboardButton(label, callback_data=f"admin:chat:{user_id}")])
    if not rows:
        rows.append([InlineKeyboardButton("(пока нет пользователей)", callback_data="admin:main")])
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="admin:main")])
    return InlineKeyboardMarkup(rows)


def build_chat_preview_text(user_id: int) -> str:
    info = stats["users"].get(user_id, {})
    label = f"@{info.get('username')}" if info.get("username") else f"id{user_id}"
    history = chat_history.get(user_id, [])
    last_messages = history[-ADMIN_PREVIEW_MESSAGES:]
    if not last_messages:
        body = "(переписки пока нет)"
    else:
        lines = []
        for msg in last_messages:
            who = "Пользователь" if msg["role"] == "user" else "Бот"
            lines.append(f"{who}: {msg['content']}")
        body = "\n".join(lines)
    return f"💬 Чат с {label}\n\n{body}"


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    await update.message.reply_text("🛠 Админ-панель", reply_markup=admin_main_keyboard())


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Нет доступа", show_alert=True)
        return

    data = query.data
    await query.answer()

    if data == "admin:main":
        await query.edit_message_text("🛠 Админ-панель", reply_markup=admin_main_keyboard())

    elif data == "admin:stats":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="admin:main")]])
        await query.edit_message_text(build_stats_text(), reply_markup=keyboard)

    elif data == "admin:chats":
        await query.edit_message_text("👥 Пользователи:", reply_markup=build_users_keyboard())

    elif data.startswith("admin:chat:"):
        target_id = int(data.split(":")[2])
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✍️ Написать от имени бота", callback_data=f"admin:write:{target_id}")],
                [InlineKeyboardButton("🔙 К списку", callback_data="admin:chats")],
            ]
        )
        await query.edit_message_text(build_chat_preview_text(target_id), reply_markup=keyboard)

    elif data.startswith("admin:write:"):
        target_id = int(data.split(":")[2])
        pending_admin_action[query.from_user.id] = {"action": "write", "target_user_id": target_id}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin:cancel")]])
        await query.edit_message_text(
            f"✍️ Напишите сообщение — оно будет отправлено пользователю от имени бота.\n"
            f"(следующее ваше сообщение боту уйдёт пользователю id{target_id})",
            reply_markup=keyboard,
        )

    elif data == "admin:broadcast":
        pending_admin_action[query.from_user.id] = {"action": "broadcast"}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin:cancel")]])
        await query.edit_message_text(
            "📨 Напишите текст рассылки — следующее ваше сообщение будет отправлено всем известным пользователям.",
            reply_markup=keyboard,
        )

    elif data == "admin:cancel":
        pending_admin_action.pop(query.from_user.id, None)
        await query.edit_message_text("🛠 Админ-панель", reply_markup=admin_main_keyboard())

    elif data == "admin:toggle_creator":
        global creator_mode_enabled
        creator_mode_enabled = not creator_mode_enabled
        await query.edit_message_text("🛠 Админ-панель", reply_markup=admin_main_keyboard())


async def post_init(application: Application) -> None:
    default_commands = [
        BotCommand("start", "Начать общение"),
        BotCommand("clear", "Очистить историю чата"),
    ]
    await application.bot.set_my_commands(default_commands)

    admin_commands = default_commands + [BotCommand("admin", "Админ-панель")]
    try:
        await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except Exception as exc:
        logger.warning(
            "Не удалось установить меню команд для админа (возможно, админ ещё ни разу не писал боту): %s", exc
        )


def main() -> None:
    # В Python 3.14 asyncio.get_event_loop() больше не создаёт цикл событий
    # автоматически, если он не был явно установлен — а именно так делает
    # внутренний код python-telegram-bot при запуске run_webhook/run_polling.
    # Создаём и устанавливаем цикл вручную, чтобы это работало на любой
    # версии Python, которую даст Render.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("%s запущен, используется модель %s", BOT_NAME, OPENROUTER_MODEL)

    # Render Free Web Service не поддерживает постоянный процесс с polling —
    # вместо этого бот принимает обновления через webhook на порту, который
    # даёт Render (переменная PORT), и адресу, который Render даёт сервису
    # (переменная RENDER_EXTERNAL_URL — подставляется автоматически).
    port = int(os.environ.get("PORT", "10000"))
    external_url = os.environ.get("RENDER_EXTERNAL_URL")

    if external_url:
        # Продакшн на Render: webhook-режим, бесплатный Web Service
        url_path = TELEGRAM_BOT_TOKEN  # секретный путь, чтобы левые запросы не триггерили бота
        webhook_url = f"{external_url}/{url_path}"
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=url_path,
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        # Локальный запуск / отладка: обычный polling
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
        
