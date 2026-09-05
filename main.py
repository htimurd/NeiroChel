import os
import logging
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
logger = logging.getLogger("KangerAI")

# --- Настройки берутся из переменных окружения (задаются в Render, не в коде!) ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
HF_API_KEY = os.environ["HF_API_KEY"]
HF_MODEL = os.environ.get("HF_MODEL", "HuggingFaceH4/zephyr-7b-beta")

HF_API_URL = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
HF_HEADERS = {"Authorization": f"Bearer {HF_API_KEY}"}

BOT_NAME = "NeiroChel"

# Простая память последних сообщений на чат (в оперативной памяти, сбрасывается при рестарте)
chat_history: dict[int, list[str]] = {}
MAX_HISTORY_MESSAGES = 6


def build_prompt(chat_id: int, user_message: str) -> str:
    history = chat_history.get(chat_id, [])
    history_text = "\n".join(history)
    prompt = (
        f"Ты — {BOT_NAME}, дружелюбный и полезный ассистент.\n"
        f"{history_text}\n"
        f"Пользователь: {user_message}\n"
        f"{BOT_NAME}:"
    )
    return prompt


def query_huggingface(prompt: str) -> str:
    payload = {
        "inputs": prompt,
        "parameters": {
            "max_new_tokens": 300,
            "temperature": 0.7,
            "return_full_text": False,
        },
        "options": {"wait_for_model": True},
    }
    try:
        response = requests.post(HF_API_URL, headers=HF_HEADERS, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list) and data and "generated_text" in data[0]:
            return data[0]["generated_text"].strip()
        if isinstance(data, dict) and "error" in data:
            logger.error("HF API error: %s", data["error"])
            return "Модель сейчас недоступна, попробуйте через минуту."
        return str(data)
    except requests.exceptions.RequestException as exc:
        logger.exception("Ошибка запроса к Hugging Face: %s", exc)
        return "Не получилось связаться с моделью ИИ. Попробуйте позже."


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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_message = update.message.text

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    prompt = build_prompt(chat_id, user_message)
    reply = query_huggingface(prompt)

    history = chat_history.setdefault(chat_id, [])
    history.append(f"Пользователь: {user_message}")
    history.append(f"{BOT_NAME}: {reply}")
    chat_history[chat_id] = history[-MAX_HISTORY_MESSAGES:]

    await update.message.reply_text(reply)


def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("%s запущен, используется модель %s", BOT_NAME, HF_MODEL)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

