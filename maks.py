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

async def js_click_text(page, keywords):
    """Click first element whose text matches any keyword. Returns clicked text or None."""
    kw_js = [k.lower() for k in keywords]
    kw_str = str(kw_js).replace("'", '"')
    return await page.evaluate(f"""
        () => {{
            const keywords = {kw_str};
            const all = [...document.querySelectorAll('a, button, [role="button"]')];
            const el = all.find(e => {{
                const t = e.textContent.trim().toLowerCase().replace(/\\s+/g,' ');
                return keywords.some(k => t.includes(k));
            }});
            if (el) {{ el.click(); return el.textContent.trim().replace(/\\s+/g,' '); }}
            return null;
        }}
    """)

async def auto_apply(link):
    async with semaphore:
        print(f"\n{'='*50}\nAPPLY -> {link}")
        page = None
        immomio_page = None
        try:
            # Step 1: Open SAGA page
            page = await bcontext.new_page()
            await page.goto(link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            await accept_cookies(page)

            # Step 2: Get Immomio href
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
                print(f"  ❌ No Immomio link found")
                await page.close()
                return False

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

            # ── THE CORRECT FLOW ──────────────────────────────
            #
            # Page shows: [Jetzt bewerben] [Registrieren und bewerben] [Bereits registriert? Anmelden]
            #
            # Step A: Click "Bereits registriert? Anmelden"  → login modal/page opens
            # Step B: Fill email + password → submit
            # Step C: Now logged in → click "Jetzt bewerben"
            # Step D: Confirmation
            # ─────────────────────────────────────────────────

            # Step A: Click "Bereits registriert? Anmelden"
            print(f"  [A] Clicking 'Bereits registriert? Anmelden'...")
            clicked_a = await js_click_text(immomio_page, ["bereits registriert", "anmelden", "login"])
            print(f"  [A] Clicked: '{clicked_a}'")
            await immomio_page.wait_for_timeout(3000)

            # Step B: Fill login form
            email_sel = 'input[type="email"], input[name="email"], input[name="username"]'
            pass_sel  = 'input[type="password"]'

            # Wait up to 5s for login form to appear
            login_appeared = False
            for _ in range(10):
                if await immomio_page.locator(email_sel).count() > 0:
                    login_appeared = True
                    break
                await immomio_page.wait_for_timeout(500)

            if login_appeared:
                print(f"  [B] Login form found, filling...")
                await immomio_page.locator(email_sel).first.fill(IMMOMIO_EMAIL)
                await immomio_page.locator(pass_sel).first.fill(IMMOMIO_PASSWORD)

                # Submit
                submitted = await js_click_text(immomio_page, ["einloggen", "anmelden", "login", "weiter"])
                if not submitted:
                    await immomio_page.locator('button[type="submit"]').first.click(force=True)
                print(f"  [B] Submitted login")
                await immomio_page.wait_for_timeout(8000)
                print(f"  [B] After login URL: {immomio_page.url}")
            else:
                print(f"  [B] No login form appeared — may already be logged in")

            # If redirected away from flat page, go back
            if immomio_href.split("?")[0].replace("/apply/", "/de/apply/") not in immomio_page.url:
                print(f"  [B] Navigating back to flat page...")
                await immomio_page.goto(
                    immomio_href.replace("/apply/", "/de/apply/"),
                    timeout=60000, wait_until="domcontentloaded"
                )
                await immomio_page.wait_for_timeout(3000)
                await accept_cookies(immomio_page)
                print(f"  [B] Back at: {immomio_page.url}")

            # Step C: Now click "Jetzt bewerben" for real
            print(f"  [C] Clicking 'Jetzt bewerben'...")
            clicked_c = await js_click_text(immomio_page, ["jetzt bewerben", "interesse bekunden"])
            if not clicked_c:
                print(f"  [C] ❌ 'Jetzt bewerben' not found")
                await page.close()
                await immomio_page.close()
                return False

            print(f"  [C] Clicked: '{clicked_c}'")
            await immomio_page.wait_for_timeout(6000)

            # Step D: Check confirmation
            url_after  = immomio_page.url
            body_text  = await immomio_page.evaluate("() => document.body.innerText.toLowerCase()")
            success_kw = ["erfolgreich", "eingegangen", "danke", "vielen dank",
                          "successfully", "submitted", "beworben", "bewerbung erhalten"]
            success = any(kw in body_text for kw in success_kw)

            if success:
                print(f"  [D] ✅ CONFIRMED — application submitted!")
            elif url_after != immomio_page.url:
                success = True
                print(f"  [D] ✅ URL changed — likely submitted")
            else:
                # Check if Jetzt bewerben disappeared (means form submitted)
                still_has_button = "jetzt bewerben" in body_text
                if not still_has_button:
                    success = True
                    print(f"  [D] ✅ Button gone — submitted!")
                else:
                    print(f"  [D] ⚠️ Button still there. Page text:\n{body_text[:400]}")

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
