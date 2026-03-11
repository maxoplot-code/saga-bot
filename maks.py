import asyncio
import requests
import time
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ---------- НАЛАШТУВАННЯ ----------
TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"
SCAN_INTERVAL = 120  # Оптимально 2 хвилини

seen = set()

# ---------- ЗАВАНТАЖЕННЯ ДАНИХ ----------
if os.path.exists("seen.txt"):
    with open("seen.txt") as f:
        for line in f:
            seen.add(line.strip())

def save_link(link):
    with open("seen.txt", "a") as f:
        f.write(link + "\n")

# ---------- МЕНЮ ----------
main_menu = ReplyKeyboardMarkup(
    [["🔎 Scan now"], ["📊 Status", "♻ Reset"]],
    resize_keyboard=True
)

# ---------- ГОЛОВНА ФУНКЦІЯ СКАНУВАННЯ ----------
async def run_scan(context: ContextTypes.DEFAULT_TYPE):
    print(f"🔎 [{time.strftime('%H:%M:%S')}] Початок сканування...")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        }

        # Використовуємо таймаут, щоб бот не "висів"
        response = requests.get(
            "https://www.immomio.com",
            headers=headers,
            timeout=15
        )

        if response.status_code != 200:
            print(f"❌ Помилка сайту: {response.status_code}")
            return

        html = response.text
        parts = html.split("/expose/")[1:] # беремо все після першого розбиття
        
        found_new = 0
        for part in parts:
            # Витягуємо ID до першої лапки
            expose_id = part.split('"')[0]
            link = f"https://www.immomio.com{expose_id}"

            if link in seen:
                continue

            seen.add(link)
            save_link(link)
            found_new += 1

            # Надсилаємо повідомлення в Telegram
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Відкрити оголошення", url=link)]])
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🏠 **Знайдено нову квартиру!**\n\nЛокація: Hamburg\n🔗 [Переглянути на Immomio]({link})",
                reply_markup=keyboard,
                parse_mode="Markdown"
            )

        print(f"✅ Сканування завершено. Нових оголошень: {found_new}")

    except Exception as e:
        print(f"💥 Помилка під час сканування: {e}")

# ---------- ОБРОБНИКИ КОМАНД ТА КНОПОК ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Бот-ріелтор запущений!", reply_markup=main_menu)

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🔎 Scan now":
        await update.message.reply_text("⏳ Запускаю перевірку...")
        await run_scan(context)

    elif text == "📊 Status":
        await update.message.reply_text(f"📊 В базі: {len(seen)} оголошень.\nСтатус: Працює.")

    elif text == "♻ Reset":
        seen.clear()
        if os.path.exists("seen.txt"):
            os.remove("seen.txt")
        await update.message.reply_text("♻ Базу даних очищено.")

# ---------- ФОНОВЕ ЗАВДАННЯ ----------
async def background_job(context: ContextTypes.DEFAULT_TYPE):
    await run_scan(context)

# ---------- ЗАПУСК ----------
def main():
    print("🚀 Бот запускається...")
    
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))

    # Налаштування автоматичного сканування
    if app.job_queue:
        app.job_queue.run_repeating(background_job, interval=SCAN_INTERVAL, first=10)

    print("🚀 Бот у мережі! Натисніть /start у Telegram.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
