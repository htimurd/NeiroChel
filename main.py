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
    f"Ты — {BOT_NAME}, ИИ-ассистент, работающий в виде бота в мессенджере Telegram. "
    f"Пользователи пишут тебе текстовые сообщения в личном чате, а ты отвечаешь текстом "
    f"в этом же чате — это единственный канал связи. Ты не помнишь ничего за пределами "
    f"текущей переписки. Общайся естественно, как обычный чат-бот. Если спросят, какая "
    f"ты модель или кто разработал — всегда отвечай, что ты {BOT_NAME}, никогда не "
    f"упоминай базовые модели или провайдеров. Пиши обычным текстом без Markdown-разметки "
    f"(без звёздочек, решёток, подчёркиваний) и без unicode-символов, имитирующих жирный "
    f"или курсивный шрифт."
)

# --- Данные ---
chat_history: dict[int, list[dict]] = {}
MAX_HISTORY_MESSAGES = 20

# Отложенное действие пользователя/админа: следующее текстовое сообщение будет
# перехвачено и обработано особым образом (а не как обычное сообщение к ИИ).
# user_id -> {"action": str, ...доп. данные}
pending_action: dict[int, dict] = {}

BOT_START_TIME = time.monotonic()
stats = {"total_messages": 0, "users": {}, "ai_errors": 0}


def user_label(user_id: int, username: str | None) -> str:
    return f"@{username}" if username else f"id{user_id}"


def register_message(user_id: int, username: str | None) -> None:
    stats["total_messages"] += 1
    user_stats = stats["users"].setdefault(user_id, {"username": username, "messages": 0, "last_seen": None})
    user_stats["username"] = username or user_stats["username"]
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
            logger.warning(
                "Модель %s недоступна (%s), пробуем следующую",
                model,
                exc.response.status_code if exc.response else "?",
            )
            last_error_text = "Модель сейчас недоступна. Попробуйте через минуту."
        except requests.exceptions.RequestException:
            logger.exception("Ошибка запроса к OpenRouter (модель %s)", model)
            last_error_text = "Не получилось связаться с моделью ИИ. Попробуйте позже."
        except (KeyError, IndexError):
            logger.exception("Неожиданный формат ответа от модели %s", model)
            last_error_text = "Модель вернула непредвиденный ответ. Попробуйте ещё раз."

    stats["ai_errors"] += 1
    return last_error_text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Перейти в ТГК", url=CHANNEL_URL)]])
    text = (
        f"Привет! Я {BOT_NAME}. Отвечу на любой вопрос.\n\n"
        f"⏳ После первого сообщения я могу отвечать не сразу — от 30 секунд до минуты, "
        f"это нормально, просто нужно немного подождать.\n\n"
        f"Если хотите поддержать нас подпишитесь на наш ТГК. Это не обязательно."
    )
    await update.message.reply_text(text, reply_markup=keyboard)


# --- Меню ---

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
        chat_history.pop(query.message.chat_id, None)
        await query.message.reply_text("✅ Память чата очищена.")

    elif data == "menu:support":
        pending_action[user_id] = {"action": "support_ticket"}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="menu:cancel")]])
        await query.edit_message_text(
            "🆘 Опишите вашу проблему одним сообщением — мы создадим тикет и администратор ответит вам прямо здесь.",
            reply_markup=keyboard,
        )

    elif data == "menu:send_msg":
        pending_action[user_id] = {"action": "send_msg_target"}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="menu:cancel")]])
        await query.edit_message_text(
            "Введите числовой Telegram ID пользователя, которому хотите написать:",
            reply_markup=keyboard,
        )

    elif data == "menu:admin":
        if not admin.is_admin(user_id):
            await query.answer("⛔ Нет доступа", show_alert=True)
            return
        await query.edit_message_text("🛠 Админ-панель", reply_markup=admin.admin_main_keyboard(user_id))

    elif data == "menu:cancel":
        pending_action.pop(user_id, None)
        await query.edit_message_text("Действие отменено.")


# --- Обработка обычных сообщений и отложенных действий ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username
    chat_id = update.effective_chat.id
    user_message = update.message.text

    if user_id in pending_action:
        action = pending_action.pop(user_id)
        await handle_pending_action(update, context, user_id, username, action, user_message)
        return

    register_message(user_id, username)
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    history = chat_history.setdefault(chat_id, [])
    reply = query_ai(history, user_message)

    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    chat_history[chat_id] = history[-MAX_HISTORY_MESSAGES:]

    await update.message.reply_text(reply)


