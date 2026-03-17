"""
SAGA Apartment Bot — Secure Edition
────────────────────────────────────
Автоматично знаходить квартири SAGA Hamburg,
подає заявки та приймає запрошення на огляд.

Запуск:
  1. Скопіюй .env.example → .env та заповни всі поля
  2. pip install -r requirements.txt
  3. playwright install chromium
  4. python bot.py
"""

import asyncio
import base64
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

import aiohttp
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# ═══════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("saga_bot")


def log(msg: str, level: str = "info") -> None:
    getattr(logger, level)(msg)


# ═══════════════════════════════════════════════════════════════════════
#  CONFIG — завантаження з .env (без жодних секретів у коді!)
# ═══════════════════════════════════════════════════════════════════════

load_dotenv()

_BOT_START_TIME = datetime.now()


def _require(key: str) -> str:
    """Отримує обов'язкову змінну середовища, зупиняє бот якщо відсутня."""
    val = os.getenv(key, "").strip()
    if not val:
        raise RuntimeError(
            f"❌ Змінна середовища '{key}' не задана!\n"
            f"   Скопіюй .env.example → .env та заповни всі поля."
        )
    return val


ADMIN_TOKEN      = _require("ADMIN_TOKEN")
ADMIN_CHAT_ID    = int(_require("ADMIN_CHAT_ID"))
ADMIN_IDS        = {ADMIN_CHAT_ID}
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")
TON_WALLET       = _require("TON_WALLET")
TON_API_KEY      = _require("TON_API_KEY")
# Telegram Stars — не потребує токена, працює автоматично
# Встанови STARS_ENABLED=true щоб увімкнути
STARS_TOKEN      = ""  # для Stars завжди порожній
STARS_ENABLED    = os.getenv("STARS_ENABLED", "true").lower() == "true"

# ── Проксі для Playwright (rotating proxies) ────────────────────────
# Формат: "http://user:pass@host:port,http://user:pass@host2:port2"
# Якщо порожньо — заявки йдуть з IP Railway (ризик блокування Immomio)
PROXY_LIST_RAW   = os.getenv("PROXY_LIST", "")
PROXY_LIST: list[str] = [p.strip() for p in PROXY_LIST_RAW.split(",") if p.strip()]

# ── Реферальна система ───────────────────────────────────────────────
REF_BONUS_DAYS   = int(os.getenv("REF_BONUS_DAYS", "3"))   # днів обом за реферал

# ── Щоденний звіт адміну ────────────────────────────────────────────
DAILY_REPORT     = os.getenv("DAILY_REPORT", "true").lower() == "true"
DB_PATH          = os.getenv("DB_PATH", "./data/users.db")
SAGA_URL         = "https://www.saga.hamburg/immobiliensuche?Kategorie=APARTMENT"
SCAN_INTERVAL    = int(os.getenv("SCAN_INTERVAL", "15"))
INV_INTERVAL     = int(os.getenv("INV_INTERVAL", "15"))
TRIAL_DAYS       = int(os.getenv("TRIAL_DAYS", "10"))

# ── Шифрування паролів ──────────────────────────────────────────────
_RAW_KEY = os.getenv("ENCRYPTION_KEY", "")
if _RAW_KEY and len(_RAW_KEY) == 64:
    # hex-рядок → bytes → Fernet key (base64url)
    _fernet = Fernet(base64.urlsafe_b64encode(bytes.fromhex(_RAW_KEY)))
else:
    logger.warning(
        "⚠️  ENCRYPTION_KEY не задано або невірна довжина (потрібно 64 hex-символи). "
        "Паролі зберігатимуться у відкритому вигляді — змініть це в продакшн!"
    )
    _fernet = None


def encrypt_password(plain: str) -> str:
    """Шифрує пароль перед збереженням у БД."""
    if _fernet is None:
        return plain
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_password(stored: str) -> str:
    """Розшифровує пароль зі збереженого значення."""
    if _fernet is None:
        return stored
    try:
        return _fernet.decrypt(stored.encode()).decode()
    except (InvalidToken, Exception):
        # Якщо пароль збережений у відкритому вигляді (міграція)
        return stored


# ── Тарифи ──────────────────────────────────────────────────────────
PLANS = [
    ("plan_1", 19,  30),
    ("plan_2", 29,  60),
    ("plan_3", 39, 120),
]

EXCLUDE = [
    "gewerbe", "einstellplatz", "garage", "stellplatz", "buroflache",
    "buro", "praxis", "existenzgrunder", "lager", "laden", "shop",
]

# ── PDF гід (зовнішній файл замість inline base64) ────────────────────
_GUIDE_PDF_PATH = Path(__file__).parent / "guide_hamburg.pdf"



# ═══════════════════════════════════════════════════════════════════════
#  REFERRAL SYSTEM
# ═══════════════════════════════════════════════════════════════════════

def make_ref_code(chat_id: int) -> str:
    """Генерує унікальний реферальний код для юзера."""
    return f"R{chat_id}"


def get_ref_code(chat_id: int) -> str:
    """Повертає реферальний код юзера (створює якщо немає)."""
    u = get_user(chat_id)
    if u and u.get("ref_code"):
        return u["ref_code"]
    code = make_ref_code(chat_id)
    upsert_user(chat_id, ref_code=code)
    return code


def process_referral(new_chat_id: int, ref_code: str) -> bool:
    """
    Обробляє реферал при реєстрації нового юзера.
    Обидва отримують REF_BONUS_DAYS днів.
    Повертає True якщо реферал успішно застосований.
    """
    if not ref_code or not ref_code.startswith("R"):
        return False
    try:
        referrer_id = int(ref_code[1:])
    except ValueError:
        return False
    if referrer_id == new_chat_id:
        return False
    referrer = get_user(referrer_id)
    if not referrer:
        return False
    # Перевіряємо чи не реєструвався вже з цим кодом
    with get_db() as db:
        exists = db.execute(
            "SELECT referred_id FROM referrals WHERE referred_id=?", (new_chat_id,)
        ).fetchone()
        if exists:
            return False
        db.execute(
            "INSERT INTO referrals (referrer_id,referred_id,created_at) VALUES (?,?,?)",
            (referrer_id, new_chat_id, datetime.now().isoformat()),
        )
        db.commit()
    # Бонус новому юзеру
    upsert_user(new_chat_id, referred_by=referrer_id)
    new_trial = datetime.now() + timedelta(days=TRIAL_DAYS + REF_BONUS_DAYS)
    upsert_user(new_chat_id, trial_until=new_trial.isoformat())
    # Бонус реферу
    now = datetime.now()
    paid = referrer.get("paid_until", "") or ""
    trial = referrer.get("trial_until", "") or ""
    base = datetime.fromisoformat(paid) if paid > now.isoformat() else (
        datetime.fromisoformat(trial) if trial > now.isoformat() else now
    )
    upsert_user(referrer_id, paid_until=(base + timedelta(days=REF_BONUS_DAYS)).isoformat())
    return True


def get_referral_stats(chat_id: int) -> dict:
    """Повертає статистику рефералів юзера."""
    with get_db() as db:
        count = db.execute(
            "SELECT COUNT(*) as c FROM referrals WHERE referrer_id=?", (chat_id,)
        ).fetchone()["c"]
        total_bonus = count * REF_BONUS_DAYS
    return {"count": count, "bonus_days": total_bonus}


# ═══════════════════════════════════════════════════════════════════════
#  TRANSLATIONS
# ═══════════════════════════════════════════════════════════════════════

