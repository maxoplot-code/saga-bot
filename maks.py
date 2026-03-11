import asyncio
import requests
import time
import os

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ---------- НАЛАШТУВАННЯ ----------
TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"
SCAN_INTERVAL = 60 # Рекомендую 5 хвилин (300 сек), щоб не забанили

seen = set()

# ---------- ЗАВАНТАЖЕННЯ ДАНИХ ----------
if os.path.exists("seen.txt"):
    with open("seen.txt") as f:
        for line in f:
            seen.add(line.strip())

def save_link(link):
    with open("seen.txt", "a") as f:
        f.write(link + "\n")

# ---------- МЕНЮ (КНОПКИ) ----------
main_menu = ReplyKeyboardMarkup(
    [
        ["🔎 Scan now"],
        ["📊 Status", "♻ Reset"]
    ],
    resize_keyboard=True
)

# ---------- ЛОГІКА СКАНУВАННЯ ----------
async def run_scan(context: ContextTypes.DEFAULT_TYPE):
    print("🔎 Scanning Immomio...")
    try:
        r = requests.get(
            "https://www.immomio.com",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15
        )

        if r.status_code != 200:
            print(f"Site error: {r.status_code}")
            return

        html = r.text
        parts = html.split("/expose/")

        for part in parts[1:]:
            expose_id = part.split('"')[0]
            link = f"https://www.immomio.com{expose_id}"

            if link in seen:
                continue

            seen.add(link)
            save_link(link)

            # Надсилаємо повідомлення
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Open listing", url=link)]])
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🏠 **New apartment found!**\n\n🌐 Source: Immomio\n🔗 [Link]({link})",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )

    except Exception as e:
        print(f"SCAN ERROR: {e}")

# ---------- ОБРОБНИКИ КОМАНД ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Бот запущено! Використовуйте кнопки нижче:",
        reply_markup=main_menu
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = len(seen)
    await update.message.reply_text(f"📊 Стан бота:\n✅ База знає про {count} оголошень.\n⏱ Інтервал сканування: {SCAN_INTERVAL}с.")

# ---------- ГОЛОВНИЙ ОБРОБНИК ПОВІДОМЛЕНЬ (MENU HANDLER) ----------
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🔎 Scan now":
        await update.message.reply_text("🔎 Запускаю примусове сканування...")
        await run_scan(context)

    elif text == "📊 Status":
        await status(update, context)

    elif text == "♻ Reset":
        seen.clear()
        if os.path.exists("seen.txt"):
            open("seen.txt", "w").close()
        await update.message.reply_text("♻ Список переглянутих оголошень очищено!")

# ---------- BACKGROUND JOB ----------
async def background_scan_job(context: ContextTypes.DEFAULT_TYPE):
    await run_scan(context)

# ---------- MAIN ----------
def main():
    print("🚀 BOT STARTING...")
    
    app = ApplicationBuilder().token(TOKEN).build()

    # Додаємо обробники
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))

    # Налаштування автоматичного фонового сканування
    job_queue = app.job_queue
    job_queue.run_repeating(background_scan_job, interval=SCAN_INTERVAL, first=10)

    print("🚀 BOT IS RUNNING")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

