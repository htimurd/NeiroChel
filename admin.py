from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
import logging

logger = logging.getLogger("NeiroChel")

# Хранение администраторов с их правами
# admin_id -> {"name": "username/id", "rights": ["manage_admins", "broadcast", "view_stats"]}
admins: dict[int, dict] = {}


def init_admin(main_admin_id: int) -> None:
    """Инициализация главного администратора"""
    global admins
    admins = {
        main_admin_id: {
            "name": f"id{main_admin_id}",
            "rights": ["manage_admins", "broadcast", "view_stats", "manage_messages"],
        }
    }


def is_admin(user_id: int) -> bool:
    """Проверить, является ли пользователь администратором"""
    return user_id in admins


def has_right(user_id: int, right: str) -> bool:
    """Проверить, есть ли у админа конкретное право"""
    if user_id not in admins:
        return False
    return right in admins[user_id].get("rights", [])


def add_admin(user_id: int, name: str, rights: list[str]) -> bool:
    """Добавить администратора"""
    if user_id not in admins:
        admins[user_id] = {"name": name, "rights": rights}
        return True
    return False


def remove_admin(user_id: int) -> bool:
    """Удалить администратора"""
    if user_id in admins and user_id not in [list(admins.keys())[0]]:  # не удалять главного админа
        del admins[user_id]
        return True
    return False


def update_admin_rights(user_id: int, rights: list[str]) -> bool:
    """Обновить права администратора"""
    if user_id in admins:
        admins[user_id]["rights"] = rights
        return True
    return False


def get_admin_list() -> str:
    """Получить список всех администраторов"""
    if not admins:
        return "Администраторов нет"
    lines = []
    for uid, info in admins.items():
        rights_text = ", ".join(info["rights"]) if info["rights"] else "нет прав"
        lines.append(f"{info['name']} (id{uid}): {rights_text}")
    return "\n".join(lines)


def admin_main_keyboard(main_admin_id: int) -> InlineKeyboardMarkup:
    """Главное меню админки"""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Статистика", callback_data="adm:stats")],
            [InlineKeyboardButton("👥 Управление админами", callback_data="adm:manage_admins")],
            [InlineKeyboardButton("👤 Пользователи", callback_data="adm:users")],
            [InlineKeyboardButton("📨 Рассылка", callback_data="adm:broadcast")],
        ]
    )


async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, main_admin_id: int) -> None:
    """Показать главное меню админки"""
    await update.message.reply_text("🛠 Админ-панель", reply_markup=admin_main_keyboard(main_admin_id))
    
