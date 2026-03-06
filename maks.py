import requests
print("BOT FILE STARTED")
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler

TOKEN = "8652232123:AAG49ew_SSGAdg_jeyjA-BWVSy8IGb_Hd3s"
CHAT_ID = "8349459166"

BASE_URL = "https://www.saga.hamburg/immobiliensuche"
MAX_PAGES = 20
MAX_PRICE = 800

seen = set()

headers = {
    "User-Agent": "Mozilla/5.0"
}


async def start(update, context):
    await update.message.reply_text("🚀 SAGA BOT V3 started")


async def scan(context):

    print("🔎 scanning SAGA...")

    for page in range(1, MAX_PAGES + 1):

        try:

            url = f"{BASE_URL}?Kategorie=APARTMENT&Seite={page}"

            r = requests.get(url, headers=headers, timeout=10)

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
                        price_number = int(p.replace("€", "").replace(".", ""))
                        break

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
            print("Error:", e)


def main():

    print("🚀 SAGA BOT V3 STARTED")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.job_queue.run_repeating(
        scan,
        interval=5,
        first=5
    )

    app.run


