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

# =====================================================================
# АДМИНКА: администраторы, права, тикеты техподдержки (всё в этом файле)
# =====================================================================

RIGHTS = ["view_stats", "broadcast", "manage_admins", "manage_tickets"]
RIGHT_LABELS = {
    "view_stats": "📊 Статистика",
    "broadcast": "📨 Рассылка",
    "manage_admins": "🛡 Управление админами",
    "manage_tickets": "🎫 Тикеты",
}

# user_id -> {"name": str, "rights": [str, ...]}
admins: dict[int, dict] = {}

# ticket_id -> {"id", "user_id", "username", "status", "messages": [{"from","text"}]}
tickets: dict[int, dict] = {}
_next_ticket_id = 1


def init_admin(main_admin_id: int) -> None:
    """Инициализация главного администратора со всеми правами."""
    global admins
    admins = {main_admin_id: {"name": f"id{main_admin_id}", "rights": list(RIGHTS)}}


def is_admin(user_id: int) -> bool:
    return user_id in admins


def has_right(user_id: int, right: str) -> bool:
    return right in admins.get(user_id, {}).get("rights", [])


def add_admin(user_id: int, name: str, rights: list[str] | None = None) -> bool:
    if user_id in admins:
        return False
    admins[user_id] = {"name": name, "rights": rights or []}
    return True


def remove_admin(user_id: int, main_admin_id: int) -> bool:
    if user_id == main_admin_id:
        return False  # главного админа снять нельзя
    if user_id in admins:
        del admins[user_id]
        return True
    return False


def toggle_right(user_id: int, right: str) -> None:
    if user_id not in admins:
        return
    rights = admins[user_id]["rights"]
    if right in rights:
        rights.remove(right)
    else:
        rights.append(right)


def create_ticket(user_id: int, username: str | None, message: str) -> int:
    global _next_ticket_id
    tid = _next_ticket_id
    _next_ticket_id += 1
    tickets[tid] = {
        "id": tid,
        "user_id": user_id,
        "username": f"@{username}" if username else f"id{user_id}",
        "status": "open",
        "messages": [{"from": "user", "text": message}],
    }
    return tid


def get_ticket(ticket_id: int) -> dict | None:
    return tickets.get(ticket_id)


def get_open_tickets() -> dict:
    return {tid: t for tid, t in tickets.items() if t["status"] == "open"}


def add_ticket_message(ticket_id: int, from_role: str, text: str) -> None:
    if ticket_id in tickets:
        tickets[ticket_id]["messages"].append({"from": from_role, "text": text})


def close_ticket(ticket_id: int) -> bool:
    if ticket_id in tickets:
        tickets[ticket_id]["status"] = "closed"
        return True
    return False


def admin_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    if has_right(user_id, "view_stats"):
        rows.append([InlineKeyboardButton("📊 Статистика", callback_data="adm:stats")])
    rows.append([InlineKeyboardButton("👤 Пользователи", callback_data="adm:users")])
    if has_right(user_id, "manage_tickets"):
        open_count = len(get_open_tickets())
        label = f"🎫 Тикеты ({open_count})" if open_count else "🎫 Тикеты"
        rows.append([InlineKeyboardButton(label, callback_data="adm:tickets")])
    if has_right(user_id, "broadcast"):
        rows.append([InlineKeyboardButton("📨 Рассылка", callback_data="adm:broadcast")])
    if has_right(user_id, "manage_admins"):
        rows.append([InlineKeyboardButton("🛡 Управление админами", callback_data="adm:manage_admins")])
    return InlineKeyboardMarkup(rows)


def admins_list_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(info["name"], callback_data=f"adm:admin_detail:{uid}")] for uid, info in admins.items()]
    rows.append([InlineKeyboardButton("➕ Добавить админа", callback_data="adm:add_admin")])
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="adm:main")])
    return InlineKeyboardMarkup(rows)


