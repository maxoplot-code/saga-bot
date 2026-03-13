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

playwright_instance = None
browser = None
context = None  # single browser context to track all pages/tabs
scan_page = None

async def init_browser():
    global playwright_instance, browser, context, scan_page
    if browser:
        return
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    )
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    scan_page = await context.new_page()
    print("BROWSER INITIALIZED")

# ================= HELPERS ================

async def accept_cookies(page):
    for text in ["Alle akzeptieren", "Akzeptieren", "Accept all", "alle akzeptieren"]:
        try:
            btn = page.locator(f"text={text}")
            if await btn.count() > 0:
                await btn.first.click(force=True)
                await page.wait_for_timeout(800)
                return
        except:
            pass

async def force_click_text(page, texts, wait_ms=4000):
    """
    Click button/link by text using JavaScript — bypasses visibility checks.
    Returns True if clicked.
    """
    for text in texts:
        try:
            # JS click — works even if element is off-screen or hidden
            clicked = await page.evaluate(f"""
                () => {{
                    const all = [...document.querySelectorAll('a, button, input[type=submit], [role=button]')];
                    const el = all.find(e => e.textContent.trim().toUpperCase().includes('{text.upper()}'));
                    if (el) {{ el.click(); return true; }}
                    return false;
                }}
            """)
            if clicked:
                print(f"  ✅ JS-clicked: '{text}'")
                await page.wait_for_timeout(wait_ms)
                return True
        except Exception as e:
            print(f"  JS click '{text}' err: {e}")
    return False

