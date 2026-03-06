import requests
from bs4 import BeautifulSoup
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"

BASE_URL = "https://www.saga.hamburg/immobiliensuche"
MAX_PAGES = 20
MAX_PRICE = 800

seen = set()
last_scan = 0

headers = {
    "User-Agent": "Mozilla/5.0"
}

# START
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 SAGA BOT запущений")

# STATUS
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global last_scan

    now = int(time.time())
    diff = now - last_scan

    text = f"""
🤖 BOT STATUS

🟢 Bot працює
⏱ Останній скан: {diff} сек тому
🏠 Перевірених квартир: {len(seen)}
"""

    await update.message.reply_text(text)

# SCAN
async def scan(context: ContextTypes.DEFAULT_TYPE):

    global last_scan

    print("🔎 scanning SAGA...", flush=True)

    last_scan = int(time.time())

    try:

        for page in range(1, MAX_PAGES + 1):

            url = f"{BASE_URL}?Kategorie=APARTMENT&Seite={page}"

            r = requests.get(url, headers=headers, timeout=30)

            soup = BeautifulSoup(r.text, "html.parser")

            listings = soup.select(".property")

            for item in listings:

                link_tag = item.select_one("a")

                if not link_tag:
                    continue

                link = "https://www.saga.hamburg" + link_tag["href"]

                if link in seen:
                    continue

                title_tag = item.select_one(".property__title")
                price_tag = item.select_one(".property__price")
                img_tag = item.select_one("img")

                title = title_tag.text.strip() if title_tag else "Apartment"
                price = price_tag.text.strip() if price_tag else "N/A"
                image = img_tag["src"] if img_tag else None

                price_number = 0

                for p in price.split():
                    if "€" in p:
                        try:
                            price_number = int(p.replace("€", "").replace(".", ""))
                        except:
                            pass

                if price_number > MAX_PRICE:
                    continue

                seen.add(link)

                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚡ Apply Now", url=link)]
                ])

                text = f"""
🏠 *New SAGA Apartment*

📍 {title}
💶 {price}

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

    except Exception as e:

        print("❌ ERROR:", e)

        # auto restart логіки
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="⚠️ SAGA scan error — restarting scan..."
        )

# MAIN
def main():

    print("🚀 SAGA BOT STARTED", flush=True)

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))

    app.job_queue.run_repeating(
        scan,
        interval=10,
        first=5
    )

    app.run_polling()

if __name__ == "__main__":
    main()
