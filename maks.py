import requests
import time
import os
from bs4 import BeautifulSoup

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"

MAX_PRICE = 800

seen = set()
last_scan = 0

if os.path.exists("seen.txt"):
    with open("seen.txt") as f:
        for line in f:
            seen.add(line.strip())


def save(link):
    with open("seen.txt", "a") as f:
        f.write(link + "\n")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Immomio bot running")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    diff = int(time.time()) - last_scan

    await update.message.reply_text(
        f"🤖 Running\nLast scan: {diff}s\nSeen: {len(seen)}"
    )


async def send_listing(context, title, price, link):

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Apply / Open", url=link)]
    ])

    text = f"""
🏠 New apartment

📋 {title}

💶 {price} €

🌐 Immomio
"""

    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        reply_markup=keyboard
    )


async def scan(context: ContextTypes.DEFAULT_TYPE):

    global last_scan
    last_scan = int(time.time())

    print("🔎 scanning")

    try:

        url = "https://www.immomio.com/de/search/hamburg"

        headers = {
            "User-Agent": "Mozilla/5.0"
        }

        r = requests.get(url, headers=headers)

        soup = BeautifulSoup(r.text, "html.parser")

        listings = soup.find_all("a", href=True)

        for l in listings:

            link = l["href"]

            if "/expose/" not in link:
                continue

            link = "https://www.immomio.com" + link

            if link in seen:
                continue

            seen.add(link)
            save(link)

            title = l.text.strip()

            price = 0

            await send_listing(
                context,
                title,
                price,
                link
            )

    except Exception as e:
        print("ERROR:", e)


def main():

    print("🚀 BOT STARTED")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))

    app.job_queue.run_repeating(
        scan,
        interval=20,
        first=5
    )

    app.run_polling()


if __name__ == "__main__":
    main()
