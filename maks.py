from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import requests
from bs4 import BeautifulSoup

TOKEN = "8652232123:AAG49ew_SSGAdg_jeyjA-BWVSy8IGb_Hd3s"

URL = "https://www.saga.hamburg/immobiliensuche"

found_links = set()
scan_enabled = False
user_chat_id = None


async def start(update, context):

    global user_chat_id

    user_chat_id = update.effective_chat.id

    await update.message.reply_text(
        "🏠 SAGA Apartment Bot\n\n"
        "Команди:\n"
        "/scan - почати пошук квартир\n"
        "/stop - зупинити пошук"
    )


async def scan(update, context):

    global scan_enabled

    scan_enabled = True

    await update.message.reply_text("🔎 Почав перевіряти нові квартири...")


async def stop(update, context):

    global scan_enabled

    scan_enabled = False

    await update.message.reply_text("⛔ Сканування зупинено")


async def scanner(context: ContextTypes.DEFAULT_TYPE):

    global scan_enabled
    global found_links
    global user_chat_id

    if not scan_enabled or user_chat_id is None:
        return

    try:

        print("🔎 SCANNING SAGA SITE...")

        response = requests.get(URL, timeout=10)

        soup = BeautifulSoup(response.text, "html.parser")

        found_any = False

        for a in soup.find_all("a"):

            href = a.get("href")

            if href and "/immobilie/" in href:

                found_any = True

                full_link = "https://www.saga.hamburg" + href

                if full_link not in found_links:

                    found_links.add(full_link)

                    print("🏠 NEW APARTMENT:", full_link)

                    await context.bot.send_message(
                        chat_id=user_chat_id,
                        text=f"🏠 Нова квартира!\n{full_link}"
                    )

        if not found_any:
            print("❌ Квартир зараз немає")

    except Exception as e:

        print("⚠️ ERROR:", e)


def main():

    print("🚀 SAGA BOT STARTED")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("stop", stop))

    job = app.job_queue

    job.run_repeating(
        scanner,
        interval=20,
        first=5
    )

    app.run_polling()


if __name__ == "__main__":
    main()