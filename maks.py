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
    await browser_login()
    print("BROWSER INITIALIZED")

async def browser_login():
    print("  Logging in to Immomio...")
    p = await bcontext.new_page()
    try:
        await p.goto("https://tenant.immomio.com/de/auth/login", timeout=30000, wait_until="domcontentloaded")
        await p.wait_for_timeout(3000)
        await accept_cookies(p)
        email_sel = 'input[type="email"], input[name="email"]'
        for _ in range(10):
            if await p.locator(email_sel).count() > 0:
                break
            await p.wait_for_timeout(500)
        if await p.locator(email_sel).count() > 0:
            await p.locator(email_sel).first.fill(IMMOMIO_EMAIL)
            await p.locator('input[type="password"]').first.fill(IMMOMIO_PASSWORD)
            await p.locator('button[type="submit"]').first.click(force=True)
            await p.wait_for_timeout(6000)
            url = p.url
            if "login" not in url.lower() and "auth" not in url.lower():
                print(f"  ✅ Login OK: {url}")
            else:
                print(f"  ⚠️ Still on login: {url}")
        else:
            print("  ⚠️ No login form")
    except Exception as e:
        print(f"  Login err: {e}")
    finally:
        await p.close()

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

def is_apartment(link):
    lower = link.lower()
    return not any(kw in lower for kw in EXCLUDE_KEYWORDS)

async def scan_saga():
    global scan_page
    links = []
    try:
        await scan_page.goto(SAGA_URL, timeout=60000, wait_until="domcontentloaded")
        await scan_page.wait_for_timeout(3000)
        await accept_cookies(scan_page)
        elements = await scan_page.query_selector_all("a[href*='immo-detail']")
        print(f"Found {len(elements)} listings")
        seen_hrefs = set()
        for el in elements:
            href = await el.get_attribute("href")
            if not href:
                continue
            link = href if href.startswith("http") else "https://www.saga.hamburg" + href
            if link in seen_hrefs or not is_apartment(link) or link in seen:
                continue
            seen_hrefs.add(link)
            links.append(link)
        print(f"New apartments: {len(links)}")
    except Exception as e:
        print(f"SCAN ERROR: {e}")
    return links

# ================= APPLY ==================

semaphore = asyncio.Semaphore(1)

async def dump_buttons(page, label):
    btns = await page.evaluate("""
        () => [...document.querySelectorAll('a, button, [role="button"]')]
               .map(e => e.textContent.trim().replace(/\\s+/g,' '))
               .filter(t => t && t.length < 80)
    """)
    print(f"  [{label}] buttons: {btns}")
    return btns

