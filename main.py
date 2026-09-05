import os
import logging
import time
import asyncio
from datetime import datetime, timezone
import requests
from telegram import Update
from telegram.ext import (
    Application,
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
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8080874290"))

BOT_NAME = "NeiroChel"
SYSTEM_PROMPT = f"Ты — {BOT_NAME}, дружелюбный и полезный ассистент."

# Простая память последних сообщений на чат (в оперативной памяти, сбрасывается при рестарте)
chat_history: dict[int, list[dict]] = {}
MAX_HISTORY_MESSAGES = 10

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


def query_ai(chat_id: int, user_message: str) -> str:
    history = chat_history.get(chat_id, [])
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Привет! Я {BOT_NAME} 🤖\nПросто напиши мне сообщение, и я отвечу с помощью ИИ."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Команды:\n/start — начать\n/reset — очистить историю диалога\n"
        "Просто пиши сообщения — отвечаю с помощью модели ИИ."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_history.pop(update.effective_chat.id, None)
    await update.message.reply_text("История диалога очищена.")


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

    text = (
        f"📊 *Статистика {BOT_NAME}*\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"💬 Сообщений обработано: {total_messages}\n"
        f"⚠️ Ошибок ИИ: {ai_errors}\n"
        f"⏱ Аптайм: {hours}ч {minutes}м {seconds}с\n"
        f"🧠 Модель: {OPENROUTER_MODEL}\n\n"
        f"🏆 Топ активных:\n{top_text}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_message = update.message.text

    register_message(update.effective_user.id, update.effective_user.username)

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    reply = query_ai(chat_id, user_message)

    history = chat_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    chat_history[chat_id] = history[-MAX_HISTORY_MESSAGES:]

    await update.message.reply_text(reply)


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

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("stats", admin_stats))
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
    