async def wait_for_immomio_tab(timeout_ms=10000):
    """Wait for a new Immomio tab to open, return it"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for p in context.pages:
            if "immomio" in p.url.lower():
                return p
        await asyncio.sleep(0.5)
    return None

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

semaphore = asyncio.Semaphore(1)  # one at a time to avoid tab confusion

async def auto_apply(link):
    async with semaphore:
        print(f"\n{'='*50}\nAPPLY -> {link}")
        page = None
        try:
            # ── STEP 1: Open SAGA flat page ──────────────────
            page = await context.new_page()
            await page.goto(link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            await accept_cookies(page)
            print(f"  [1] Loaded: {page.url}")

            # ── STEP 2: Click "ANZEIGEN" ─────────────────────
            clicked = await force_click_text(page, ["ANZEIGEN", "Anzeigen"], wait_ms=3000)
            if not clicked:
                print(f"  [2] ANZEIGEN not found — continuing anyway")
            else:
                print(f"  [2] ANZEIGEN clicked")

            # ── STEP 3: Click "Zum Exposé" → opens new tab ───
            # Before clicking, set up listener for new tab
            async with context.expect_page(timeout=15000) as new_page_info:
                clicked = await force_click_text(page, ["Zum Exposé", "ZUM EXPOSÉ", "Zum Expose", "ZUM EXPOSE"], wait_ms=1000)
                if not clicked:
                    print(f"  [3] ❌ 'Zum Exposé' not found")
                    # debug
                    all_texts = await page.evaluate("""
                        () => [...document.querySelectorAll('a, button')].map(e => e.textContent.trim()).filter(t => t).slice(0, 30)
                    """)
                    print(f"  [3] Available: {all_texts}")
                    await page.close()
                    return False

            immomio_page = await new_page_info.value
            print(f"  [3] New tab opened: {immomio_page.url}")

        except Exception as e:
            # New tab might not have opened — check existing tabs
            print(f"  [3] expect_page err: {e}")
            immomio_page = await wait_for_immomio_tab(5000)
            if not immomio_page:
                # Maybe redirect happened on same page
                if page and "immomio" in page.url.lower():
                    immomio_page = page
                else:
                    print(f"  [3] ❌ No Immomio tab found")
                    if page:
                        await page.close()
                    return False

        try:
            await immomio_page.wait_for_load_state("domcontentloaded", timeout=15000)
            await immomio_page.wait_for_timeout(2000)
            await accept_cookies(immomio_page)
            print(f"  [4] Immomio loaded: {immomio_page.url}")

            # Close any modal
            for sel in ["button[aria-label='Close']", "button[aria-label='close']", ".modal-close button"]:
                try:
                    el = immomio_page.locator(sel)
                    if await el.count() > 0:
                        await el.first.click(force=True)
                        await immomio_page.wait_for_timeout(500)
                except:
                    pass

            # ── STEP 5: Login if needed ──────────────────────
            has_email = await immomio_page.evaluate("""
                () => !!document.querySelector('input[type="email"], input[name="email"], input[name="username"]')
            """)
            if has_email:
                print(f"  [5] Logging in...")
                await immomio_page.evaluate(f"""
                    () => {{
                        const email = document.querySelector('input[type="email"], input[name="email"], input[name="username"]');
                        const pass  = document.querySelector('input[type="password"]');
                        if (email) email.value = '{IMMOMIO_EMAIL}';
                        if (pass)  pass.value  = '{IMMOMIO_PASSWORD}';
                    }}
                """)
                # Trigger React input events
                await immomio_page.locator('input[type="email"], input[name="email"]').first.fill(IMMOMIO_EMAIL)
                await immomio_page.locator('input[type="password"]').first.fill(IMMOMIO_PASSWORD)
                await immomio_page.locator('button[type="submit"]').first.click(force=True)
                await immomio_page.wait_for_timeout(8000)
                print(f"  [5] After login: {immomio_page.url}")
            else:
                print(f"  [5] Already logged in")

            # ── STEP 6: Click "Jetzt bewerben" ──────────────
            clicked = await force_click_text(
                immomio_page,
                ["Jetzt bewerben", "JETZT BEWERBEN", "Interesse bekunden", "Bewerben"],
                wait_ms=4000
            )
            if clicked:
                print(f"  [6] ✅ APPLICATION SUBMITTED!")
                await immomio_page.wait_for_timeout(2000)
                try:
                    await page.close()
                    await immomio_page.close()
                except:
                    pass
                return True
            else:
                # Debug
                btn_texts = await immomio_page.evaluate("""
                    () => [...document.querySelectorAll('button, a')].map(e => e.textContent.trim()).filter(t => t).slice(0, 40)
                """)
                print(f"  [6] ❌ Jetzt bewerben not found. Buttons: {btn_texts}")
                try:
                    await page.close()
                    await immomio_page.close()
                except:
                    pass
                return False

        except Exception as e:
            print(f"  ❌ EXCEPTION on Immomio: {e}")
            try:
                if page: await page.close()
                if immomio_page: await immomio_page.close()
            except:
                pass
            return False

# ================= BACKGROUND APPLY =======

async def apply_and_notify(bot, link):
    result = await auto_apply(link)
    if result:
        await bot.send_message(chat_id=CHAT_ID, text=f"✅ Заявку надіслано!\n{link}")
    else:
        await bot.send_message(chat_id=CHAT_ID, text=f"❌ Не вдалось подати заявку\n{link}")

# ================= WORKER =================

async def scanner(tg_context: ContextTypes.DEFAULT_TYPE):
    print(f"\nSCAN: {time.strftime('%H:%M:%S')}")
    try:
        flats = await scan_saga()
        print(f"New flats: {len(flats)}")
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

# ================= TELEGRAM COMMANDS ======

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Бот запущено!\nСканую SAGA кожні 60 секунд.\n\n/status — статус\n/reset — скинути список"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"✅ Бот живий\n📋 Відомо квартир: {len(seen)}")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    seen.clear()
    if os.path.exists(SEEN_FILE):
        os.remove(SEEN_FILE)
    await update.message.reply_text("🔄 Скинуто! Наступний скан перевірить всі квартири.")

# ================= MAIN ===================

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
