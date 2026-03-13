import asyncio
import os
import time
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================= CONFIG =================

TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"

IMMOMIO_EMAIL = "maksymsheveliuk@gmail.com"
IMMOMIO_PASSWORD = "Maksoplot2007"

SAGA_URL = "https://www.saga.hamburg/immobiliensuche?Kategorie=APARTMENT"
SCAN_INTERVAL = 10

SEEN_FILE = "seen.txt"

# ==========================================

seen = set()

if os.path.exists(SEEN_FILE):
    with open(SEEN_FILE) as f:
        for line in f:
            seen.add(line.strip())


async def save_seen(link):
    seen.add(link)
    with open(SEEN_FILE, "a") as f:
        f.write(link + "\n")


# ================= SCAN ===================

async def scan_saga():

    links = []

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )

        page = await browser.new_page()

        await page.goto(SAGA_URL, timeout=60000)

        await page.wait_for_timeout(5000)

        elements = await page.query_selector_all("a[href*='immo-detail']")

        for el in elements:

            href = await el.get_attribute("href")

            if not href:
                continue

            link = "https://www.saga.hamburg" + href

            if link not in seen:
                links.append(link)

        await browser.close()

    return list(set(links))


# ================= APPLY ==================

async def auto_apply(link):

    print("APPLY ->", link)

    try:

        async with async_playwright() as p:

            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )

            page = await browser.new_page()

            await page.goto(link, timeout=60000)

            await page.wait_for_timeout(4000)

            # saga redirect
            for text in ["Jetzt bewerben", "ZUM EXPOSÉ"]:

                btn = page.locator(f"text={text}")

                if await btn.count() > 0:
                    await btn.first.click()
                    await page.wait_for_timeout(8000)
                    break

            # immomio login
            if "immomio" in page.url:

                if await page.locator('input[type="email"]').count() > 0:

                    await page.fill('input[type="email"]', IMMOMIO_EMAIL)
                    await page.fill('input[type="password"]', IMMOMIO_PASSWORD)

                    await page.click('button[type="submit"]')

                    await page.wait_for_timeout(7000)

                for text in ["Jetzt bewerben", "Interesse bekunden"]:

                    btn = page.locator(f"text={text}")

                    if await btn.count() > 0:
                        await btn.first.click()
                        await page.wait_for_timeout(3000)
                        await browser.close()
                        return True

            await browser.close()

    except Exception as e:
        print("ERROR APPLY:", e)

    return False


# ================= WORKER =================

async def scanner(context: ContextTypes.DEFAULT_TYPE):

    print("SCAN:", time.strftime("%H:%M:%S"))

    try:

        flats = await scan_saga()

        if not flats:
            return

        tasks = []

        for link in flats:

            await save_seen(link)

            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🏠 New flat!\n{link}\n⏳ Applying..."
            )

            tasks.append(auto_apply(link))

        results = await asyncio.gather(*tasks)

        for r in results:

            if r:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text="✅ Application sent"
                )
            else:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text="❌ Apply failed"
                )

    except Exception as e:
        print("SCAN ERROR:", e)


# ================= TELEGRAM ===============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 PRO Apartment Bot Started")


# ================= MAIN ===================

def main():

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.job_queue.run_repeating(
        scanner,
        interval=SCAN_INTERVAL,
        first=5
    )

    print("BOT RUNNING...")

    app.run_polling()


if __name__ == "__main__":
    main()
