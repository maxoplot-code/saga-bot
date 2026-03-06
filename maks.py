import requests
from bs4 import BeautifulSoup
from telegram import ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"

URL = "https://www.saga.hamburg/immobiliensuche"

seen = set()
running = False
chat_id = None


# меню
keyboard = [["▶ Start scan", "⛔ Stop scan"]]
menu = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def start(update, context):

    global chat_id
    chat_id = update.effective_chat.id

    await update.message.reply_text(
        "🏠 SAGA Apartment Bot\n\n"
        "Натисни кнопку щоб почати",
        reply_markup=menu
    )


async def buttons(update, context):

    global running

    text = update.message.text

    if text == "▶ Start scan":

        running = True

        await update.message.reply_text(
            "🔎 Сканування квартир кожні 10 секунд"
        )

    if text == "⛔ Stop scan":

        running = False

        await update.message.reply_text(
            "⛔ Сканування зупинено"
        )


async def scanner(context):

    global running, chat_id

    if not running or not chat_id:
        return

    print("🔎 scanning SAGA...")

    try:

        r = requests.get(URL, timeout=30)

        soup = BeautifulSoup(r.text, "html.parser")

        for a in soup.find_all("a"):

            href = a.get("href")

            if href and "/immobilie/" in href:

                link = "https://www.saga.hamburg" + href

                if link not in seen:

                    seen.add(link)

                    print("NEW:", link)

                    await context.bot.send_message(
                        chat_id,
                        f"🏠 Нова квартира\n{link}"
                    )

    except Exception as e:

        print("ERROR:", e)


def main():

    print("🚀 SAGA BOT STARTED")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT, buttons))

    # перевірка кожні 10 секунд
    app.job_queue.run_repeating(
        scanner,
        interval=10,
        first=5
    )

    app.run_polling()


if __name__ == "__main__":
    main()
