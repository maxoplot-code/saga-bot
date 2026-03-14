import asyncio
import os
import time
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

os.environ["PYTHONUNBUFFERED"] = "1"
def log(msg): print(msg, flush=True)

TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"
IMMOMIO_EMAIL = "maksymsheveliuk@gmail.com"
IMMOMIO_PASSWORD = "Maksoplot2007"
SAGA_URL = "https://www.saga.hamburg/immobiliensuche?Kategorie=APARTMENT"
BESICHTIGUNGEN_URL = "https://tenant.immomio.com/de/properties/applications?tab=appointments"
SCAN_INTERVAL = 30
INV_INTERVAL = 20

EXCLUDE_KEYWORDS = ["gewerbe","einstellplatz","garage","stellplatz","buroflache",
                    "büroflache","buro","büro","praxis","existenzgrunder","lager","laden","shop"]

seen = set()
if os.path.exists("seen.txt"):
    with open("seen.txt") as f:
        for l in f: seen.add(l.strip())

seen_inv = set()
if os.path.exists("seen_inv.txt"):
    with open("seen_inv.txt") as f:
        for l in f: seen_inv.add(l.strip())

def save_seen(link):
    seen.add(link)
    with open("seen.txt","a") as f: f.write(link+"\n")

def save_inv(i):
    seen_inv.add(i)
    with open("seen_inv.txt","a") as f: f.write(i+"\n")

playwright_instance = browser = bcontext = scan_page = inv_page = None

async def accept_cookies(page):
    for t in ["Alle akzeptieren","Alles akzeptieren","Alle erlauben"]:
        try:
            b = page.locator(f"text={t}")
            if await b.count() > 0:
                await b.first.click(force=True)
                await page.wait_for_timeout(400)
                return
        except: pass

async def immomio_login(page):
    await page.wait_for_timeout(1000)
    await accept_cookies(page)
    try:
        await page.wait_for_selector('input[type="email"]', timeout=8000)
        await page.fill('input[type="email"]', IMMOMIO_EMAIL)
        await page.locator('button[type="submit"]').first.click(force=True)
        await page.wait_for_timeout(2500)
    except Exception as e:
        log(f"Login step1: {e}"); return False

    if "sso.immomio.com" in page.url:
        try: await page.wait_for_selector('input[name="username"]', timeout=6000)
        except: pass
        for s in ['input[name="username"]','input[type="text"]']:
            if await page.locator(s).count()>0:
                await page.locator(s).first.fill(IMMOMIO_EMAIL); break
        for s in ['input[name="password"]','input[type="password"]']:
            if await page.locator(s).count()>0:
                await page.locator(s).first.fill(IMMOMIO_PASSWORD); break
        for s in ['input[type="submit"]','button[type="submit"]']:
            if await page.locator(s).count()>0:
                await page.locator(s).first.click(force=True); break
        await page.wait_for_timeout(4000)

    ok = "sso.immomio.com" not in page.url and "auth" not in page.url
    log(f"Login {'✅' if ok else '❌'}: {page.url}")
    return ok

async def init_browser():
    global playwright_instance, browser, bcontext, scan_page, inv_page
    if browser: return
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(
        headless=True, args=["--disable-blink-features=AutomationControlled","--no-sandbox"])
    bcontext = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        viewport={"width":1280,"height":900})
    scan_page = await bcontext.new_page()
    inv_page = await bcontext.new_page()
    p = await bcontext.new_page()
    await p.goto("https://tenant.immomio.com/de/auth/login", timeout=30000, wait_until="domcontentloaded")
    await immomio_login(p)
    await p.close()
    log("BROWSER INITIALIZED")

def is_apartment(link):
    return not any(k in link.lower() for k in EXCLUDE_KEYWORDS)

# ================= INVITATIONS ============

