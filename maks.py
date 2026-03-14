import asyncio
import os
import time
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

os.environ["PYTHONUNBUFFERED"] = "1"

def log(msg):
    print(msg, flush=True)

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
tg_bot = None

async def send_debug(msg):
    try:
        if tg_bot:
            await tg_bot.send_message(chat_id=CHAT_ID, text=f"🔧 {msg}")
    except:
        pass

async def immomio_login(page):
    """
    Immomio uses 2-step login:
    Step 1: enter email → click Anmelden
    Step 2: password field appears → enter password → click Anmelden
    """
    log("  [LOGIN] Starting 2-step login...")

    # Step 1: Enter email
    await page.wait_for_timeout(2000)
    await accept_cookies(page)

    # Wait for email input
    try:
        await page.wait_for_selector('input[type="email"]', timeout=10000)
    except:
        log("  [LOGIN] ⚠️ Email input not found after wait")
        txt = await page.evaluate("() => document.body.innerText.slice(0,300)")
        await send_debug(f"Email input missing. Page: {txt[:200]}")
        return False

    await page.fill('input[type="email"]', IMMOMIO_EMAIL)
    log("  [LOGIN] Email filled, clicking Anmelden...")

    # Click first submit/Anmelden button
    for sel in ['button[type="submit"]']:
        if await page.locator(sel).count() > 0:
            await page.locator(sel).first.click(force=True)
            break
    else:
        await page.evaluate("""
            () => {
                const b = [...document.querySelectorAll('button')]
                    .find(e => e.textContent.trim().toLowerCase().includes('anmelden') ||
                               e.textContent.trim().toLowerCase().includes('weiter') ||
                               e.textContent.trim().toLowerCase().includes('next'));
                if (b) b.click();
            }
        """)

    # Step 2: Wait for password field to appear
    log("  [LOGIN] Waiting for password field...")
    password_appeared = False
    for _ in range(20):
        if await page.locator('input[type="password"]').count() > 0:
            password_appeared = True
            break
        await page.wait_for_timeout(500)

    if not password_appeared:
        # Maybe it's all on one page — check inputs again
        inputs = await page.evaluate("""
            () => [...document.querySelectorAll('input')].map(i => ({
                type: i.type, name: i.name, placeholder: i.placeholder
            }))
        """)
        log(f"  [LOGIN] Password not appeared. Inputs: {inputs}")
        await send_debug(f"Password field not appeared. Inputs: {inputs}")
        return False

    await page.fill('input[type="password"]', IMMOMIO_PASSWORD)
    log("  [LOGIN] Password filled, submitting...")

    for sel in ['button[type="submit"]']:
        if await page.locator(sel).count() > 0:
            await page.locator(sel).first.click(force=True)
            break
    else:
        await page.evaluate("""
            () => {
                const b = [...document.querySelectorAll('button')]
                    .find(e => e.textContent.trim().toLowerCase().includes('anmelden') ||
                               e.textContent.trim().toLowerCase().includes('einloggen'));
                if (b) b.click();
            }
        """)

    await page.wait_for_timeout(7000)
    url = page.url
    log(f"  [LOGIN] After login URL: {url}")

    if "login" in url.lower() or "auth" in url.lower():
        txt = await page.evaluate("() => document.body.innerText.slice(0,300)")
        log(f"  [LOGIN] ⚠️ Still on login. Page: {txt}")
        await send_debug(f"Still on login after submit. URL={url}\nPage: {txt[:200]}")
        return False

    log("  [LOGIN] ✅ Login successful!")
    await send_debug(f"✅ Login OK! URL: {url}")
    return True

async def init_browser():
    global playwright_instance, browser, bcontext, scan_page
    if browser:
        return
    log("Starting browser...")
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

    # Pre-login to Immomio
    p = await bcontext.new_page()
    await p.goto("https://tenant.immomio.com/de/auth/login",
                 timeout=30000, wait_until="domcontentloaded")
    await immomio_login(p)
    await p.close()

    log("BROWSER INITIALIZED")

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
    return not any(kw in link.lower() for kw in EXCLUDE_KEYWORDS)

