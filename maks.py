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
SCAN_INTERVAL = 60
SEEN_FILE = "seen.txt"
SCREENSHOT_DIR = "screenshots"

os.makedirs(SCREENSHOT_DIR, exist_ok=True)

EXCLUDE_KEYWORDS = [
    "gewerbe", "einstellplatz", "garage", "stellplatz",
    "buroflache", "büroflache", "buro", "büro",
    "praxis", "existenzgrunder", "existenzgründer",
    "lager", "laden", "shop"
]

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

playwright_instance = None
browser = None
bcontext = None
scan_page = None

async def init_browser():
    global playwright_instance, browser, bcontext, scan_page
    if browser:
        return
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    )
    bcontext = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900}
    )
    scan_page = await bcontext.new_page()
    print("BROWSER INITIALIZED")

async def accept_cookies(page):
    for text in ["Alle akzeptieren", "Alles akzeptieren", "Alle erlauben", "Akzeptieren"]:
        try:
            btn = page.locator(f"text={text}")
            if await btn.count() > 0:
                await btn.first.click(force=True)
                await page.wait_for_timeout(800)
                return
        except:
            pass

def is_apartment(link: str) -> bool:
    lower = link.lower()
    for kw in EXCLUDE_KEYWORDS:
        if kw in lower:
            return False
    return True

async def scan_saga():
    global scan_page
    links = []
    try:
        await scan_page.goto(SAGA_URL, timeout=60000, wait_until="domcontentloaded")
        await scan_page.wait_for_timeout(3000)
        await accept_cookies(scan_page)
        elements = await scan_page.query_selector_all("a[href*='immo-detail']")
        print(f"Found {len(elements)} listings total")
        seen_hrefs = set()
        for el in elements:
            href = await el.get_attribute("href")
            if not href:
                continue
            link = href if href.startswith("http") else "https://www.saga.hamburg" + href
            if link in seen_hrefs:
                continue
            seen_hrefs.add(link)
            if not is_apartment(link):
                print(f"  SKIP: {link}")
                continue
            if link not in seen:
                links.append(link)
        print(f"New apartments (after filter): {len(links)}")
    except Exception as e:
        print(f"SCAN ERROR: {e}")
    return links

# ================= APPLY ==================

semaphore = asyncio.Semaphore(1)

