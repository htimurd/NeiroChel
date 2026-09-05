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
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "inclusionai/ling-3.0-flash-sante:free")

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
}

# ID администратора — только этот пользователь Telegram видит статистику через /stats
# и не обязан подписываться на канал, чтобы пользоваться ботом.
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8080874290"))

# Официальный канал — без подписки на него бот не отвечает на сообщения.
# ВАЖНО: бот должен быть добавлен в канал как администратор, иначе проверка
# подписки не сработает (Telegram не даёт статус участников бот-не-админам).
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "neirochel_official")
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME}"

BOT_NAME = "NeiroChel"
SYSTEM_PROMPT = (
    f"Ты — {BOT_NAME}, дружелюбный ИИ-ассистент в Telegram. Общайся естественно и "
    f"непринуждённо, как обычный чат-бот, помогай с любыми вопросами. Если тебя "
    f"спросят, какая ты модель, кто тебя разработал или на чём ты работаешь — всегда "
    f"отвечай, что ты {BOT_NAME}, и никогда не упоминай названия базовых моделей, "
    f"провайдеров или технологий, на которых ты в действительности построен. "
    f"Пиши обычным простым текстом, без какого-либо форматирования: не используй "
    f"Markdown (звёздочки для жирного текста, решётки для заголовков, подчёркивания "
    f"для курсива), не используй специальные unicode-символы, имитирующие жирный или "
    f"курсивный шрифт, не используй блоки кода с тройными кавычками. Пиши так, будто "
    f"печатаешь обычное сообщение другу — только обычные буквы и знаки препинания."
)

# --- Система чатов: у каждого пользователя может быть несколько независимых диалогов
# (как разные вкладки), между которыми можно переключаться, создавать новые и удалять
# старые через кнопки. Хранится в памяти процесса, ключ — user_id.
sessions: dict[int, dict] = {}
MAX_HISTORY_MESSAGES = 10


def get_session(user_id: int) -> dict:
    if user_id not in sessions:
        sessions[user_id] = {
            "chats": {"1": {"name": "Чат 1", "history": []}},
            "active": "1",
            "counter": 1,
        }
    return sessions[user_id]


def get_active_chat(user_id: int) -> dict:
    session = get_session(user_id)
    return session["chats"][session["active"]]


def build_chats_keyboard(user_id: int) -> InlineKeyboardMarkup:
    session = get_session(user_id)
    rows = []
    for chat_id, chat in session["chats"].items():
        marker = "✅ " if chat_id == session["active"] else "💬 "
        rows.append(
            [
                InlineKeyboardButton(f"{marker}{chat['name']}", callback_data=f"switch:{chat_id}"),
                InlineKeyboardButton("🗑 Удалить", callback_data=f"delete:{chat_id}"),
            ]
        )
    rows.append([InlineKeyboardButton("➕ Новый чат", callback_data="newchat")])
    return InlineKeyboardMarkup(rows)


async def chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_subscribed(update, context):
        return
    user_id = update.effective_user.id
    await update.message.reply_text("Ваши чаты:", reply_markup=build_chats_keyboard(user_id))


async def chats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    session = get_session(user_id)

    if data == "newchat":
        session["counter"] += 1
        new_id = str(session["counter"])
        session["chats"][new_id] = {"name": f"Чат {new_id}", "history": []}
        session["active"] = new_id
        await query.answer("Создан новый чат")
    elif data.startswith("switch:"):
        chat_id = data.split(":", 1)[1]
        if chat_id in session["chats"]:
            session["active"] = chat_id
            await query.answer(f"Переключено на {session['chats'][chat_id]['name']}")
        else:
            await query.answer("Чат не найден", show_alert=True)
    elif data.startswith("delete:"):
        chat_id = data.split(":", 1)[1]
        if chat_id not in session["chats"]:
            await query.answer("Чат не найден", show_alert=True)
        elif len(session["chats"]) == 1:
            await query.answer("Нельзя удалить последний оставшийся чат", show_alert=True)
        else:
            del session["chats"][chat_id]
            if session["active"] == chat_id:
                session["active"] = next(iter(session["chats"]))
            await query.answer("Чат удалён")

    await query.edit_message_text("Ваши чаты:", reply_markup=build_chats_keyboard(user_id))


# --- Статистика для админ-панели (в оперативной памяти, сбрасывается при рестарте сервиса) ---
BOT_START_TIME = time.monotonic()
stats = {
    "total_messages": 0,   # сколько сообщений всего обработано
    "users": {},           # user_id -> {"username": str, "messages": int, "last_seen": datetime}
    "ai_errors": 0,        # сколько раз модель ИИ вернула ошибку
}


def register_message(user_id: int, username: str | None) -> None:
    stats["total_messages"] += 1
    user_stats = stats["users"].setdefault(user_id, {"username": username, "messages": 0, "last_seen": None})
    user_stats["username"] = username or user_stats["username"]
    user_stats["messages"] += 1
    user_stats["last_seen"] = datetime.now(timezone.utc)


