import requests
import time
import os
from bs4 import BeautifulSoup

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters
)

TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"

MAX_PRICE = 800

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


# ---------- SEND LISTING ----------

async def send_listing(context, title, price, area, rooms, link):

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Apply / Open", url=link)]
    ])

    text = f"""
🏠 New apartment

📋 {title}

💶 {price}
📏 {area}
🛏 {rooms}

🌐 Immomio
"""

    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        reply_markup=keyboard
    )


# ---------- SCAN ----------

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

        cards = soup.find_all("a", href=True)

        for card in cards:

            href = card.get("href")

            if not href or "/expose/" not in href:
                continue

            link = "https://www.immomio.com" + href

            if link in seen:
                continue

            parent = card.find_parent()

            text = parent.get_text(" ", strip=True)

            price = "?"
            area = "?"
            rooms = "?"

            for part in text.split():

                if "€" in part:
                    price = part

                if "m²" in part:
                    area = part

            if "Zimmer" in text:
                try:
                    rooms = text.split("Zimmer")[0].split()[-1]
                except:
                    pass

            try:
                price_value = int(price.replace("€", "").replace(".", "").replace(",", ""))
                if price_value > MAX_PRICE:
                    continue
            except:
                pass

            title = card.text.strip()

            seen.add(link)
            save(link)

            await send_listing(
                context,
                title,
                price,
                area,
                rooms,
                link
            )

    except Exception as e:
        print("ERROR:", e)


# ---------- MENU HANDLER ----------

async def scan(context: ContextTypes.DEFAULT_TYPE):

    global last_scan
    last_scan = int(time.time())

    print("🔎 scanning")

    try:

        url = "https://www.immomio.com/api/v1/properties"

        params = {
            "city": "hamburg"
        }

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        }

        r = requests.get(url, headers=headers, params=params, timeout=10)

        if r.status_code != 200:
            print("API ERROR:", r.status_code)
            return

        data = r.json()

        for item in data:

            link = "https://www.immomio.com/expose/" + str(item["id"])

            if link in seen:
                continue

            price = item.get("totalRent")
            area = item.get("livingSpace")
            rooms = item.get("numberOfRooms")
            title = item.get("title")

            if price and price > MAX_PRICE:
                continue

            seen.add(link)
            save(link)

            await send_listing(
                context,
                title,
                price,
                area,
                rooms,
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

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler)
    )

    app.job_queue.run_repeating(
        scan,
        interval=5,
        first=5
    )

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