async def auto_apply(link):
    async with semaphore:
        print(f"\n{'='*50}\nAPPLY -> {link}")
        page = None
        ipage = None
        try:
            # Step 1: SAGA
            page = await bcontext.new_page()
            await page.goto(link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            await accept_cookies(page)

            # Step 2: Immomio href
            immomio_href = await page.evaluate("""
                () => {
                    const el = [...document.querySelectorAll('a')].find(e =>
                        (e.href||'').includes('immomio.com') || e.textContent.includes('Zum Expos'));
                    return el ? el.href : null;
                }
            """)
            if not immomio_href:
                print("  ❌ No Immomio link")
                await page.close()
                return False
            print(f"  href: {immomio_href}")

            # Step 3: Open Immomio
            try:
                async with bcontext.expect_page(timeout=12000) as npi:
                    await page.evaluate("""
                        () => {
                            const el = [...document.querySelectorAll('a')].find(e =>
                                (e.href||'').includes('immomio.com') || e.textContent.includes('Zum Expos'));
                            if (el) el.click();
                        }
                    """)
                ipage = await npi.value
            except:
                ipage = await bcontext.new_page()
                await ipage.goto(immomio_href, timeout=60000, wait_until="domcontentloaded")

            await ipage.wait_for_load_state("domcontentloaded", timeout=15000)
            await ipage.wait_for_timeout(3000)
            await accept_cookies(ipage)
            print(f"  Immomio: {ipage.url}")

            # Step 4: If not logged in, do login
            body = await ipage.evaluate("() => document.body.innerText")
            if "Registrieren und bewerben" in body or "Bereits registriert" in body:
                print("  Not logged in, doing login...")
                await ipage.goto("https://tenant.immomio.com/de/auth/login", timeout=30000, wait_until="domcontentloaded")
                await ipage.wait_for_timeout(2000)
                await accept_cookies(ipage)
                email_sel = 'input[type="email"], input[name="email"]'
                for _ in range(10):
                    if await ipage.locator(email_sel).count() > 0:
                        break
                    await ipage.wait_for_timeout(500)
                if await ipage.locator(email_sel).count() > 0:
                    await ipage.locator(email_sel).first.fill(IMMOMIO_EMAIL)
                    await ipage.locator('input[type="password"]').first.fill(IMMOMIO_PASSWORD)
                    await ipage.locator('button[type="submit"]').first.click(force=True)
                    await ipage.wait_for_timeout(8000)
                target = immomio_href.replace("/apply/", "/de/apply/")
                await ipage.goto(target, timeout=60000, wait_until="domcontentloaded")
                await ipage.wait_for_timeout(3000)
                await accept_cookies(ipage)
            else:
                print("  ✅ Logged in")

            # Step 5: Click "Jetzt bewerben"
            await dump_buttons(ipage, "BEFORE-CLICK")

            clicked = await ipage.evaluate("""
                () => {
                    const el = [...document.querySelectorAll('a, button, [role="button"]')]
                        .find(e => e.textContent.trim().toLowerCase().includes('jetzt bewerben') ||
                                   e.textContent.trim().toLowerCase().includes('interesse bekunden'));
                    if (el) { el.click(); return el.textContent.trim(); }
                    return null;
                }
            """)
            if not clicked:
                print("  ❌ Jetzt bewerben not found")
                await page.close()
                await ipage.close()
                return False
            print(f"  Clicked: '{clicked}'")

            # Step 6: Wait for modal/dialog/new content to appear
            # After click, a modal opens — wait for it
            await ipage.wait_for_timeout(3000)
            await dump_buttons(ipage, "AFTER-CLICK-3s")

            # Check for any new buttons in a modal (confirm dialog)
            # Immomio shows a confirmation modal with another "Jetzt bewerben" or "Bestätigen"
            confirm_clicked = await ipage.evaluate("""
                () => {
                    // Look for modal/dialog first
                    const modal = document.querySelector('[role="dialog"], .modal, [class*="modal"], [class*="overlay"], [class*="Dialog"]');
                    if (modal) {
                        const btn = [...modal.querySelectorAll('button, a, [role="button"]')]
                            .find(e => {
                                const t = e.textContent.trim().toLowerCase();
                                return t.includes('bewerben') || t.includes('bestätigen') ||
                                       t.includes('confirm') || t.includes('senden') ||
                                       t.includes('weiter') || t.includes('absenden');
                            });
                        if (btn) { btn.click(); return 'modal:' + btn.textContent.trim(); }
                    }
                    // No modal found — try page-level confirm buttons
                    const btn = [...document.querySelectorAll('button, a, [role="button"]')]
                        .find(e => {
                            const t = e.textContent.trim().toLowerCase();
                            return t.includes('bestätigen') || t.includes('confirm') ||
                                   t.includes('absenden') || t.includes('senden');
                        });
                    if (btn) { btn.click(); return 'page:' + btn.textContent.trim(); }
                    return null;
                }
            """)
            if confirm_clicked:
                print(f"  Confirm clicked: '{confirm_clicked}'")
                await ipage.wait_for_timeout(5000)
            else:
                print("  No confirm button found (may not be needed)")
                await ipage.wait_for_timeout(3000)

            # Step 7: Final state
            await dump_buttons(ipage, "FINAL")
            body_final = await ipage.evaluate("() => document.body.innerText.toLowerCase()")
            url_final = ipage.url
            print(f"  Final URL: {url_final}")
            print(f"  Final text (500):\n{body_final[:500]}")

            success = any(kw in body_final for kw in [
                "erfolgreich", "eingegangen", "danke", "vielen dank",
                "successfully", "submitted", "beworben", "bewerbung erhalten",
                "ihre bewerbung"
            ])
            # Also check if URL changed to a success/applications page
            if not success and any(kw in url_final for kw in ["success", "applications", "dashboard", "bewerbung"]):
                success = True

            print(f"  Result: {'✅ SUCCESS' if success else '❌ FAILED'}")
            await page.close()
            await ipage.close()
            return success

        except Exception as e:
            print(f"  ❌ EXCEPTION: {e}")
            try:
                if page: await page.close()
                if ipage: await ipage.close()
            except:
                pass
            return False

async def apply_and_notify(bot, link):
    result = await auto_apply(link)
    if result:
        await bot.send_message(chat_id=CHAT_ID, text=f"✅ Заявку надіслано!\n{link}")
    else:
        await bot.send_message(chat_id=CHAT_ID, text=f"❌ Не вдалось\n{link}")

async def scanner(tg_context: ContextTypes.DEFAULT_TYPE):
    print(f"\nSCAN: {time.strftime('%H:%M:%S')}")
    try:
        flats = await scan_saga()
        if not flats:
            return
        for link in flats:
            await save_seen(link)
            await tg_context.bot.send_message(
                chat_id=CHAT_ID, text=f"🏠 Нова квартира!\n{link}\n⏳ Подаю заявку..."
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