async def check_invitations(tg_context: ContextTypes.DEFAULT_TYPE):
    """Check Besichtigungen tab every 20s and accept instantly"""
    log(f"INV CHECK: {time.strftime('%H:%M:%S')}")
    try:
        await inv_page.goto(BESICHTIGUNGEN_URL, timeout=20000, wait_until="domcontentloaded")
        await inv_page.wait_for_timeout(1500)
        await accept_cookies(inv_page)

        # Re-login if needed
        body = await inv_page.evaluate("() => document.body.innerText")
        if "login" in inv_page.url or "Anmelden" in body[:200]:
            await inv_page.goto("https://tenant.immomio.com/de/auth/login",
                                timeout=20000, wait_until="domcontentloaded")
            await immomio_login(inv_page)
            await inv_page.goto(BESICHTIGUNGEN_URL, timeout=20000, wait_until="domcontentloaded")
            await inv_page.wait_for_timeout(1500)
            body = await inv_page.evaluate("() => document.body.innerText")

        # Click Besichtigungen tab if needed
        besi_tab = inv_page.locator("text=Besichtigungen")
        if await besi_tab.count() > 0:
            await besi_tab.first.click(force=True)
            await inv_page.wait_for_timeout(1000)
            body = await inv_page.evaluate("() => document.body.innerText")

        log(f"  Besichtigungen page loaded")

        # Accept keywords for buttons on invitation cards
        accept_kws = [
            "einladung annehmen", "termin annehmen", "termin bestätigen",
            "zusagen", "annehmen", "bestätigen", "ja", "accept",
            "wunschtermin", "termin auswählen"
        ]

        # Find all accept buttons on the page
        clicked_any = await inv_page.evaluate(f"""
            () => {{
                const kws = {accept_kws};
                const all = [...document.querySelectorAll('button, a, [role="button"]')];
                const results = [];
                for (const el of all) {{
                    const t = el.textContent.trim().toLowerCase();
                    if (kws.some(k => t.includes(k)) && t.length < 60) {{
                        el.click();
                        results.push(el.textContent.trim());
                    }}
                }}
                return results;
            }}
        """)

        if clicked_any:
            log(f"  ✅ Clicked invitations: {clicked_any}")
            await inv_page.wait_for_timeout(3000)

            # After clicking, check for time slot confirmation
            await inv_page.evaluate("""
                () => {
                    // Select first available time slot
                    const radio = document.querySelector('input[type="radio"]');
                    if (radio) radio.click();

                    // Click confirm/submit
                    const btns = [...document.querySelectorAll('button, a')];
                    const confirm = btns.find(b => {
                        const t = b.textContent.trim().toLowerCase();
                        return t.includes('bestätigen') || t.includes('absenden') ||
                               t.includes('senden') || t.includes('weiter') ||
                               t.includes('confirm');
                    });
                    if (confirm) confirm.click();
                }
            """)
            await inv_page.wait_for_timeout(2000)

            # Get page content for notification
            final_body = await inv_page.evaluate("() => document.body.innerText.slice(0,300)")

            for btn_text in clicked_any:
                inv_key = f"{btn_text}_{int(time.time()//3600)}"  # unique per hour
                if inv_key not in seen_inv:
                    save_inv(inv_key)
                    await tg_context.bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"🗓 Запрошення прийнято!\nКнопка: '{btn_text}'\n\n{final_body[:200]}"
                    )
        else:
            # Check if there's "Noch keine Besichtigungstermine" = no invitations
            if "noch keine" not in body.lower():
                log(f"  Page text (no buttons found):\n{body[:300]}")

    except Exception as e:
        log(f"INV ERROR: {e}")

# ================= SCAN & APPLY ===========

