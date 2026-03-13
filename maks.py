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


# ============ GLOBAL BROWSER ==============

playwright_instance = None
browser = None
scan_page = None


async def init_browser():
    global playwright_instance, browser, scan_page

    if browser:
        return

    playwright_instance = await async_playwright().start()

    browser = await playwright_instance.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"]
    )

    scan_page = await browser.new_page()

    await scan_page.set_extra_http_headers({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    print("BROWSER INITIALIZED")


# ================= SCAN ===================

async def scan_saga():
    global scan_page

    links = []

    await scan_page.goto(SAGA_URL, timeout=60000)
    await scan_page.wait_for_timeout(2000)

    # accept cookies if present
    try:
        cookie_btn = scan_page.locator("text=Alle akzeptieren")
        if await cookie_btn.count() > 0:
            await cookie_btn.first.click()
            await scan_page.wait_for_timeout(1000)
    except:
        pass

    elements = await scan_page.query_selector_all("a[href*='immo-detail']")

    for el in elements:
        href = await el.get_attribute("href")
        if not href:
            continue
        link = "https://www.saga.hamburg" + href

        # filter only apartments
        lower = link.lower()
        if any(x in lower for x in ["gewerbe", "einstellplatz", "garage", "stellplatz"]):
            continue
        if not any(x in lower for x in ["wohnung", "zimmer", "apartment"]):
            continue

        if link not in seen:
            links.append(link)

    return list(set(links))


# ================= APPLY ==================

semaphore = asyncio.Semaphore(3)


async def auto_apply(link):
    async with semaphore:
        print("APPLY ->", link)
        try:
            page = await browser.new_page()
            await page.goto(link, timeout=60000)
            await page.wait_for_timeout(2000)

            # accept cookies if present
            try:
                cookie_btn = page.locator("text=Alle akzeptieren")
                if await cookie_btn.count() > 0:
                    await cookie_btn.first.click()
                    await page.wait_for_timeout(1000)
            except:
                pass

            for text in ["Jetzt bewerben", "ZUM EXPOSÉ"]:
                btn = page.locator(f"text={text}")
                if await btn.count() > 0:
                    await btn.first.click(force=True)
                    await page.wait_for_timeout(6000)
                    break

            if "immomio" in page.url:
                if await page.locator('input[type="email"]').count() > 0:
                    await page.fill('input[type="email"]', IMMOMIO_EMAIL)
                    await page.fill('input[type="password"]', IMMOMIO_PASSWORD)
                    await page.click('button[type="submit"]')
                    await page.wait_for_timeout(5000)

                for text in ["Jetzt bewerben", "Interesse bekunden"]:
                    btn = page.locator(f"text={text}")
                    if await btn.count() > 0:
                        await btn.first.click(force=True)
                        await page.wait_for_timeout(2000)
                        await page.close()
                        return True

            await page.close()
        except Exception as e:
            print("ERROR APPLY:", e)

    return False


# ================= WORKER =================

scan_lock = asyncio.Lock()

async def scanner(context: ContextTypes.DEFAULT_TYPE):
    if scan_lock.locked():
        print("SCAN SKIPPED (previous still running)")
        return

    async with scan_lock:
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
                    text=f"🏠 New flat! {link}\n⏳ Applying..."
                )
                tasks.append(auto_apply(link))

            results = await asyncio.gather(*tasks)

            for r in results:
                if r:
                    await context.bot.send_message(chat_id=CHAT_ID, text="✅ Application sent")
                else:
                    await context.bot.send_message(chat_id=CHAT_ID, text="❌ Apply failed")

        except Exception as e:
            print("SCAN ERROR:", e)


# ================= TELEGRAM ===============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 PRO Apartment Bot Started")


# ================= MAIN ===================

async def post_init(app):
    await init_browser()


def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))

    app.job_queue.run_repeating(scanner, interval=SCAN_INTERVAL, first=5)

    print("BOT RUNNING...")
    app.run_polling()


if __name__ == "__main__":
    main()
