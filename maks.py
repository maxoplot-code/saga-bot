import requests
from bs4 import BeautifulSoup
import time
import re
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"

MAX_PRICE = 800

headers = {
    "User-Agent": "Mozilla/5.0"
}

seen_links = set()
last_scan = 0


# -------- LOAD SAVED ADS --------

if os.path.exists("seen_ads.txt"):
    with open("seen_ads.txt", "r") as f:
        for line in f:
            seen_links.add(line.strip())


def save_ad(link):
    with open("seen_ads.txt", "a") as f:
        f.write(link + "\n")


# ---------------- START ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Apartment bot working")


# ---------------- STATUS ----------------

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):

    diff = int(time.time()) - last_scan

    text = f"""
🤖 BOT STATUS

🟢 Bot running
⏱ Last scan: {diff} sec ago
🏠 Seen listings: {len(seen_links)}
"""

    await update.message.reply_text(text)


# ---------------- SEND ----------------

async def send_listing(context, title, price, link, source):

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Open listing", url=link)]
    ])

    text = f"""
🏠 New apartment

📍 {title}
💶 {price}
🌐 {source}
"""

    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        reply_markup=keyboard
    )


# ---------------- PRICE PARSER ----------------

def get_price_number(price_text):

    numbers = re.findall(r"\d+", price_text)

    if not numbers:
        return None

    return int(numbers[0])


# ---------------- IMMOWELT ----------------

async def scan_immowelt(context):

    print("🔎 Immowelt scan")

    url = "https://www.immowelt.de/liste/hamburg/wohnungen/mieten"

    r = requests.get(url, headers=headers)
    soup = BeautifulSoup(r.text, "html.parser")

    listings = soup.select("article")

    for item in listings:

        a = item.select_one("a")

        if not a:
            continue

        link = "https://www.immowelt.de" + a["href"]

        if link in seen_links:
            continue

        title_tag = item.select_one("h2")
        price_tag = item.select_one('[data-testid="price"]')

        if not title_tag or not price_tag:
            continue

        title = title_tag.text.strip()
        price = price_tag.text.strip()

        price_number = get_price_number(price)

        if not price_number or price_number > MAX_PRICE:
            continue

        seen_links.add(link)
        save_ad(link)

        await send_listing(context, title, price, link, "Immowelt")


# ---------------- IMMOMIO ----------------

async def scan_immomio(context):

    print("🔎 Immomio scan")

    url = "https://www.immomio.com/de/suche/wohnung-mieten/hamburg"

    r = requests.get(url, headers=headers)
    soup = BeautifulSoup(r.text, "html.parser")

    listings = soup.select("a[href*='/expose/']")

    for item in listings:

        link = "https://www.immomio.com" + item["href"]

        if link in seen_links:
            continue

        title = item.text.strip()

        if len(title) < 5:
            continue

        price_number = None

        if price_number and price_number > MAX_PRICE:
            continue

        seen_links.add(link)
        save_ad(link)

        await send_listing(context, title, "check price", link, "Immomio")


# ---------------- MAIN SCAN ----------------

async def scan(context: ContextTypes.DEFAULT_TYPE):

    global last_scan

    last_scan = int(time.time())

    try:

        await scan_immowelt(context)
        await scan_immomio(context)

    except Exception as e:

        print("ERROR:", e)


# ---------------- MAIN ----------------

def main():

    print("🚀 BOT STARTED")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))

    app.job_queue.run_repeating(
        scan,
        interval=30,
        first=5
    )

    app.run_polling()


if __name__ == "__main__":
    main()