T = {
"uk": {
    "welcome": "👋 Привіт! Я SAGA Apartment Bot\n\nАвтоматично знаходжу квартири SAGA Hamburg,\nподаю заявки та приймаю запрошення на огляд.\n\n🆓 {trial} днів безкоштовно\n💰 Далі від €19/місяць\n\n━━━━━━━━━━━━━━\nУ тебе є акаунт на Immomio?",
    "has_account":   "✅ Так, є акаунт",
    "no_account":    "❌ Немає акаунту",
    "what_immomio":  "❓ Що таке Immomio?",
    "immomio_explain": "❓ Що таке Immomio?\n\ntenant.immomio.com — платформа через яку SAGA приймає всі заявки.\n\n✅ Реєстрація безкоштовна\n✅ Заповни профіль і завантаж документи\n\nУ тебе вже є акаунт?",
    "no_immomio": "❌ Без акаунту Immomio бот не може подавати заявки.\n\nЯк зареєструватись:\n1. Відкрий tenant.immomio.com\n2. Натисни Registrieren\n3. Заповни профіль на 100%\n4. Завантаж документи\n5. Повернись сюди",
    "already_reg": "✅ Вже зареєструвався!",
    "enter_email":    "📧 Введи свій Immomio email:",
    "enter_password": "🔑 Введи Immomio пароль:",
    "checking":       "⏳ Перевіряю дані Immomio...",
    "wrong_creds":    "❌ Невірний email або пароль. Спробуй ще раз.",
    "try_again":      "🔄 Спробувати знову",
    "reg_success":    "✅ Реєстрація успішна!\n\n🆓 {trial} днів безкоштовно\n📅 До: {date}\n\n⚙️ Налаштуй фільтри пошуку:",
    "setup_filters":  "⚙️ Налаштування фільтрів\n\nОбери параметри і натисни Зберегти:",
    "setup_rooms_btn":"🛏 Кімнати: {mn}–{mx}",
    "setup_price_btn":"💶 Ціна: €{mn}–€{mx}/мт",
    "save_start":     "✅ Зберегти і почати пошук",
    "choose_rooms":   "🛏 Скільки кімнат шукаєш?",
    "choose_price":   "💶 Максимальна ціна оренди?",
    "any_price":      "будь-яка",
    "bot_started":    "🚀 Бот запущено!\n\n🛏 Кімнати: {mr}–{xr}\n💶 Ціна: до {price}\n\nЯк тільки з'явиться квартира — одразу подам заявку! 🏠",
    "main_menu":      "🏠 Головне меню",
    "btn_status":     "📊 Мій статус та фільтри",
    "btn_pay":        "💳 Оплатити підписку",
    "btn_guide":      "📖 Гід по пошуку квартир",
    "btn_support":    "🆘 Підтримка",
    "btn_stop":       "⏹ Зупинити бота",
    "btn_admin":      "👑 Адмін",
    "btn_register":   "🚀 Зареєструватись",
    "btn_expired":    "⚠️ Підписка закінчилась",
    "change_lang":    "🌐 Змінити мову",
    "back":           "◀️ Назад",
    "status_text":    "📊 Статус\n\n{icon} Підписка до: {expires}\n🏠 Квартир переглянуто: {seen}\n\n🔧 Фільтри:\n  🛏 Кімнати: {mr}–{xr}\n  💶 Ціна: €{mp}–€{xp}/мт",
    "reset_list":     "🔄 Скинути список",
    "reset_filters":  "♻️ Скинути фільтри",
    "filters_reset":  "♻️ Фільтри скинуто.",
    "list_reset":     "🔄 Список скинуто!",
    "expired_text":   "⚠️ Підписка закінчилась!\n\nОнови щоб продовжити пошук квартир.",
    "pay_title":      "💳 Оплата підписки\n\n🥈 1 місяць — €19\n🥇 2 місяці — €29 (знижка 24%)\n💎 3 місяці — €39 (+1 місяць!)\n\n━━━━━━━━━━━━━━\nОбери тариф:",
    "plan_1_btn":     "🥈 1 місяць — €19",
    "plan_2_btn":     "🥇 2 місяці — €29",
    "plan_3_btn":     "💎 3 місяці — €39 (+1 місяць!)",
    "pay_details":    "✅ Тариф: {plan}\n\n💎 Сума: {ton} TON\n📈 Курс: 1 TON = €{rate}\n\n📤 Надішли {ton} TON на адресу:\n`{wallet}`\n\n💬 Коментар (обов'язково!):\n`{comment}`\n\n✅ Підписку буде активовано автоматично",
    "check_payment":  "🔄 Перевірити оплату",
    "wallet_guide":   "📖 Як створити гаманець?",
    "pay_confirmed":  "✅ Оплату підтверджено!\n\n📅 Підписка до: {date}\n\nБот шукає квартири! 🏠",
    "pay_pending":    "⏳ Оплату ще не знайдено.\n\nПереконайся що надіслав {ton} TON\nз коментарем: `{comment}`\n\n✅ Підписку буде активовано автоматично",
    "no_payment":     "❌ Немає активного платежу. Обери тариф.",
    "pay_received":   "✅ Оплату отримано!\n\n📅 Підписка до: {date}\n\nБот шукає квартири! 🏠",
    "choose_payment":  "💳 Обери спосіб оплати:",
    "pay_stars":       "⭐ Telegram Stars (просто)",
    "pay_ton":         "💎 TON крипто",
    "stars_title":     "⭐ Оплата через Telegram Stars\n\n🥈 1 місяць — {s1} Stars\n🥇 2 місяці — {s2} Stars (знижка 24%)\n💎 3 місяці — {s3} Stars (+1 місяць!)\n\n━━━━━━━━━━━━━━\nОбери тариф:",
    "stars_desc":      "SAGA Apartment Bot — підписка {months} міс.",
    "stars_pending":   "⭐ Оплата Stars — очікую підтвердження...",
    "stars_ok":        "✅ Оплату Stars підтверджено!\n\n📅 Підписка до: {date}\n\nБот шукає квартири! 🏠",
    "how_to_stars":    "⭐ Як оплатити через Telegram Stars?\n\n1. Натисни кнопку тарифу\n2. З\'явиться вікно оплати Telegram\n3. Підтверди оплату\n4. Підписка активується автоматично\n\n💡 Stars можна купити прямо в Telegram:\nНалаштування → Telegram Stars → Купити",
    "how_to_pay_choice": "Обери спосіб оплати для інструкції:",
    "wallet_guide_text": "📖 Як створити гаманець і оплатити?\n\n📱 Крок 1 — Створи гаманець:\n1. Telegram → знайди @wallet\n2. Start → Гаманець → Створити\n3. ⚠️ Збережи seed-фразу (12 слів)!\n\n💰 Крок 2 — Купи TON:\n1. @wallet → Поповнити → TON → Купити\n2. Обери карткою або P2P\n\n📤 Крок 3 — Надішли оплату:\n1. @wallet → Надіслати\n2. Вставити адресу та коментар\n3. Ввести суму і підтвердити\n\n✅ Підписку буде активовано автоматично",
    "guide_caption":  "📖 Повний гід по пошуку квартири в Гамбурзі\n\nДокументи, поради, корисні посилання.",
    "guide_text":     "📖 Корисні посилання:\n\n• saga.hamburg — офіційний сайт\n• tenant.immomio.com — подача заявок\n• meineschufa.de — SCHUFA онлайн\n• hamburg.de/wbs — WBS заявка\n• mieterverein-hamburg.de — права орендарів\n\nПитання? @{username}",
    "profile_incomplete": "⚠️ Твій профіль Immomio заповнений не повністю!\n\nЗаявки можуть відхилятись.\n\n👉 Заповни на tenant.immomio.com/de/profile\n\nПісля заповнення — заявки підуть автоматично",
    "blacklist_added":    "🚫 Квартиру додано в чорний список.",
    "apply_viewed":       "👀 Твою заявку переглянули!\n\n{link}",
    "apply_invited":      "🎉 Тебе запросили на огляд!\n\n{link}\n\nПеревір Immomio → Besichtigungen",
    "apply_rejected":     "❌ Заявку відхилено.\n\n{link}",
    "btn_blacklist":      "🚫 Не цікавить",
    "btn_website":        "🌐 Наш сайт",
    "website_text":       "🌐 Сайт:\nhttps://patrickkassparov.github.io/saga_bot/",
    "support_text":   "🆘 Підтримка\n\nПиши напряму:\n👤 @{username}\n\nВідповімо якнайшвидше!",
    "ref_menu":       "👥 Реферальна програма",
    "ref_text":       "👥 Реферальна програма\n\n🔗 Твоє посилання:\n`t.me/{bot}?start={code}`\n\n📊 Запрошено юзерів: {count}\n🎁 Зароблено бонусів: +{bonus} днів\n\n✅ За кожного нового юзера — обидва отримуєте +{ref_days} днів безкоштовно!",
    "ref_share":      "📤 Поділитись посиланням",
    "ref_bonus_msg":  "🎁 +{days} днів за реферала! Хтось зареєструвався по твоєму посиланню.",
    "ref_welcome":    "🎁 +{days} днів бонус за реєстрацію по реферальному посиланню!",
    "stats_menu":     "📈 Моя статистика",
    "stats_text":     "📈 Статистика\n\n🏠 Заявок подано: {total}\n✅ Успішних: {success}\n❌ Невдалих: {fail}\n📊 Успішність: {rate}%\n\n📅 Активний з: {since}",
    "btn_ref":        "👥 Реферали",
    "btn_stats":      "📈 Статистика",
    "new_flat":       "🏠 Нова квартира!\n🛏 {rooms} кімн. | 💶 €{price}/мт\n\n{link}\n⏳ Подаю заявку...",
    "apply_ok":       "✅ Заявку надіслано!\n{link}",
    "apply_fail":     "❌ Не вдалось подати заявку\n{link}",
    "inv_accepted":   "🗓 Запрошення на огляд прийнято!\n\nПеревір деталі в Immomio!",
    "bot_stopped":    "⏹ Бот зупинено.\n/start щоб запустити знову.",
    "admin_stats":    "👑 Адмін\n\n👥 Всього: {total}\n✅ Активних: {active}\n💰 Оплачено разів: {paid}",
    "users_btn":      "👥 Користувачі",
    "broadcast_btn":  "📢 Розіслати всім",
    "broadcast_ask":  "📢 Введи повідомлення для розсилки:",
    "broadcast_done": "📢 Надіслано {sent}/{total}",
    "activated":      "✅ Активовано на {days} днів",
    "deactivated":    "⏹ Деактивовано",
    "not_interested":   "❌ Не цікавить",
    "blacklisted":      "🚫 Квартиру додано до чорного списку.",
    "profile_warn":     "⚠️ Твій профіль Immomio заповнений не повністю!\n\nЗаявки можуть не надсилатись.\n\n👉 Заповни профіль:\ntenant.immomio.com/de/profile\n\nПісля заповнення все запрацює автоматично ✅",
    "app_status_viewed":  "👀 Твою заявку переглянули!\n\n🏠 {link}\n\nЧекай на запрошення на огляд.",
    "app_status_invited": "🎉 Тебе запросили на огляд квартири!\n\n🏠 {link}\n\nПеревір деталі в Immomio → Besichtigungen",
    "app_status_rejected":"😔 Заявку відхилено.\n\n🏠 {link}",
    "btn_pause":      "⏸ Пауза",
    "btn_resume":     "▶️ Продовжити",
    "paused_text":    "⏸ Бот на паузі.\n\nЗаявки тимчасово зупинено. Натисни ▶️ щоб продовжити.",
    "resumed_text":   "▶️ Бот продовжує роботу! Шукаю квартири 🏠",
    "expiry_warn":    "⚠️ Підписка закінчується через {days} дн.!\n\nПродовж щоб не пропустити квартири.",
    "btn_renew":      "🔄 Продовжити підписку",
    "admin_status_btn": "🟢 Статус системи",
    "admin_status_ok":  "🟢 Система працює нормально\n\n⏱ Аптайм: {uptime}\n👥 Активних: {active}\n🌐 Проксі: {proxy}\n🖥 Браузерів: {browsers}/5\n\n✅ Всі сервіси OK",
    "admin_error_notify": "🔴 Помилка бота!\n\n{error}\n\nЧас: {time}",
    "choose_lang":    "🌐 Обери мову / Wähle Sprache / Choose language:",
},
"de": {
    "welcome": "👋 Hallo! Ich bin der SAGA Apartment Bot\n\nIch finde automatisch SAGA Hamburg Wohnungen,\nbewerbe mich und nehme Besichtigungseinladungen an.\n\n🆓 {trial} Tage kostenlos\n💰 Danach ab €19/Monat\n\n━━━━━━━━━━━━━━\nHast du ein Immomio-Konto?",
    "has_account":   "✅ Ja, ich habe ein Konto",
    "no_account":    "❌ Kein Konto",
    "what_immomio":  "❓ Was ist Immomio?",
    "immomio_explain": "❓ Was ist Immomio?\n\ntenant.immomio.com — Plattform über die SAGA alle Bewerbungen annimmt.\n\n✅ Registrierung kostenlos\n✅ Profil ausfüllen und Dokumente hochladen\n\nHast du bereits ein Konto?",
    "no_immomio": "❌ Ohne Immomio-Konto kann der Bot keine Bewerbungen senden.\n\nSo registrierst du dich:\n1. tenant.immomio.com öffnen\n2. Registrieren klicken\n3. Profil zu 100% ausfüllen\n4. Dokumente hochladen\n5. Hierher zurückkehren",
    "already_reg": "✅ Bereits registriert!",
    "enter_email":    "📧 Gib deine Immomio E-Mail ein:",
    "enter_password": "🔑 Gib dein Immomio Passwort ein:",
    "checking":       "⏳ Überprüfe Immomio-Daten...",
    "wrong_creds":    "❌ Falsche E-Mail oder Passwort. Bitte erneut versuchen.",
    "try_again":      "🔄 Erneut versuchen",
    "reg_success":    "✅ Registrierung erfolgreich!\n\n🆓 {trial} Tage kostenlos\n📅 Bis: {date}\n\n⚙️ Suchfilter einstellen:",
    "setup_filters":  "⚙️ Filter einrichten\n\nWähle Parameter und speichere:",
    "setup_rooms_btn":"🛏 Zimmer: {mn}–{mx}",
    "setup_price_btn":"💶 Preis: €{mn}–€{mx}/Monat",
    "save_start":     "✅ Speichern & Suche starten",
    "choose_rooms":   "🛏 Wie viele Zimmer suchst du?",
    "choose_price":   "💶 Maximaler Mietpreis?",
    "any_price":      "beliebig",
    "bot_started":    "🚀 Bot gestartet!\n\n🛏 Zimmer: {mr}–{xr}\n💶 Preis: bis {price}\n\nSobald eine Wohnung erscheint — bewerbe ich mich sofort! 🏠",
    "main_menu":      "🏠 Hauptmenü",
    "btn_status":     "📊 Mein Status & Filter",
    "btn_pay":        "💳 Abonnement kaufen",
    "btn_guide":      "📖 Wohnungssuche-Guide",
    "btn_support":    "🆘 Support",
    "btn_stop":       "⏹ Bot stoppen",
    "btn_admin":      "👑 Admin",
    "btn_register":   "🚀 Registrieren",
    "btn_expired":    "⚠️ Abonnement abgelaufen",
    "change_lang":    "🌐 Sprache ändern",
    "back":           "◀️ Zurück",
    "status_text":    "📊 Status\n\n{icon} Abonnement bis: {expires}\n🏠 Gesehene Wohnungen: {seen}\n\n🔧 Filter:\n  🛏 Zimmer: {mr}–{xr}\n  💶 Preis: €{mp}–€{xp}/Monat",
    "reset_list":     "🔄 Liste zurücksetzen",
    "reset_filters":  "♻️ Filter zurücksetzen",
    "filters_reset":  "♻️ Filter zurückgesetzt.",
    "list_reset":     "🔄 Liste zurückgesetzt!",
    "expired_text":   "⚠️ Abonnement abgelaufen!\n\nErneuere es um die Suche fortzusetzen.",
    "pay_title":      "💳 Abonnement kaufen\n\n🥈 1 Monat — €19\n🥇 2 Monate — €29 (24% Rabatt)\n💎 3 Monate — €39 (+1 Bonusmonat!)\n\n━━━━━━━━━━━━━━\nWähle einen Tarif:",
    "plan_1_btn":     "🥈 1 Monat — €19",
    "plan_2_btn":     "🥇 2 Monate — €29",
    "plan_3_btn":     "💎 3 Monate — €39 (+1 Bonus!)",
    "pay_details":    "✅ Tarif: {plan}\n\n💎 Betrag: {ton} TON\n📈 Kurs: 1 TON = €{rate}\n\n📤 Sende {ton} TON an:\n`{wallet}`\n\n💬 Kommentar (Pflicht!):\n`{comment}`\n\n✅ Abonnement wird automatisch aktiviert",
    "check_payment":  "🔄 Zahlung prüfen",
    "wallet_guide":   "📖 Wie erstelle ich eine Wallet?",
    "pay_confirmed":  "✅ Zahlung bestätigt!\n\n📅 Abonnement bis: {date}\n\nBot sucht Wohnungen! 🏠",
    "pay_pending":    "⏳ Zahlung noch nicht gefunden.\n\nStelle sicher, dass du {ton} TON gesendet hast\nmit Kommentar: `{comment}`\n\n✅ Abonnement wird automatisch aktiviert",
    "no_payment":     "❌ Keine aktive Zahlung. Wähle einen Tarif.",
    "pay_received":   "✅ Zahlung erhalten!\n\n📅 Abonnement bis: {date}\n\nBot sucht Wohnungen! 🏠",
        "choose_payment":  "💳 Wähle Zahlungsmethode:",
    "pay_stars":       "⭐ Telegram Stars (einfach)",
    "pay_ton":         "💎 TON Krypto",
    "stars_title":     "⭐ Zahlung per Telegram Stars\n\n🥈 1 Monat — {s1} Stars\n🥇 2 Monate — {s2} Stars (24% Rabatt)\n💎 3 Monate — {s3} Stars (+1 Bonusmonat!)\n\n━━━━━━━━━━━━━━\nWähle einen Tarif:",
    "stars_desc":      "SAGA Apartment Bot — Abo {months} Mon.",
    "stars_pending":   "⭐ Stars-Zahlung — warte auf Bestätigung...",
    "stars_ok":        "✅ Stars-Zahlung bestätigt!\n\n📅 Abonnement bis: {date}\n\nBot sucht Wohnungen! 🏠",
    "how_to_stars":    "⭐ Wie mit Telegram Stars zahlen?\n\n1. Tarif-Taste drücken\n2. Telegram-Zahlungsfenster erscheint\n3. Zahlung bestätigen\n4. Abonnement wird automatisch aktiviert\n\n💡 Stars kaufen in Telegram:\nEinstellungen → Telegram Stars → Kaufen",
    "how_to_pay_choice": "Wähle Zahlungsmethode für Anleitung:",
    "wallet_guide_text": "📖 Wie Wallet erstellen und zahlen?\n\n📱 Schritt 1 — Wallet erstellen:\n1. Telegram → @wallet suchen\n2. Start → Wallet → Erstellen\n3. ⚠️ Seed-Phrase (12 Wörter) sichern!\n\n💰 Schritt 2 — TON kaufen:\n1. @wallet → Aufladen → TON → Kaufen\n2. Per Karte oder P2P\n\n📤 Schritt 3 — Zahlung senden:\n1. @wallet → Senden\n2. Adresse und Kommentar einfügen\n3. Betrag eingeben und bestätigen\n\n✅ Abonnement wird automatisch aktiviert",
    "guide_caption":  "📖 Vollständiger Wohnungssuche-Guide Hamburg\n\nDokumente, Tipps, nützliche Links.",
    "guide_text":     "📖 Nützliche Links:\n\n• saga.hamburg — offizielle Website\n• tenant.immomio.com — Bewerbungen\n• meineschufa.de — SCHUFA online\n• hamburg.de/wbs — WBS beantragen\n• mieterverein-hamburg.de — Mieterrechte\n\nFragen? @{username}",
    "profile_incomplete": "⚠️ Dein Immomio-Profil ist nicht vollständig!\n\n👉 Ausfüllen auf tenant.immomio.com/de/profile",
    "blacklist_added":    "🚫 Wohnung zur Blacklist hinzugefügt.",
    "apply_viewed":       "👀 Deine Bewerbung wurde angesehen!\n\n{link}",
    "apply_invited":      "🎉 Du wurdest zur Besichtigung eingeladen!\n\n{link}",
    "apply_rejected":     "❌ Bewerbung abgelehnt.\n\n{link}",
    "btn_blacklist":      "🚫 Nicht interessiert",
    "btn_website":        "🌐 Unsere Website",
    "website_text":       "🌐 Website:\nhttps://patrickkassparov.github.io/saga_bot/",
    "support_text":   "🆘 Support\n\nSchreib direkt:\n👤 @{username}\n\nWir antworten so schnell wie möglich!",
    "ref_menu":       "👥 Empfehlungsprogramm",
    "ref_text":       "👥 Empfehlungsprogramm\n\n🔗 Dein Link:\n`t.me/{bot}?start={code}`\n\n📊 Eingeladene Nutzer: {count}\n🎁 Bonus erhalten: +{bonus} Tage\n\n✅ Für jeden neuen Nutzer — beide erhalten +{ref_days} Tage kostenlos!",
    "ref_share":      "📤 Link teilen",
    "ref_bonus_msg":  "🎁 +{days} Tage Bonus! Jemand hat sich über deinen Link registriert.",
    "ref_welcome":    "🎁 +{days} Tage Bonus für Registrierung über Empfehlungslink!",
    "stats_menu":     "📈 Meine Statistik",
    "stats_text":     "📈 Statistik\n\n🏠 Bewerbungen: {total}\n✅ Erfolgreich: {success}\n❌ Fehlgeschlagen: {fail}\n📊 Erfolgsrate: {rate}%\n\n📅 Aktiv seit: {since}",
    "btn_ref":        "👥 Empfehlungen",
    "btn_stats":      "📈 Statistik",
    "new_flat":       "🏠 Neue Wohnung!\n🛏 {rooms} Zi. | 💶 €{price}/Monat\n\n{link}\n⏳ Bewerbe mich...",
    "apply_ok":       "✅ Bewerbung gesendet!\n{link}",
    "apply_fail":     "❌ Bewerbung fehlgeschlagen\n{link}",
    "inv_accepted":   "🗓 Besichtigungseinladung angenommen!\n\nDetails in Immomio prüfen!",
    "bot_stopped":    "⏹ Bot gestoppt.\n/start zum Neustart.",
    "admin_stats":    "👑 Admin\n\n👥 Gesamt: {total}\n✅ Aktiv: {active}\n💰 Gezahlt: {paid}x",
    "users_btn":      "👥 Benutzer",
    "broadcast_btn":  "📢 An alle senden",
    "broadcast_ask":  "📢 Nachricht für alle eingeben:",
    "broadcast_done": "📢 Gesendet {sent}/{total}",
    "activated":      "✅ Aktiviert für {days} Tage",
    "deactivated":    "⏹ Deaktiviert",
    "not_interested":   "❌ Nicht interessiert",
    "blacklisted":      "🚫 Wohnung zur schwarzen Liste hinzugefügt.",
    "profile_warn":     "⚠️ Dein Immomio-Profil ist nicht vollständig!\n\nBewerbungen werden möglicherweise nicht gesendet.\n\n👉 Profil vervollständigen:\ntenant.immomio.com/de/profile\n\nDanach läuft alles automatisch ✅",
    "app_status_viewed":  "👀 Deine Bewerbung wurde angesehen!\n\n🏠 {link}\n\nWarte auf eine Besichtigungseinladung.",
    "app_status_invited": "🎉 Du wurdest zu einer Besichtigung eingeladen!\n\n🏠 {link}\n\nDetails in Immomio → Besichtigungen",
    "app_status_rejected":"😔 Bewerbung abgelehnt.\n\n🏠 {link}",
    "btn_pause":      "⏸ Pause",
    "btn_resume":     "▶️ Fortsetzen",
    "paused_text":    "⏸ Bot pausiert.\n\nBewerbungen gestoppt. Drücke ▶️ um fortzufahren.",
    "resumed_text":   "▶️ Bot läuft wieder! Suche Wohnungen 🏠",
    "expiry_warn":    "⚠️ Abonnement läuft in {days} Tagen ab!\n\nVerlängere es jetzt.",
    "btn_renew":      "🔄 Verlängern",
    "admin_status_btn": "🟢 Systemstatus",
    "admin_status_ok":  "🟢 System läuft normal\n\n⏱ Uptime: {uptime}\n👥 Aktiv: {active}\n🌐 Proxy: {proxy}\n🖥 Browser: {browsers}/5\n\n✅ OK",
    "admin_error_notify": "🔴 Bot-Fehler!\n\n{error}\n\nZeit: {time}",
    "choose_lang":    "🌐 Обери мову / Wähle Sprache / Choose language:",
},
"en": {
    "welcome": "👋 Hi! I'm the SAGA Apartment Bot\n\nI automatically find SAGA Hamburg apartments,\napply and accept viewing invitations.\n\n🆓 {trial} days free\n💰 Then from €19/month\n\n━━━━━━━━━━━━━━\nDo you have an Immomio account?",
    "has_account":   "✅ Yes, I have an account",
    "no_account":    "❌ No account",
    "what_immomio":  "❓ What is Immomio?",
    "immomio_explain": "❓ What is Immomio?\n\ntenant.immomio.com — platform through which SAGA accepts all applications.\n\n✅ Registration is free\n✅ Fill profile and upload documents\n\nDo you already have an account?",
    "no_immomio": "❌ Without an Immomio account the bot cannot apply.\n\nHow to register:\n1. Open tenant.immomio.com\n2. Click Registrieren\n3. Fill profile 100%\n4. Upload documents\n5. Come back here",
    "already_reg": "✅ Already registered!",
    "enter_email":    "📧 Enter your Immomio email:",
    "enter_password": "🔑 Enter your Immomio password:",
    "checking":       "⏳ Checking Immomio credentials...",
    "wrong_creds":    "❌ Wrong email or password. Please try again.",
    "try_again":      "🔄 Try again",
    "reg_success":    "✅ Registration successful!\n\n🆓 {trial} days free\n📅 Until: {date}\n\n⚙️ Set up search filters:",
    "setup_filters":  "⚙️ Filter settings\n\nChoose parameters and save:",
    "setup_rooms_btn":"🛏 Rooms: {mn}–{mx}",
    "setup_price_btn":"💶 Price: €{mn}–€{mx}/mo",
    "save_start":     "✅ Save & start search",
    "choose_rooms":   "🛏 How many rooms are you looking for?",
    "choose_price":   "💶 Maximum rent price?",
    "any_price":      "any",
    "bot_started":    "🚀 Bot started!\n\n🛏 Rooms: {mr}–{xr}\n💶 Price: up to {price}\n\nAs soon as an apartment appears — I'll apply immediately! 🏠",
    "main_menu":      "🏠 Main menu",
    "btn_status":     "📊 My status & filters",
    "btn_pay":        "💳 Buy subscription",
    "btn_guide":      "📖 Apartment search guide",
    "btn_support":    "🆘 Support",
    "btn_stop":       "⏹ Stop bot",
    "btn_admin":      "👑 Admin",
    "btn_register":   "🚀 Register",
    "btn_expired":    "⚠️ Subscription expired",
    "change_lang":    "🌐 Change language",
    "back":           "◀️ Back",
    "status_text":    "📊 Status\n\n{icon} Subscription until: {expires}\n🏠 Apartments seen: {seen}\n\n🔧 Filters:\n  🛏 Rooms: {mr}–{xr}\n  💶 Price: €{mp}–€{xp}/mo",
    "reset_list":     "🔄 Reset list",
    "reset_filters":  "♻️ Reset filters",
    "filters_reset":  "♻️ Filters reset.",
    "list_reset":     "🔄 List reset!",
    "expired_text":   "⚠️ Subscription expired!\n\nRenew to continue apartment search.",
    "pay_title":      "💳 Buy subscription\n\n🥈 1 month — €19\n🥇 2 months — €29 (24% off)\n💎 3 months — €39 (+1 bonus month!)\n\n━━━━━━━━━━━━━━\nChoose a plan:",
    "plan_1_btn":     "🥈 1 month — €19",
    "plan_2_btn":     "🥇 2 months — €29",
    "plan_3_btn":     "💎 3 months — €39 (+1 bonus!)",
    "pay_details":    "✅ Plan: {plan}\n\n💎 Amount: {ton} TON\n📈 Rate: 1 TON = €{rate}\n\n📤 Send {ton} TON to:\n`{wallet}`\n\n💬 Comment (required!):\n`{comment}`\n\n✅ Subscription will be activated automatically",
    "check_payment":  "🔄 Check payment",
    "wallet_guide":   "📖 How to create a wallet?",
    "pay_confirmed":  "✅ Payment confirmed!\n\n📅 Subscription until: {date}\n\nBot is searching! 🏠",
    "pay_pending":    "⏳ Payment not found yet.\n\nMake sure you sent {ton} TON\nwith comment: `{comment}`\n\n✅ Subscription will be activated automatically",
    "no_payment":     "❌ No active payment. Choose a plan.",
    "pay_received":   "✅ Payment received!\n\n📅 Subscription until: {date}\n\nBot is searching! 🏠",
        "choose_payment":  "💳 Choose payment method:",
    "pay_stars":       "⭐ Telegram Stars (easy)",
    "pay_ton":         "💎 TON crypto",
    "stars_title":     "⭐ Pay with Telegram Stars\n\n🥈 1 month — {s1} Stars\n🥇 2 months — {s2} Stars (24% off)\n💎 3 months — {s3} Stars (+1 bonus month!)\n\n━━━━━━━━━━━━━━\nChoose a plan:",
    "stars_desc":      "SAGA Apartment Bot — {months} mo. subscription",
    "stars_pending":   "⭐ Stars payment — waiting for confirmation...",
    "stars_ok":        "✅ Stars payment confirmed!\n\n📅 Subscription until: {date}\n\nBot is searching! 🏠",
    "how_to_stars":    "⭐ How to pay with Telegram Stars?\n\n1. Press the plan button\n2. Telegram payment window appears\n3. Confirm payment\n4. Subscription activates automatically\n\n💡 Buy Stars in Telegram:\nSettings → Telegram Stars → Buy",
    "how_to_pay_choice": "Choose payment method for instructions:",
    "wallet_guide_text": "📖 How to create a wallet and pay?\n\n📱 Step 1 — Create wallet:\n1. Telegram → find @wallet\n2. Start → Wallet → Create\n3. ⚠️ Save seed phrase (12 words)!\n\n💰 Step 2 — Buy TON:\n1. @wallet → Top Up → TON → Buy\n2. Card or P2P\n\n📤 Step 3 — Send payment:\n1. @wallet → Send\n2. Paste address and comment\n3. Enter amount and confirm\n\n✅ Subscription will be activated automatically",
    "guide_caption":  "📖 Complete apartment search guide Hamburg\n\nDocuments, tips, useful links.",
    "guide_text":     "📖 Useful links:\n\n• saga.hamburg — official website\n• tenant.immomio.com — applications\n• meineschufa.de — SCHUFA online\n• hamburg.de/wbs — apply for WBS\n• mieterverein-hamburg.de — tenant rights\n\nQuestions? @{username}",
    "profile_incomplete": "⚠️ Your Immomio profile is incomplete!\n\n👉 Fill in at tenant.immomio.com/de/profile",
    "blacklist_added":    "🚫 Apartment added to blacklist.",
    "apply_viewed":       "👀 Your application was viewed!\n\n{link}",
    "apply_invited":      "🎉 You are invited to a viewing!\n\n{link}",
    "apply_rejected":     "❌ Application rejected.\n\n{link}",
    "btn_blacklist":      "🚫 Not interested",
    "btn_website":        "🌐 Our website",
    "website_text":       "🌐 Website:\nhttps://patrickkassparov.github.io/saga_bot/",
    "support_text":   "🆘 Support\n\nWrite directly:\n👤 @{username}\n\nWe'll reply as soon as possible!",
    "ref_menu":       "👥 Referral program",
    "ref_text":       "👥 Referral program\n\n🔗 Your link:\n`t.me/{bot}?start={code}`\n\n📊 Users invited: {count}\n🎁 Bonus earned: +{bonus} days\n\n✅ For each new user — both get +{ref_days} free days!",
    "ref_share":      "📤 Share link",
    "ref_bonus_msg":  "🎁 +{days} days bonus! Someone registered via your link.",
    "ref_welcome":    "🎁 +{days} days bonus for registering via referral link!",
    "stats_menu":     "📈 My statistics",
    "stats_text":     "📈 Statistics\n\n🏠 Applications: {total}\n✅ Successful: {success}\n❌ Failed: {fail}\n📊 Success rate: {rate}%\n\n📅 Active since: {since}",
    "btn_ref":        "👥 Referrals",
    "btn_stats":      "📈 Statistics",
    "new_flat":       "🏠 New apartment!\n🛏 {rooms} rooms | 💶 €{price}/mo\n\n{link}\n⏳ Applying...",
    "apply_ok":       "✅ Application sent!\n{link}",
    "apply_fail":     "❌ Application failed\n{link}",
    "inv_accepted":   "🗓 Viewing invitation accepted!\n\nCheck details in Immomio!",
    "bot_stopped":    "⏹ Bot stopped.\n/start to restart.",
    "admin_stats":    "👑 Admin\n\n👥 Total: {total}\n✅ Active: {active}\n💰 Paid: {paid}x",
    "users_btn":      "👥 Users",
    "broadcast_btn":  "📢 Broadcast",
    "broadcast_ask":  "📢 Enter broadcast message:",
    "broadcast_done": "📢 Sent {sent}/{total}",
    "activated":      "✅ Activated for {days} days",
    "deactivated":    "⏹ Deactivated",
    "not_interested":   "❌ Not interested",
    "blacklisted":      "🚫 Apartment added to blacklist.",
    "profile_warn":     "⚠️ Your Immomio profile is not complete!\n\nApplications may not be sent.\n\n👉 Complete your profile:\ntenant.immomio.com/de/profile\n\nAfter that everything works automatically ✅",
    "app_status_viewed":  "👀 Your application was viewed!\n\n🏠 {link}\n\nWait for a viewing invitation.",
    "app_status_invited": "🎉 You were invited to a viewing!\n\n🏠 {link}\n\nCheck details in Immomio → Besichtigungen",
    "app_status_rejected":"😔 Application rejected.\n\n🏠 {link}",
    "btn_pause":      "⏸ Pause",
    "btn_resume":     "▶️ Resume",
    "paused_text":    "⏸ Bot paused.\n\nApplications stopped. Press ▶️ to resume.",
    "resumed_text":   "▶️ Bot resumed! Searching 🏠",
    "expiry_warn":    "⚠️ Subscription expires in {days} days!\n\nRenew now.",
    "btn_renew":      "🔄 Renew",
    "admin_status_btn": "🟢 System status",
    "admin_status_ok":  "🟢 System running\n\n⏱ Uptime: {uptime}\n👥 Active: {active}\n🌐 Proxy: {proxy}\n🖥 Browsers: {browsers}/5\n\n✅ OK",
    "admin_error_notify": "🔴 Bot error!\n\n{error}\n\nTime: {time}",
    "choose_lang":    "🌐 Обери мову / Wähle Sprache / Choose language:",
},
}


