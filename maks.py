import requests
from bs4 import BeautifulSoup
import time
import json

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"

MAX_PRICE = 800
MAX_RESULTS_PER_SCAN = 5

CACHE_FILE = "seen.json"

headers = {
    "User-Agent": "Mozilla/5.0"
}

last_scan = 0

# ---------- LOAD CACHE ----------

try:
    with open(CACHE_FILE) as f:
        seen = set(json.load(f))
except:
    seen = set()

def save_seen():
    with open(CACHE_FILE, "w") as f:
        json.dump(list(seen), f)

# ---------------- START ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 Apartment BOT started")

# ---------------- STATUS ----------------

async def scan(context: ContextTypes.DEFAULT_TYPE):

    global last_scan

    print("⚡ SCAN STARTED", flush=True)

    last_scan = int(time.time())

    try:

        await scan_saga(context)
        await scan_immowelt(context)
        await scan_wg(context)
        await scan_kleinanzeigen(context)

        print("✅ SCAN FINISHED", flush=True)

    except Exception as e:

        print("❌ ERROR:", e, flush=True)

    global last_scan

    now = int(time.time())
    diff = now - last_scan

    text = f"""
🤖 BOT STATUS

🟢 Bot running
⏱ Last scan: {diff} sec ago
🏠 Seen apartments: {len(seen)}
"""

    await update.message.reply_text(text)

# ---------------- SEND ----------------

async def send_listing(context, title, price, link, image, source):

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Apply Now", url=link)]
    ])

    text = f"""
🏠 *New Apartment*

📍 {title}
💶 {price}
🌐 {source}

⚡ Apply FAST
"""

    if image:

        await context.bot.send_photo(
            chat_id=CHAT_ID,
            photo=image,
            caption=text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )

    else:

        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )

# ---------------- SAGA ----------------

async def scan_saga(context):

    print("🔎 SAGA scan")

    base = "https://www.saga.hamburg/immobiliensuche"

    sent = 0

    for page in range(1, 6):

        url = f"{base}?Kategorie=APARTMENT&Seite={page}"

        r = requests.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")

        listings = soup.select(".property")

        for item in listings:

            link_tag = item.select_one("a")
            if not link_tag:
                continue

            link = "https://www.saga.hamburg" + link_tag["href"]

            if link in seen:
                continue

            title = item.select_one(".property__title").text.strip()
            price = item.select_one(".property__price").text.strip()

            img = item.select_one("img")
            image = img["src"] if img else None

            try:
                price_number = int(''.join(filter(str.isdigit, price)))
            except:
                continue

            if price_number > MAX_PRICE:
                continue

            seen.add(link)
            save_seen()

            await send_listing(context, title, price, link, image, "SAGA")

            sent += 1

            if sent >= MAX_RESULTS_PER_SCAN:
                return

# ---------------- IMMOWELT ----------------

async def scan_immowelt(context):

    print("🔎 IMMOWELT scan")

    sent = 0

    for page in range(1,4):

        url = f"https://www.immowelt.de/liste/hamburg/wohnungen/mieten?page={page}"

        r = requests.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")

        listings = soup.select("article")

        for item in listings:

            a = item.select_one("a")
            if not a:
                continue

            link = "https://www.immowelt.de" + a["href"]

            if link in seen:
                continue

            title_tag = item.select_one("h2")
            price_tag = item.select_one('[data-testid="price"]')

            if not title_tag or not price_tag:
                continue

            title = title_tag.text.strip()
            price = price_tag.text.strip()

            try:
                price_number = int(''.join(filter(str.isdigit, price)))
            except:
                continue

            if price_number > MAX_PRICE:
                continue

            img = item.select_one("img")
            image = img["src"] if img else None

            seen.add(link)
            save_seen()

            await send_listing(context, title, price, link, image, "Immowelt")

            sent += 1

            if sent >= MAX_RESULTS_PER_SCAN:
                return

# ---------------- WG-GESUCHT ----------------

async def scan_wg(context):

    print("🔎 WG-GESUCHT scan")

    url = "https://www.wg-gesucht.de/wohnungen-in-Hamburg.55.2.1.0.html"

    r = requests.get(url, headers=headers, timeout=20)
    soup = BeautifulSoup(r.text, "html.parser")

    listings = soup.select(".offer_list_item")

    sent = 0

    for item in listings:

        a = item.select_one("a")
        if not a:
            continue

        link = "https://www.wg-gesucht.de" + a["href"]

        if link in seen:
            continue

        title = a.text.strip()

        price_tag = item.select_one(".col-xs-3")
        price = price_tag.text.strip() if price_tag else "N/A"

        try:
            price_number = int(''.join(filter(str.isdigit, price)))
        except:
            continue

        if price_number > MAX_PRICE:
            continue

        seen.add(link)
        save_seen()

        await send_listing(context, title, price, link, None, "WG-Gesucht")

        sent += 1

        if sent >= MAX_RESULTS_PER_SCAN:
            return

# ---------------- KLEINANZEIGEN ----------------

async def scan_kleinanzeigen(context):

    print("🔎 Kleinanzeigen scan")

    url = "https://www.kleinanzeigen.de/s-wohnung-mieten/hamburg/c203l9409"

    r = requests.get(url, headers=headers, timeout=20)
    soup = BeautifulSoup(r.text, "html.parser")

    listings = soup.select(".aditem")

    sent = 0

    for item in listings:

        a = item.select_one("a")
        if not a:
            continue

        link = "https://www.kleinanzeigen.de" + a["href"]

        if link in seen:
            continue

        title = a.text.strip()

        price_tag = item.select_one(".aditem-main--middle--price")
        if not price_tag:
            continue

        price = price_tag.text.strip()

        try:
            price_number = int(''.join(filter(str.isdigit, price)))
        except:
            continue

        if price_number > MAX_PRICE:
            continue

        seen.add(link)
        save_seen()

        await send_listing(context, title, price, link, None, "Kleinanzeigen")

        sent += 1

        if sent >= MAX_RESULTS_PER_SCAN:
            return

# ---------------- MAIN SCAN ----------------

async def scan(context: ContextTypes.DEFAULT_TYPE):

    global last_scan

    last_scan = int(time.time())

    try:

        await scan_saga(context)
        time.sleep(2)

        await scan_immowelt(context)
        time.sleep(2)

        await scan_wg(context)
        time.sleep(2)

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
        interval=20,
        first=5
    )

    # ВАЖЛИВО
    asyncio.run(app.bot.delete_webhook(drop_pending_updates=True))

    app.run_polling()

