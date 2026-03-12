import asyncio
import os
import time
import requests
import subprocess
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- НАЛАШТУВАННЯ ---
TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"
IMMOMIO_EMAIL = "maksymsheveliuk@gmail.com"
IMMOMIO_PASSWORD = "Maksoplot2007"

seen = set()
if os.path.exists("seen.txt"):
    with open("seen.txt") as f:
        seen = {line.strip() for line in f if line.strip()}

BROWSER_ARGS = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]

# --- ФУНКЦІЯ АВТО-ВІДГУКУ ---
async def perform_auto_apply(link):
    print(f"🚀 ЗАПУСК БРАУЗЕРА для відгуку: {link}")
    browser = None
    try:
        async with async_playwright() as p:
            # Спроба запустити браузер
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            context = await browser.new_context()
            page = await context.new_page()
            
            # Логін на Immomio
            await page.goto("https://app.immomio.com", timeout=60000)
            await page.fill('input[type="email"]', IMMOMIO_EMAIL)
            await page.fill('input[type="password"]', IMMOMIO_PASSWORD)
            await page.click('button[type="submit"]')
            
            # Очікування завантаження кабінету (перевірка чи пройшов логін)
            await page.wait_for_timeout(7000)

            # Перехід до квартири
            await page.goto(link, timeout=60000)
            await page.wait_for_load_state("networkidle")

            # Клік по кнопці (Тут текст кнопки для Immomio)
            apply_btn = page.get_by_text("Interesse bekunden", exact=False)
            
            if await apply_btn.is_visible():
                await apply_btn.click()
                await page.wait_for_timeout(3000)
                print(f"✅ ЗАЯВКУ НАДІСЛАНО!")
                return True
            else:
                print("⚠️ Кнопку не знайдено (можливо, інший сайт або вже подано)")
                return False
                
    except Exception as e:
        print(f"❌ Помилка браузера: {e}")
        return False
    finally:
        if browser:
            await browser.close()

# --- СКАНЕР ---
async def fast_scan(context: ContextTypes.DEFAULT_TYPE):
    print(f"🔎 [{time.strftime('%H:%M:%S')}] Швидка перевірка...")
    
    # --- ТЕСТОВИЙ БЛОК ---
    # Примітка: Посилання SAGA не спрацює через логіку логіну Immomio, 
    # але ми перевіримо чи запускається сам браузер!
    test_link = "https://www.saga.hamburg/immobiliensuche/immo-detail/6424/schone-2-zimmer-wohnung-in-allermohe" 
    if test_link not in seen:
        seen.add(test_link)
        await context.bot.send_message(chat_id=CHAT_ID, text=f"🧪 Тест браузера на: {test_link}")
        status = await perform_auto_apply(test_link)
        msg = "✅ БРАУЗЕР ПРАЦЮЄ!" if status else "❌ ТЕСТ ПРОВАЛЕНО (див. логи)"
        await context.bot.send_message(chat_id=CHAT_ID, text=msg)

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        # Шлях до пошуку Hamburg
        r = requests.get("https://www.immomio.com", headers=headers, timeout=10)
        
        if r.status_code == 200:
            parts = r.text.split("/expose/")[1:]
            for part in parts:
                expose_id = part.split('"')[0]
                link = f"https://www.immomio.com{expose_id}"

                if link not in seen:
                    seen.add(link)
                    with open("seen.txt", "a") as f: f.write(link + "\n")
                    await context.bot.send_message(chat_id=CHAT_ID, text=f"🏠 Нова квартира!\n{link}")
                    status = await perform_auto_apply(link)
                    res = "✅ Подано!" if status else "❌ Помилка"
                    await context.bot.send_message(chat_id=CHAT_ID, text=res)
    except Exception as e:
        print(f"Помилка: {e}")

# --- MAIN ---
def main():
    # ФОРСОВАНЕ ВСТАНОВЛЕННЯ ДЛЯ RAILWAY
    try:
        print("📦 Встановлення Chromium...")
        subprocess.run(["playwright", "install", "chromium"], check=True)
        subprocess.run(["playwright", "install-deps"], check=True)
    except Exception as e:
        print(f"Попередження при інсталяції: {e}")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.job_queue.run_repeating(fast_scan, interval=60, first=5)
    
    print("🚀 Бот працює...")
    app.run_polling()

async def start(u, c):
    await u.message.reply_text("🤖 Бот запущено!")

if __name__ == "__main__":
    main()
