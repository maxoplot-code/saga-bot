import asyncio
import os
import time
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- CONFIG ---

TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"

IMMOMIO_EMAIL = "maksymsheveliuk@gmail.com"
IMMOMIO_PASSWORD = "Maksoplot2007"

SAGA_URL = "https://www.saga.hamburg/immobiliensuche?Kategorie=APARTMENT"

seen = set()

if os.path.exists("seen.txt"):
    with open("seen.txt") as f:
        for line in f:
            seen.add(line.strip())

# --- AUTO APPLY ---

async def perform_auto_apply(link):

    print("🚀 APPLY:", link)

    try:

        async with async_playwright() as p:

            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            await page.goto(link, timeout=60000)

            await page.wait_for_timeout(4000)

            # якщо сторінка SAGA
            if "saga.hamburg" in page.url:

                for text in ["Jetzt bewerben", "ZUM EXPOSÉ"]:

                    try:
                        btn = page.locator(f"text={text}")
                        if await btn.count() > 0:
                            await btn.first.click()
                            print("➡️ Redirect to Immomio")
                            await page.wait_for_timeout(8000)
                            break
                    except:
                        pass

            # якщо Immomio
            if "immomio.com" in page.url:

                # login
                if await page.locator('input[type="email"]').count() > 0:

                    print("🔑 Login Immomio")

                    await page.fill('input[type="email"]', IMMOMIO_EMAIL)
                    await page.fill('input[type="password"]', IMMOMIO_PASSWORD)

                    await page.click('button[type="submit"]')

                    await page.wait_for_timeout(7000)

                # apply
                for text in ["Jetzt bewerben", "Interesse bekunden"]:

                    try:
                        btn = page.locator(f"text={text}")
                        if await btn.count() > 0:
                            await btn.first.click()
                            print("✅ Application sent")
                            await page.wait_for_timeout(3000)
                            await browser.close()
                            return True
                    except:
                        pass

            await browser.close()

    except Exception as e:
        print("❌ APPLY ERROR:", e)

    return False


# --- SCAN SAGA ---

def get_new_flats():

    headers = {"User-Agent": "Mozilla/5.0"}

    r = requests.get(SAGA_URL, headers=headers, timeout=20)

    soup = BeautifulSoup(r.text, "html.parser")

    flats = []

    for a in soup.select("a[href*='immo-detail']"):

        href = a.get("href")

        if not href:
            continue

        link = "https://www.saga.hamburg" + href

        link = link.strip()

        if link not in seen:
            flats.append(link)

    return list(set(flats))


# --- SCANNER ---

async def fast_scan(context: ContextTypes.DEFAULT_TYPE):

    print("🔎 SCAN:", time.strftime("%H:%M:%S"))

    try:

        flats = get_new_flats()

        for link in flats:

            seen.add(link)

            with open("seen.txt", "a") as f:
                f.write(link + "\n")

            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🏠 Нова квартира!\n{link}\n⏳ Подаю заявку..."
            )

            status = await perform_auto_apply(link)

            if status:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text="✅ Заявку відправлено!"
                )
            else:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text="❌ Не вдалося подати заявку."
                )

    except Exception as e:
        print("SCAN ERROR:", e)


# --- TELEGRAM ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Бот запущено")


# --- MAIN ---

def main():

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.job_queue.run_repeating(
        fast_scan,
        interval=30,
        first=5
    )

    print("🚀 BOT STARTED")

    app.run_polling()


if __name__ == "__main__":
    main()
