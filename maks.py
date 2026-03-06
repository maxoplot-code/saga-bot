import requests
from bs4 import BeautifulSoup
import time
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"

MAX_PRICE = 800

seen_links = set()
last_scan = 0

headers = {
    "User-Agent": "Mozilla/5.0"
}


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


# ---------------- WG-GESUCHT ----------------

async def scan_wg(context):

    print("🔎 WG scan")

    url = "https://www.wg-gesucht.de/wohnungen-in-Hamburg.55.2.1.0.html"

    r = requests.get(url, headers=headers)
    soup = BeautifulSoup(r.text, "html.parser")

    listings = soup.select(".offer_list_item")

    for item in listings:

        title_tag = item.select_one(".truncate_title")

        if not title_tag:
            continue

        title = title_tag.text.strip()

        a = item.select_one("a")

        if not a:
            continue

        link = "https://www.wg-gesucht.de" + a["href"]

        if link in seen_links:
            continue

        if "housinganywhere" in link or "wunderflats" in link:
            continue

        price_tag = item.select_one(".col-xs-3")

        if not price_tag:
            continue

        price = price_tag.text.strip()

        price_number = get_price_number(price)

        if not price_number:
            continue

        if price_number > MAX_PRICE:
            continue

        seen_links.add(link)

        await send_listing(context, title, price, link, "WG-Gesucht")


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

        if not price_number:
            continue

        if price_number > MAX_PRICE:
            continue

        seen_links.add(link)

        await send_listing(context, title, price, link, "Immowelt")


# ---------------- KLEINANZEIGEN ----------------

async def scan_kleinanzeigen(context):

    print("🔎 Kleinanzeigen scan")

    url = "https://www.kleinanzeigen.de/s-wohnung-mieten/hamburg/c203l9409"

    r = requests.get(url, headers=headers)
    soup = BeautifulSoup(r.text, "html.parser")

    listings = soup.select(".aditem")

    for item in listings:

        a = item.select_one("a")

        if not a:
            continue

        link = "https://www.kleinanzeigen.de" + a["href"]

        if link in seen_links:
            continue

        title = a.text.strip()

        price_tag = item.select_one(".aditem-main--middle--price")

        if not price_tag:
            continue

        price = price_tag.text.strip()

        price_number = get_price_number(price)

        if not price_number:
            continue

        if price_number > MAX_PRICE:
            continue

        seen_links.add(link)

        await send_listing(context, title, price, link, "Kleinanzeigen")


# ---------------- MAIN SCAN ----------------

async def scan(context: ContextTypes.DEFAULT_TYPE):

    global last_scan

    last_scan = int(time.time())

    try:

        await scan_wg(context)
        await scan_immowelt(context)
        await scan_kleinanzeigen(context)

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