async def scan_saga():
    global scan_page
    links = []
    try:
        await scan_page.goto(SAGA_URL, timeout=60000, wait_until="domcontentloaded")
        await scan_page.wait_for_timeout(1500)
        await accept_cookies(scan_page)
        elements = await scan_page.query_selector_all("a[href*='immo-detail']")
        log(f"Scan: {len(elements)} listings")
        seen_hrefs = set()
        for el in elements:
            href = await el.get_attribute("href")
            if not href: continue
            link = href if href.startswith("http") else "https://www.saga.hamburg" + href
            if link in seen_hrefs or not is_apartment(link) or link in seen: continue
            seen_hrefs.add(link)
            links.append(link)
        log(f"New: {len(links)}")
    except Exception as e:
        log(f"SCAN ERROR: {e}")
    return links

semaphore = asyncio.Semaphore(3)

async def auto_apply(link):
    async with semaphore:
        log(f"APPLY -> {link}")
        page = ipage = None
        try:
            page = await bcontext.new_page()
            await page.goto(link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
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
                await page.close(); return False

            target = immomio_href.replace("/apply/", "/de/apply/")
            ipage = await bcontext.new_page()
            await ipage.goto(target, timeout=60000, wait_until="domcontentloaded")
            await ipage.wait_for_timeout(1500)
            await accept_cookies(ipage)

            body = await ipage.evaluate("() => document.body.innerText")
            if "Registrieren und bewerben" in body or "Bereits registriert" in body:
                log("  Re-login...")
                await ipage.goto("https://tenant.immomio.com/de/auth/login",
                                 timeout=20000, wait_until="domcontentloaded")
                ok = await immomio_login(ipage)
                if not ok:
                    await page.close(); await ipage.close(); return False
                await ipage.goto(target, timeout=60000, wait_until="domcontentloaded")
                await ipage.wait_for_timeout(1500)
                await accept_cookies(ipage)

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
                log("  ❌ No button"); await page.close(); await ipage.close(); return False

            await ipage.wait_for_timeout(3000)
            url_f = ipage.url
            body_f = await ipage.evaluate("() => document.body.innerText.toLowerCase()")
            success = ("applications" in url_f or "expose" in url_f or
                       "registrieren und bewerben" not in body_f)
            log(f"  {'✅' if success else '❌'} {url_f}")
            await page.close(); await ipage.close()
            return success
        except Exception as e:
            log(f"  ❌ {e}")
            try:
                if page: await page.close()
                if ipage: await ipage.close()
            except: pass
            return False

async def apply_and_notify(bot, link):
    result = await auto_apply(link)
    msg = f"✅ Заявку надіслано!\n{link}" if result else f"❌ Не вдалось\n{link}"
    await bot.send_message(chat_id=CHAT_ID, text=msg)

async def scanner(tg_context: ContextTypes.DEFAULT_TYPE):
    log(f"SCAN: {time.strftime('%H:%M:%S')}")
    try:
        flats = await scan_saga()
        if not flats: return
        for link in flats:
            save_seen(link)
            await tg_context.bot.send_message(chat_id=CHAT_ID,
                text=f"🏠 Нова квартира!\n{link}\n⏳ Подаю заявку...")
            asyncio.create_task(apply_and_notify(tg_context.bot, link))
    except Exception as e:
        log(f"SCANNER ERROR: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Бот запущено!\n"
        "🏠 Скан SAGA: кожні 45 сек\n"
        "🗓 Перевірка запрошень: кожні 20 сек\n\n"
        "/status — статус\n/reset — скинути список"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"✅ Живий\n🏠 Квартир: {len(seen)}\n🗓 Запрошень: {len(seen_inv)}")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    seen.clear()
    if os.path.exists("seen.txt"): os.remove("seen.txt")
    await update.message.reply_text("🔄 Скинуто!")

async def post_init(app):
    await init_browser()

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.job_queue.run_repeating(scanner, interval=SCAN_INTERVAL, first=5)
    app.job_queue.run_repeating(check_invitations, interval=INV_INTERVAL, first=10)
    log("BOT RUNNING...")
    app.run_polling()

if __name__ == "__main__":
    main()
