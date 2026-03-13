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

    # Pre-login to Immomio once so session cookie is saved in context
    await immomio_login_once()
    print("BROWSER INITIALIZED")

async def immomio_login_once():
    """Login to Immomio once at startup — session cookie saved in bcontext"""
    print("  Logging in to Immomio...")
    login_page = await bcontext.new_page()
    try:
        await login_page.goto("https://tenant.immomio.com/de/auth/login", timeout=60000, wait_until="domcontentloaded")
        await login_page.wait_for_timeout(3000)
        await accept_cookies(login_page)

        # Fill login form
        email_sel = 'input[type="email"], input[name="email"], input[name="username"]'
        await login_page.locator(email_sel).first.fill(IMMOMIO_EMAIL)
        await login_page.locator('input[type="password"]').first.fill(IMMOMIO_PASSWORD)
        await login_page.locator('button[type="submit"]').first.click(force=True)
        await login_page.wait_for_timeout(6000)

        url_after = login_page.url
        print(f"  Login done. URL: {url_after}")

        if "login" in url_after.lower():
            print("  ⚠️ Still on login page — check credentials!")
        else:
            print("  ✅ Immomio login successful!")
    except Exception as e:
        print(f"  ❌ Login error: {e}")
    finally:
        await login_page.close()

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
            # Step 1: Open SAGA detail page
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
                return False

            print(f"  Immomio href: {immomio_href}")

            # Step 3: Open Immomio tab
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
            print(f"  Immomio: {immomio_page.url}")

            # Step 4: Check if logged in — look for login/register buttons
            buttons_now = await immomio_page.evaluate("""
                () => [...document.querySelectorAll('a, button')]
                       .map(e => e.textContent.trim().replace(/\\s+/g,' '))
                       .filter(t => t)
            """)
            print(f"  Buttons: {buttons_now}")

            needs_login = any(
                kw in " ".join(buttons_now).lower()
                for kw in ["anmelden", "registrieren", "login", "sign in"]
            )

            if needs_login:
                print(f"  Not logged in — clicking 'Bereits registriert? Anmelden'...")

                # Click "Bereits registriert? Anmelden"
                clicked_login = await immomio_page.evaluate("""
                    () => {
                        const all = [...document.querySelectorAll('a, button')];
                        const el = all.find(e =>
                            e.textContent.toLowerCase().includes('anmelden') ||
                            e.textContent.toLowerCase().includes('bereits registriert')
                        );
                        if (el) { el.click(); return el.textContent.trim(); }
                        return null;
                    }
                """)
                print(f"  Clicked login link: '{clicked_login}'")
                await immomio_page.wait_for_timeout(3000)

                # Now fill login form
                email_sel = 'input[type="email"], input[name="email"], input[name="username"]'
                if await immomio_page.locator(email_sel).count() > 0:
                    await immomio_page.locator(email_sel).first.fill(IMMOMIO_EMAIL)
                    await immomio_page.locator('input[type="password"]').first.fill(IMMOMIO_PASSWORD)
                    await immomio_page.locator('button[type="submit"]').first.click(force=True)
                    await immomio_page.wait_for_timeout(8000)
                    print(f"  After login URL: {immomio_page.url}")
                else:
                    # Maybe it redirected to login page
                    await immomio_page.goto("https://tenant.immomio.com/de/auth/login", timeout=30000, wait_until="domcontentloaded")
                    await immomio_page.wait_for_timeout(2000)
                    await immomio_page.locator(email_sel).first.fill(IMMOMIO_EMAIL)
                    await immomio_page.locator('input[type="password"]').first.fill(IMMOMIO_PASSWORD)
                    await immomio_page.locator('button[type="submit"]').first.click(force=True)
                    await immomio_page.wait_for_timeout(8000)
                    # Go back to flat page
                    await immomio_page.goto(immomio_href, timeout=60000, wait_until="domcontentloaded")
                    await immomio_page.wait_for_timeout(3000)
                    await accept_cookies(immomio_page)
                    print(f"  Back to flat: {immomio_page.url}")
            else:
                print(f"  Already logged in ✅")

            # Step 5: Click "Jetzt bewerben"
            # Wait for it to be present
            await immomio_page.wait_for_timeout(1000)

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
                print(f"  ❌ 'Jetzt bewerben' not found after login")
                await page.close()
                await immomio_page.close()
                return False

            print(f"  Clicked: '{clicked}'")
            await immomio_page.wait_for_timeout(5000)

            # Step 6: Check result
            url_after = immomio_page.url
            page_text = await immomio_page.evaluate("() => document.body.innerText.slice(0, 500)")
            print(f"  URL after: {url_after}")
            print(f"  Text after: {page_text[:300]}")

            buttons_after = await immomio_page.evaluate("""
                () => [...document.querySelectorAll('button, a')]
                       .map(e => e.textContent.trim().replace(/\\s+/g,' '))
                       .filter(t => t && t.length < 60)
            """)
            print(f"  Buttons after: {buttons_after}")

            # Success indicators
            success_keywords = ["erfolgreich", "eingegangen", "danke", "vielen dank", "successfully", "submitted", "beworben"]
            body_lower = page_text.lower()
            success = any(kw in body_lower for kw in success_keywords)

            # Also success if URL changed to a confirmation/dashboard page
            if not success and url_after != immomio_href:
                if any(kw in url_after.lower() for kw in ["success", "confirm", "dashboard", "applications", "bewerbung"]):
                    success = True

            print(f"  Result: {'✅ SUCCESS' if success else '⚠️ UNCERTAIN — button clicked but no confirmation text found'}")

            await page.close()
            await immomio_page.close()
            return True  # button was clicked, treat as submitted

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
        await bot.send_message(chat_id=CHAT_ID, text=f"❌ Не вдалось подати заявку\n{link}")

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
