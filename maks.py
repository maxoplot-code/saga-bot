import asyncio
import os
import time
import sqlite3
from datetime import datetime, timedelta
from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (ApplicationBuilder, CommandHandler, CallbackQueryHandler,
                           ContextTypes, MessageHandler, filters, ConversationHandler)

os.environ["PYTHONUNBUFFERED"] = "1"
def log(msg): print(msg, flush=True)

# ================= CONFIG =================

ADMIN_TOKEN   = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
ADMIN_CHAT_ID = 8349459166
ADMIN_IDS     = {8349459166}

SAGA_URL           = "https://www.saga.hamburg/immobiliensuche?Kategorie=APARTMENT"
SCAN_INTERVAL      = 30
INV_INTERVAL       = 20
TRIAL_DAYS         = 10
SUBSCRIPTION_PRICE = "€15/місяць"

EXCLUDE_KEYWORDS = [
    "gewerbe","einstellplatz","garage","stellplatz",
    "buroflache","büroflache","buro","büro",
    "praxis","existenzgrunder","existenzgründer",
    "lager","laden","shop"
]

ASK_EMAIL, ASK_PASSWORD, ASK_FILTER = range(3)

# ================= DATABASE ===============

def init_db():
    db = sqlite3.connect("users.db")
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY, username TEXT, email TEXT, password TEXT,
        active INTEGER DEFAULT 0, trial_until TEXT, paid_until TEXT, created_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS seen (
        chat_id INTEGER, link TEXT, PRIMARY KEY (chat_id, link))""")
    db.execute("""CREATE TABLE IF NOT EXISTS seen_inv (
        chat_id INTEGER, inv_id TEXT, PRIMARY KEY (chat_id, inv_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS filters (
        chat_id    INTEGER PRIMARY KEY,
        min_rooms  REAL DEFAULT 1,
        max_rooms  REAL DEFAULT 10,
        min_price  REAL DEFAULT 0,
        max_price  REAL DEFAULT 9999)""")
    db.commit(); db.close()

def get_db():
    db = sqlite3.connect("users.db")
    db.row_factory = sqlite3.Row
    return db

def get_user(chat_id):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    db.close()
    return dict(u) if u else None

def upsert_user(chat_id, **kw):
    db = get_db()
    u = db.execute("SELECT chat_id FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    if u:
        sets = ", ".join(f"{k}=?" for k in kw)
        db.execute(f"UPDATE users SET {sets} WHERE chat_id=?", (*kw.values(), chat_id))
    else:
        kw["chat_id"] = chat_id
        kw.setdefault("created_at", datetime.now().isoformat())
        cols = ", ".join(kw.keys()); vals = ",".join("?"*len(kw))
        db.execute(f"INSERT INTO users ({cols}) VALUES ({vals})", list(kw.values()))
    db.commit(); db.close()

def is_subscribed(chat_id):
    if chat_id in ADMIN_IDS: return True
    u = get_user(chat_id)
    if not u: return False
    now = datetime.now().isoformat()
    if u.get("paid_until") and u["paid_until"] > now: return True
    if u.get("trial_until") and u["trial_until"] > now: return True
    return False

def get_seen(chat_id):
    db = get_db()
    rows = db.execute("SELECT link FROM seen WHERE chat_id=?", (chat_id,)).fetchall()
    db.close()
    return set(r["link"] for r in rows)

def add_seen(chat_id, link):
    db = get_db()
    db.execute("INSERT OR IGNORE INTO seen (chat_id,link) VALUES (?,?)", (chat_id, link))
    db.commit(); db.close()

def add_seen_inv(chat_id, inv_id):
    db = get_db()
    db.execute("INSERT OR IGNORE INTO seen_inv (chat_id,inv_id) VALUES (?,?)", (chat_id, inv_id))
    db.commit(); db.close()

def get_seen_inv(chat_id):
    db = get_db()
    rows = db.execute("SELECT inv_id FROM seen_inv WHERE chat_id=?", (chat_id,)).fetchall()
    db.close()
    return set(r["inv_id"] for r in rows)

def get_filters(chat_id):
    db = get_db()
    r = db.execute("SELECT * FROM filters WHERE chat_id=?", (chat_id,)).fetchone()
    db.close()
    if r: return dict(r)
    return {"min_rooms": 1, "max_rooms": 10, "min_price": 0, "max_price": 9999}

def save_filters(chat_id, min_rooms, max_rooms, min_price, max_price):
    db = get_db()
    db.execute("""INSERT OR REPLACE INTO filters
        (chat_id, min_rooms, max_rooms, min_price, max_price)
        VALUES (?,?,?,?,?)""", (chat_id, min_rooms, max_rooms, min_price, max_price))
    db.commit(); db.close()

def get_all_active():
    db = get_db()
    rows = db.execute("SELECT * FROM users WHERE active=1").fetchall()
    db.close()
    return [dict(r) for r in rows]

# ================= BROWSER ================

playwright_instance = browser = None
# Persistent contexts only for scan+inv pages (lightweight)
# Apply uses temp contexts that are created/destroyed per-job
user_contexts = {}      # {chat_id: {context, scan_page, inv_page, logged_in, email, password}}
user_semaphores = {}    # per-user apply semaphore (already defined below but referenced here)

async def init_browser():
    global playwright_instance, browser
    if browser: return
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(
        headless=True, args=["--disable-blink-features=AutomationControlled","--no-sandbox"])
    log("BROWSER INITIALIZED")

async def get_uctx(chat_id):
    """Get persistent context for scan/inv pages. Shared across scans."""
    if chat_id in user_contexts: return user_contexts[chat_id]
    u = get_user(chat_id)
    if not u or not u.get("email"): return None
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        viewport={"width":1280,"height":900})
    user_contexts[chat_id] = {
        "context": ctx,
        "scan_page": await ctx.new_page(),
        "inv_page":  await ctx.new_page(),
        "logged_in": False,
        "email": u["email"], "password": u["password"]
    }
    return user_contexts[chat_id]

async def make_temp_context(email, password):
    """Create a TEMPORARY browser context for one apply job, then close it.
    This keeps RAM low for 100+ users — no persistent pages sitting idle."""
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        viewport={"width":1280,"height":900})
    return ctx

async def accept_cookies(page):
    for t in ["Alle akzeptieren","Alles akzeptieren","Alle erlauben"]:
        try:
            b = page.locator(f"text={t}")
            if await b.count() > 0:
                await b.first.click(force=True); await page.wait_for_timeout(400); return
        except: pass

async def immomio_login(page, email, password):
    await page.wait_for_timeout(1000)
    await accept_cookies(page)
    try:
        await page.wait_for_selector('input[type="email"]', timeout=8000)
        await page.fill('input[type="email"]', email)
        await page.locator('button[type="submit"]').first.click(force=True)
        await page.wait_for_timeout(2500)
    except: return False
    if "sso.immomio.com" in page.url:
        try: await page.wait_for_selector('input[name="username"]', timeout=6000)
        except: pass
        for s in ['input[name="username"]','input[type="text"]']:
            if await page.locator(s).count()>0:
                await page.locator(s).first.fill(email); break
        for s in ['input[name="password"]','input[type="password"]']:
            if await page.locator(s).count()>0:
                await page.locator(s).first.fill(password); break
        for s in ['input[type="submit"]','button[type="submit"]']:
            if await page.locator(s).count()>0:
                await page.locator(s).first.click(force=True); break
        await page.wait_for_timeout(4000)
    return "sso.immomio.com" not in page.url and "auth" not in page.url

def is_apartment(link):
    return not any(k in link.lower() for k in EXCLUDE_KEYWORDS)

# ================= KEYBOARDS ==============

def kb_main(chat_id):
    u = get_user(chat_id)
    sub = is_subscribed(chat_id)
    is_admin = chat_id in ADMIN_IDS
    rows = []
    if not u or not u.get("active"):
        rows.append([InlineKeyboardButton("🚀 Зареєструватись", callback_data="register")])
    else:
        rows.append([
            InlineKeyboardButton("📊 Статус", callback_data="status"),
            InlineKeyboardButton("🔄 Скинути список", callback_data="reset")
        ])
        rows.append([InlineKeyboardButton("🔧 Мої фільтри", callback_data="filters")])
        if u.get("active"):
            rows.append([InlineKeyboardButton("⏹ Зупинити бота", callback_data="stop")])
        if not sub:
            rows.append([InlineKeyboardButton("💳 Оплатити підписку", callback_data="pay")])
    if is_admin:
        rows.append([InlineKeyboardButton("👑 Адмін панель", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def kb_filters(chat_id):
    f = get_filters(chat_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🛏 Кімнат: {f['min_rooms']:.0f}–{f['max_rooms']:.0f}", callback_data="filter_rooms")],
        [InlineKeyboardButton(f"💶 Ціна: €{f['min_price']:.0f}–€{f['max_price']:.0f}/мт", callback_data="filter_price")],
        [InlineKeyboardButton("♻️ Скинути фільтри", callback_data="filter_reset")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_main")]
    ])

def kb_room_options():
    opts = [
        ("1+", "1", "10"), ("2+", "2", "10"), ("3+", "3", "10"),
        ("1-2", "1", "2"), ("2-3", "2", "3"), ("3-4", "3", "4"),
    ]
    rows = []
    row = []
    for label, mn, mx in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"setrooms_{mn}_{mx}"))
        if len(row) == 3: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="filters")])
    return InlineKeyboardMarkup(rows)

def kb_price_options():
    opts = [
        ("до €800", "0", "800"), ("до €1000", "0", "1000"),
        ("до €1200", "0", "1200"), ("до €1500", "0", "1500"),
        ("до €2000", "0", "2000"), ("будь-яка", "0", "9999"),
    ]
    rows = []
    row = []
    for label, mn, mx in opts:
        row.append(InlineKeyboardButton(label, callback_data=f"setprice_{mn}_{mx}"))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="filters")])
    return InlineKeyboardMarkup(rows)

def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Всі користувачі", callback_data="admin_users")],
        [InlineKeyboardButton("✅ Активувати юзера", callback_data="admin_activate_help"),
         InlineKeyboardButton("❌ Деактивувати", callback_data="admin_deactivate_help")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_main")]
    ])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back_main")]])

# ================= SCAN & APPLY ===========

user_semaphores = {}  # per-user semaphore

async def auto_apply(chat_id, link):
    """Each apply uses its OWN temp context — closed after done.
    Supports 100+ users with low RAM: no idle pages sitting open."""
    sem = user_semaphores.setdefault(chat_id, asyncio.Semaphore(1))
    async with sem:
        u = get_user(chat_id)
        if not u or not u.get("email"): return False
        email, password = u["email"], u["password"]

        ctx = await make_temp_context(email, password)
        page = ipage = None
        try:
            # Login
            lp = await ctx.new_page()
            await lp.goto("https://tenant.immomio.com/de/auth/login",
                          timeout=30000, wait_until="domcontentloaded")
            ok = await immomio_login(lp, email, password)
            await lp.close()
            if not ok: await ctx.close(); return False

            # Get Immomio href from SAGA
            page = await ctx.new_page()
            await page.goto(link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            await accept_cookies(page)
            href = await page.evaluate("""
                () => { const el = [...document.querySelectorAll('a')].find(e =>
                    (e.href||'').includes('immomio.com') || e.textContent.includes('Zum Expos'));
                return el ? el.href : null; }""")
            if not href: await page.close(); await ctx.close(); return False

            # Apply
            target = href.replace("/apply/", "/de/apply/")
            ipage = await ctx.new_page()
            await ipage.goto(target, timeout=60000, wait_until="domcontentloaded")
            await ipage.wait_for_timeout(1500)
            await accept_cookies(ipage)

            clicked = await ipage.evaluate("""
                () => { const el = [...document.querySelectorAll('a,button,[role="button"]')]
                    .find(e => e.textContent.trim().toLowerCase().includes('jetzt bewerben') ||
                               e.textContent.trim().toLowerCase().includes('interesse bekunden'));
                if (el) { el.click(); return true; } return false; }""")
            if not clicked: await page.close(); await ipage.close(); await ctx.close(); return False

            await ipage.wait_for_timeout(3000)
            url_f = ipage.url
            body_f = await ipage.evaluate("() => document.body.innerText.toLowerCase()")
            success = "applications" in url_f or "expose" in url_f or "registrieren" not in body_f
            return success
        except Exception as e:
            log(f"Apply error {chat_id}: {e}")
            return False
        finally:
            # Always close temp context — frees RAM immediately
            try:
                if page: await page.close()
                if ipage: await ipage.close()
                await ctx.close()
            except: pass

async def scan_and_apply_all(tg_context: ContextTypes.DEFAULT_TYPE):
    users = get_all_active()
    if not users: return
    log(f"SCAN {time.strftime('%H:%M:%S')} ({len(users)} users)")
    links = []
    try:
        first = users[0]
        uctx = await get_uctx(first["chat_id"])
        if not uctx: return
        sp = uctx["scan_page"]
        await sp.goto(SAGA_URL, timeout=60000, wait_until="domcontentloaded")
        await sp.wait_for_timeout(1500)
        await accept_cookies(sp)
        elements = await sp.query_selector_all("a[href*='immo-detail']")
        seen_h = set()
        for el in elements:
            href = await el.get_attribute("href")
            if not href: continue
            link = href if href.startswith("http") else "https://www.saga.hamburg" + href
            if link in seen_h or not is_apartment(link): continue
            seen_h.add(link); links.append(link)
        log(f"  {len(links)} apartments")
    except Exception as e:
        log(f"Scan error: {e}"); return

    # Fire-and-forget — does NOT block next scan
    for u in users:
        cid = u["chat_id"]
        if not is_subscribed(cid): continue
        user_seen = get_seen(cid)
        for link in links:
            if link in user_seen: continue
            add_seen(cid, link)
            asyncio.create_task(_apply_task(tg_context.bot, cid, link))

async def get_flat_details(link):
    """Fetch rooms and price from SAGA detail page"""
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    page = await ctx.new_page()
    try:
        await page.goto(link, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        text = await page.evaluate("() => document.body.innerText")
        rooms = None; price = None
        import re
        # Rooms: "2 Zimmer", "3,5 Zimmer", "2.5 Zimmer"
        m = re.search(r'(\d+[,.]?\d*)\s*Zimmer', text)
        if m: rooms = float(m.group(1).replace(',','.'))
        # Price: "1.234,56 €" or "1234.56 €"
        m = re.search(r'([\d.]+,\d{2})\s*€', text)
        if m: price = float(m.group(1).replace('.','').replace(',','.'))
        return rooms, price
    except: return None, None
    finally:
        await page.close(); await ctx.close()

flat_details_cache = {}  # {link: (rooms, price)} — shared across users

async def _apply_task(bot, chat_id, link):
    try:
        # Get flat details (cached so we fetch once per link for all users)
        if link not in flat_details_cache:
            flat_details_cache[link] = await get_flat_details(link)
        rooms, price = flat_details_cache[link]

        # Check user filters
        f = get_filters(chat_id)
        if rooms is not None:
            if rooms < f["min_rooms"] or rooms > f["max_rooms"]:
                log(f"  SKIP user {chat_id}: {rooms} rooms not in [{f['min_rooms']}-{f['max_rooms']}]")
                return
        if price is not None:
            if price < f["min_price"] or price > f["max_price"]:
                log(f"  SKIP user {chat_id}: €{price} not in [{f['min_price']}-{f['max_price']}]")
                return

        rooms_str = f"{rooms} Zi." if rooms else "?"
        price_str = f"€{price:.0f}" if price else "?"
        await bot.send_message(chat_id=chat_id,
            text=f"🏠 *Нова квартира!*\n🛏 {rooms_str} | 💶 {price_str}/мт\n{link}\n⏳ Подаю заявку...",
            parse_mode="Markdown")
        ok = await auto_apply(chat_id, link)
        await bot.send_message(chat_id=chat_id,
            text=f"{'✅ Заявку надіслано!' if ok else '❌ Не вдалось'}\n{link}")
    except Exception as e:
        log(f"Apply task error {chat_id}: {e}")

async def check_invitations_all(tg_context: ContextTypes.DEFAULT_TYPE):
    for u in get_all_active():
        cid = u["chat_id"]
        if not is_subscribed(cid): continue
        uctx = await get_uctx(cid)
        if not uctx: continue
        try:
            ip = uctx["inv_page"]
            await ip.goto("https://tenant.immomio.com/de/properties/applications",
                          timeout=20000, wait_until="domcontentloaded")
            await ip.wait_for_timeout(1000)
            await accept_cookies(ip)
            besi = ip.locator("text=Besichtigungen")
            if await besi.count() > 0:
                await besi.first.click(force=True)
                await ip.wait_for_timeout(800)
            kws = ["einladung annehmen","termin annehmen","termin bestätigen","zusagen","annehmen"]
            clicked = await ip.evaluate(f"""
                () => {{ const kws={kws};
                const all=[...document.querySelectorAll('button,a,[role="button"]')];
                const res=[];
                for(const el of all){{const t=el.textContent.trim().toLowerCase();
                if(kws.some(k=>t.includes(k))&&t.length<60){{el.click();res.push(el.textContent.trim());}}}}
                return res; }}""")
            if clicked:
                inv_id = f"{cid}_{int(time.time()//3600)}"
                if inv_id not in get_seen_inv(cid):
                    add_seen_inv(cid, inv_id)
                    await tg_context.bot.send_message(chat_id=cid,
                        text=f"🗓 *Запрошення прийнято!*\nКнопка: {clicked}", parse_mode="Markdown")
        except Exception as e:
            log(f"Inv error {cid}: {e}")

# ================= HANDLERS ===============

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    u = get_user(chat_id)
    sub = is_subscribed(chat_id)

    if u and u.get("active") and sub:
        text = (f"👋 Привіт! Бот активний.\n\n"
                f"🏠 Сканую SAGA кожні {SCAN_INTERVAL}с\n"
                f"🗓 Перевіряю запрошення кожні {INV_INTERVAL}с\n\n"
                f"Що хочеш зробити?")
    elif u and u.get("active") and not sub:
        text = "⚠️ *Підписка закінчилась!*\nОнови щоб продовжити."
    else:
        text = (f"🏠 *SAGA Apartment Bot*\n\n"
                f"Автоматично:\n"
                f"• Знаходить нові квартири SAGA Hamburg\n"
                f"• Подає заявки за тебе\n"
                f"• Приймає запрошення на огляд\n\n"
                f"💰 {SUBSCRIPTION_PRICE}\n"
                f"🆓 {TRIAL_DAYS} дні безкоштовно\n\n"
                f"Натисни *Зареєструватись* щоб почати:")
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb_main(chat_id))

async def safe_edit(q, text, **kwargs):
    try:
        await q.edit_message_text(text, **kwargs)
    except Exception as e:
        if "not modified" not in str(e).lower(): raise

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.from_user.id
    data = q.data

    if data == "back_main":
        await safe_edit(q, "Головне меню:", reply_markup=kb_main(chat_id))

    elif data == "register":
        await safe_edit(q, 
            "📧 Введи свій *Immomio email*\n(той що на tenant.immomio.com):",
            parse_mode="Markdown")
        context.user_data["awaiting"] = "email"

    elif data == "status":
        u = get_user(chat_id)
        if not u:
            await safe_edit(q, "❌ Не зареєстрований.", reply_markup=kb_main(chat_id))
            return
        sub = is_subscribed(chat_id)
        status_icon = "✅" if sub else "❌"
        if chat_id in ADMIN_IDS: status_icon = "👑"
        trial = u.get("trial_until","")[:10] if u.get("trial_until") else "—"
        paid  = u.get("paid_until","")[:10]  if u.get("paid_until")  else "—"
        seen_c = len(get_seen(chat_id))
        await safe_edit(q, 
            f"📊 *Твій статус*\n\n"
            f"📧 {u.get('email','—')}\n"
            f"{status_icon} Підписка: {'Активна' if sub else 'Неактивна'}\n"
            f"🆓 Пробний до: {trial}\n"
            f"💳 Оплачено до: {paid}\n"
            f"🏠 Квартир переглянуто: {seen_c}\n"
            f"🔍 Сканую кожні: {SCAN_INTERVAL}с",
            parse_mode="Markdown", reply_markup=kb_back())

    elif data == "reset":
        db = get_db()
        db.execute("DELETE FROM seen WHERE chat_id=?", (chat_id,))
        db.commit(); db.close()
        await safe_edit(q, "🔄 Список скинуто!\nБот знову подасть заявки на всі квартири.",
                                  reply_markup=kb_main(chat_id))

    elif data == "stop":
        upsert_user(chat_id, active=0)
        if chat_id in user_contexts:
            try: await user_contexts[chat_id]["context"].close()
            except: pass
            del user_contexts[chat_id]
        await safe_edit(q, "⏹ Бот зупинено.\nНатисни /start щоб запустити знову.",
                                  reply_markup=kb_main(chat_id))

    elif data == "pay":
        await safe_edit(q, 
            f"💳 *Оплата підписки*\n\n"
            f"Вартість: {SUBSCRIPTION_PRICE}\n\n"
            f"Напиши адміну для активації:\n"
            f"Надішли свій Chat ID: `{chat_id}`\n\n"
            f"Після оплати отримаєш повідомлення ✅",
            parse_mode="Markdown", reply_markup=kb_back())

    elif data == "admin" and chat_id in ADMIN_IDS:
        users = get_all_active()
        total_db = get_db().execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        get_db().close()
        subbed = sum(1 for u in users if is_subscribed(u["chat_id"]))
        await safe_edit(q, 
            f"👑 *Адмін панель*\n\n"
            f"👥 Всього в БД: {total_db}\n"
            f"✅ Активних: {len(users)}\n"
            f"💳 З підпискою: {subbed}",
            parse_mode="Markdown", reply_markup=kb_admin())

    elif data == "admin_users" and chat_id in ADMIN_IDS:
        db = get_db()
        users = db.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 20").fetchall()
        db.close()
        text = "👥 *Користувачі:*\n\n"
        for u in users:
            sub = "✅" if is_subscribed(u["chat_id"]) else "❌"
            act = "🟢" if u["active"] else "🔴"
            text += f"{sub}{act} `{u['chat_id']}` @{u['username'] or '—'}\n   {u['email'] or '—'}\n\n"
        await safe_edit(q, text, parse_mode="Markdown", reply_markup=kb_admin())

    elif data == "admin_activate_help" and chat_id in ADMIN_IDS:
        await safe_edit(q, 
            "Для активації надішли:\n`/activate <chat_id> <days>`\n\nНаприклад:\n`/activate 123456789 30`",
            parse_mode="Markdown", reply_markup=kb_admin())

    elif data == "admin_deactivate_help" and chat_id in ADMIN_IDS:
        await safe_edit(q, 
            "Для деактивації:\n`/deactivate <chat_id>`",
            parse_mode="Markdown", reply_markup=kb_admin())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle registration flow via text messages"""
    chat_id = update.effective_chat.id
    awaiting = context.user_data.get("awaiting")

    if awaiting == "email":
        context.user_data["reg_email"] = update.message.text.strip()
        context.user_data["awaiting"] = "password"
        await update.message.reply_text("🔑 Тепер введи *Immomio пароль*:", parse_mode="Markdown")

    elif awaiting == "password":
        email    = context.user_data.get("reg_email","")
        password = update.message.text.strip()
        context.user_data["awaiting"] = None

        msg = await update.message.reply_text("⏳ Перевіряю дані Immomio...")

        test_ctx  = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        test_page = await test_ctx.new_page()
        await test_page.goto("https://tenant.immomio.com/de/auth/login",
                             timeout=30000, wait_until="domcontentloaded")
        ok = await immomio_login(test_page, email, password)
        await test_page.close(); await test_ctx.close()

        if not ok:
            await msg.edit_text("❌ Невірний email або пароль!\nСпробуй ще раз — натисни Зареєструватись.",
                                reply_markup=kb_main(chat_id))
            return

        trial_until = (datetime.now() + timedelta(days=TRIAL_DAYS)).isoformat()
        upsert_user(chat_id,
                    username=update.effective_user.username or "",
                    email=email, password=password,
                    active=1, trial_until=trial_until)

        if chat_id in user_contexts:
            try: await user_contexts[chat_id]["context"].close()
            except: pass
            del user_contexts[chat_id]

        await msg.edit_text(
            f"✅ *Реєстрація успішна!*\n\n"
            f"🆓 Пробний період: {TRIAL_DAYS} дні\n"
            f"📅 До: {trial_until[:10]}\n"
            f"💰 Після: {SUBSCRIPTION_PRICE}\n\n"
            f"🏠 Бот вже шукає квартири для тебе!",
            parse_mode="Markdown", reply_markup=kb_main(chat_id))

# ================= ADMIN COMMANDS =========

async def cmd_activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ADMIN_IDS: return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /activate <chat_id> <days>"); return
    target_id = int(args[0]); days = int(args[1])
    paid_until = (datetime.now() + timedelta(days=days)).isoformat()
    upsert_user(target_id, paid_until=paid_until, active=1)
    await update.message.reply_text(f"✅ Активовано {target_id} на {days} днів (до {paid_until[:10]})")
    try:
        await context.bot.send_message(chat_id=target_id,
            text=f"🎉 *Твою підписку активовано!*\n📅 До: {paid_until[:10]}\n\nБот працює 🏠",
            parse_mode="Markdown")
    except: pass

async def cmd_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in ADMIN_IDS: return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /deactivate <chat_id>"); return
    target_id = int(args[0])
    upsert_user(target_id, active=0, paid_until="")
    await update.message.reply_text(f"⏹ Деактивовано {target_id}")

async def post_init(app):
    init_db()
    await init_browser()
    upsert_user(ADMIN_CHAT_ID, username="admin",
                email="maksymsheveliuk@gmail.com",
                password="Maksoplot2007",
                active=1, paid_until="2099-01-01T00:00:00")
    # Set bot commands menu
    await app.bot.set_my_commands([
        BotCommand("start",  "🏠 Головне меню"),
        BotCommand("activate",  "✅ Активувати юзера (адмін)"),
        BotCommand("deactivate","❌ Деактивувати юзера (адмін)"),
    ])
    log("READY")

def main():
    app = ApplicationBuilder().token(ADMIN_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("activate",   cmd_activate))
    app.add_handler(CommandHandler("deactivate", cmd_deactivate))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.job_queue.run_repeating(scan_and_apply_all,    interval=SCAN_INTERVAL, first=10)
    app.job_queue.run_repeating(check_invitations_all, interval=INV_INTERVAL,  first=15)
    log("BOT RUNNING...")
    app.run_polling()

if __name__ == "__main__":
    main()