def admin_detail_keyboard(uid: int, main_admin_id: int) -> InlineKeyboardMarkup:
    info = admins.get(uid, {"rights": []})
    rows = []
    for right in RIGHTS:
        mark = "✅" if right in info["rights"] else "◻️"
        rows.append([InlineKeyboardButton(f"{mark} {RIGHT_LABELS[right]}", callback_data=f"adm:toggle_right:{uid}:{right}")])
    if uid != main_admin_id:
        rows.append([InlineKeyboardButton("🗑 Снять администратора", callback_data=f"adm:remove_admin:{uid}")])
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="adm:manage_admins")])
    return InlineKeyboardMarkup(rows)


def tickets_list_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🎫 #{tid} — {t['username']}", callback_data=f"adm:ticket:{tid}")]
        for tid, t in get_open_tickets().items()
    ]
    if not rows:
        rows.append([InlineKeyboardButton("(открытых тикетов нет)", callback_data="adm:main")])
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="adm:main")])
    return InlineKeyboardMarkup(rows)


def ticket_detail_text(ticket_id: int) -> str:
    t = tickets.get(ticket_id)
    if not t:
        return "Тикет не найден."
    lines = [f"🎫 Тикет #{ticket_id} — {t['username']} (статус: {t['status']})", ""]
    for m in t["messages"]:
        who = "Пользователь" if m["from"] == "user" else "Админ"
        lines.append(f"{who}: {m['text']}")
    return "\n".join(lines)


def ticket_detail_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    t = tickets.get(ticket_id)
    rows = [[InlineKeyboardButton("📝 Ответить", callback_data=f"adm:ticket_reply:{ticket_id}")]]
    if t and t["status"] == "open":
        rows.append([InlineKeyboardButton("✅ Закрыть тикет", callback_data=f"adm:ticket_close:{ticket_id}")])
    rows.append([InlineKeyboardButton("🔙 К тикетам", callback_data="adm:tickets")])
    return InlineKeyboardMarkup(rows)


# =====================================================================
# ОСНОВНОЙ БОТ
# =====================================================================

chat_history: dict[int, list[dict]] = {}
MAX_HISTORY_MESSAGES = 20

# Отложенное действие: следующее текстовое сообщение будет перехвачено
# и обработано особым образом (а не как обычное сообщение к ИИ).
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


def build_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🧹 Очистить память бота", callback_data="menu:clear")],
        [InlineKeyboardButton("💬 Написать другому пользователю", callback_data="menu:send_msg")],
        [InlineKeyboardButton("🆘 Техподдержка", callback_data="menu:support")],
    ]
    if is_admin(user_id):
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
        if not is_admin(user_id):
            await query.answer("⛔ Нет доступа", show_alert=True)
            return
        await query.edit_message_text("🛠 Админ-панель", reply_markup=admin_main_keyboard(user_id))

    elif data == "menu:cancel":
        pending_action.pop(user_id, None)
        await query.edit_message_text("Действие отменено.")


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
        ticket_id = create_ticket(user_id, username, text)
        await update.message.reply_text(f"✅ Тикет #{ticket_id} создан. Мы ответим вам прямо в этом чате.")
        for admin_id in admins:
            if not has_right(admin_id, "manage_tickets"):
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
            await context.bot.send_message(
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
        added = add_admin(new_admin_id, f"id{new_admin_id}", rights=[])
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
        ticket = get_ticket(ticket_id)
        if not ticket:
            await update.message.reply_text("Тикет не найден (возможно, был закрыт).")
            return
        add_ticket_message(ticket_id, "admin", text)
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


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    await update.message.reply_text("🛠 Админ-панель", reply_markup=admin_main_keyboard(user_id))


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
        f"🎫 Открытых тикетов: {len(get_open_tickets())}\n\n"
        f"🏆 Топ активных:\n{top_text}"
    )


def build_users_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(user_lab