async def handle_pending_action(update, context, user_id, username, action, text) -> None:
    kind = action["action"]

    if kind == "support_ticket":
        ticket_id = admin.create_ticket(user_id, username, text)
        await update.message.reply_text(f"✅ Тикет #{ticket_id} создан. Мы ответим вам прямо в этом чате.")
        for admin_id, info in admin.admins.items():
            if not admin.has_right(admin_id, "manage_tickets"):
                continue
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"🎫 Новый тикет #{ticket_id} от {user_label(user_id, username)}:\n\n{text}",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("📝 Ответить", callback_data=f"adm:ticket_reply:{ticket_id}")]]
                    ),
                )
            except Exception:
                logger.exception("Не удалось уведомить админа %s о новом тикете", admin_id)

    elif kind == "send_msg_target":
        pending_action[user_id] = {"action": "send_msg_text", "target_id": text.strip()}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="menu:cancel")]])
        await update.message.reply_text("Теперь напишите текст сообщения:", reply_markup=keyboard)

    elif kind == "send_msg_text":
        try:
            target_id = int(action["target_id"])
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом. Попробуйте ещё раз через меню.")
            return
        try:
            sent = await context.bot.send_message(
                chat_id=target_id,
                text=f"📬 Сообщение от {user_label(user_id, username)}:\n\n{text}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("📝 Ответить", callback_data=f"pm_reply:{user_id}"),
                            InlineKeyboardButton("🗑 Удалить", callback_data="pm_delete"),
                        ]
                    ]
                ),
            )
            await update.message.reply_text("✅ Сообщение отправлено.")
        except Exception as exc:
            logger.exception("Не удалось отправить сообщение пользователю %s", target_id)
            await update.message.reply_text(f"❌ Не удалось отправить: {exc}")

    elif kind == "admin_add_id":
        try:
            new_admin_id = int(text.strip())
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом.")
            return
        added = admin.add_admin(new_admin_id, f"id{new_admin_id}", rights=[])
        if added:
            await update.message.reply_text(
                f"✅ Пользователь id{new_admin_id} добавлен в администраторы (без прав — настройте их в списке админов)."
            )
        else:
            await update.message.reply_text("⚠️ Этот пользователь уже администратор.")

    elif kind == "admin_broadcast":
        sent, failed = 0, 0
        for uid in list(stats["users"].keys()):
            try:
                await context.bot.send_message(chat_id=uid, text=text)
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(f"📨 Рассылка завершена.\nОтправлено: {sent}\nОшибок: {failed}")

    elif kind == "ticket_reply":
        ticket_id = action["ticket_id"]
        ticket = admin.get_ticket(ticket_id)
        if not ticket:
            await update.message.reply_text("Тикет не найден (возможно, был закрыт).")
            return
        admin.add_ticket_message(ticket_id, "admin", text)
        try:
            await context.bot.send_message(
                chat_id=ticket["user_id"],
                text=f"💬 Ответ по тикету #{ticket_id}:\n\n{text}",
            )
            await update.message.reply_text("✅ Ответ отправлен пользователю.")
        except Exception as exc:
            await update.message.reply_text(f"❌ Не удалось отправить ответ: {exc}")


async def pm_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    await query.answer()

    if data.startswith("pm_reply:"):
        target_id = int(data.split(":")[1])
        pending_action[query.from_user.id] = {"action": "send_msg_text", "target_id": str(target_id)}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="menu:cancel")]])
        await query.message.reply_text("Напишите ответ:", reply_markup=keyboard)

    elif data == "pm_delete":
        await query.message.delete()


# --- Админ-панель ---

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not admin.is_admin(user_id):
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    await update.message.reply_text("🛠 Админ-панель", reply_markup=admin.admin_main_keyboard(user_id))


def build_stats_text() -> str:
    uptime_seconds = int(time.monotonic() - BOT_START_TIME)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    total_users = len(stats["users"])
    top_users = sorted(stats["users"].items(), key=lambda item: item[1]["messages"], reverse=True)[:5]
    top_lines = [f"  • {user_label(uid, info['username'])} — {info['messages']} сообщ." for uid, info in top_users]
    top_text = "\n".join(top_lines) if top_lines else "  (пока нет данных)"
    return (
        f"📊 Статистика {BOT_NAME}\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"💬 Сообщений обработано: {stats['total_messages']}\n"
        f"⚠️ Ошибок ИИ: {stats['ai_errors']}\n"
        f"⏱ Аптайм: {hours}ч {minutes}м {seconds}с\n"
        f"🎫 Открытых тикетов: {len(admin.get_open_tickets())}\n\n"
        f"🏆 Топ активных:\n{top_text}"
    )


def build_users_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(user_label(uid, info["username"]), callback_data=f"adm:user:{uid}")]
        for uid, info in stats["users"].items()
    ]
    if not rows:
        rows.append([InlineKeyboardButton("(пока нет пользователей)", callback_data="adm:main")])
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="adm:main")])
    return InlineKeyboardMarkup(rows)