def t(chat_id: int, key: str, **kw) -> str:
    lang = get_lang(chat_id)
    text = T.get(lang, T["uk"]).get(key, T["uk"].get(key, key))
    if kw:
        try:
            text = text.format(**kw)
        except (KeyError, ValueError):
            pass
    return text


# ═══════════════════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════════════════

def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    # WAL mode: дозволяє читати і писати одночасно без блокувань.
    # Критично при 100 юзерах — без WAL кожен INSERT лочить файл цілком.
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")   # баланс безпека/швидкість
    db.execute("PRAGMA busy_timeout=5000")    # чекаємо 5с перед SQLITE_BUSY
    db.execute("PRAGMA cache_size=-32000")    # 32 MB page cache
    db.execute("PRAGMA temp_store=MEMORY")    # temp таблиці в RAM
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        chat_id    INTEGER PRIMARY KEY,
        username   TEXT,
        email      TEXT,
        password   TEXT,
        active     INTEGER DEFAULT 0,
        lang       TEXT    DEFAULT 'uk',
        trial_until TEXT,
        paid_until  TEXT,
        created_at  TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS seen (
        chat_id INTEGER,
        link    TEXT,
        PRIMARY KEY (chat_id, link)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS seen_inv (
        chat_id INTEGER,
        inv_id  TEXT,
        PRIMARY KEY (chat_id, inv_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS filters (
        chat_id    INTEGER PRIMARY KEY,
        min_rooms  REAL DEFAULT 1,
        max_rooms  REAL DEFAULT 10,
        min_price  REAL DEFAULT 0,
        max_price  REAL DEFAULT 9999
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS payments (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id    INTEGER,
        ton_amount REAL,
        eur_amount REAL,
        days       INTEGER,
        comment    TEXT,
        status     TEXT DEFAULT 'pending',
        created_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS blacklist (
        chat_id INTEGER,
        link    TEXT,
        PRIMARY KEY (chat_id, link)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS apply_status (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id    INTEGER,
        link       TEXT,
        status     TEXT DEFAULT 'sent',
        notified   INTEGER DEFAULT 0,
        created_at TEXT,
        updated_at TEXT
    )""")
    # Реферальна система
    db.execute("""CREATE TABLE IF NOT EXISTS referrals (
        referrer_id  INTEGER,
        referred_id  INTEGER PRIMARY KEY,
        created_at   TEXT
    )""")
    # Статистика заявок
    db.execute("""CREATE TABLE IF NOT EXISTS apply_stats (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id      INTEGER,
        link         TEXT,
        success      INTEGER DEFAULT 0,
        rooms        REAL,
        price        REAL,
        created_at   TEXT
    )""")
    # Індекси для швидких запитів при 100+ юзерах
    db.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users(active)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_seen_chat ON seen(chat_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status, created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_apply_stats_chat ON apply_stats(chat_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_referrals_ref ON referrals(referrer_id)")
    # Міграції
    # Таблиця чорного списку квартир
    db.execute("""CREATE TABLE IF NOT EXISTS blacklist (
        chat_id  INTEGER,
        link     TEXT,
        PRIMARY KEY (chat_id, link)
    )""")
    # Таблиця статусів заявок
    db.execute("""CREATE TABLE IF NOT EXISTS application_status (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id     INTEGER,
        link        TEXT,
        msg_id      INTEGER,
        status      TEXT DEFAULT 'sent',
        checked_at  TEXT,
        created_at  TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_appstatus_chat ON application_status(chat_id, status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_blacklist_chat ON blacklist(chat_id)")

    for migration in [
        "ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'uk'",
        "ALTER TABLE users ADD COLUMN ref_code TEXT",
        "ALTER TABLE users ADD COLUMN referred_by INTEGER",
        "ALTER TABLE users ADD COLUMN paused INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN profile_warned INTEGER DEFAULT 0",
    ]:
        try:
            db.execute(migration)
        except sqlite3.OperationalError:
            pass
    db.commit()
    db.close()
    log(f"Database initialized: {DB_PATH} (WAL mode)")


# Пул з'єднань до SQLite.
# При 100 юзерах одночасні INSERT/SELECT без пулу викликають SQLITE_BUSY.
# Розмір пулу = кількість потоків що можуть писати одночасно (WAL підтримує 1 writer).
def _init_db_pool() -> None:
    """Заглушка — WAL mode вже встановлено в init_db."""
    log("DB ready (WAL mode)")


class _DBContext:
    """Відкриває з'єднання при вході, закриває при виході."""

    def __init__(self):
        self._db = None

    def __enter__(self):
        self._db = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        return self._db

    def __exit__(self, exc_type, *_):
        if self._db is not None:
            if exc_type:
                try:
                    self._db.rollback()
                except Exception:
                    pass
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None


def get_db():
    """with get_db() as db: db.execute(...)"""
    return _DBContext()



def get_user(chat_id: int) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row) if row else None


def upsert_user(chat_id: int, **kw) -> None:
    with get_db() as db:
        exists = db.execute(
            "SELECT chat_id FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if exists:
            sets = ", ".join(k + "=?" for k in kw)
            db.execute(
                "UPDATE users SET " + sets + " WHERE chat_id=?",
                (*kw.values(), chat_id),
            )
        else:
            kw["chat_id"] = chat_id
            kw.setdefault("created_at", datetime.now().isoformat())
            cols = ", ".join(kw.keys())
            vals = ",".join("?" * len(kw))
            db.execute(
                "INSERT INTO users (" + cols + ") VALUES (" + vals + ")",
                list(kw.values()),
            )
        db.commit()


def get_lang(chat_id: int) -> str:
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT lang FROM users WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return row["lang"] if row and row["lang"] else "uk"
    except Exception:
        return "uk"


def set_lang(chat_id: int, lang: str) -> None:
    with get_db() as db:
        exists = db.execute(
            "SELECT chat_id FROM users WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if exists:
            db.execute("UPDATE users SET lang=? WHERE chat_id=?", (lang, chat_id))
        else:
            db.execute(
                "INSERT INTO users (chat_id, lang, created_at) VALUES (?,?,?)",
                (chat_id, lang, datetime.now().isoformat()),
            )
        db.commit()


def is_subscribed(chat_id: int) -> bool:
    if chat_id in ADMIN_IDS:
        return True
    u = get_user(chat_id)
    if not u:
        return False
    now = datetime.now().isoformat()
    return bool(
        (u.get("paid_until") and u["paid_until"] > now)
        or (u.get("trial_until") and u["trial_until"] > now)
    )


def get_filters(chat_id: int) -> dict:
    with get_db() as db:
        row = db.execute("SELECT * FROM filters WHERE chat_id=?", (chat_id,)).fetchone()
    return (
        dict(row)
        if row
        else {"min_rooms": 1, "max_rooms": 10, "min_price": 0, "max_price": 9999}
    )


def save_filters(
    chat_id: int, mn_r: float, mx_r: float, mn_p: float, mx_p: float
) -> None:
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO filters VALUES (?,?,?,?,?)",
            (chat_id, mn_r, mx_r, mn_p, mx_p),
        )
        db.commit()


def get_seen(chat_id: int) -> set:
    with get_db() as db:
        rows = db.execute(
            "SELECT link FROM seen WHERE chat_id=?", (chat_id,)
        ).fetchall()
    return {r["link"] for r in rows}


def add_seen(chat_id: int, link: str) -> None:
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO seen (chat_id,link) VALUES (?,?)", (chat_id, link)
        )
        db.commit()


def add_seen_inv(chat_id: int, inv_id: str) -> None:
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO seen_inv (chat_id,inv_id) VALUES (?,?)",
            (chat_id, inv_id),
        )
        db.commit()


def get_seen_inv(chat_id: int) -> set:
    with get_db() as db:
        rows = db.execute(
            "SELECT inv_id FROM seen_inv WHERE chat_id=?", (chat_id,)
        ).fetchall()
    return {r["inv_id"] for r in rows}


def add_blacklist(chat_id: int, link: str) -> None:
    """Додає квартиру до чорного списку юзера."""
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO blacklist (chat_id,link) VALUES (?,?)",
            (chat_id, link),
        )
        db.commit()


def is_blacklisted(chat_id: int, link: str) -> bool:
    """Перевіряє чи квартира в чорному списку."""
    with get_db() as db:
        row = db.execute(
            "SELECT 1 FROM blacklist WHERE chat_id=? AND link=?", (chat_id, link)
        ).fetchone()
    return row is not None


def save_application_status(chat_id: int, link: str, msg_id: int) -> int:
    """Зберігає новостворену заявку для подальшого відстеження."""
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO application_status (chat_id,link,msg_id,status,created_at)"
            " VALUES (?,?,?,'sent',?)",
            (chat_id, link, msg_id, datetime.now().isoformat()),
        )
        db.commit()
        return cur.lastrowid


def get_pending_applications(chat_id: int) -> list[dict]:
    """Повертає заявки зі статусом 'sent' для перевірки."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM application_status"
            " WHERE chat_id=? AND status='sent'"
            " ORDER BY created_at DESC LIMIT 20",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_application_status(app_id: int, status: str) -> None:
    """Оновлює статус заявки."""
    with get_db() as db:
        db.execute(
            "UPDATE application_status SET status=?, checked_at=? WHERE id=?",
            (status, datetime.now().isoformat(), app_id),
        )
        db.commit()


def get_all_active() -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM users WHERE active=1 AND (paused IS NULL OR paused=0)"
        ).fetchall()
    return [dict(r) for r in rows]


def is_blacklisted(chat_id: int, link: str) -> bool:
    with get_db() as db:
        r = db.execute(
            "SELECT 1 FROM blacklist WHERE chat_id=? AND link=?", (chat_id, link)
        ).fetchone()
    return bool(r)


def add_to_blacklist(chat_id: int, link: str) -> None:
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO blacklist (chat_id,link) VALUES (?,?)", (chat_id, link))
        db.commit()


def save_apply_status(chat_id: int, link: str, status: str = "sent") -> int:
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO apply_status (chat_id,link,status,created_at,updated_at)"
            " VALUES (?,?,?,?,?)",
            (chat_id, link, status, datetime.now().isoformat(), datetime.now().isoformat()),
        )
        db.commit()
        row = db.execute(
            "SELECT id FROM apply_status WHERE chat_id=? AND link=? ORDER BY id DESC LIMIT 1",
            (chat_id, link),
        ).fetchone()
    return row["id"] if row else 0


def update_apply_status(apply_id: int, status: str) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE apply_status SET status=?, updated_at=?, notified=1 WHERE id=?",
            (status, datetime.now().isoformat(), apply_id),
        )
        db.commit()


def get_pending_apply_statuses() -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM apply_status WHERE status='sent'"
            " AND created_at > datetime('now','-7 days')"
        ).fetchall()
    return [dict(r) for r in rows]


def get_uptime() -> str:
    """Повертає аптайм бота у зручному форматі."""
    delta = datetime.now() - _BOT_START_TIME
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m = rem // 60
    if h >= 24:
        return f"{h // 24}д {h % 24}г {m}хв"
    return f"{h}г {m}хв"


# ═══════════════════════════════════════════════════════════════════════
#  TON PAYMENT
# ═══════════════════════════════════════════════════════════════════════

async def get_ton_price_eur() -> float:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://tonapi.io/v2/rates?tokens=ton&currencies=eur",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                data = await r.json()
                return float(data["rates"]["TON"]["prices"]["EUR"])
    except Exception as e:
        log(f"TON price error: {e}", "warning")
        return 2.5


async def get_ton_amount(eur: int) -> tuple[float, float]:
    rate = await get_ton_price_eur()
    return round(eur / rate, 2), round(rate, 2)


def make_comment(chat_id: int) -> str:
    return f"SAGA-{chat_id}"


async def check_ton_payment(
    chat_id: int, expected: float, comment: str
) -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            headers = {"Authorization": "Bearer " + TON_API_KEY}
            url = (
                "https://toncenter.com/api/v2/getTransactions"
                f"?address={TON_WALLET}&limit=30"
            )
            async with s.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
                if not data.get("ok"):
                    return False
                for tx in data.get("result", []):
                    msg = tx.get("in_msg", {})
                    mc = ""
                    if msg.get("msg_data", {}).get("text"):
                        try:
                            mc = base64.b64decode(
                                msg["msg_data"]["text"]
                            ).decode("utf-8", errors="ignore")
                        except Exception:
                            pass
                    if comment.lower() in mc.lower():
                        if int(msg.get("value", 0)) / 1e9 >= expected * 0.95:
                            return True
        return False
    except Exception as e:
        log(f"TON check error: {e}", "warning")
        return False


async def ton_payment_checker(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    with get_db() as db:
        pending = db.execute(
            "SELECT * FROM payments WHERE status='pending'"
            " AND created_at > datetime('now','-3 hours')"
        ).fetchall()
    for p in [dict(x) for x in pending]:
        if await check_ton_payment(p["chat_id"], p["ton_amount"], p["comment"]):
            paid_until = (datetime.now() + timedelta(days=p["days"])).isoformat()
            upsert_user(p["chat_id"], paid_until=paid_until, active=1)
            with get_db() as db:
                db.execute(
                    "UPDATE payments SET status='paid' WHERE id=?", (p["id"],)
                )
                db.commit()
            try:
                await ctx.bot.send_message(
                    p["chat_id"],
                    t(p["chat_id"], "pay_received", date=paid_until[:10]),
                    reply_markup=kb_main(p["chat_id"]),
                )
                await ctx.bot.send_message(
                    ADMIN_CHAT_ID,
                    f"💰 Нова оплата!\n👤 {p['chat_id']}\n"
                    f"📦 {p['days']} днів\n💎 {p['ton_amount']} TON",
                )
            except Exception as e:
                log(f"Payment notify error: {e}", "warning")


async def subscription_expiry_checker(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now()
    now_iso = now.isoformat()
    warn_threshold = (now + timedelta(days=3)).isoformat()

    with get_db() as db:
        all_users = db.execute("SELECT * FROM users WHERE active=1").fetchall()

    for u in [dict(r) for r in all_users]:
        cid = u["chat_id"]
        if cid in ADMIN_IDS:
            continue
        paid  = u.get("paid_until", "") or ""
        trial = u.get("trial_until", "") or ""

        # Підписка закінчилась — деактивуємо
        if not ((paid and paid > now_iso) or (trial and trial > now_iso)):
            upsert_user(cid, active=0)
            try:
                await ctx.bot.send_message(
                    cid,
                    t(cid, "expired_text"),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(t(cid, "btn_pay"),     callback_data="pay")],
                        [InlineKeyboardButton(t(cid, "btn_support"), callback_data="support")],
                    ]),
                )
            except Exception:
                pass
            continue

        # Нагадування за 3 дні — тільки якщо не надсилали сьогодні
        expiry = paid if (paid and paid > now_iso) else trial
        if expiry and now_iso < expiry <= warn_threshold:
            days_left = (datetime.fromisoformat(expiry) - now).days + 1
            try:
                await ctx.bot.send_message(
                    cid,
                    t(cid, "expiry_warn", days=days_left),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(t(cid, "btn_renew"), callback_data="pay")],
                    ]),
                )
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════
#  BROWSER POOL
#  При появі квартири всі 100 юзерів хочуть подати заявку одночасно.
#  Один браузер не витримає — використовуємо пул з N екземплярів.
# ═══════════════════════════════════════════════════════════════════════

BROWSER_POOL_SIZE = int(os.getenv("BROWSER_POOL_SIZE", "5"))

playwright_instance = None
_browser_pool: asyncio.Queue  # черга вільних браузерів
_pool_lock = asyncio.Lock()   # захист ініціалізації пулу

# Семафор: не більше N одночасних auto_apply по всіх юзерах.
# Кожен apply займає ~10–30с — без ліміту 100 задач одразу вичерпають пам'ять.
_APPLY_GLOBAL_SEM = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT_APPLY", "8")))

user_contexts: dict = {}
user_sems: dict = {}          # per-user semaphore (1 apply на юзера)
flat_cache: dict = {}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


async def _launch_one_browser():
    """Запускає один екземпляр Chromium."""
    return await playwright_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
            "--memory-pressure-off",
        ],
    )


async def init_browser() -> None:
    """Ініціалізує playwright та наповнює пул браузерів."""
    global playwright_instance, _browser_pool
    playwright_instance = await async_playwright().start()
    _browser_pool = asyncio.Queue(maxsize=BROWSER_POOL_SIZE)
    for i in range(BROWSER_POOL_SIZE):
        b = await _launch_one_browser()
        await _browser_pool.put(b)
        log(f"Browser {i + 1}/{BROWSER_POOL_SIZE} ready")
    log(f"Browser pool initialized ({BROWSER_POOL_SIZE} instances)")


class BrowserLease:
    """
    Контекстний менеджер для безпечного отримання/повернення браузера з пулу.

    Використання:
        async with BrowserLease() as browser:
            ctx = await browser.new_context(...)
    """

    def __init__(self):
        self._browser = None

    async def __aenter__(self):
        self._browser = await _browser_pool.get()
        # Якщо браузер впав — замінюємо його живим
        if not self._browser.is_connected():
            log("Dead browser in pool — replacing", "warning")
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = await _launch_one_browser()
        return self._browser

    async def __aexit__(self, *_):
        if self._browser is not None:
            # Перевіряємо стан перед поверненням у пул
            if self._browser.is_connected():
                await _browser_pool.put(self._browser)
            else:
                log("Browser crashed — spawning replacement", "warning")
                try:
                    await self._browser.close()
                except Exception:
                    pass
                new_b = await _launch_one_browser()
                await _browser_pool.put(new_b)


async def ensure_browser() -> None:
    """
    Перевіряє що пул живий.
    Якщо всі браузери впали одночасно (OOM тощо) — перезапускає пул.
    """
    global playwright_instance, _browser_pool, user_contexts
    # Перевіряємо чи пул взагалі існує
    try:
        if _browser_pool and not _browser_pool.empty():
            return
    except Exception:
        pass

    log("Entire browser pool is down — reinitializing...", "warning")
    # Закриваємо всі кешовані контексти юзерів
    for ctx_data in list(user_contexts.values()):
        try:
            await ctx_data["context"].close()
        except Exception:
            pass
    user_contexts = {}

    try:
        if playwright_instance:
            await playwright_instance.stop()
    except Exception:
        pass

    await asyncio.sleep(3)
    await init_browser()


async def check_immomio_profile(email: str, password: str) -> tuple[bool, str]:
    """
    Перевіряє заповненість профілю Immomio.
    Повертає (is_complete, warning_message).
    Шукає індикатори незаповненого профілю на сторінці профілю.
    """
    try:
        async with BrowserLease() as b:
            ctx = await b.new_context(user_agent=_USER_AGENT)
            page = await ctx.new_page()
            try:
                await page.goto(
                    "https://tenant.immomio.com/de/auth/login",
                    timeout=30000, wait_until="domcontentloaded"
                )
                if not await immomio_login(page, email, password):
                    return True, ""  # не можемо перевірити — не блокуємо
                await page.goto(
                    "https://tenant.immomio.com/de/profile",
                    timeout=30000, wait_until="domcontentloaded"
                )
                await page.wait_for_timeout(2000)
                body = await page.evaluate("() => document.body.innerText")
                # Індикатори незаповненого профілю
                incomplete_signals = [
                    "Bitte vervollständige",
                    "nicht vollständig",
                    "unvollständig",
                    "fehlende Angaben",
                    "Profil vervollständigen",
                    "Complete your profile",
                ]
                is_incomplete = any(sig.lower() in body.lower() for sig in incomplete_signals)
                # Також перевіряємо прогрес-бар якщо є
                progress = await page.evaluate("""
                    () => {
                        const el = document.querySelector('[class*="progress"], [role="progressbar"]');
                        return el ? el.getAttribute('aria-valuenow') || el.textContent : null;
                    }
                """)
                if progress:
                    try:
                        pct = int(''.join(filter(str.isdigit, str(progress))))
                        if pct < 100:
                            is_incomplete = True
                    except Exception:
                        pass
                return not is_incomplete, ""
            finally:
                try:
                    await page.close()
                    await ctx.close()
                except Exception:
                    pass
    except Exception as e:
        log(f"Profile check error: {e}", "warning")
        return True, ""  # при помилці не блокуємо


async def get_uctx(chat_id: int) -> dict | None:
    """
    Повертає кешований контекст браузера для юзера (сторінки для сканування
    та запрошень). Контекст створюється один раз і живе до зупинки бота.
    Якщо браузер, в якому жив контекст, впав — контекст видаляється і
    повертається None (наступний виклик створить новий).
    """
    if chat_id in user_contexts:
        ctx_data = user_contexts[chat_id]
        # Перевіряємо живість контексту
        try:
            if ctx_data["context"].browser.is_connected():
                return ctx_data
        except Exception:
            pass
        # Контекст мертвий — видаляємо
        try:
            await ctx_data["context"].close()
        except Exception:
            pass
        del user_contexts[chat_id]

    u = get_user(chat_id)
    if not u or not u.get("email"):
        return None

    # Беремо браузер з пулу для створення контексту
    async with BrowserLease() as browser:
        ctx = await browser.new_context(
            user_agent=_USER_AGENT, viewport={"width": 1280, "height": 900}
        )
        user_contexts[chat_id] = {
            "context":   ctx,
            "scan_page": await ctx.new_page(),
            "inv_page":  await ctx.new_page(),
            "email":     u["email"],
            "password":  decrypt_password(u["password"]),
            "browser":   browser,   # зберігаємо посилання
        }
    return user_contexts[chat_id]


async def accept_cookies(page) -> None:
    for txt in ["Alle akzeptieren", "Alles akzeptieren", "Alle erlauben"]:
        try:
            b = page.locator("text=" + txt)
            if await b.count() > 0:
                await b.first.click(force=True)
                await page.wait_for_timeout(400)
                return
        except Exception:
            pass


async def immomio_login(page, email: str, password: str) -> bool:
    await page.wait_for_timeout(1000)
    await accept_cookies(page)
    try:
        await page.wait_for_selector('input[type="email"]', timeout=8000)
        await page.fill('input[type="email"]', email)
        await page.locator('button[type="submit"]').first.click(force=True)
        await page.wait_for_timeout(2500)
    except Exception:
        return False
    if "sso.immomio.com" in page.url:
        try:
            await page.wait_for_selector('input[name="username"]', timeout=6000)
        except Exception:
            pass
        for s in ['input[name="username"]', 'input[type="text"]']:
            if await page.locator(s).count() > 0:
                await page.locator(s).first.fill(email)
                break
        for s in ['input[name="password"]', 'input[type="password"]']:
            if await page.locator(s).count() > 0:
                await page.locator(s).first.fill(password)
                break
        for s in ['input[type="submit"]', 'button[type="submit"]']:
            if await page.locator(s).count() > 0:
                await page.locator(s).first.click(force=True)
                break
        await page.wait_for_timeout(4000)
    return "sso.immomio.com" not in page.url and "auth" not in page.url


def is_apartment(link: str) -> bool:
    return not any(k in link.lower() for k in EXCLUDE)


async def check_immomio_profile(page, chat_id: int) -> bool:
    """Перевіряє чи заповнений профіль Immomio на 100%."""
    try:
        await page.goto(
            "https://tenant.immomio.com/de/profile",
            timeout=20000, wait_until="domcontentloaded"
        )
        await page.wait_for_timeout(800)
        body = await page.evaluate("() => document.body.innerText")
        incomplete_markers = [
            "Profil vervollständigen",
            "Angaben fehlen",
            "nicht vollständig",
            "bitte ergänze",
        ]
        is_incomplete = any(m.lower() in body.lower() for m in incomplete_markers)
        try:
            await page.go_back()
        except Exception:
            pass
        return not is_incomplete
    except Exception as e:
        log(f"Profile check error {chat_id}: {e}", "warning")
        return True


async def get_flat_details(link: str) -> tuple[float | None, float | None]:
    if link in flat_cache:
        return flat_cache[link]
    rooms = price = None
    try:
        async with BrowserLease() as b:
            ctx = await b.new_context(user_agent=_USER_AGENT)
            page = await ctx.new_page()
            try:
                await page.goto(link, timeout=30000, wait_until="domcontentloaded")
                await page.wait_for_timeout(1000)
                text = await page.evaluate("() => document.body.innerText")
                # All SAGA room formats:
                # "Zimmer: 2,5" / "Zimmer: 2"  (detail page)
                # "2,5 Zimmer" / "2 Zimmer"    (card)
                # "2,5-Zimmer-Wohnung"          (title)
                # "zweieinhalbzimmer" etc.       (verbal)
                _VERBAL_MAP = [
                    ("einzimmer", 1.0), ("1-zimmer", 1.0),
                    ("zweizimmer", 2.0), ("2-zimmer", 2.0),
                    ("zweieinhalbzimmer", 2.5), ("2,5-zimmer", 2.5),
                    ("dreizimmer", 3.0), ("3-zimmer", 3.0),
                    ("dreieinhalb", 3.5), ("3,5-zimmer", 3.5),
                    ("vierzimmer", 4.0), ("4-zimmer", 4.0),
                    ("fuenfzimmer", 5.0), ("fünfzimmer", 5.0), ("5-zimmer", 5.0),
                ]
                _tl = text.lower()
                for _kw, _val in _VERBAL_MAP:
                    if _kw in _tl:
                        rooms = _val
                        break
                if rooms is None:
                    _rm = re.search(r"Zimmer\s*:\s*([\d,\.]+)", text)
                    if _rm:
                        try: rooms = float(_rm.group(1).replace(",", "."))
                        except ValueError: pass
                if rooms is None:
                    _rm = re.search(r"([\d]+[,\.]?[\d]*)\s*-?\s*Zimmer", text)
                    if _rm:
                        try: rooms = float(_rm.group(1).replace(",", "."))
                        except ValueError: pass
                m = re.search(r"([\d\.]+,\d{2})\s*\u20ac", text)
                if m:
                    price = float(m.group(1).replace(".", "").replace(",", "."))
            finally:
                try:
                    await page.close()
                    await ctx.close()
                except Exception:
                    pass
    except Exception as e:
        log(f"Flat details error for {link}: {e}", "warning")
    flat_cache[link] = (rooms, price)
    return rooms, price



# ═══════════════════════════════════════════════════════════════════════
#  PROXY HELPERS
# ═══════════════════════════════════════════════════════════════════════

_proxy_index = 0
_proxy_lock  = asyncio.Lock()


async def get_next_proxy() -> dict | None:
    """
    Повертає наступний проксі з пулу (round-robin).
    Якщо PROXY_LIST порожній — повертає None (без проксі).

    Формат в .env:
      PROXY_LIST=http://user:pass@host1:port,http://user:pass@host2:port

    Рекомендовані провайдери (rotating proxies):
      - webshare.io — від $2.99/міс, є безкоштовний план
      - proxymesh.com — від $10/міс
      - brightdata.com — від $15/міс
    """
    global _proxy_index
    if not PROXY_LIST:
        return None
    async with _proxy_lock:
        proxy_url = PROXY_LIST[_proxy_index % len(PROXY_LIST)]
        _proxy_index += 1
    return {"server": proxy_url}


async def auto_apply(chat_id: int, link: str) -> bool:
    """
    Подає заявку від імені юзера.

    Подвійний семафор:
      _APPLY_GLOBAL_SEM  — не більше MAX_CONCURRENT_APPLY паралельних заявок
                           по всіх юзерах (захист від OOM при одночасному burst)
      user_sems[chat_id] — не більше 1 заявки одночасно на одного юзера
                           (Immomio не любить паралельні сесії одного акаунту)
    Кожна заявка бере браузер з пулу через BrowserLease — повертає його після.
    """
    async with _APPLY_GLOBAL_SEM:
        async with user_sems.setdefault(chat_id, asyncio.Semaphore(1)):
            u = get_user(chat_id)
            if not u or not u.get("email"):
                return False
            password = decrypt_password(u["password"])

            proxy = await get_next_proxy()
            async with BrowserLease() as b:
                ctx_kwargs: dict = {
                    "user_agent": _USER_AGENT,
                    "viewport": {"width": 1280, "height": 900},
                }
                if proxy:
                    ctx_kwargs["proxy"] = proxy
                ctx = await b.new_context(**ctx_kwargs)
                page = ipage = None
                try:
                    lp = await ctx.new_page()
                    await lp.goto(
                        "https://tenant.immomio.com/de/auth/login",
                        timeout=30000,
                        wait_until="domcontentloaded",
                    )
                    if not await immomio_login(lp, u["email"], password):
                        await lp.close()
                        await ctx.close()
                        return False
                    await lp.close()

                    page = await ctx.new_page()
                    await page.goto(link, timeout=60000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(600)
                    await accept_cookies(page)

                    href = await page.evaluate("""
                        () => {
                            const el = [...document.querySelectorAll('a')].find(e =>
                                (e.href||'').includes('immomio.com') ||
                                e.textContent.includes('Zum Expos'));
                            return el ? el.href : null;
                        }
                    """)
                    if not href:
                        await page.close()
                        await ctx.close()
                        return False

                    target = href.replace("/apply/", "/de/apply/")
                    ipage = await ctx.new_page()
                    await ipage.goto(target, timeout=60000, wait_until="domcontentloaded")
                    await ipage.wait_for_timeout(600)
                    await accept_cookies(ipage)

                    for _ in range(2):
                        body = await ipage.evaluate("() => document.body.innerText")
                        if (
                            "Registrieren und bewerben" in body
                            or "Bereits registriert" in body
                        ):
                            await ipage.goto(
                                "https://tenant.immomio.com/de/auth/login",
                                timeout=20000,
                                wait_until="domcontentloaded",
                            )
                            await immomio_login(ipage, u["email"], password)
                            await ipage.goto(
                                target, timeout=60000, wait_until="domcontentloaded"
                            )
                            await ipage.wait_for_timeout(600)

                        clicked = await ipage.evaluate("""
                            () => {
                                const el = [...document.querySelectorAll('a,button,[role="button"]')]
                                    .find(e =>
                                        e.textContent.trim().toLowerCase().includes('jetzt bewerben') ||
                                        e.textContent.trim().toLowerCase().includes('interesse bekunden'));
                                if (el) { el.click(); return true; }
                                return false;
                            }
                        """)
                        if clicked:
                            await ipage.wait_for_timeout(2000)
                            url_f = ipage.url
                            body_f = await ipage.evaluate(
                                "() => document.body.innerText.toLowerCase()"
                            )
                            if (
                                "applications" in url_f
                                or "expose" in url_f
                                or "registrieren" not in body_f
                            ):
                                return True
                        await asyncio.sleep(2)
                    return False

                except Exception as e:
                    log(f"Apply error {chat_id}: {e}", "warning")
                    return False
                finally:
                    try:
                        if page:
                            await page.close()
                        if ipage:
                            await ipage.close()
                        await ctx.close()
                    except Exception:
                        pass


async def _apply_task(bot, chat_id: int, link: str) -> None:
    try:
        # Перевіряємо чорний список
        if is_blacklisted(chat_id, link):
            return

        rooms, price = await get_flat_details(link)
        f = get_filters(chat_id)
        if rooms is not None and (
            rooms < f["min_rooms"] or rooms > f["max_rooms"]
        ):
            return
        if price is not None and (
            price < f["min_price"] or price > f["max_price"]
        ):
            return
        rs = str(int(rooms)) if rooms and rooms == int(rooms) else (str(rooms) if rooms else "?")
        ps = str(int(price)) if price else "?"

        # Повідомлення про нову квартиру з кнопкою "Не цікавить"
        msg = await bot.send_message(
            chat_id,
            t(chat_id, "new_flat", rooms=rs, price=ps, link=link),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    t(chat_id, "not_interested"),
                    callback_data=f"blacklist_{link[:60]}",
                )
            ]]),
        )

        ok = await auto_apply(chat_id, link)
        key = "apply_ok" if ok else "apply_fail"
        result_msg = await bot.send_message(chat_id, t(chat_id, key, link=link))

        # Зберігаємо статус заявки для відстеження
        if ok:
            save_application_status(chat_id, link, result_msg.message_id)

        # Зберігаємо статистику
        try:
            with get_db() as db:
                db.execute(
                    "INSERT INTO apply_stats (chat_id,link,success,rooms,price,created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (chat_id, link, 1 if ok else 0, rooms, price,
                     datetime.now().isoformat()),
                )
                db.commit()
        except Exception:
            pass

        # Перевіряємо профіль після першої невдалої заявки
        if not ok:
            u = get_user(chat_id)
            if u and not u.get("profile_warned"):
                upsert_user(chat_id, profile_warned=1)
                asyncio.create_task(_check_profile_task(bot, chat_id))

    except Exception as e:
        log(f"Apply task error {chat_id}: {e}", "warning")


async def _check_profile_task(bot, chat_id: int) -> None:
    """Перевіряє профіль Immomio і попереджає юзера якщо незаповнений."""
    try:
        u = get_user(chat_id)
        if not u or not u.get("email"):
            return
        is_complete, _ = await check_immomio_profile(
            u["email"], decrypt_password(u["password"])
        )
        if not is_complete:
            await bot.send_message(chat_id, t(chat_id, "profile_warn"))
    except Exception as e:
        log(f"Profile check task error: {e}", "warning")


async def scan_and_apply_all(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Головний сканер. Запускається кожні SCAN_INTERVAL секунд.

    При появі нової квартири потенційно 100 юзерів отримують задачу одночасно.
    Щоб не створювати 100 задач миттєво (burst → OOM):
    - задачі створюються батчами по TASK_BATCH_SIZE з паузою між батчами
    - кожна задача додатково обмежена _APPLY_GLOBAL_SEM
    """
    users = get_all_active()
    if not users:
        return
    log(f"SCAN {time.strftime('%H:%M:%S')} ({len(users)} users)")

    await ensure_browser()
    links: list[str] = []

    try:
        uctx = await get_uctx(users[0]["chat_id"])
        if not uctx:
            return
        sp = uctx["scan_page"]
        await sp.goto(SAGA_URL, timeout=60000, wait_until="domcontentloaded")
        await sp.wait_for_timeout(800)
        await accept_cookies(sp)
        elements = await sp.query_selector_all("a[href*='immo-detail']")
        seen_h: set[str] = set()
        for el in elements:
            href = await el.get_attribute("href")
            if not href:
                continue
            link = (
                href if href.startswith("http")
                else "https://www.saga.hamburg" + href
            )
            if link in seen_h or not is_apartment(link):
                continue
            seen_h.add(link)
            links.append(link)
        log(f"  {len(links)} apartments found")
    except Exception as e:
        log(f"Scan error: {e}", "warning")
        await notify_admin_error(ctx.bot, f"Scan error: {e}")
        return

    if not links:
        return

    # Батчева обробка: TASK_BATCH_SIZE задач → пауза → наступний батч.
    # Це запобігає одночасному запуску 100 задач при появі однієї квартири.
    TASK_BATCH_SIZE = 10
    BATCH_DELAY = 0.5  # секунд між батчами

    tasks_created = 0
    for u in users:
        cid = u["chat_id"]
        if not is_subscribed(cid):
            continue
        user_seen = get_seen(cid)
        for link in links:
            if link in user_seen:
                continue
            add_seen(cid, link)
            task = asyncio.create_task(_apply_task(ctx.bot, cid, link))
            task.add_done_callback(
                lambda t: log(f"Apply task exception: {t.exception()}", "warning")
                if not t.cancelled() and t.exception()
                else None
            )
            tasks_created += 1
            # Пауза після кожного батчу
            if tasks_created % TASK_BATCH_SIZE == 0:
                await asyncio.sleep(BATCH_DELAY)

    if tasks_created:
        log(f"  Queued {tasks_created} apply tasks")


async def check_application_statuses(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Перевіряє статуси поданих заявок.
    Запускається кожні 30 хвилин.
    Перевіряє сторінку applications в Immomio і шукає зміни статусу.
    """
    for u in get_all_active():
        cid = u["chat_id"]
        apps = get_pending_applications(cid)
        if not apps:
            continue
        uctx = await get_uctx(cid)
        if not uctx:
            continue
        try:
            ip = uctx["inv_page"]
            await ip.goto(
                "https://tenant.immomio.com/de/properties/applications",
                timeout=20000, wait_until="domcontentloaded"
            )
            await ip.wait_for_timeout(800)
            body = await ip.evaluate("() => document.body.innerText.toLowerCase()")

            for app in apps:
                link = app["link"]
                app_id = app["id"]
                # Шукаємо ознаки перегляду або запрошення
                short_link = link.split("/")[-1] if "/" in link else link

                # Запрошення на огляд
                if "besichtigung" in body and short_link in body:
                    update_application_status(app_id, "invited")
                    try:
                        await ctx.bot.send_message(
                            cid, t(cid, "app_status_invited", link=link)
                        )
                    except Exception:
                        pass

                # Заявку переглянули (але ще не запросили)
                elif "angesehen" in body or "gelesen" in body:
                    if short_link in body:
                        update_application_status(app_id, "viewed")
                        try:
                            await ctx.bot.send_message(
                                cid, t(cid, "app_status_viewed", link=link)
                            )
                        except Exception:
                            pass

                # Відмова
                elif "abgelehnt" in body and short_link in body:
                    update_application_status(app_id, "rejected")
                    try:
                        await ctx.bot.send_message(
                            cid, t(cid, "app_status_rejected", link=link)
                        )
                    except Exception:
                        pass

        except Exception as e:
            log(f"App status check error {cid}: {e}", "warning")


async def check_invitations_all(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    for u in get_all_active():
        cid = u["chat_id"]
        if not is_subscribed(cid):
            continue
        uctx = await get_uctx(cid)
        if not uctx:
            continue
        try:
            ip = uctx["inv_page"]
            await ip.goto(
                "https://tenant.immomio.com/de/properties/applications",
                timeout=20000,
                wait_until="domcontentloaded",
            )
            await ip.wait_for_timeout(600)
            await accept_cookies(ip)
            body = await ip.evaluate("() => document.body.innerText")
            if "login" in ip.url or "Anmelden" in body[:300]:
                await ip.goto(
                    "https://tenant.immomio.com/de/auth/login",
                    timeout=20000,
                    wait_until="domcontentloaded",
                )
                await immomio_login(ip, uctx["email"], uctx["password"])
                await ip.goto(
                    "https://tenant.immomio.com/de/properties/applications",
                    timeout=20000,
                    wait_until="domcontentloaded",
                )
                await ip.wait_for_timeout(600)

            besi = ip.locator("text=Besichtigungen")
            if await besi.count() > 0:
                await besi.first.click(force=True)
                await ip.wait_for_timeout(800)

            # ── Step 1: Select time slot if present (comes before confirm button) ──
            slot_selected = await ip.evaluate("""
                () => {
                    // Immomio sometimes shows time slots first (radio/checkbox list)
                    // Pick the first available slot
                    const slot = document.querySelector(
                        'input[type="radio"]:not(:disabled), input[type="checkbox"]:not(:disabled)'
                    );
                    if (slot) { slot.click(); return true; }
                    // Also try clicking first date/time card element
                    const cards = [...document.querySelectorAll('[class*="slot"],[class*="termin"],[class*="date"],[class*="time"]')];
                    for (const c of cards) {
                        if (c.offsetParent !== null) { c.click(); return true; }
                    }
                    return false;
                }
            """)
            if slot_selected:
                await ip.wait_for_timeout(600)

            # ── Step 2: Click accept/confirm button ───────────────────────────────
            clicked = await ip.evaluate("""
                () => {
                    const kws = [
                        "einladung annehmen","termin annehmen","termin bestätigen",
                        "termin bestaetigen","zusagen","annehmen","bestätigen",
                        "bestaetigen","confirm","accept","weiter","absenden",
                        "termin auswählen","datum auswählen","wunschtermin"
                    ];
                    const all = [...document.querySelectorAll('button,a,[role="button"]')];
                    const res = [];
                    for (const el of all) {
                        const txt = el.textContent.trim().toLowerCase();
                        if (kws.some(k => txt.includes(k)) && txt.length < 80) {
                            el.click(); res.push(txt);
                        }
                    }
                    return res;
                }
            """)
            if clicked:
                await ip.wait_for_timeout(800)
                # ── Step 3: if confirm opened another slot form — pick & confirm again
                await ip.evaluate("""
                    () => {
                        const slot = document.querySelector(
                            'input[type="radio"]:not(:disabled), input[type="checkbox"]:not(:disabled)'
                        );
                        if (slot) slot.click();
                        const confirmKws = ["bestätigen","bestaetigen","confirm","weiter","absenden"];
                        const btns = [...document.querySelectorAll('button,[role="button"]')];
                        for (const b of btns) {
                            const txt = b.textContent.trim().toLowerCase();
                            if (confirmKws.some(k => txt.includes(k)) && txt.length < 50) {
                                b.click(); break;
                            }
                        }
                    }
                """)
                await ip.wait_for_timeout(600)
                inv_id = str(cid) + "_" + str(int(time.time() // 3600))
                if inv_id not in get_seen_inv(cid):
                    add_seen_inv(cid, inv_id)
                    await ctx.bot.send_message(cid, t(cid, "inv_accepted"))
            elif slot_selected:
                # Slot selected but no confirm button found yet — try confirm alone
                await ip.evaluate("""
                    () => {
                        const confirmKws = ["bestätigen","bestaetigen","confirm","weiter","absenden","zusagen"];
                        const btns = [...document.querySelectorAll('button,[role="button"]')];
                        for (const b of btns) {
                            const txt = b.textContent.trim().toLowerCase();
                            if (confirmKws.some(k => txt.includes(k)) && txt.length < 50) {
                                b.click(); break;
                            }
                        }
                    }
                """)
                await ip.wait_for_timeout(600)
                inv_id = str(cid) + "_" + str(int(time.time() // 3600))
                if inv_id not in get_seen_inv(cid):
                    add_seen_inv(cid, inv_id)
                    await ctx.bot.send_message(cid, t(cid, "inv_accepted"))
        except Exception as e:
            log(f"Inv error {cid}: {e}", "warning")


# ═══════════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════

def kb_main(chat_id: int) -> InlineKeyboardMarkup:
    """
    Головне меню — розсортоване по логічних блоках:
      [Статус / Реєстрація]
      [Оплата]           — тільки якщо немає активної платної підписки
      ─────────────────────────────────
      [Статистика] [Реферали]
      [Гід]        [Підтримка]
      ─────────────────────────────────
      [Пауза / Стоп]
      [Мова]
      [Адмін]            — тільки для адміна
    """
    u = get_user(chat_id)
    sub = is_subscribed(chat_id)
    paused = bool(u and u.get("paused"))
    rows = []

    if not u or not u.get("email"):
        # ── Незареєстрований ──────────────────────────
        rows.append([InlineKeyboardButton(t(chat_id, "btn_register"), callback_data="register")])
        rows.append([
            InlineKeyboardButton(t(chat_id, "btn_guide"),   callback_data="send_guide"),
            InlineKeyboardButton(t(chat_id, "btn_support"), callback_data="support"),
        ])
    else:
        # ── Блок 1: Статус ────────────────────────────
        has_paid = (u.get("paid_until", "") or "") > datetime.now().isoformat()
        if sub:
            pause_label = t(chat_id, "btn_resume") if paused else t(chat_id, "btn_pause")
            pause_cb    = "resume_bot" if paused else "pause_bot"
            rows.append([InlineKeyboardButton(t(chat_id, "btn_status"), callback_data="status")])
        else:
            rows.append([InlineKeyboardButton(t(chat_id, "btn_expired"), callback_data="pay")])
            pause_label = t(chat_id, "btn_pause")
            pause_cb    = "pause_bot"

        # ── Блок 2: Оплата ────────────────────────────
        if not has_paid:
            rows.append([InlineKeyboardButton(t(chat_id, "btn_pay"), callback_data="pay")])

        # ── Блок 3: Статистика + Реферали ────────────
        rows.append([
            InlineKeyboardButton(t(chat_id, "btn_stats"), callback_data="my_stats"),
            InlineKeyboardButton(t(chat_id, "btn_ref"),   callback_data="referral"),
        ])

        # ── Блок 4: Гід + Підтримка + Сайт ─────────────
        rows.append([
            InlineKeyboardButton(t(chat_id, "btn_guide"),   callback_data="send_guide"),
            InlineKeyboardButton(t(chat_id, "btn_support"), callback_data="support"),
        ])
        rows.append([InlineKeyboardButton(t(chat_id, "btn_website"), callback_data="website")])

        # ── Блок 5: Пауза / Стоп ─────────────────────
        rows.append([
            InlineKeyboardButton(pause_label,              callback_data=pause_cb),
            InlineKeyboardButton(t(chat_id, "btn_stop"),   callback_data="stop"),
        ])

    # ── Мова ─────────────────────────────────────────
    rows.append([InlineKeyboardButton(t(chat_id, "change_lang"), callback_data="change_lang")])

    # ── Адмін ────────────────────────────────────────
    if chat_id in ADMIN_IDS:
        rows.append([InlineKeyboardButton(t(chat_id, "btn_admin"), callback_data="admin")])

    return InlineKeyboardMarkup(rows)


def kb_lang() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇦 Українська", callback_data="lang_uk")],
        [InlineKeyboardButton("🇩🇪 Deutsch",     callback_data="lang_de")],
        [InlineKeyboardButton("🇬🇧 English",     callback_data="lang_en")],
    ])


def kb_admin_panel() -> InlineKeyboardMarkup:
    with get_db() as db:
        total  = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        active = db.execute("SELECT COUNT(*) as c FROM users WHERE active=1").fetchone()["c"]
        paid   = db.execute("SELECT COUNT(*) as c FROM payments WHERE status='paid'").fetchone()["c"]
        today  = db.execute(
            "SELECT COUNT(*) as c FROM payments WHERE status='paid'"
            " AND created_at > datetime('now','-1 day')"
        ).fetchone()["c"]
    return InlineKeyboardMarkup([
        # ── Статус ──────────────────────────────────
        [InlineKeyboardButton("🟢 Статус системи",          callback_data="admin_status")],
        # ── Статистика ──────────────────────────────
        [InlineKeyboardButton(f"👥 Юзери: {total} (✅{active})", callback_data="admin_users")],
        [InlineKeyboardButton(f"💰 Оплат всього: {paid} (сьогодні: {today})", callback_data="admin_payments")],
        # ── Дії ─────────────────────────────────────
        [InlineKeyboardButton("📢 Розіслати всім",           callback_data="admin_broadcast")],
        [
            InlineKeyboardButton("✅ Активувати",            callback_data="admin_activate_prompt"),
            InlineKeyboardButton("❌ Деактивувати",          callback_data="admin_deactivate_prompt"),
        ],
    ])


async def safe_edit(q, text: str, **kwargs) -> None:
    try:
        await q.edit_message_text(text, **kwargs)
    except Exception as e:
        if "not modified" not in str(e).lower():
            log(f"Edit error: {e}", "warning")


async def show_admin_panel(reply_fn) -> None:
    with get_db() as db:
        total  = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        active = db.execute("SELECT COUNT(*) as c FROM users WHERE active=1").fetchone()["c"]
        paid   = db.execute("SELECT COUNT(*) as c FROM payments WHERE status='paid'").fetchone()["c"]
    text = (
        f"👑 SAGA Admin Panel\n\n"
        f"👥 Всього юзерів: {total}\n"
        f"✅ Активних: {active}\n"
        f"💰 Оплат всього: {paid}\n\n"
        f"Що робимо?"
    )
    await reply_fn(text, reply_markup=kb_admin_panel())


# ═══════════════════════════════════════════════════════════════════════
#  HANDLERS
# ═══════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if chat_id in ADMIN_IDS:
        await show_admin_panel(update.message.reply_text)
        return

    # Перевіряємо реферальний код з /start REF123
    ref_code = context.args[0] if context.args else ""
    if ref_code and ref_code.startswith("R"):
        context.user_data["pending_ref"] = ref_code

    u = get_user(chat_id)
    sub = is_subscribed(chat_id)

    if not u or not u.get("lang"):
        await update.message.reply_text(
            "🌐 Оберіть мову / Wähle Sprache / Choose language:",
            reply_markup=kb_lang(),
        )
        return

    if u and u.get("email") and sub:
        f = get_filters(chat_id)
        mr = int(f["min_rooms"]); xr = int(f["max_rooms"])
        mp = int(f["min_price"]); xp = int(f["max_price"])
        paid  = (u.get("paid_until", "") or "")[:10]
        trial = (u.get("trial_until", "") or "")[:10]
        expires = paid if paid and paid < "2090" else trial
        await update.message.reply_text(
            t(chat_id, "status_text",
              icon="✅", expires=expires, seen=len(get_seen(chat_id)),
              mr=mr, xr=xr, mp=mp, xp=xp),
            reply_markup=kb_main(chat_id),
        )
    elif u and u.get("email") and not sub:
        await update.message.reply_text(
            t(chat_id, "expired_text"), reply_markup=kb_main(chat_id)
        )
    else:
        await update.message.reply_text(
            t(chat_id, "welcome", trial=TRIAL_DAYS),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(chat_id, "has_account"),  callback_data="has_immomio")],
                [InlineKeyboardButton(t(chat_id, "no_account"),   callback_data="no_immomio")],
                [InlineKeyboardButton(t(chat_id, "what_immomio"), callback_data="what_immomio")],
            ]),
        )


async def _trigger_scan_for_user(chat_id: int, bot) -> None:
    await asyncio.sleep(2)
    try:
        await ensure_browser()
        uctx = await get_uctx(chat_id)
        if not uctx:
            return
        sp = uctx["scan_page"]
        await sp.goto(SAGA_URL, timeout=60000, wait_until="domcontentloaded")
        await sp.wait_for_timeout(800)
        await accept_cookies(sp)
        elements = await sp.query_selector_all("a[href*='immo-detail']")
        links = []
        seen_h: set[str] = set()
        for el in elements:
            href = await el.get_attribute("href")
            if not href:
                continue
            link = (
                href
                if href.startswith("http")
                else "https://www.saga.hamburg" + href
            )
            if link in seen_h or not is_apartment(link):
                continue
            seen_h.add(link)
            links.append(link)
        user_seen = get_seen(chat_id)
        for link in links:
            if link in user_seen:
                continue
            add_seen(chat_id, link)
            asyncio.create_task(_apply_task(bot, chat_id, link))
    except Exception as e:
        log(f"Trigger scan error: {e}", "warning")


async def handle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    q = update.callback_query
    await q.answer()
    chat_id = q.from_user.id
    data = q.data

    # ── Language ────────────────────────────────────────
    if data in ("lang_uk", "lang_de", "lang_en"):
        new_lang = {"lang_uk": "uk", "lang_de": "de", "lang_en": "en"}[data]
        set_lang(chat_id, new_lang)
        u = get_user(chat_id)
        if u and u.get("email"):
            await safe_edit(q, t(chat_id, "main_menu"), reply_markup=kb_main(chat_id))
        else:
            await safe_edit(
                q, t(chat_id, "welcome", trial=TRIAL_DAYS),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(chat_id, "has_account"),  callback_data="has_immomio")],
                    [InlineKeyboardButton(t(chat_id, "no_account"),   callback_data="no_immomio")],
                    [InlineKeyboardButton(t(chat_id, "what_immomio"), callback_data="what_immomio")],
                ]),
            )

    elif data == "change_lang":
        await safe_edit(q, t(chat_id, "choose_lang"), reply_markup=kb_lang())

    elif data == "back_main":
        await safe_edit(q, t(chat_id, "main_menu"), reply_markup=kb_main(chat_id))

    # ── Immomio flow ────────────────────────────────────
    elif data in ("has_immomio", "register"):
        await safe_edit(q, t(chat_id, "enter_email"))
        context.user_data["awaiting"] = "email"

    elif data == "no_immomio":
        await safe_edit(
            q, t(chat_id, "no_immomio"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(chat_id, "already_reg"), callback_data="has_immomio")],
                [InlineKeyboardButton(t(chat_id, "btn_support"), callback_data="support")],
            ]),
        )

    elif data == "what_immomio":
        await safe_edit(
            q, t(chat_id, "immomio_explain"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(chat_id, "has_account"), callback_data="has_immomio")],
                [InlineKeyboardButton(t(chat_id, "no_account"),  callback_data="no_immomio")],
            ]),
        )

    # ── Setup filters ───────────────────────────────────
    elif data == "setup_filters":
        f = get_filters(chat_id)
        mr = int(f["min_rooms"]); xr = int(f["max_rooms"])
        mp = int(f["min_price"]); xp = int(f["max_price"])
        await safe_edit(
            q, t(chat_id, "setup_filters"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(chat_id, "setup_rooms_btn", mn=mr, mx=xr), callback_data="setup_rooms")],
                [InlineKeyboardButton(t(chat_id, "setup_price_btn", mn=mp, mx=xp), callback_data="setup_price")],
                [InlineKeyboardButton(t(chat_id, "save_start"), callback_data="setup_done")],
            ]),
        )

    elif data == "setup_rooms":
        await safe_edit(
            q, t(chat_id, "choose_rooms"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1+", callback_data="ssr_1_10"),
                 InlineKeyboardButton("2+", callback_data="ssr_2_10"),
                 InlineKeyboardButton("3+", callback_data="ssr_3_10")],
                [InlineKeyboardButton("1–2", callback_data="ssr_1_2"),
                 InlineKeyboardButton("2–3", callback_data="ssr_2_3"),
                 InlineKeyboardButton("3–4", callback_data="ssr_3_4")],
                [InlineKeyboardButton(t(chat_id, "back"), callback_data="setup_filters")],
            ]),
        )

    elif data == "setup_price":
        ap = t(chat_id, "any_price")
        await safe_edit(
            q, t(chat_id, "choose_price"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("≤€800",  callback_data="ssp_0_800"),
                 InlineKeyboardButton("≤€1000", callback_data="ssp_0_1000")],
                [InlineKeyboardButton("≤€1200", callback_data="ssp_0_1200"),
                 InlineKeyboardButton("≤€1500", callback_data="ssp_0_1500")],
                [InlineKeyboardButton("≤€2000", callback_data="ssp_0_2000"),
                 InlineKeyboardButton(ap,        callback_data="ssp_0_9999")],
                [InlineKeyboardButton(t(chat_id, "back"), callback_data="setup_filters")],
            ]),
        )

    elif data.startswith("ssr_"):
        _, mn, mx = data.split("_")
        f = get_filters(chat_id)
        save_filters(chat_id, float(mn), float(mx), f["min_price"], f["max_price"])
        f2 = get_filters(chat_id)
        mr = int(f2["min_rooms"]); xr = int(f2["max_rooms"])
        mp = int(f2["min_price"]); xp = int(f2["max_price"])
        await safe_edit(
            q, t(chat_id, "setup_filters"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(chat_id, "setup_rooms_btn", mn=mr, mx=xr), callback_data="setup_rooms")],
                [InlineKeyboardButton(t(chat_id, "setup_price_btn", mn=mp, mx=xp), callback_data="setup_price")],
                [InlineKeyboardButton(t(chat_id, "save_start"), callback_data="setup_done")],
            ]),
        )

    elif data.startswith("ssp_"):
        _, mn, mx = data.split("_")
        f = get_filters(chat_id)
        save_filters(chat_id, f["min_rooms"], f["max_rooms"], float(mn), float(mx))
        f2 = get_filters(chat_id)
        mr = int(f2["min_rooms"]); xr = int(f2["max_rooms"])
        mp = int(f2["min_price"]); xp = int(f2["max_price"])
        await safe_edit(
            q, t(chat_id, "setup_filters"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(chat_id, "setup_rooms_btn", mn=mr, mx=xr), callback_data="setup_rooms")],
                [InlineKeyboardButton(t(chat_id, "setup_price_btn", mn=mp, mx=xp), callback_data="setup_price")],
                [InlineKeyboardButton(t(chat_id, "save_start"), callback_data="setup_done")],
            ]),
        )

    elif data == "setup_done":
        upsert_user(chat_id, active=1)
        f = get_filters(chat_id)
        mr = int(f["min_rooms"]); xr = int(f["max_rooms"])
        xp = int(f["max_price"])
        price_str = t(chat_id, "any_price") if xp == 9999 else f"€{xp}/мт"
        await safe_edit(
            q, t(chat_id, "bot_started", mr=mr, xr=xr, price=price_str),
            reply_markup=kb_main(chat_id),
        )
        asyncio.create_task(_trigger_scan_for_user(chat_id, q._bot))

    # ── Status ──────────────────────────────────────────
    elif data == "status":
        u = get_user(chat_id)
        if not u:
            await safe_edit(q, t(chat_id, "main_menu"), reply_markup=kb_main(chat_id))
            return
        f = get_filters(chat_id)
        mr = int(f["min_rooms"]); xr = int(f["max_rooms"])
        mp = int(f["min_price"]); xp = int(f["max_price"])
        paid  = (u.get("paid_until", "") or "")[:10]
        trial = (u.get("trial_until", "") or "")[:10]
        expires = paid if paid and paid < "2090" else trial
        icon = "👑" if chat_id in ADMIN_IDS else "✅"
        await safe_edit(
            q,
            t(chat_id, "status_text", icon=icon, expires=expires,
              seen=len(get_seen(chat_id)), mr=mr, xr=xr, mp=mp, xp=xp),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(chat_id, "setup_rooms_btn", mn=mr, mx=xr), callback_data="filter_rooms"),
                 InlineKeyboardButton(t(chat_id, "setup_price_btn", mn=mp, mx=xp), callback_data="filter_price")],
                [InlineKeyboardButton(t(chat_id, "reset_list"),    callback_data="reset_list"),
                 InlineKeyboardButton(t(chat_id, "reset_filters"), callback_data="reset_filters")],
                [InlineKeyboardButton(t(chat_id, "back"), callback_data="back_main")],
            ]),
        )

    elif data == "filter_rooms":
        await safe_edit(
            q, t(chat_id, "choose_rooms"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1+", callback_data="fr_1_10"),
                 InlineKeyboardButton("2+", callback_data="fr_2_10"),
                 InlineKeyboardButton("3+", callback_data="fr_3_10")],
                [InlineKeyboardButton("1–2", callback_data="fr_1_2"),
                 InlineKeyboardButton("2–3", callback_data="fr_2_3"),
                 InlineKeyboardButton("3–4", callback_data="fr_3_4")],
                [InlineKeyboardButton(t(chat_id, "back"), callback_data="status")],
            ]),
        )

    elif data == "filter_price":
        ap = t(chat_id, "any_price")
        await safe_edit(
            q, t(chat_id, "choose_price"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("≤€800",  callback_data="fp_0_800"),
                 InlineKeyboardButton("≤€1000", callback_data="fp_0_1000")],
                [InlineKeyboardButton("≤€1200", callback_data="fp_0_1200"),
                 InlineKeyboardButton("≤€1500", callback_data="fp_0_1500")],
                [InlineKeyboardButton("≤€2000", callback_data="fp_0_2000"),
                 InlineKeyboardButton(ap,        callback_data="fp_0_9999")],
                [InlineKeyboardButton(t(chat_id, "back"), callback_data="status")],
            ]),
        )

    elif data.startswith("fr_"):
        _, mn, mx = data.split("_")
        f = get_filters(chat_id)
        save_filters(chat_id, float(mn), float(mx), f["min_price"], f["max_price"])
        await safe_edit(q, t(chat_id, "filters_reset"), reply_markup=kb_main(chat_id))

    elif data.startswith("fp_"):
        _, mn, mx = data.split("_")
        f = get_filters(chat_id)
        save_filters(chat_id, f["min_rooms"], f["max_rooms"], float(mn), float(mx))
        await safe_edit(q, t(chat_id, "filters_reset"), reply_markup=kb_main(chat_id))

    elif data == "reset_filters":
        save_filters(chat_id, 1, 10, 0, 9999)
        await safe_edit(q, t(chat_id, "filters_reset"), reply_markup=kb_main(chat_id))

    elif data == "reset_list":
        with get_db() as db:
            db.execute("DELETE FROM seen WHERE chat_id=?", (chat_id,))
            db.commit()
        await safe_edit(q, t(chat_id, "list_reset"), reply_markup=kb_main(chat_id))

    # ── Payment ─────────────────────────────────────────
    elif data == "pay":
        # Показуємо вибір методу оплати
        rows = []
        if STARS_ENABLED:
            rows.append([InlineKeyboardButton(t(chat_id, "pay_stars"), callback_data="pay_method_stars")])
        rows.append([InlineKeyboardButton(t(chat_id, "pay_ton"), callback_data="pay_method_ton")])
        rows.append([InlineKeyboardButton(t(chat_id, "back"), callback_data="back_main")])
        await safe_edit(q, t(chat_id, "choose_payment"), reply_markup=InlineKeyboardMarkup(rows))

    elif data == "pay_method_stars":
        # Вибір тарифу для Stars
        s1, s2, s3 = 1370, 2090, 2810
        await safe_edit(
            q, t(chat_id, "stars_title", s1=s1, s2=s2, s3=s3),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"⭐ 1 міс — {s1} Stars", callback_data="stars_19_30_1370")],
                [InlineKeyboardButton(f"⭐ 2 міс — {s2} Stars", callback_data="stars_29_60_2090")],
                [InlineKeyboardButton(f"⭐ 3 міс — {s3} Stars", callback_data="stars_39_120_2810")],
                [InlineKeyboardButton(t(chat_id, "how_to_stars"), callback_data="how_to_stars")],
                [InlineKeyboardButton(t(chat_id, "back"), callback_data="pay")],
            ]),
        )

    elif data.startswith("stars_"):
        # stars_EUR_DAYS_STARS напр. stars_19_30_1500
        parts = data.split("_")
        eur = int(parts[1]); days = int(parts[2]); stars = int(parts[3])
        months = days // 30
        plan_title = t(chat_id, "stars_desc", months=months)
        try:
            await context.bot.send_invoice(
                chat_id=chat_id,
                title=plan_title,
                description=plan_title,
                payload=f"stars_{chat_id}_{days}",
                provider_token="",  # порожній для Telegram Stars
                currency="XTR",  # XTR = Telegram Stars
                prices=[LabeledPrice(label=plan_title, amount=stars)],
            )
            await safe_edit(q, t(chat_id, "stars_pending"))
        except Exception as e:
            log(f"Stars invoice error: {e}", "warning")
            await safe_edit(q, "❌ Помилка Stars. Спробуй TON оплату.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(chat_id, "back"), callback_data="pay")]]))

    elif data == "how_to_stars":
        await safe_edit(
            q, t(chat_id, "how_to_stars"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(chat_id, "back"), callback_data="pay_method_stars")],
            ]),
        )

    elif data == "pay_method_ton":
        # Стара TON оплата
        await safe_edit(
            q, t(chat_id, "pay_title"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(chat_id, "plan_1_btn"), callback_data="plan_19_30")],
                [InlineKeyboardButton(t(chat_id, "plan_2_btn"), callback_data="plan_29_60")],
                [InlineKeyboardButton(t(chat_id, "plan_3_btn"), callback_data="plan_39_120")],
                [InlineKeyboardButton(t(chat_id, "check_payment"), callback_data="check_payment")],
                [InlineKeyboardButton(t(chat_id, "wallet_guide"),  callback_data="how_to_pay")],
                [InlineKeyboardButton(t(chat_id, "back"),          callback_data="pay")],
            ]),
        )

    elif data.startswith("plan_"):
        parts = data.split("_")
        eur = int(parts[1]); days = int(parts[2])
        ton, rate = await get_ton_amount(eur)
        comment = make_comment(chat_id)
        plan_names = {
            19: t(chat_id, "plan_1_btn"),
            29: t(chat_id, "plan_2_btn"),
            39: t(chat_id, "plan_3_btn"),
        }
        with get_db() as db:
            db.execute(
                "INSERT INTO payments (chat_id,ton_amount,eur_amount,days,comment,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (chat_id, ton, eur, days, comment, datetime.now().isoformat()),
            )
            db.commit()

        _lang = get_lang(chat_id)
        copy_labels = {
            "uk": ("📋 Копіювати адресу", "📋 Копіювати коментар"),
            "de": ("📋 Adresse kopieren",  "📋 Kommentar kopieren"),
            "en": ("📋 Copy address",      "📋 Copy comment"),
        }
        lbl_addr, lbl_comm = copy_labels.get(_lang, copy_labels["uk"])

        copy_rows = []
        try:
            from telegram import CopyTextButton as CTB  # PTB >= 21.3
            copy_rows = [
                [InlineKeyboardButton(lbl_addr, copy_text=CTB(text=TON_WALLET))],
                [InlineKeyboardButton(lbl_comm, copy_text=CTB(text=comment))],
            ]
        except ImportError:
            pass

        await safe_edit(
            q,
            t(chat_id, "pay_details",
              plan=plan_names.get(eur, str(eur) + "€"),
              ton=ton, rate=rate, wallet=TON_WALLET, comment=comment),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                copy_rows + [
                    [InlineKeyboardButton(t(chat_id, "check_payment"), callback_data="check_payment")],
                    [InlineKeyboardButton(t(chat_id, "wallet_guide"),  callback_data="how_to_pay")],
                    [InlineKeyboardButton(t(chat_id, "back"),          callback_data="pay")],
                ]
            ),
        )

    elif data == "check_payment":
        with get_db() as db:
            p = db.execute(
                "SELECT * FROM payments WHERE chat_id=? AND status='pending'"
                " ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        if not p:
            await safe_edit(
                q, t(chat_id, "no_payment"),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(t(chat_id, "back"), callback_data="pay")]]
                ),
            )
            return
        p = dict(p)
        if await check_ton_payment(chat_id, p["ton_amount"], p["comment"]):
            paid_until = (datetime.now() + timedelta(days=p["days"])).isoformat()
            upsert_user(chat_id, paid_until=paid_until, active=1)
            with get_db() as db:
                db.execute("UPDATE payments SET status='paid' WHERE id=?", (p["id"],))
                db.commit()
            await safe_edit(
                q, t(chat_id, "pay_confirmed", date=paid_until[:10]),
                reply_markup=kb_main(chat_id),
            )
        else:
            await safe_edit(
                q,
                t(chat_id, "pay_pending", ton=p["ton_amount"], comment=p["comment"]),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(chat_id, "check_payment"), callback_data="check_payment")],
                    [InlineKeyboardButton(t(chat_id, "back"),          callback_data="pay")],
                ]),
            )

    elif data == "how_to_pay":
        await safe_edit(
            q, t(chat_id, "wallet_guide_text"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(chat_id, "btn_pay"), callback_data="pay")],
                [InlineKeyboardButton(t(chat_id, "back"),    callback_data="back_main")],
            ]),
        )

    # ── Guide ────────────────────────────────────────────
    elif data == "send_guide":
        try:
            lang = get_lang(chat_id)
            _GUIDE_URLS = {
                "uk": "https://patrickkassparov.github.io/saga_bot/guide_uk.html",
                "de": "https://patrickkassparov.github.io/saga_bot/guide_de.html",
                "en": "https://patrickkassparov.github.io/saga_bot/guide_en.html",
            }
            guide_url = _GUIDE_URLS.get(lang, _GUIDE_URLS["uk"])
            await safe_edit(
                q,
                t(chat_id, "guide_caption"),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        t(chat_id, "btn_guide"),
                        web_app=WebAppInfo(url=guide_url),
                    )],
                    [InlineKeyboardButton(t(chat_id, "back"), callback_data="back_main")],
                ]),
            )
        except Exception as e:
            log(f"Guide error: {e}", "warning")
            await context.bot.send_message(
                chat_id, t(chat_id, "guide_text", username=SUPPORT_USERNAME)
            )

    # ── Support ──────────────────────────────────────────
    elif data == "support":
        await safe_edit(
            q, t(chat_id, "support_text", username=SUPPORT_USERNAME),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(t(chat_id, "back"), callback_data="back_main")]]
            ),
        )

    # ── Statistics ──────────────────────────────────────
    elif data == "my_stats":
        u = get_user(chat_id)
        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) as total, SUM(success) as succ FROM apply_stats WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
        total = row["total"] or 0
        success = int(row["succ"] or 0)
        fail = total - success
        rate = round(success / total * 100) if total else 0
        since = (u.get("created_at") or "")[:10] if u else "—"
        await safe_edit(
            q,
            t(chat_id, "stats_text", total=total, success=success,
              fail=fail, rate=rate, since=since),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(chat_id, "back"), callback_data="back_main")],
            ]),
        )

    # ── Referral ─────────────────────────────────────────
    elif data == "referral":
        code = get_ref_code(chat_id)
        stats = get_referral_stats(chat_id)
        try:
            bot_info = await context.bot.get_me()
            bot_username = bot_info.username
        except Exception:
            bot_username = "your_bot"
        await safe_edit(
            q,
            t(chat_id, "ref_text",
              bot=bot_username, code=code,
              count=stats["count"], bonus=stats["bonus_days"],
              ref_days=REF_BONUS_DAYS),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    t(chat_id, "ref_share"),
                    url=f"https://t.me/share/url?url=t.me/{bot_username}%3Fstart%3D{code}"
                       f"&text=🏠+SAGA+Apartment+Bot+-+автоматичні+заявки+на+квартири+Hamburg!",
                )],
                [InlineKeyboardButton(t(chat_id, "back"), callback_data="back_main")],
            ]),
        )

    # ── Blacklist ─────────────────────────────────────────
    elif data.startswith("blacklist_"):
        # callback_data = "blacklist_" + link[:60]
        # Знаходимо повне посилання в БД seen
        short = data[len("blacklist_"):]
        with get_db() as db:
            row = db.execute(
                "SELECT link FROM seen WHERE chat_id=? AND link LIKE ?",
                (chat_id, short + "%"),
            ).fetchone()
        if row:
            full_link = row["link"]
            add_blacklist(chat_id, full_link)
            await safe_edit(q, t(chat_id, "blacklisted"))
        else:
            await q.answer("🚫 Додано до чорного списку")

    # ── Blacklist ─────────────────────────────────────────
    elif data.startswith("bl_"):
        import urllib.parse
        link = urllib.parse.unquote(data[3:])
        if not link.startswith("http"):
            link = "https://www.saga.hamburg" + link
        add_to_blacklist(chat_id, link)
        await safe_edit(q, t(chat_id, "blacklist_added"))

    # ── Website ──────────────────────────────────────────
    elif data == "website":
        await safe_edit(q, t(chat_id, "website_text"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 patrickkassparov.github.io/saga_bot", url="https://patrickkassparov.github.io/saga_bot/")],
                [InlineKeyboardButton(t(chat_id, "back"), callback_data="back_main")],
            ]))

    # ── Pause / Resume ───────────────────────────────────
    elif data == "pause_bot":
        upsert_user(chat_id, paused=1)
        await safe_edit(
            q, t(chat_id, "paused_text"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(chat_id, "btn_resume"), callback_data="resume_bot")],
                [InlineKeyboardButton(t(chat_id, "back"),       callback_data="back_main")],
            ]),
        )

    elif data == "resume_bot":
        upsert_user(chat_id, paused=0)
        await safe_edit(
            q, t(chat_id, "resumed_text"),
            reply_markup=kb_main(chat_id),
        )

    # ── Admin status ─────────────────────────────────────
    elif data == "admin_status" and chat_id in ADMIN_IDS:
        status_text = await admin_system_status(context.bot)
        await safe_edit(
            q, status_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Оновити", callback_data="admin_status")],
                [InlineKeyboardButton("◀️ Назад",   callback_data="admin")],
            ]),
        )

    # ── Stop ─────────────────────────────────────────────
    elif data == "stop":
        upsert_user(chat_id, active=0)
        if chat_id in user_contexts:
            try:
                await user_contexts[chat_id]["context"].close()
            except Exception:
                pass
            del user_contexts[chat_id]
        await safe_edit(q, t(chat_id, "bot_stopped"))

    # ── Admin ────────────────────────────────────────────
    elif data == "admin" and chat_id in ADMIN_IDS:
        await show_admin_panel(
            lambda text, reply_markup: safe_edit(q, text, reply_markup=reply_markup)
        )

    elif data == "admin_users" and chat_id in ADMIN_IDS:
        with get_db() as db:
            users = db.execute(
                "SELECT * FROM users ORDER BY created_at DESC LIMIT 30"
            ).fetchall()
        lines = ["👥 Всі юзери:\n"]
        for u in users:
            s = "✅" if is_subscribed(u["chat_id"]) else "❌"
            a = "🟢" if u["active"] else "🔴"
            paid = (u["paid_until"] or "")[:10] if u["paid_until"] else "—"
            lines.append(
                f"{s}{a} {u['chat_id']}\n"
                f"   📧 {u['email'] or '—'}\n"
                f"   📅 {paid}"
            )
        await safe_edit(
            q, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Назад", callback_data="admin")]]
            ),
        )

    elif data == "admin_active" and chat_id in ADMIN_IDS:
        with get_db() as db:
            users = db.execute("SELECT * FROM users WHERE active=1").fetchall()
        lines = ["✅ Активні юзери:\n"]
        for u in users:
            paid = (u["paid_until"] or "")[:10] if u["paid_until"] else "trial"
            lines.append(f"🟢 {u['chat_id']} — {u['email'] or '—'} | до {paid}")
        await safe_edit(
            q, "\n".join(lines) if len(lines) > 1 else "Немає активних юзерів",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Назад", callback_data="admin")]]
            ),
        )

    elif data == "admin_payments" and chat_id in ADMIN_IDS:
        with get_db() as db:
            payments = db.execute(
                "SELECT * FROM payments WHERE status='paid' ORDER BY id DESC LIMIT 20"
            ).fetchall()
        lines = ["💰 Оплати:\n"]
        for p in payments:
            lines.append(
                f"💎 {p['chat_id']} — {p['eur_amount']}€ / {p['days']}д "
                f"| {(p['created_at'] or '')[:10]}"
            )
        await safe_edit(
            q, "\n".join(lines) if len(lines) > 1 else "Немає оплат",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Назад", callback_data="admin")]]
            ),
        )

    elif data == "admin_activate_prompt" and chat_id in ADMIN_IDS:
        context.user_data["awaiting"] = "admin_activate"
        await safe_edit(
            q, "✅ Введи chat_id і кількість днів через пробіл:\nПриклад: 123456789 30",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Назад", callback_data="admin")]]
            ),
        )

    elif data == "admin_deactivate_prompt" and chat_id in ADMIN_IDS:
        context.user_data["awaiting"] = "admin_deactivate"
        await safe_edit(
            q, "❌ Введи chat_id юзера для деактивації:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Назад", callback_data="admin")]]
            ),
        )

    elif data == "admin_broadcast" and chat_id in ADMIN_IDS:
        context.user_data["awaiting"] = "broadcast"
        await safe_edit(
            q, "📢 Введи повідомлення для розсилки всім юзерам:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Назад", callback_data="admin")]]
            ),
        )


# ═══════════════════════════════════════════════════════════════════════
#  MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════════════════

async def test_immomio_login(email: str, password: str) -> bool:
    """Перевіряє облікові дані Immomio (3 спроби). Бере браузер з пулу."""
    for attempt in range(3):
        try:
            async with BrowserLease() as b:
                ctx = await b.new_context(user_agent=_USER_AGENT)
                page = await ctx.new_page()
                try:
                    await page.goto(
                        "https://tenant.immomio.com/de/auth/login",
                        timeout=30000,
                        wait_until="domcontentloaded",
                    )
                    ok = await immomio_login(page, email, password)
                    return ok
                finally:
                    try:
                        await page.close()
                        await ctx.close()
                    except Exception:
                        pass
        except Exception as e:
            log(f"Login attempt {attempt + 1} failed: {e}", "warning")
            await asyncio.sleep(3)
    return False


async def handle_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat_id = update.effective_chat.id
    awaiting = context.user_data.get("awaiting")

    if awaiting == "email":
        context.user_data["reg_email"] = update.message.text.strip()
        context.user_data["awaiting"] = "password"
        await update.message.reply_text(t(chat_id, "enter_password"))

    elif awaiting == "password":
        email    = context.user_data.get("reg_email", "")
        password = update.message.text.strip()
        context.user_data["awaiting"] = None
        msg = await update.message.reply_text(t(chat_id, "checking"))
        ok = await test_immomio_login(email, password)
        if not ok:
            await msg.edit_text(
                t(chat_id, "wrong_creds"),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(t(chat_id, "try_again"), callback_data="has_immomio")]]
                ),
            )
            return
        trial_until = (datetime.now() + timedelta(days=TRIAL_DAYS)).isoformat()
        # Зберігаємо зашифрований пароль
        upsert_user(
            chat_id,
            username=update.effective_user.username or "",
            email=email,
            password=encrypt_password(password),
            active=0,
            trial_until=trial_until,
            ref_code=make_ref_code(chat_id),
        )
        # Застосовуємо реферальний код якщо є
        pending_ref = context.user_data.pop("pending_ref", "")
        if pending_ref and process_referral(chat_id, pending_ref):
            referrer_id = int(pending_ref[1:])
            try:
                await context.bot.send_message(
                    referrer_id,
                    t(referrer_id, "ref_bonus_msg", days=REF_BONUS_DAYS),
                )
            except Exception:
                pass
            await update.message.reply_text(
                t(chat_id, "ref_welcome", days=REF_BONUS_DAYS)
            )
        # Очищаємо кешований контекст браузера
        if chat_id in user_contexts:
            try:
                await user_contexts[chat_id]["context"].close()
            except Exception:
                pass
            del user_contexts[chat_id]
        f = get_filters(chat_id)
        mr = int(f["min_rooms"]); xr = int(f["max_rooms"])
        mp = int(f["min_price"]); xp = int(f["max_price"])
        await msg.edit_text(
            t(chat_id, "reg_success", trial=TRIAL_DAYS, date=trial_until[:10]),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(chat_id, "setup_rooms_btn", mn=mr, mx=xr), callback_data="setup_rooms")],
                [InlineKeyboardButton(t(chat_id, "setup_price_btn", mn=mp, mx=xp), callback_data="setup_price")],
                [InlineKeyboardButton(t(chat_id, "save_start"), callback_data="setup_done")],
            ]),
        )

    elif awaiting == "admin_activate" and chat_id in ADMIN_IDS:
        context.user_data["awaiting"] = None
        parts = update.message.text.strip().split()
        if len(parts) == 2:
            try:
                target = int(parts[0]); days = int(parts[1])
                paid_until = (datetime.now() + timedelta(days=days)).isoformat()
                upsert_user(target, paid_until=paid_until, active=1)
                await update.message.reply_text(
                    f"✅ Активовано {target} на {days} днів"
                )
                try:
                    await context.bot.send_message(
                        target, t(target, "pay_received", date=paid_until[:10])
                    )
                except Exception:
                    pass
            except (ValueError, TypeError):
                await update.message.reply_text("❌ Помилка. Формат: 123456789 30")
        else:
            await update.message.reply_text("❌ Формат: chat_id кількість_днів")
        await show_admin_panel(update.message.reply_text)

    elif awaiting == "admin_deactivate" and chat_id in ADMIN_IDS:
        context.user_data["awaiting"] = None
        try:
            target = int(update.message.text.strip())
            upsert_user(target, active=0, paid_until="")
            await update.message.reply_text(f"⏹ Деактивовано {target}")
        except (ValueError, TypeError):
            await update.message.reply_text("❌ Невірний chat_id")
        await show_admin_panel(update.message.reply_text)

    elif awaiting == "broadcast" and chat_id in ADMIN_IDS:
        text = update.message.text.strip()
        context.user_data["awaiting"] = None
        users = get_all_active()
        sent = 0
        for u in users:
            try:
                await context.bot.send_message(u["chat_id"], "📢 " + text)
                sent += 1
            except Exception:
                pass
        await update.message.reply_text(f"📢 Надіслано {sent}/{len(users)}")
        await show_admin_panel(update.message.reply_text)


# ═══════════════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════════

async def cmd_activate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_chat.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /activate <chat_id> <days>")
        return
    target = int(args[0]); days = int(args[1])
    paid_until = (datetime.now() + timedelta(days=days)).isoformat()
    upsert_user(target, paid_until=paid_until, active=1)
    await update.message.reply_text(
        t(update.effective_chat.id, "activated", days=days)
    )
    try:
        await context.bot.send_message(
            target, t(target, "pay_received", date=paid_until[:10])
        )
    except Exception:
        pass


async def cmd_deactivate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_chat.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /deactivate <chat_id>")
        return
    upsert_user(int(context.args[0]), active=0, paid_until="")
    await update.message.reply_text(
        t(update.effective_chat.id, "deactivated")
    )


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

async def post_init(app) -> None:
    init_db()
    _init_db_pool()          # пул з'єднань SQLite
    await init_browser()     # пул браузерів Playwright
    upsert_user(
        ADMIN_CHAT_ID,
        username="admin",
        active=1,
        paid_until="2099-01-01T00:00:00",
        lang="uk",
    )
    await app.bot.set_my_commands([
        BotCommand("start",      "🏠 Головне меню"),
        BotCommand("activate",   "✅ Активувати (адмін)"),
        BotCommand("deactivate", "❌ Деактивувати (адмін)"),
    ])
    log(
        f"✅ Bot ready | "
        f"Browser pool: {BROWSER_POOL_SIZE} | "
        f"Max concurrent apply: {_APPLY_GLOBAL_SEM._value}"
    )




async def check_apply_statuses(ctx) -> None:
    """Перевіряє статуси заявок кожні 30 хв і повідомляє юзера про зміни."""
    pending = get_pending_apply_statuses()
    if not pending:
        return
    by_user: dict = {}
    for row in pending:
        by_user.setdefault(row["chat_id"], []).append(row)
    for chat_id, applies in by_user.items():
        u = get_user(chat_id)
        if not u or not u.get("email"):
            continue
        password = decrypt_password(u["password"])
        try:
            async with BrowserLease() as b:
                c2 = await b.new_context(user_agent=_USER_AGENT)
                lp = await c2.new_page()
                try:
                    await lp.goto("https://tenant.immomio.com/de/auth/login",
                                  timeout=20000, wait_until="domcontentloaded")
                    if not await immomio_login(lp, u["email"], password):
                        continue
                    await lp.goto(
                        "https://tenant.immomio.com/de/properties/applications",
                        timeout=20000, wait_until="domcontentloaded")
                    await lp.wait_for_timeout(2000)
                    body = await lp.evaluate("() => document.body.innerText")
                    for row in applies:
                        new_status = None
                        if "Besichtigung" in body or "Einladung" in body:
                            new_status = "invited"
                        elif "Abgelehnt" in body or "abgelehnt" in body:
                            new_status = "rejected"
                        elif "Angesehen" in body or "gelesen" in body:
                            new_status = "viewed"
                        if new_status and new_status != row["status"]:
                            update_apply_status(row["id"], new_status)
                            msg_key = {"invited": "apply_invited",
                                       "rejected": "apply_rejected",
                                       "viewed": "apply_viewed"}.get(new_status)
                            if msg_key:
                                try:
                                    await ctx.bot.send_message(
                                        chat_id, t(chat_id, msg_key, link=row["link"]))
                                except Exception:
                                    pass
                finally:
                    try:
                        await lp.close()
                        await c2.close()
                    except Exception:
                        pass
        except Exception as e:
            log(f"Status check error {chat_id}: {e}", "warning")


async def notify_admin_error(bot, error: str) -> None:
    """Надсилає сповіщення адміну про критичну помилку."""
    try:
        time_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"🔴 Помилка бота!\n\n{error}\n\nЧас: {time_str}",
        )
    except Exception:
        pass


async def admin_system_status(bot) -> str:
    """Формує рядок статусу системи для адмін-панелі."""
    try:
        with get_db() as db:
            active = db.execute(
                "SELECT COUNT(*) as c FROM users WHERE active=1"
            ).fetchone()["c"]
        proxy_str = f"✅ {len(PROXY_LIST)} шт." if PROXY_LIST else "⚠️ Без проксі"
        # Перевіряємо браузерний пул
        try:
            browsers_alive = sum(
                1 for _ in range(_browser_pool.qsize())
            )
        except Exception:
            browsers_alive = 0
        uptime = get_uptime()
        return (
            f"🟢 Система працює нормально\n\n"
            f"⏱ Аптайм: {uptime}\n"
            f"👥 Активних юзерів: {active}\n"
            f"🌐 Проксі: {proxy_str}\n"
            f"🖥 Браузерів у пулі: {BROWSER_POOL_SIZE}\n\n"
            f"✅ Всі сервіси OK"
        )
    except Exception as e:
        return f"🔴 Помилка отримання статусу: {e}"


async def daily_report(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Щоденний звіт адміну о 9:00 UTC."""
    if not DAILY_REPORT:
        return
    try:
        now = datetime.now()
        yesterday = (now - timedelta(days=1)).isoformat()
        date_str = now.strftime("%d.%m.%Y")

        with get_db() as db:
            total_users  = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
            active_users = db.execute("SELECT COUNT(*) as c FROM users WHERE active=1").fetchone()["c"]
            new_users    = db.execute(
                "SELECT COUNT(*) as c FROM users WHERE created_at > ?", (yesterday,)
            ).fetchone()["c"]
            new_payments = db.execute(
                "SELECT COUNT(*) as c, COALESCE(SUM(eur_amount),0) as total"
                " FROM payments WHERE status='paid' AND created_at > ?", (yesterday,)
            ).fetchone()
            applies = db.execute(
                "SELECT COUNT(*) as c, COALESCE(SUM(success),0) as succ"
                " FROM apply_stats WHERE created_at > ?", (yesterday,)
            ).fetchone()

        pay_count = new_payments["c"] or 0
        pay_total = new_payments["total"] or 0
        app_total = applies["c"] or 0
        app_succ  = int(applies["succ"] or 0)
        proxy_status = f"✅ {len(PROXY_LIST)} шт." if PROXY_LIST else "⚠️ Без проксі (ризик блокування!)"

        lines = [
            f"📊 Щоденний звіт — {date_str}",
            "",
            f"👥 Всього юзерів: {total_users}",
            f"✅ Активних: {active_users}",
            f"🆕 Нових сьогодні: {new_users}",
            "",
            f"💰 Оплат: {pay_count} (€{pay_total:.0f})",
            "",
            f"🏠 Заявок подано: {app_total}",
            f"✅ Успішних: {app_succ}",
            f"❌ Невдалих: {app_total - app_succ}",
            "",
            f"🌐 Проксі: {proxy_status}",
        ]
        await ctx.bot.send_message(ADMIN_CHAT_ID, "\n".join(lines))
    except Exception as e:
        log(f"Daily report error: {e}", "warning")


async def handle_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram викликає це перед списанням Stars — треба підтвердити."""
    query = update.pre_checkout_query
    # Завжди підтверджуємо — перевірку робимо після оплати
    await query.answer(ok=True)


async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Викликається після успішної оплати Stars."""
    chat_id = update.effective_chat.id
    payment = update.message.successful_payment
    payload = payment.invoice_payload  # stars_CHATID_DAYS

    try:
        parts = payload.split("_")
        days = int(parts[2])
        paid_until = (datetime.now() + timedelta(days=days)).isoformat()
        upsert_user(chat_id, paid_until=paid_until, active=1)

        # Зберігаємо в БД
        with get_db() as db:
            db.execute(
                "INSERT INTO payments (chat_id,ton_amount,eur_amount,days,comment,status,created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (chat_id, 0, 0, days, f"stars_{payment.telegram_payment_charge_id}",
                 "paid", datetime.now().isoformat()),
            )
            db.commit()

        await update.message.reply_text(
            t(chat_id, "stars_ok", date=paid_until[:10]),
            reply_markup=kb_main(chat_id),
        )
        # Повідомляємо адміна
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"⭐ Stars оплата!\n👤 {chat_id}\n📦 {days} днів\n"
                f"⭐ {payment.total_amount} Stars",
            )
        except Exception:
            pass
    except Exception as e:
        log(f"Stars payment processing error: {e}", "warning")
        await update.message.reply_text("❌ Помилка обробки оплати. Звернись до підтримки.")


def build_app():
    from telegram.request import HTTPXRequest
    req = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = (
        ApplicationBuilder()
        .token(ADMIN_TOKEN)
        .request(req)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("activate",   cmd_activate))
    app.add_handler(CommandHandler("deactivate", cmd_deactivate))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(PreCheckoutQueryHandler(handle_pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment))
    app.job_queue.run_repeating(scan_and_apply_all,          interval=SCAN_INTERVAL, first=5)
    app.job_queue.run_repeating(check_invitations_all,       interval=INV_INTERVAL,  first=8)
    app.job_queue.run_repeating(ton_payment_checker,         interval=60,            first=30)
    app.job_queue.run_repeating(subscription_expiry_checker, interval=3600,          first=60)
    app.job_queue.run_repeating(check_application_statuses,  interval=1800,          first=300)
    app.job_queue.run_daily(daily_report, time=datetime.strptime("09:00", "%H:%M").time())
    app.job_queue.run_repeating(check_apply_statuses, interval=1800, first=300)
    return app


def main() -> None:
    while True:
        try:
            log("BOT RUNNING...")
            build_app().run_polling(drop_pending_updates=True)
        except KeyboardInterrupt:
            log("Bot stopped by user")
            break
        except Exception as e:
            log(f"BOT CRASHED: {e} — restarting in 15s...", "error")
            try:
                loop = asyncio.get_event_loop()
                if not loop.is_closed():
                    loop.close()
            except Exception:
                pass
            asyncio.set_event_loop(asyncio.new_event_loop())
            time.sleep(15)


if __name__ == "__main__":
    main()