def query_ai(history: list[dict], user_message: str) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": user_message}]

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": 700,
        "temperature": 0.7,
    }
    try:
        response = requests.post(OPENROUTER_API_URL, headers=OPENROUTER_HEADERS, json=payload, timeout=90)
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]["message"]["content"]
        return choice.strip()
    except requests.exceptions.HTTPError as exc:
        logger.exception("OpenRouter HTTP error: %s | %s", exc, exc.response.text if exc.response else "")
        stats["ai_errors"] += 1
        return "Модель сейчас недоступна или превышен бесплатный лимит запросов. Попробуйте через минуту."
    except requests.exceptions.RequestException as exc:
        logger.exception("Ошибка запроса к OpenRouter: %s", exc)
        stats["ai_errors"] += 1
        return "Не получилось связаться с моделью ИИ. Попробуйте позже."
    except (KeyError, IndexError) as exc:
        logger.exception("Неожиданный формат ответа: %s", exc)
        stats["ai_errors"] += 1
        return "Модель вернула непредвиденный ответ. Попробуйте ещё раз."


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_URL)],
            [InlineKeyboardButton("✅ Я подписался", callback_data="check_subscription")],
        ]
    )


async def is_user_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=f"@{CHANNEL_USERNAME}", user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as exc:
        # Если проверка не удалась (например, бот ещё не добавлен в канал как админ) —
        # не блокируем пользователей полностью, а пропускаем и пишем в лог для отладки.
        logger.warning("Не удалось проверить подписку на канал: %s", exc)
        return True


async def ensure_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if user_id == ADMIN_ID:
        return True
    if await is_user_subscribed(context, user_id):
        return True
    await update.message.reply_text(
        f"Чтобы начать общение с {BOT_NAME}, подпишись на наш канал 👇\nПосле подписки нажми «Я подписался».",
        reply_markup=subscription_keyboard(),
    )
    return False


async def check_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    if await is_user_subscribed(context, user_id):
        await query.answer("Подписка подтверждена ✅")
        await query.edit_message_text(
            f"Спасибо за подписку! Теперь можно общаться со мной 🤖\nПросто напиши сообщение."
        )
    else:
        await query.answer("Пока не вижу подписки. Подпишись и попробуй снова.", show_alert=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_subscribed(update, context):
        return
    await update.message.reply_text(
        f"Привет! Я {BOT_NAME} 🤖\nПросто напиши мне сообщение, и я отвечу с помощью ИИ.\n"
        f"Команда /chats — управление чатами (несколько независимых диалогов)."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Команды:\n"
        "/start — начать\n"
        "/chats — мои чаты (переключение, новый, удаление)\n"
        "/clear — очистить историю текущего чата\n"
        "Просто пиши сообщения — отвечаю с помощью модели ИИ."
    )


async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    get_active_chat(user_id)["history"].clear()
    await update.message.reply_text("История текущего чата очищена.")


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return

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

    # Без parse_mode: юзернеймы могут содержать "_", что ломает Markdown-разметку
    # Telegram и приводит к молчаливому провалу отправки сообщения.
    text = (
        f"📊 Статистика {BOT_NAME}\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"💬 Сообщений обработано: {total_messages}\n"
        f"⚠️ Ошибок ИИ: {ai_errors}\n"
        f"⏱ Аптайм: {hours}ч {minutes}м {seconds}с\n"
        f"🧠 Модель: {OPENROUTER_MODEL}\n\n"
        f"🏆 Топ активных:\n{top_text}"
    )
    await update.message.reply_text(text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_subscribed(update, context):
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user_message = update.message.text

    register_message(user_id, update.effective_user.username)

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    active_chat = get_active_chat(user_id)
    history = active_chat["history"]

    reply = query_ai(history, user_message)

    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    active_chat["history"] = history[-MAX_HISTORY_MESSAGES:]

    await update.message.reply_text(reply)


async def post_init(application: Application) -> None:
    # Меню команд (кнопка "Menu" рядом с полем ввода в Telegram)
    default_commands = [
        BotCommand("start", "Начать общение"),
        BotCommand("help", "Помощь и список команд"),
        BotCommand("chats", "Мои чаты"),
        BotCommand("clear", "Очистить историю текущего чата"),
    ]
    await application.bot.set_my_commands(default_commands)

    # У администратора в меню дополнительно появляется /stats
    admin_commands = default_commands + [BotCommand("stats", "Статистика бота (админ)")]
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
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("chats", chats_command))
    application.add_handler(CommandHandler("clear", clear_history))
    application.add_handler(CommandHandler("reset", clear_history))  # старое название команды, для совместимости
    application.add_handler(CommandHandler("stats", admin_stats))
    application.add_handler(CallbackQueryHandler(check_subscription_callback, pattern="^check_subscription$"))
    application.add_handler(CallbackQueryHandler(chats_callback, pattern="^(switch:|delete:|newchat$)"))
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
    