def build_user_preview_text(user_id: int) -> str:
    info = stats["users"].get(user_id, {})
    label = user_label(user_id, info.get("username"))
    history = chat_history.get(user_id, [])[-10:]
    if not history:
        body = "(переписки с ИИ пока нет)"
    else:
        body = "\n".join(f"{'Пользователь' if m['role'] == 'user' else 'Бот'}: {m['content']}" for m in history)
    return f"💬 {label}\n\n{body}"


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    if not admin.is_admin(user_id):
        await query.answer("⛔ Нет доступа", show_alert=True)
        return

    data = query.data
    await query.answer()

    if data == "adm:main":
        await query.edit_message_text("🛠 Админ-панель", reply_markup=admin.admin_main_keyboard(user_id))

    elif data == "adm:stats":
        if not admin.has_right(user_id, "view_stats"):
            await query.answer("⛔ Нет прав", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="adm:main")]])
        await query.edit_message_text(build_stats_text(), reply_markup=keyboard)

    elif data == "adm:users":
        await query.edit_message_text("👤 Пользователи:", reply_markup=build_users_keyboard())

    elif data.startswith("adm:user:"):
        target_id = int(data.split(":")[2])
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✍️ Написать от имени бота", callback_data=f"adm:write:{target_id}")],
                [InlineKeyboardButton("🔙 К списку", callback_data="adm:users")],
            ]
        )
        await query.edit_message_text(build_user_preview_text(target_id), reply_markup=keyboard)

    elif data.startswith("adm:write:"):
        target_id = int(data.split(":")[2])
        pending_action[user_id] = {"action": "send_msg_text", "target_id": str(target_id)}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="menu:cancel")]])
        await query.edit_message_text(f"✍️ Напишите сообщение — уйдёт пользователю id{target_id} от имени бота:", reply_markup=keyboard)

    elif data == "adm:broadcast":
        if not admin.has_right(user_id, "broadcast"):
            await query.answer("⛔ Нет прав", show_alert=True)
            return
        pending_action[user_id] = {"action": "admin_broadcast"}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="menu:cancel")]])
        await query.edit_message_text("📨 Напишите текст рассылки для всех пользователей:", reply_markup=keyboard)

    # --- Тикеты ---
    elif data == "adm:tickets":
        if not admin.has_right(user_id, "manage_tickets"):
            await query.answer("⛔ Нет прав", show_alert=True)
            return
        await query.edit_message_text("🎫 Открытые тикеты:", reply_markup=admin.tickets_list_keyboard())

    elif data.startswith("adm:ticket:"):
        ticket_id = int(data.split(":")[2])
        await query.edit_message_text(admin.ticket_detail_text(ticket_id), reply_markup=admin.ticket_detail_keyboard(ticket_id))

    elif data.startswith("adm:ticket_reply:"):
        ticket_id = int(data.split(":")[2])
        if not admin.has_right(user_id, "manage_tickets"):
            await query.answer("⛔ Нет прав", show_alert=True)
            return
        pending_action[user_id] = {"action": "ticket_reply", "ticket_id": ticket_id}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="menu:cancel")]])
        await query.message.reply_text(f"Напишите ответ по тикету #{ticket_id}:", reply_markup=keyboard)

    elif data.startswith("adm:ticket_close:"):
        ticket_id = int(data.split(":")[2])
        admin.close_ticket(ticket_id)
        ticket = admin.get_ticket(ticket_id)
        if ticket:
            try:
                await context.bot.send_message(chat_id=ticket["user_id"], text=f"✅ Тикет #{ticket_id} закрыт администратором.")
            except Exception:
                pass
        await query.edit_message_text("🎫 Открытые тикеты:", reply_markup=admin.tickets_list_keyboard())

    # --- Управление админами ---
    elif data == "adm:manage_admins":
        if not admin.has_right(user_id, "manage_admins"):
            await query.answer("⛔ Нет прав", show_alert=True)
            return
        await query.edit_message_text("🛡 Администраторы:", reply_markup=admin.admins_list_keyboard())

    elif data == "adm:add_admin":
        if not admin.has_right(user_id, "manage_admins"):
            await query.answer("⛔ Нет прав", show_alert=True)
            return
        pending_action[user_id] = {"action": "admin_add_id"}
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="menu:cancel")]])
        await query.edit_message_text("Введите числовой Telegram ID нового администратора:", reply_markup=keyboard)

    elif data.startswith("adm:admin_detail:"):
        target_id = int(data.split(":")[2])
        await query.edit_message_text(
            f"🛡 Администратор: {admin.admins.get(target_id, {}).get('name', target_id)}\n\nПрава (нажмите, чтобы переключить):",
            reply_markup=admin.admin_detail_keyboard(target_id, MAIN_ADMIN_ID),
        )

    elif data.star
