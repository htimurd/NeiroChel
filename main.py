import os
import logging
import time
import asyncio
from datetime import datetime, timezone
import requests
import admin
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

# --- Настройки ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODELS = os.environ.get(
    "OPENROUTER_MODELS",
    "google/gemma-4-31b-it:free,"
    "minimax/minimax-m3:free,"
    "nvidia/nemotron-3-ultra-550b-a55b:free,"
    "thinkingmachines/inkling:free",
).split(",")

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
}

MAIN_ADMIN_ID = int(os.environ.get("ADMIN_ID", "8080874290"))
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "neirochel_official")
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME}"

BOT_NAME = "NeiroChel"
SYSTEM_PROMPT = (
    f"Ты — {BOT_NAME}, ИИ-ассистент в Telegram. Пиши обычным текстом без форматирования. "
    f"Всегда называй себя {BOT_NAME}, не упоминай базовые модели или провайдеров."
)

# --- Данные ---
chat_history: dict[int, list[dict]] = {}
MAX_HISTORY_MESSAGES = 20
pending_admin_action: dict[int, dict] = {}
forwarded_messages: dict[str, dict] = {}  # message_id -> {"from_id": int, "text": str, "timestamp": float}

BOT_START_TIME = time.monotonic()
stats = {"total_messages": 0, "users": {}, "ai_errors": 0}


def register_message(user_id: int, username: str | None = None) -> None:
    stats["total_messages"] += 1
    user_stats = stats["users"].setdefault(user_id, {"username": username or f"id{user_id}", "messages": 0, "last_seen": None})
    user_stats["username"] = username or f"id{user_id}"
    user_stats["messages"] += 1
    user_stats["last_seen"] = datetime.now(timezone.utc)


def query_ai(history: list[dict], user_message: str) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": user_message}]
    last_error_text = "Не получилось связаться с моделью ИИ. Попробуйте позже."

    for model in OPENROUTER_MODELS:
        payload = {"model": model, "messages": messages, "max_tokens": 700, "temperature": 0.7}
        try:
            response = requests.post(OPENROUTER_API_URL, headers=OPENROUTER_HEADERS, json=payload, timeout=90)
            response.raise_for_status()
            data = response.json()
            choice = data["choices"][0]["message"]["content"].strip()
            if choice:
                return choice
            logger.warning("Модель %s вернула пустой ответ, пробуем следующую", model)
        except requests.exceptions.HTTPError as exc:
            logger.warning("Модель %s недоступна, пробуем следующую", model)
            last_error_text = "Модель сейчас недоступна. Попробуйте через минуту."
        except Exception as exc:
            logger.exception("Ошибка запроса к OpenRouter (модель %s)", model)
            last_error_text = "Ошибка при запросе к ИИ. Попробуйте позже."

    stats["ai_errors"] += 1
    return last_error_text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Перейти в ТГК", url=CHANNEL_URL)]])
    text = (
        f"Привет! Я {BOT_NAME}. Отвечу на любой вопрос.\n\n"
        f"⏳ Первый ответ может занять 30 секунд - минуту.\n\n"
        f"Поддержи нас - подпишись на канал @{CHANNEL_USERNAME} (необязательно)."
    )
    await update.message.reply_text(text, reply_markup=keyboard)


def build_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🧹 Очистить память бота", callback_data="menu:clear")],
        [InlineKeyboardButton("💬 Написать другому пользователю", callback_data="menu:send_msg")],
        [InlineKeyboardButton("🆘 Техподдержка", callback_data="menu:support")],
    ]
    if admin.is_admin(user_id):
        rows.append([InlineKeyboardButton("🛠 Админ-панель", callback_data="menu:admin")])
    return InlineKeyboardMarkup(rows)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("📋 Меню:", reply_markup=build_menu_keyboard(update.effective_user.id))


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    await query.answer()

    if data == "menu:clear":
        chat_history.clear()
        await query.message.reply_text("✅ Память бота очищена.")

    elif data == "menu:support":
        await query.message.reply_text("🆘 Техподдержка: свяжитесь через канал @{CHANNEL_USERNAME}")

    elif data == "menu:send_msg":
        pending_admin_action[user_id] = {"action": "send_msg_userid"}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="menu:cancel")]])
        await query.edit_message_text(
            "Введите ID или юзернейм пользователя, которому хотите написать:",
            reply_markup=keyboard,
        )

    elif data == "menu:admin":
        if not admin.is_admin(user_id):
            await query.answer("⛔ Нет доступа", show_alert=True)
            return
        await query.edit_message_text("🛠 Админ-панель", reply_markup=admin.admin_main_keyboard(MAIN_ADMIN_ID))

    elif data == "menu:cancel":
        pending_admin_action.pop(user_id, None)
        await query.edit_message_text("Действие отменено.")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin.is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    await update.message.reply_text("🛠 Админ-панель", reply_markup=admin.admin_main_keyboard(MAIN_ADMIN_ID))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user_message = update.message.text

    # Проверка ожидающего действия
    if user_id in pending_admin_action:
        action = pending_admin_action.pop(user_id)
        if action["action"] == "send_msg_userid":
            target_id = user_message
            pending_admin_action[user_id] = {"action": "send_msg_text", "target_id": target_id}
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="menu:cancel")]])
            await update.message.reply_text("Напишите сообщение для отправки:", reply_markup=keyboard)
            return
        elif action["action"] == "send_msg_text":
            try:
                target_id = int(action["target_id"])
                msg = await context.bot.send_message(
                    chat_id=target_id,
                    text=f"📬 Сообщение от пользователя:\n\n{user_message}",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("📝 Ответить", callback_data=f"msg_reply:{user_id}"),
                          InlineKeyboardButton("🗑 Удалить", callback_data="msg_delete")]]
                    ),
                )
                forwarded_messages[str(msg.message_id)] = {"from_id": user_id, "text": user_message, "timestamp": time.time()}
                await update.message.reply_text("✅ Сообщение отправлено.")
            except Exception as exc:
                await update.message.reply_text(f"❌ Ошибка: {exc}")
            return

    register_message(user_id, update.effective_user.username)
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    history = chat_history.setdefault(chat_id, [])
    reply = query_ai(history, user_message)

    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    chat_history[chat_id] = history[-MAX_HISTORY_MESSAGES:]

    await update.message.reply_text(reply)


async def message_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data

    if data.startswith("msg_reply:"):
        target_id = int(data.split(":")[1])
        pending_admin_action[query.from_user.id] = {"action": "send_msg_text", "target_id": str(target_id)}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="menu:cancel")]])
        await query.edit_message_text("Напишите ответ:", reply_markup=keyboard)

    elif data == "msg_delete":
        await query.message.delete()
        await query.answer("Сообщение удалено.")


async def post_init(application: Application) -> None:
    admin.init_admin(MAIN_ADMIN_ID)
    commands = [
        BotCommand("start", "Начать"),
        BotCommand("menu", "Меню"),
    ]
    await application.bot.set_my_commands(commands)

    admin_commands = commands + [BotCommand("admin", "Админ-панель")]
    try:
        await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=MAIN_ADMIN_ID))
    except Exception as exc:
        logger.warning("Не удалось установить меню для админа: %s", exc)


def main() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu:"))
    application.add_handler(CallbackQueryHandler(message_button_callback, pattern="^msg_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("%s запущен", BOT_NAME)

    port = int(os.environ.get("PORT", "10000"))
    external_url = os.environ.get("RENDER_EXTERNAL_URL")

    if external_url:
        url_path = TELEGRAM_BOT_TOKEN
        webhook_url = f"{external_url}/{url_path}"
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=url_path,
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
    
