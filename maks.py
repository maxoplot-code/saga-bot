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

TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"

SCAN_INTERVAL = 5

seen = set()
last_scan = 0


# ---------- LOAD SEEN ----------

if os.path.exists("seen.txt"):
    with open("seen.txt") as f:
        for line in f:
            seen.add(line.strip())


def save(link):
    with open("seen.txt", "a") as f:
        f.write(link + "\n")


# ---------- MENU ----------

menu = ReplyKeyboardMarkup(
    [
        ["🔎 Scan now"],
        ["📊 Status", "♻ Reset"]
    ],
    resize_keyboard=True
)


# ---------- COMMANDS ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🤖 Immomio bot started",
        reply_markup=menu
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):

    diff = int(time.time()) - last_scan

    await update.message.reply_text(
        f"🤖 Running\nLast scan: {diff}s\nSeen: {len(seen)}"
    )


# ---------- SEND ----------

async def send_listing(context, title, link):

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Open listing", url=link)]
    ])

    text = f"""
🏠 New apartment

📋 {title}

🌐 Immomio
"""

    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        reply_markup=keyboard
    )


# ---------- SCAN ----------

async def scan(context):

    global last_scan
    last_scan = int(time.time())

    print("🔎 scanning")

    try:

        r = requests.get(
            "https://www.immomio.com/de/search/hamburg",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )

        if r.status_code != 200:
            print("Site error")
            return

        html = r.text

        parts = html.split("/expose/")

        for part in parts[1:]:

            expose_id = part.split('"')[0]

            link = f"https://www.immomio.com/expose/{expose_id}"

            if link in seen:
                continue

            seen.add(link)
            save(link)

            await send_listing(
                context,
                "New apartment",
                link
            )

    except Exception as e:
        print("SCAN ERROR:", e)


# ---------- BACKGROUND SCANNER ----------

async def scanner(app):

    await asyncio.sleep(10)

    while True:

        await scan(app)

        await asyncio.sleep(SCAN_INTERVAL)


# ---------- MENU ----------

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    if text == "📊 Status":
        await status(update, context)

    elif text == "🔎 Scan now":
        await update.message.reply_text("🔎 scanning...")
        await scan(context)

    elif text == "♻ Reset":

        seen.clear()
        open("seen.txt", "w").close()

        await update.message.reply_text("Seen list cleared")


# ---------- MAIN ----------

async def main():

    print("🚀 BOT STARTED")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler)
    )

    asyncio.create_task(scanner(app))

    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
