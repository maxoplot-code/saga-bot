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
    """Login to Immomio once at startup to set session cookies"""
    print("  Logging in to Immomio...")
    p = await bcontext.new_page()
    try:
        await p.goto("https://tenant.immomio.com/de/auth/login", timeout=30000, wait_until="domcontentloaded")
        await p.wait_for_timeout(3000)
        await accept_cookies(p)

        email_sel = 'input[type="email"], input[name="email"], input[name="username"]'
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
                print(f"  ✅ Immomio login OK — {url}")
            else:
                print(f"  ⚠️ Still on login page: {url}")
        else:
            print("  ⚠️ Login form not found")
    except Exception as e:
        print(f"  Login error: {e}")
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
        print(f"Found {len(elements)} listings")
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
                continue
            if link not in seen:
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
        immomio_page = None
        try:
            # Step 1: SAGA page
            page = await bcontext.new_page()
            await page.goto(link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            await accept_cookies(page)

            # Step 2: Find Immomio link
            immomio_href = await page.evaluate("""
                () => {
                    const el = [...document.querySelectorAll('a')].find(e =>
                        (e.href||'').includes('immomio.com') ||
                        e.textContent.includes('Zum Expos')
                    );
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
                async with bcontext.expect_page(timeout=12000) as new_page_info:
                    await page.evaluate("""
                        () => {
                            const el = [...document.querySelectorAll('a')].find(e =>
                                (e.href||'').includes('immomio.com') ||
                                e.textContent.includes('Zum Expos')
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
            print(f"  Immomio: {immomio_page.url}")

            # Step 4: Check if logged in
            body = await immomio_page.evaluate("() => document.body.innerText")
            not_logged = "Registrieren und bewerben" in body or "Bereits registriert" in body

            if not_logged:
                print("  Not logged in — doing login flow...")
                # Navigate to login with redirect back
                target = immomio_href.replace("/apply/", "/de/apply/")
                login_url = f"https://tenant.immomio.com/de/auth/login"
                await immomio_page.goto(login_url, timeout=30000, wait_until="domcontentloaded")
                await immomio_page.wait_for_timeout(2000)
                await accept_cookies(immomio_page)

                email_sel = 'input[type="email"], input[name="email"], input[name="username"]'
                for _ in range(10):
                    if await immomio_page.locator(email_sel).count() > 0:
                        break
                    await immomio_page.wait_for_timeout(500)

                if await immomio_page.locator(email_sel).count() > 0:
                    await immomio_page.locator(email_sel).first.fill(IMMOMIO_EMAIL)
                    await immomio_page.locator('input[type="password"]').first.fill(IMMOMIO_PASSWORD)
                    await immomio_page.locator('button[type="submit"]').first.click(force=True)
                    await immomio_page.wait_for_timeout(8000)
                    print(f"  After login: {immomio_page.url}")

                # Go to flat page
                await immomio_page.goto(target, timeout=60000, wait_until="domcontentloaded")
                await immomio_page.wait_for_timeout(3000)
                await accept_cookies(immomio_page)
                print(f"  Back to flat: {immomio_page.url}")
            else:
                print("  ✅ Already logged in")

            # Step 5: Click Jetzt bewerben
            body2 = await immomio_page.evaluate("() => document.body.innerText")
            print(f"  Has 'Jetzt bewerben': {'Jetzt bewerben' in body2}")
            print(f"  Has 'Registrieren': {'Registrieren und bewerben' in body2}")

            clicked = await immomio_page.evaluate("""
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
                await immomio_page.close()
                return False

            print(f"  Clicked: '{clicked}'")
            await immomio_page.wait_for_timeout(6000)

            # Step 6: Verify success
            body3 = await immomio_page.evaluate("() => document.body.innerText.toLowerCase()")
            print(f"  After click text (first 400):\n{body3[:400]}")

            success = any(kw in body3 for kw in [
                "erfolgreich", "eingegangen", "danke", "vielen dank",
                "successfully", "submitted", "beworben"
            ]) or "registrieren und bewerben" not in body3

            print(f"  Result: {'✅ SUCCESS' if success else '❌ FAILED'}")
            await page.close()
            await immomio_page.close()
            return success

        except Exception as e:
            print(f"  ❌ EXCEPTION: {e}")
            try:
                if page: await page.close()
                if immomio_page: await immomio_page.close()
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
