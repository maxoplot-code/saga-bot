import requests
from bs4 import BeautifulSoup
from telegram.ext import ApplicationBuilder, CommandHandler
import asyncio

TOKEN = "8652232123:AAG49ew_SSGAdg_jeyjA-BWVSy8IGb_Hd3s"

URL = "https://www.saga.hamburg/immobiliensuche"

found_links = set()
running = False
CHAT_ID = None


async def start(update, context):

    global CHAT_ID
    CHAT_ID = update.effective_chat.id

    await update.message.reply_text(
        "🏠 SAGA Apartment Bot\n\n"
        "/scan - почати пошук\n"
        "/stop - зупинити"
    )


async def scan(update, context):

    global running
    running = True

    await update.message.reply_text("🔎 Почав шукати нові квартири...")


async def stop(update, context):

    global running
    running = False

    await update.message.reply_text("⛔ Сканування зупинено")


async def scanner(context):

    global running, CHAT_ID

    if not running or CHAT_ID is None:
        return

    try:

        print("Scanning SAGA...")

        r = requests.get(URL, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")

        links = []

        for a in soup.find_all("a", href=True):

            href = a["href"]

            if "immobilien" in href or "immobilie" in href:

                if href.startswith("/"):
                    href = "https://www.saga.hamburg" + href

                links.append(href)

        for link in links:

            if link not in found_links:

                found_links.add(link)

                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=f"🏠 Нова квартира:\n{link}"
                )

                print("NEW:", link)

    except Exception as e:

        print("ERROR:", e)


def main():

    print("🚀 SAGA BOT STARTED")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("stop", stop))

    app.job_queue.run_repeating(scanner, interval=10, first=5)

    app.run_polling()


if __name__ == "__main__":
    main()
