"""Логика администрирования: админы, их права, тикеты техподдержки."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# --- Права администраторов ---
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


def get_admin_list_text() -> str:
    if not admins:
        return "Администраторов нет"
    lines = []
    for uid, info in admins.items():
        rights_text = ", ".join(RIGHT_LABELS[r] for r in info["rights"]) or "нет прав"
        lines.append(f"{info['name']} (id{uid}): {rights_text}")
    return "\n".join(lines)


# --- Тикеты техподдержки ---

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


# --- Клавиатуры ---

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
    
