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

# ================= HELPERS ================

async def accept_cookies(page):
    for text in ["Alle akzeptieren", "Akzeptieren", "Accept all"]:
        try:
            btn = page.locator(f"text={text}")
            if await btn.count() > 0:
                await btn.first.click(force=True)
                await page.wait_for_timeout(800)
                return
        except:
            pass

async def dump_page_buttons(page, label=""):
    """Print all button/link texts for debugging"""
    texts = await page.evaluate("""
        () => [...document.querySelectorAll('a, button')]
              .map(e => e.textContent.trim().replace(/\\s+/g,' '))
              .filter(t => t.length > 0 && t.length < 80)
    """)
    print(f"  [{label}] Buttons/links on page: {texts[:40]}")

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

semaphore = asyncio.Semaphore(1)

async def auto_apply(link):
    async with semaphore:
        print(f"\n{'='*50}\nAPPLY -> {link}")
        page = None
        immomio_page = None
        try:
            page = await bcontext.new_page()
            await page.goto(link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            await accept_cookies(page)
            print(f"  [1] Loaded: {page.url}")

            # ── DEBUG: dump what's on the detail page BEFORE clicking anything ──
            await dump_page_buttons(page, "BEFORE-ANZEIGEN")

            # Also dump full HTML snippet around any button that might be "ANZEIGEN"
            snippet = await page.evaluate("""
                () => {
                    const buttons = [...document.querySelectorAll('a, button, [role=button]')];
                    return buttons.map(b => ({
                        tag: b.tagName,
                        text: b.textContent.trim().replace(/\\s+/g,' ').slice(0,80),
                        href: b.getAttribute('href') || '',
                        class: b.className || '',
                        visible: b.offsetParent !== null
                    }));
                }
            """)
            print(f"  [1] Full element dump:")
            for el in snippet:
                if el['text']:
                    print(f"       {el['tag']:8} | visible={el['visible']} | text='{el['text'][:60]}' | href='{el['href'][:60]}'")

            # ── STEP 2: Click ANZEIGEN ──
            # Try clicking it and wait for DOM change (new element appears)
            clicked_anzeigen = await page.evaluate("""
                () => {
                    const all = [...document.querySelectorAll('a, button, [role=button]')];
                    const el = all.find(e => e.textContent.trim().toUpperCase() === 'ANZEIGEN');
                    if (el) { el.click(); return true; }
                    return false;
                }
            """)
            print(f"  [2] ANZEIGEN clicked: {clicked_anzeigen}")

            if clicked_anzeigen:
                # Wait for "Zum Exposé" to appear in DOM (up to 10s)
                print(f"  [2] Waiting for 'Zum Exposé' to appear...")
                try:
                    await page.wait_for_function("""
                        () => {
                            const all = [...document.querySelectorAll('a, button')];
                            return all.some(e => e.textContent.includes('Zum Expos'));
                        }
                    """, timeout=10000)
                    print(f"  [2] 'Zum Exposé' appeared in DOM!")
                except Exception as e:
                    print(f"  [2] 'Zum Exposé' did NOT appear after 10s: {e}")
                    # Dump again to see what changed
                    await dump_page_buttons(page, "AFTER-ANZEIGEN")

            # ── STEP 3: Click "Zum Exposé" and catch new tab ──
            # Check if it's there now
            has_expose = await page.evaluate("""
                () => [...document.querySelectorAll('a, button')]
                       .some(e => e.textContent.includes('Zum Expos'))
            """)
            print(f"  [3] 'Zum Exposé' present: {has_expose}")

            if not has_expose:
                await dump_page_buttons(page, "NO-EXPOSE")
                await page.close()
                return False

            # Capture new tab
            async with bcontext.expect_page(timeout=15000) as new_page_info:
                await page.evaluate("""
                    () => {
                        const all = [...document.querySelectorAll('a, button')];
                        const el = all.find(e => e.textContent.includes('Zum Expos'));
                        if (el) el.click();
                    }
                """)

            immomio_page = await new_page_info.value
            await immomio_page.wait_for_load_state("domcontentloaded", timeout=15000)
            await immomio_page.wait_for_timeout(2000)
            print(f"  [3] New tab: {immomio_page.url}")

            if "immomio" not in immomio_page.url.lower():
                print(f"  [3] ⚠️ Not Immomio URL, checking anyway...")

            await accept_cookies(immomio_page)

            # ── STEP 4: Close modals ──
            for sel in ["button[aria-label='Close']", "button[aria-label='close']", ".modal-close button"]:
                try:
                    el = immomio_page.locator(sel)
                    if await el.count() > 0:
                        await el.first.click(force=True)
                        await immomio_page.wait_for_timeout(500)
                except:
                    pass

            # ── STEP 5: Login if needed ──
            has_login = await immomio_page.evaluate("""
                () => !!document.querySelector('input[type="email"], input[name="email"]')
            """)
            if has_login:
                print(f"  [5] Logging in to Immomio...")
                await immomio_page.locator('input[type="email"], input[name="email"]').first.fill(IMMOMIO_EMAIL)
                await immomio_page.locator('input[type="password"]').first.fill(IMMOMIO_PASSWORD)
                await immomio_page.locator('button[type="submit"]').first.click(force=True)
                await immomio_page.wait_for_timeout(8000)
                print(f"  [5] After login: {immomio_page.url}")
            else:
                print(f"  [5] Already logged in or no login form")

            # ── STEP 6: Click "Jetzt bewerben" ──
            await dump_page_buttons(immomio_page, "IMMOMIO")

            clicked = await immomio_page.evaluate("""
                () => {
                    const all = [...document.querySelectorAll('a, button, [role=button]')];
                    const el = all.find(e => e.textContent.trim().toLowerCase().includes('jetzt bewerben')
                                          || e.textContent.trim().toLowerCase().includes('interesse bekunden')
                                          || e.textContent.trim().toLowerCase().includes('bewerben'));
                    if (el) { el.click(); return el.textContent.trim(); }
                    return null;
                }
            """)

            if clicked:
                print(f"  [6] ✅ Clicked '{clicked}' — APPLICATION SUBMITTED!")
                await immomio_page.wait_for_timeout(2000)
                try:
                    await page.close()
                    await immomio_page.close()
                except:
                    pass
                return True
            else:
                print(f"  [6] ❌ 'Jetzt bewerben' not found on Immomio")
                await dump_page_buttons(immomio_page, "IMMOMIO-FAIL")
                try:
                    await page.close()
                    await immomio_page.close()
                except:
                    pass
                return False

        except Exception as e:
            print(f"  ❌ EXCEPTION: {e}")
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
        await bot.send_message(chat_id=CHAT_ID, text=f"❌ Не вдалось\n{link}")

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
    await update.message.reply_text("🤖 Бот запущено!\n/status — статус\n/reset — скинути список")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"✅ Бот живий\n📋 Відомо квартир: {len(seen)}")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    seen.clear()
    if os.path.exists(SEEN_FILE):
        os.remove(SEEN_FILE)
    await update.message.reply_text("🔄 Скинуто!")

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
