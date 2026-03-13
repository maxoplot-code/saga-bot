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

            # ── THE REAL FLOW (discovered from logs) ──────────
            #
            # Page loads with:   [Jetzt bewerben]
            #
            # Click Jetzt bewerben →
            #   Modal opens with: [Registrieren und bewerben]
            #                     [Bereits registriert? Anmelden]
            #
            # Click "Bereits registriert? Anmelden" →
            #   Login form appears inside modal
            #
            # Fill email + password → submit →
            #   Logged in, back on flat page
            #
            # Click Jetzt bewerben again →
            #   Application submitted ✅
            # ──────────────────────────────────────────────────

            # Step 4: Click first "Jetzt bewerben" to open modal
            print("  [4] Clicking Jetzt bewerben (opens modal)...")
            await ipage.evaluate("""
                () => {
                    const el = [...document.querySelectorAll('a, button, [role="button"]')]
                        .find(e => e.textContent.trim().toLowerCase().includes('jetzt bewerben'));
                    if (el) el.click();
                }
            """)
            await ipage.wait_for_timeout(3000)

            # Step 5: In modal, click "Bereits registriert? Anmelden"
            print("  [5] Clicking 'Bereits registriert? Anmelden'...")
            clicked_login = await ipage.evaluate("""
                () => {
                    const el = [...document.querySelectorAll('a, button, [role="button"]')]
                        .find(e => e.textContent.toLowerCase().includes('bereits registriert') ||
                                   (e.textContent.toLowerCase().includes('anmelden') &&
                                    !e.textContent.toLowerCase().includes('registrieren')));
                    if (el) { el.click(); return el.textContent.trim(); }
                    return null;
                }
            """)
            print(f"  [5] Clicked: '{clicked_login}'")
            await ipage.wait_for_timeout(3000)

            # Step 6: Wait for login form and fill it
            print("  [6] Waiting for login form...")
            email_sel = 'input[type="email"], input[name="email"], input[name="username"]'
            login_appeared = False
            for _ in range(15):
                if await ipage.locator(email_sel).count() > 0:
                    login_appeared = True
                    break
                await ipage.wait_for_timeout(500)

            if login_appeared:
                print("  [6] Login form found, filling...")
                await ipage.locator(email_sel).first.fill(IMMOMIO_EMAIL)
                await ipage.locator('input[type="password"]').first.fill(IMMOMIO_PASSWORD)
                await ipage.wait_for_timeout(500)

                # Submit — try button[type=submit] first, then any submit-like button
                submitted = False
                if await ipage.locator('button[type="submit"]').count() > 0:
                    await ipage.locator('button[type="submit"]').first.click(force=True)
                    submitted = True
                if not submitted:
                    await ipage.evaluate("""
                        () => {
                            const el = [...document.querySelectorAll('button')]
                                .find(e => e.textContent.toLowerCase().includes('einloggen') ||
                                           e.textContent.toLowerCase().includes('anmelden') ||
                                           e.textContent.toLowerCase().includes('login'));
                            if (el) el.click();
                        }
                    """)
                print("  [6] Login submitted, waiting...")
                await ipage.wait_for_timeout(8000)
                print(f"  [6] URL after login: {ipage.url}")
            else:
                print("  [6] ⚠️ No login form appeared!")
                # Dump what's on screen for debugging
                btns = await ipage.evaluate("""
                    () => [...document.querySelectorAll('a, button, input')]
                           .map(e => ({tag: e.tagName, text: e.textContent.trim().slice(0,60), type: e.type||''}))
                           .filter(e => e.text)
                """)
                print(f"  [6] Current elements: {btns[:20]}")

            # If redirected away from flat, go back
            target = immomio_href.replace("/apply/", "/de/apply/")
            if target.split("?")[0] not in ipage.url:
                print(f"  Going back to flat page: {target}")
                await ipage.goto(target, timeout=60000, wait_until="domcontentloaded")
                await ipage.wait_for_timeout(3000)
                await accept_cookies(ipage)

            # Step 7: Now click "Jetzt bewerben" for real (logged in)
            body_check = await ipage.evaluate("() => document.body.innerText")
            still_unauth = "Registrieren und bewerben" in body_check
            print(f"  [7] Still shows register: {still_unauth}")
            print(f"  [7] URL: {ipage.url}")

            clicked_final = await ipage.evaluate("""
                () => {
                    const el = [...document.querySelectorAll('a, button, [role="button"]')]
                        .find(e => e.textContent.trim().toLowerCase().includes('jetzt bewerben') ||
                                   e.textContent.trim().toLowerCase().includes('interesse bekunden'));
                    if (el) { el.click(); return el.textContent.trim(); }
                    return null;
                }
            """)
            if not clicked_final:
                print("  [7] ❌ Jetzt bewerben not found")
                await page.close()
                await ipage.close()
                return False
            print(f"  [7] Clicked: '{clicked_final}'")
            await ipage.wait_for_timeout(6000)

            # Step 8: Check result
            url_final = ipage.url
            body_final = await ipage.evaluate("() => document.body.innerText.toLowerCase()")
            print(f"  [8] Final URL: {url_final}")
            print(f"  [8] Final text:\n{body_final[:400]}")

            success = any(kw in body_final for kw in [
                "erfolgreich", "eingegangen", "danke", "vielen dank",
                "successfully", "submitted", "beworben", "ihre bewerbung"
            ])
            if not success and "registrieren und bewerben" not in body_final:
                success = True
                print("  [8] ✅ Register button gone — submitted!")
            elif success:
                print("  [8] ✅ Confirmation text found!")
            else:
                print("  [8] ❌ Still showing register")

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