async def scan_saga():
    global scan_page
    links = []
    try:
        await scan_page.goto(SAGA_URL, timeout=60000, wait_until="domcontentloaded")
        await scan_page.wait_for_timeout(3000)
        await accept_cookies(scan_page)
        elements = await scan_page.query_selector_all("a[href*='immo-detail']")
        log(f"Found {len(elements)} listings")
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
        log(f"New apartments: {len(links)}")
    except Exception as e:
        log(f"SCAN ERROR: {e}")
    return links

# ================= APPLY ==================

semaphore = asyncio.Semaphore(1)

async def auto_apply(link):
    async with semaphore:
        log(f"\n{'='*50}\nAPPLY -> {link}")
        page = None
        ipage = None
        try:
            # Get Immomio href
            page = await bcontext.new_page()
            await page.goto(link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            await accept_cookies(page)

            immomio_href = await page.evaluate("""
                () => {
                    const el = [...document.querySelectorAll('a')].find(e =>
                        (e.href||'').includes('immomio.com') || e.textContent.includes('Zum Expos'));
                    return el ? el.href : null;
                }
            """)
            if not immomio_href:
                log("  ❌ No Immomio link")
                await page.close()
                return False
            log(f"  href: {immomio_href}")

            # Open Immomio apply page directly
            target = immomio_href.replace("/apply/", "/de/apply/")
            ipage = await bcontext.new_page()
            await ipage.goto(target, timeout=60000, wait_until="domcontentloaded")
            await ipage.wait_for_timeout(3000)
            await accept_cookies(ipage)
            log(f"  Immomio: {ipage.url}")

            # Check login state
            body = await ipage.evaluate("() => document.body.innerText")
            not_logged = "Registrieren und bewerben" in body or "Bereits registriert" in body
            log(f"  Logged in: {not not_logged}")

            if not_logged:
                log("  Re-logging in (2-step)...")
                await ipage.goto("https://tenant.immomio.com/de/auth/login",
                                 timeout=30000, wait_until="domcontentloaded")
                ok = await immomio_login(ipage)
                if not ok:
                    log("  ❌ Login failed")
                    await page.close()
                    await ipage.close()
                    return False

                # Back to flat
                await ipage.goto(target, timeout=60000, wait_until="domcontentloaded")
                await ipage.wait_for_timeout(3000)
                await accept_cookies(ipage)
                log(f"  Back to flat: {ipage.url}")

                body2 = await ipage.evaluate("() => document.body.innerText")
                if "Registrieren und bewerben" in body2:
                    log("  ❌ Still not logged in")
                    await send_debug(f"Still not logged in after re-login")
                    await page.close()
                    await ipage.close()
                    return False

            # Click Jetzt bewerben
            log("  Clicking Jetzt bewerben...")
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
                body_d = await ipage.evaluate("() => document.body.innerText.slice(0,300)")
                log(f"  ❌ No button. Page: {body_d}")
                await send_debug(f"No Jetzt bewerben. Page: {body_d[:200]}")
                await page.close()
                await ipage.close()
                return False

            log(f"  Clicked: '{clicked}'")
            await ipage.wait_for_timeout(6000)

            body_f = await ipage.evaluate("() => document.body.innerText.toLowerCase()")
            log(f"  Final URL: {ipage.url}")
            log(f"  Final text:\n{body_f[:400]}")

            success = any(kw in body_f for kw in [
                "erfolgreich", "eingegangen", "danke", "vielen dank",
                "successfully", "submitted", "beworben", "ihre bewerbung"
            ]) or "registrieren und bewerben" not in body_f

            log(f"  {'✅ SUCCESS' if success else '❌ FAILED'}")
            if not success:
                await send_debug(f"Apply failed. Text: {body_f[:200]}")

            await page.close()
            await ipage.close()
            return success

        except Exception as e:
            log(f"  ❌ EXCEPTION: {e}")
            await send_debug(f"Exception: {e}")
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
    log(f"\nSCAN: {time.strftime('%H:%M:%S')}")
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
        log(f"SCANNER ERROR: {e}")

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
    global tg_bot
    tg_bot = app.bot
    await init_browser()

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.job_queue.run_repeating(scanner, interval=SCAN_INTERVAL, first=5)
    log("BOT RUNNING...")
    app.run_polling()

if __name__ == "__main__":
    main()
