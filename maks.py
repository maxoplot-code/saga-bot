import requests
import time
import os
import urllib3

urllib3.disable_warnings()

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"

MAX_PRICE = 800

seen = set()
last_scan = 0


# ---------- LOAD OLD ADS ----------

if os.path.exists("seen.txt"):
    with open("seen.txt") as f:
        for line in f:
            seen.add(line.strip())


def save(link):
    with open("seen.txt","a") as f:
        f.write(link+"\n")


# ---------- TELEGRAM ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Immomio bot running")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):

    diff = int(time.time()) - last_scan

    await update.message.reply_text(
        f"🤖 Running\nLast scan: {diff}s\nSeen: {len(seen)}"
    )


async def send_listing(context,title,price,area,rooms,link):

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Apply / Open", url=link)]
    ])

    text = f"""
🏠 New apartment

📋 {title}

💶 {price} €
📏 {area} m²
🛏 {rooms} rooms

🌐 Immomio
"""

    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        reply_markup=keyboard
    )


# ---------- SCAN IMMOMIO ----------

async def scan(context: ContextTypes.DEFAULT_TYPE):

    global last_scan
    last_scan = int(time.time())

    print("🔎 scanning")

    try:

       url = "https://api.immomio.com/properties?city=hamburg"

        r = requests.get(url, verify=False)
        data = r.json()

        for item in data:

            price = item.get("totalRent")

            if not price or price > MAX_PRICE:
                continue

            link = "https://www.immomio.com/expose/" + item["id"]

            if link in seen:
                continue

            seen.add(link)
            save(link)

            await send_listing(
                context,
                item.get("title"),
                price,
                item.get("livingSpace"),
                item.get("numberOfRooms"),
                link
            )

    except Exception as e:
        print("ERROR:", e)


# ---------- MAIN ----------

def main():

    print("🚀 BOT STARTED")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))

    app.job_queue.run_repeating(
        scan,
        interval=15,
        first=5
    )

    app.run_polling()


if __name__ == "__main__":
    main()


