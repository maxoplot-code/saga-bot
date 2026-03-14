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
SCAN_INTERVAL = 45  # скануємо частіше
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

async def immomio_login(page):
    await page.wait_for_timeout(1500)
    await accept_cookies(page)

    # Step 1: email
    try:
        await page.wait_for_selector('input[type="email"]', timeout=8000)
        await page.fill('input[type="email"]', IMMOMIO_EMAIL)
        await page.locator('button[type="submit"]').first.click(force=True)
        await page.wait_for_timeout(3000)
    except Exception as e:
        log(f"  Login step1 error: {e}")
        return False

    # Step 2: SSO Keycloak
    if "sso.immomio.com" in page.url or "openid-connect" in page.url:
        await accept_cookies(page)
        await page.wait_for_timeout(800)
        try:
            await page.wait_for_selector('input[name="username"]', timeout=8000)
        except:
            pass
        for sel in ['input[name="username"]', 'input[id="username"]', 'input[type="text"]']:
            if await page.locator(sel).count() > 0:
                await page.locator(sel).first.fill(IMMOMIO_EMAIL)
                break
        for sel in ['input[name="password"]', 'input[id="password"]', 'input[type="password"]']:
            if await page.locator(sel).count() > 0:
                await page.locator(sel).first.fill(IMMOMIO_PASSWORD)
                break
        for sel in ['input[type="submit"]', 'button[type="submit"]']:
            if await page.locator(sel).count() > 0:
                await page.locator(sel).first.click(force=True)
                break
        await page.wait_for_timeout(5000)

    url = page.url
    success = "sso.immomio.com" not in url and "login" not in url and "auth" not in url
    log(f"  Login {'✅ OK' if success else '❌ FAILED'}: {url}")
    return success

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
                await page.wait_for_timeout(500)
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
        await scan_page.wait_for_timeout(2000)  # було 3000
        await accept_cookies(scan_page)
        elements = await scan_page.query_selector_all("a[href*='immo-detail']")
        log(f"Scan: {len(elements)} listings")
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
        log(f"New: {len(links)}")
    except Exception as e:
        log(f"SCAN ERROR: {e}")
    return links

# ================= APPLY ==================

# 3 паралельних заявки одночасно
semaphore = asyncio.Semaphore(3)

async def auto_apply(link):
    async with semaphore:
        log(f"APPLY -> {link}")
        page = None
        ipage = None
        try:
            page = await bcontext.new_page()
            await page.goto(link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)  # було 3000
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

            target = immomio_href.replace("/apply/", "/de/apply/")
            ipage = await bcontext.new_page()
            await ipage.goto(target, timeout=60000, wait_until="domcontentloaded")
            await ipage.wait_for_timeout(2000)  # було 3000
            await accept_cookies(ipage)

            # Re-login if session expired
            body = await ipage.evaluate("() => document.body.innerText")
            if "Registrieren und bewerben" in body or "Bereits registriert" in body:
                log("  Session expired, re-logging...")
                await ipage.goto("https://tenant.immomio.com/de/auth/login",
                                 timeout=30000, wait_until="domcontentloaded")
                ok = await immomio_login(ipage)
                if not ok:
                    await page.close()
                    await ipage.close()
                    return False
                await ipage.goto(target, timeout=60000, wait_until="domcontentloaded")
                await ipage.wait_for_timeout(2000)
                await accept_cookies(ipage)

            # Click Jetzt bewerben
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
                log("  ❌ Jetzt bewerben not found")
                await page.close()
                await ipage.close()
                return False

            await ipage.wait_for_timeout(4000)  # було 6000

            url_f  = ipage.url
            body_f = await ipage.evaluate("() => document.body.innerText.toLowerCase()")

            success = (
                "applications" in url_f or
                "expose" in url_f or
                any(kw in body_f for kw in ["erfolgreich", "eingegangen", "danke",
                                             "beworben", "ihre bewerbung"]) or
                "registrieren und bewerben" not in body_f
            )

            log(f"  {'✅' if success else '❌'} {url_f}")
            await page.close()
            await ipage.close()
            return success

        except Exception as e:
            log(f"  ❌ {e}")
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
    log(f"SCAN: {time.strftime('%H:%M:%S')}")
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
    await update.message.reply_text(
        "🤖 Бот запущено!\n"
        "Сканую SAGA кожні 45 секунд.\n\n"
        "/status — кількість відомих квартир\n"
        "/reset — скинути список"
    )

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
    log("BOT RUNNING...")
    app.run_polling()

if __name__ == "__main__":
    main()
