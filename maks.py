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

    # Login to Immomio ONCE at startup — session stored in bcontext cookies
    await do_immomio_login()
    print("BROWSER INITIALIZED")

async def do_immomio_login():
    """Open Immomio login page directly and fill credentials"""
    print("Logging in to Immomio...")
    p = await bcontext.new_page()
    try:
        await p.goto(
            "https://tenant.immomio.com/de/auth/login",
            timeout=30000, wait_until="networkidle"
        )
        await accept_cookies(p)
        await p.wait_for_timeout(2000)

        # Wait for email field
        email_sel = 'input[type="email"], input[name="email"], input[placeholder*="E-Mail"], input[placeholder*="mail"]'
        await p.wait_for_selector(email_sel, timeout=10000)

        await p.fill(email_sel, IMMOMIO_EMAIL)
        await p.fill('input[type="password"]', IMMOMIO_PASSWORD)

        # Click submit
        await p.click('button[type="submit"]')
        await p.wait_for_timeout(6000)

        url = p.url
        print(f"  After login URL: {url}")
        if "login" not in url and "auth" not in url:
            print("  ✅ Immomio login successful!")
        else:
            print(f"  ⚠️ Still on login page — check credentials")
    except Exception as e:
        print(f"  ❌ Login error: {e}")
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
    return not any(kw in link.lower() for kw in EXCLUDE_KEYWORDS)

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

async def auto_apply(link):
    async with semaphore:
        print(f"\n{'='*50}\nAPPLY -> {link}")
        page = None
        ipage = None
        try:
            # Step 1: SAGA page
            page = await bcontext.new_page()
            await page.goto(link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            await accept_cookies(page)

            # Step 2: Get Immomio href
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

            # Step 3: Open Immomio directly (navigate, don't click link)
            # This way we stay in same tab and our session cookies apply
            ipage = await bcontext.new_page()
            target = immomio_href.replace("/apply/", "/de/apply/")
            await ipage.goto(target, timeout=60000, wait_until="networkidle")
            await ipage.wait_for_timeout(2000)
            await accept_cookies(ipage)
            print(f"  Immomio: {ipage.url}")

            # Step 4: Check login state
            body = await ipage.evaluate("() => document.body.innerText")
            has_register = "Registrieren und bewerben" in body
            has_anmelden = "Bereits registriert" in body
            print(f"  Shows register/login: {has_register or has_anmelden}")

            if has_register or has_anmelden:
                # Session not preserved — login again inline
                print("  Session lost — re-logging in...")
                await ipage.goto(
                    "https://tenant.immomio.com/de/auth/login",
                    timeout=30000, wait_until="networkidle"
                )
                await accept_cookies(ipage)
                await ipage.wait_for_timeout(1000)

                email_sel = 'input[type="email"], input[name="email"], input[placeholder*="mail"]'
                await ipage.wait_for_selector(email_sel, timeout=10000)
                await ipage.fill(email_sel, IMMOMIO_EMAIL)
                await ipage.fill('input[type="password"]', IMMOMIO_PASSWORD)
                await ipage.click('button[type="submit"]')
                await ipage.wait_for_timeout(6000)
                print(f"  After re-login: {ipage.url}")

                # Go back to flat
                await ipage.goto(target, timeout=60000, wait_until="networkidle")
                await ipage.wait_for_timeout(2000)
                await accept_cookies(ipage)
                print(f"  Back to flat: {ipage.url}")

            # Step 5: Verify logged in now
            body2 = await ipage.evaluate("() => document.body.innerText")
            still_register = "Registrieren und bewerben" in body2
            print(f"  Logged in: {not still_register}")

            if still_register:
                print("  ❌ Still not logged in after attempt")
                await page.close()
                await ipage.close()
                return False

            # Step 6: Click "Jetzt bewerben"
            print("  Clicking Jetzt bewerben...")
            await ipage.evaluate("""
                () => {
                    const el = [...document.querySelectorAll('a, button, [role="button"]')]
                        .find(e => e.textContent.trim().toLowerCase().includes('jetzt bewerben') ||
                                   e.textContent.trim().toLowerCase().includes('interesse bekunden'));
                    if (el) el.click();
                }
            """)
            await ipage.wait_for_timeout(6000)

            # Step 7: Confirm result
            url_final = ipage.url
            body_final = await ipage.evaluate("() => document.body.innerText.toLowerCase()")
            print(f"  Final URL: {url_final}")
            print(f"  Final text (400):\n{body_final[:400]}")

            success = any(kw in body_final for kw in [
                "erfolgreich", "eingegangen", "danke", "vielen dank",
                "successfully", "submitted", "beworben", "ihre bewerbung",
                "bewerbung erhalten"
            ])
            if not success and "registrieren und bewerben" not in body_final:
                success = True

            print(f"  {'✅ SUCCESS' if success else '❌ FAILED'}")
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
