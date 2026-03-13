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
SCAN_INTERVAL = 60  # збільшено до 60 секунд щоб не перевантажувати
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
    await scan_page.set_extra_http_headers({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    print("BROWSER INITIALIZED")

# ================= SCAN ===================
async def scan_saga():
    global scan_page
    links = []
    await scan_page.goto(SAGA_URL, timeout=60000)
    await scan_page.wait_for_timeout(2000)
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

            try:
                cookie_btn = page.locator("text=Alle akzeptieren")
                if await cookie_btn.count() > 0:
                    await cookie_btn.first.click()
                    await page.wait_for_timeout(500)
            except:
                pass

            for text in ["Jetzt bewerben", "ZUM EXPOSÉ"]:
                try:
                    btn = page.locator(f"text={text}")
                    if await btn.count() > 0:
                        await btn.first.click(force=True)
                        await page.wait_for_timeout(4000)
                        break
                except:
                    continue

            if "immomio" in page.url.lower():
                try:
                    modal_close = page.locator("button[aria-label='Close']")
                    if await modal_close.count() > 0:
                        await modal_close.first.click(force=True)
                        await page.wait_for_timeout(500)
                except:
                    pass

                try:
                    if await page.locator('input[type="email"]').count() > 0:
                        await page.fill('input[type="email"]', IMMOMIO_EMAIL)
                        await page.fill('input[type="password"]', IMMOMIO_PASSWORD)
                        await page.click('button[type="submit"]', force=True)
                        await page.wait_for_timeout(7000)
                except Exception as e:
                    print("ERROR LOGIN:", e)

                for text in ["Jetzt bewerben", "Interesse bekunden"]:
                    try:
                        btn = page.locator(f"text={text}")
                        if await btn.count() > 0:
                            await btn.first.click(force=True)
                            await page.wait_for_timeout(3000)
                            await page.close()
                            return True
                    except:
                        continue

            await page.close()
        except Exception as e:
            print("ERROR APPLY:", e)
    return False

# ================= APPLY IN BACKGROUND ====
async def apply_and_notify(bot, link):
    """Запускається у фоні — не блокує сканер"""
    result = await auto_apply(link)
    if result:
        await bot.send_message(chat_id=CHAT_ID, text=f"✅ Application sent: {link}")
    else:
        await bot.send_message(chat_id=CHAT_ID, text=f"❌ Apply failed: {link}")

# ================= WORKER =================
async def scanner(context: ContextTypes.DEFAULT_TYPE):
    """
    Сканер ШВИДКО знаходить нові квартири і одразу повертається.
    apply запускається у фоні через asyncio.create_task — не блокує наступний скан.
    """
    print("SCAN:", time.strftime("%H:%M:%S"))
    try:
        flats = await scan_saga()
        if not flats:
            return
        for link in flats:
            await save_seen(link)
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🏠 New flat! {link}\n⏳ Applying in background..."
            )
            # Запускаємо apply у фоні — сканер не чекає результату
            asyncio.create_task(apply_and_notify(context.bot, link))
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
