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
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    )
    scan_page = await browser.new_page()
    await scan_page.set_extra_http_headers({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    print("BROWSER INITIALIZED")

# ================= HELPERS ================

async def accept_cookies(page):
    for text in ["Alle akzeptieren", "Akzeptieren", "Accept all"]:
        try:
            btn = page.locator(f"text={text}")
            if await btn.count() > 0:
                await btn.first.click(force=True)
                await page.wait_for_timeout(1000)
                return
        except:
            pass

async def click_button(page, texts, timeout=5000):
    """Try clicking buttons by text list, return True if clicked"""
    for text in texts:
        try:
            # Exact match
            btn = page.locator(f"text={text}")
            if await btn.count() > 0:
                print(f"  ✅ Clicking: '{text}'")
                await btn.first.scroll_into_view_if_needed()
                await btn.first.click(force=True)
                await page.wait_for_timeout(timeout)
                return True
        except Exception as e:
            print(f"  Button '{text}' err: {e}")

    # Case-insensitive fallback via JS
    for text in texts:
        try:
            btn = page.locator(f"button, a").filter(has_text=text)
            if await btn.count() > 0:
                print(f"  ✅ Clicking (filter): '{text}'")
                await btn.first.scroll_into_view_if_needed()
                await btn.first.click(force=True)
                await page.wait_for_timeout(timeout)
                return True
        except:
            pass

    return False

# ================= SCAN ===================

async def scan_saga():
    global scan_page
    links = []
    try:
        await scan_page.goto(SAGA_URL, timeout=60000, wait_until="domcontentloaded")
        await scan_page.wait_for_timeout(3000)
        await accept_cookies(scan_page)

        elements = await scan_page.query_selector_all("a[href*='immo-detail']")
        print(f"Found {len(elements)} listings")

        for el in elements:
            href = await el.get_attribute("href")
            if not href:
                continue
            link = href if href.startswith("http") else "https://www.saga.hamburg" + href
            lower = link.lower()
            if any(x in lower for x in ["gewerbe", "einstellplatz", "garage", "stellplatz"]):
                continue
            if link not in seen:
                links.append(link)
    except Exception as e:
        print(f"SCAN ERROR: {e}")
    return list(set(links))

# ================= APPLY ==================

semaphore = asyncio.Semaphore(2)

async def auto_apply(link):
    async with semaphore:
        print(f"\n{'='*50}\nAPPLY -> {link}")
        page = None
        try:
            page = await browser.new_page()
            await page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            })

            # ── STEP 1: Open SAGA flat page ──────────────────
            await page.goto(link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            await accept_cookies(page)
            print(f"  Step 1 loaded: {page.url}")

            # ── STEP 2: Click "ANZEIGEN" ─────────────────────
            clicked = await click_button(page, ["ANZEIGEN", "Anzeigen"], timeout=4000)
            if not clicked:
                print(f"  ⚠️ ANZEIGEN not found, trying next step anyway")
            else:
                print(f"  Step 2 done (ANZEIGEN), URL: {page.url}")

            # ── STEP 3: Click "Zum Exposé" ───────────────────
            clicked = await click_button(page, ["Zum Exposé", "ZUM EXPOSÉ", "Zum Expose", "ZUM EXPOSE"], timeout=6000)
            if not clicked:
                print(f"  ❌ 'Zum Exposé' not found")
                # Debug
                all_els = await page.query_selector_all("a, button")
                texts = []
                for el in all_els:
                    try:
                        t = (await el.inner_text()).strip()
                        if t:
                            texts.append(t[:60])
                    except:
                        pass
                print(f"  All buttons/links: {texts[:30]}")
                if page:
                    await page.close()
                return False

            print(f"  Step 3 done (Zum Exposé), URL: {page.url}")
            await page.wait_for_timeout(3000)

            # ── STEP 4: Handle Immomio ───────────────────────
            # Sometimes opens in new tab
            all_pages = browser.contexts[0].pages if browser.contexts else [page]
            immomio_page = None
            for p in all_pages:
                if "immomio" in p.url.lower():
                    immomio_page = p
                    break

            if immomio_page is None:
                # Maybe redirect happened on same page
                if "immomio" in page.url.lower():
                    immomio_page = page
                else:
                    # Wait a bit more
                    await page.wait_for_timeout(3000)
                    if "immomio" in page.url.lower():
                        immomio_page = page

            if immomio_page is None:
                print(f"  ❌ No Immomio page found. Current URL: {page.url}")
                if page:
                    await page.close()
                return False

            print(f"  Step 4: on Immomio: {immomio_page.url}")

            # Accept cookies on Immomio
            await accept_cookies(immomio_page)

            # Close any modal/popup
            for sel in ["button[aria-label='Close']", "button[aria-label='close']", "[data-dismiss='modal']"]:
                try:
                    el = immomio_page.locator(sel)
                    if await el.count() > 0:
                        await el.first.click(force=True)
                        await immomio_page.wait_for_timeout(500)
                except:
                    pass

            try:
                await immomio_page.wait_for_load_state("networkidle", timeout=10000)
            except:
                pass

            # ── STEP 5: Login on Immomio if needed ──────────
            email_sel = 'input[type="email"], input[name="email"], input[name="username"]'
            if await immomio_page.locator(email_sel).count() > 0:
                print(f"  Step 5: Logging in...")
                await immomio_page.locator(email_sel).first.fill(IMMOMIO_EMAIL)
                await immomio_page.locator('input[type="password"]').first.fill(IMMOMIO_PASSWORD)
                await immomio_page.locator('button[type="submit"]').first.click(force=True)
                await immomio_page.wait_for_timeout(8000)
                print(f"  After login: {immomio_page.url}")
            else:
                print(f"  Step 5: Already logged in")

            # ── STEP 6: Click "Jetzt bewerben" ──────────────
            clicked = await click_button(
                immomio_page,
                ["Jetzt bewerben", "jetzt bewerben", "JETZT BEWERBEN"],
                timeout=4000
            )
            if clicked:
                print(f"  ✅ SUCCESS! Application submitted for {link}")
                await immomio_page.wait_for_timeout(2000)
                try:
                    await page.close()
                except:
                    pass
                return True
            else:
                # Debug Immomio buttons
                all_btns = await immomio_page.query_selector_all("button, a")
                btn_texts = []
                for el in all_btns:
                    try:
                        t = (await el.inner_text()).strip()
                        if t:
                            btn_texts.append(t[:60])
                    except:
                        pass
                print(f"  ❌ 'Jetzt bewerben' not found. Immomio buttons: {btn_texts[:30]}")
                try:
                    await page.close()
                except:
                    pass
                return False

        except Exception as e:
            print(f"  ❌ EXCEPTION: {e}")
            if page:
                try:
                    await page.close()
                except:
                    pass
            return False

# ================= BACKGROUND APPLY =======

async def apply_and_notify(bot, link):
    result = await auto_apply(link)
    if result:
        await bot.send_message(chat_id=CHAT_ID, text=f"✅ Заявку надіслано!\n{link}")
    else:
        await bot.send_message(chat_id=CHAT_ID, text=f"❌ Не вдалось подати заявку\n{link}\n(деталі в консолі)")

# ================= WORKER =================

async def scanner(context: ContextTypes.DEFAULT_TYPE):
    print(f"\nSCAN: {time.strftime('%H:%M:%S')}")
    try:
        flats = await scan_saga()
        print(f"New flats: {len(flats)}")
        if not flats:
            return
        for link in flats:
            await save_seen(link)
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"🏠 Нова квартира!\n{link}\n⏳ Подаю заявку..."
            )
            asyncio.create_task(apply_and_notify(context.bot, link))
    except Exception as e:
        print(f"SCANNER ERROR: {e}")

# ================= TELEGRAM COMMANDS ======

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Бот запущено!\n"
        "Сканую SAGA кожні 60 секунд.\n\n"
        "Команди:\n"
        "/status — статус бота\n"
        "/reset — скинути список переглянутих квартир"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"✅ Бот живий\n📋 Відомо квартир: {len(seen)}")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    seen.clear()
    if os.path.exists(SEEN_FILE):
        os.remove(SEEN_FILE)
    await update.message.reply_text("🔄 Скинуто! На наступному скані перевірить всі квартири.")

# ================= MAIN ===================

async def post_init(app):
    await init_browser()

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset))
    app.job_queue.run_repeating(scanner, interval=SCAN_INTERVAL, first=5)
    print("BOT RUNNING...")
    app.run_polling()

if __name__ == "__main__":
    main()