async def auto_apply(link):
    async with semaphore:
        print(f"\n{'='*50}\nAPPLY -> {link}")
        page = None
        immomio_page = None
        flat_id = link.rstrip("/").split("/")[-2] if "/" in link else "flat"
        try:
            # Step 1: Open SAGA page
            page = await bcontext.new_page()
            await page.goto(link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            await accept_cookies(page)

            # Step 2: Get Immomio href
            immomio_href = await page.evaluate("""
                () => {
                    const all = [...document.querySelectorAll('a')];
                    const el = all.find(e =>
                        (e.href || '').includes('immomio.com') ||
                        e.textContent.trim().includes('Zum Expos')
                    );
                    return el ? el.href : null;
                }
            """)
            if not immomio_href:
                print(f"  ❌ No Immomio link found")
                await page.close()
                return False, "no_immomio_link"

            print(f"  Immomio href: {immomio_href}")

            # Step 3: Open Immomio
            try:
                async with bcontext.expect_page(timeout=12000) as new_page_info:
                    await page.evaluate("""
                        () => {
                            const all = [...document.querySelectorAll('a')];
                            const el = all.find(e =>
                                (e.href || '').includes('immomio.com') ||
                                e.textContent.trim().includes('Zum Expos')
                            );
                            if (el) el.click();
                        }
                    """)
                immomio_page = await new_page_info.value
            except:
                immomio_page = await bcontext.new_page()
                await immomio_page.goto(immomio_href, timeout=60000, wait_until="domcontentloaded")

            await immomio_page.wait_for_load_state("domcontentloaded", timeout=15000)
            await immomio_page.wait_for_timeout(3000)
            await accept_cookies(immomio_page)
            print(f"  Immomio loaded: {immomio_page.url}")

            # Screenshot BEFORE anything
            sc1 = f"{SCREENSHOT_DIR}/{flat_id}_1_before_login.png"
            await immomio_page.screenshot(path=sc1, full_page=True)
            print(f"  📸 Screenshot: {sc1}")

            # Step 4: Login if needed
            has_login = await immomio_page.evaluate("""
                () => !!document.querySelector('input[type="email"], input[name="email"]')
            """)
            if has_login:
                print(f"  Logging in...")
                await immomio_page.locator('input[type="email"], input[name="email"]').first.fill(IMMOMIO_EMAIL)
                await immomio_page.locator('input[type="password"]').first.fill(IMMOMIO_PASSWORD)
                await immomio_page.locator('button[type="submit"]').first.click(force=True)
                await immomio_page.wait_for_timeout(8000)
                await accept_cookies(immomio_page)
                sc2 = f"{SCREENSHOT_DIR}/{flat_id}_2_after_login.png"
                await immomio_page.screenshot(path=sc2, full_page=True)
                print(f"  📸 After login: {sc2}")

            # Step 5: Click "Jetzt bewerben"
            sc3 = f"{SCREENSHOT_DIR}/{flat_id}_3_before_bewerben.png"
            await immomio_page.screenshot(path=sc3, full_page=True)
            print(f"  📸 Before bewerben: {sc3}")

            clicked = await immomio_page.evaluate("""
                () => {
                    const all = [...document.querySelectorAll('a, button, [role="button"]')];
                    const el = all.find(e =>
                        e.textContent.trim().toLowerCase().includes('jetzt bewerben') ||
                        e.textContent.trim().toLowerCase().includes('interesse bekunden')
                    );
                    if (el) { el.click(); return el.textContent.trim(); }
                    return null;
                }
            """)

            if not clicked:
                sc_fail = f"{SCREENSHOT_DIR}/{flat_id}_FAIL_no_button.png"
                await immomio_page.screenshot(path=sc_fail, full_page=True)
                print(f"  ❌ Jetzt bewerben not found. Screenshot: {sc_fail}")
                await page.close()
                await immomio_page.close()
                return False, "no_bewerben_button"

            print(f"  Clicked: '{clicked}'")
            await immomio_page.wait_for_timeout(4000)

            # Screenshot AFTER click — this shows what happens next
            sc4 = f"{SCREENSHOT_DIR}/{flat_id}_4_after_bewerben.png"
            await immomio_page.screenshot(path=sc4, full_page=True)
            print(f"  📸 AFTER bewerben click: {sc4}")

            # Dump page text to see what's on screen
            page_text = await immomio_page.evaluate("() => document.body.innerText")
            print(f"  PAGE TEXT after click:\n{page_text[:2000]}")

            # Check URL change
            print(f"  URL after click: {immomio_page.url}")

            # Check if there are more buttons/steps
            buttons_after = await immomio_page.evaluate("""
                () => [...document.querySelectorAll('button, a[role="button"], input[type="submit"]')]
                       .map(e => e.textContent.trim())
                       .filter(t => t)
            """)
            print(f"  Buttons after click: {buttons_after}")

            await page.close()
            await immomio_page.close()
            return True, "clicked"

        except Exception as e:
            print(f"  ❌ EXCEPTION: {e}")
            if immomio_page:
                try:
                    sc_err = f"{SCREENSHOT_DIR}/{flat_id}_ERROR.png"
                    await immomio_page.screenshot(path=sc_err, full_page=True)
                    print(f"  📸 Error screenshot: {sc_err}")
                except:
                    pass
            try:
                if page: await page.close()
                if immomio_page: await immomio_page.close()
            except:
                pass
            return False, str(e)

async def apply_and_notify(bot, link):
    result, reason = await auto_apply(link)
    if result:
        await bot.send_message(chat_id=CHAT_ID, text=f"✅ Заявку надіслано!\n{link}")
    else:
        await bot.send_message(chat_id=CHAT_ID, text=f"❌ Не вдалось: {reason}\n{link}")

async def scanner(tg_context: ContextTypes.DEFAULT_TYPE):
    print(f"\nSCAN: {time.strftime('%H:%M:%S')}")
    try:
        flats = await scan_saga()
        if not flats:
            return
        for link in flats:
            await save_seen(link)
            await tg_context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🏠 Нова квартира!\n{link}\n⏳ Подаю заявку..."
            )
            asyncio.create_task(apply_and_notify(tg_context.bot, link))
    except Exception as e:
        print(f"SCANNER ERROR: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Бот запущено!\n/status — статус\n/reset — скинути список")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"✅ Бот живий\n📋 Відомо квартир: {len(seen)}")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    seen.clear()
    if os.path.exists(SEEN_FILE):
        os.remove(SEEN_FILE)
    await update.message.reply_text("🔄 Скинуто!")

async def post_init(app):
    await init_browser()

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.job_queue.run_repeating(scanner, interval=SCAN_INTERVAL, first=5)
    print("BOT RUNNING...")
    app.run_polling()

if __name__ == "__main__":
    main()
