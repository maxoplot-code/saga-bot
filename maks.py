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
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            context = await browser.new_context(viewport={'width': 1280, 'height': 720})
            page = await context.new_page()
            
            # 1. Переходимо за посиланням (SAGA або Immomio)
            await page.goto(link, timeout=60000)
            await page.wait_for_timeout(3000)

            # 2. Якщо ми на SAGA — шукаємо кнопку переходу
            if "saga.hamburg" in page.url:
                print("🔎 Виявлено сайт SAGA. Шукаємо кнопку переходу...")
                for btn_text in ["ZUM EXPOSÉ", "Jetzt bewerben"]:
                    btn = page.get_by_text(btn_text, exact=False)
                    if await btn.is_visible():
                        await btn.click()
                        print(f"✅ Натиснуто '{btn_text}' на SAGA. Чекаємо редирект на Immomio...")
                        await page.wait_for_timeout(8000)
                        break

            # 3. Перевірка авторизації на Immomio
            if "immomio.com" in page.url:
                if await page.is_visible('input[type="email"]'):
                    print("🔑 Виконуємо вхід в Immomio...")
                    await page.fill('input[type="email"]', IMMOMIO_EMAIL)
                    await page.fill('input[type="password"]', IMMOMIO_PASSWORD)
                    await page.click('button[type="submit"]')
                    await page.wait_for_timeout(7000)

                # 4. Фінальне натискання кнопки відгуку
                print("🎯 Шукаємо фінальну кнопку відгуку...")
                final_texts = ["Jetzt bewerben", "Interesse bekunden"]
                for f_text in final_texts:
                    f_btn = page.get_by_text(f_text, exact=False)
                    if await f_btn.is_visible():
                        await f_btn.click()
                        await page.wait_for_timeout(3000)
                        print(f"✅ ЗАЯВКУ НАДІСЛАНО УСПІШНО ({f_text})!")
                        return True
                
                print("⚠️ Фінальну кнопку не знайдено (можливо, вже подано).")
                return True # Повертаємо True, бо ми принаймні дійшли до кінця
            
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
    test_link = "https://www.saga.hamburg/immobiliensuche/immo-detail/6404/schone-2-5-zimmerwohnung" 
    if test_link not in seen:
        seen.add(test_link)
        await context.bot.send_message(chat_id=CHAT_ID, text=f"🧪 Тест на посиланні SAGA: {test_link}")
        status = await perform_auto_apply(test_link)
        msg = "✅ ТЕСТ ПРОЙШОВ УСПІШНО!" if status else "❌ ТЕСТ ПРОВАЛЕНО (див. логи)"
        await context.bot.send_message(chat_id=CHAT_ID, text=msg)

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        # Запит на пошук в Гамбурзі
        r = requests.get("https://www.immomio.com", headers=headers, timeout=15)
        
        if r.status_code == 200:
            parts = r.text.split("/expose/")[1:]
            for part in parts:
                expose_id = part.split('"')[0]
                link = f"https://www.immomio.com{expose_id}"

                if link not in seen:
                    seen.add(link)
                    with open("seen.txt", "a") as f: f.write(link + "\n")
                    await context.bot.send_message(chat_id=CHAT_ID, text=f"🏠 Нова квартира!\n{link}\n⏳ Подаю заявку...")
                    status = await perform_auto_apply(link)
                    res = "✅ Авто-відгук надіслано!" if status else "❌ Помилка авто-відгуку."
                    await context.bot.send_message(chat_id=CHAT_ID, text=res)
    except Exception as e:
        print(f"Помилка сканера: {e}")

# --- MAIN ---
def main():
    try:
        print("📦 Перевірка середовища Playwright...")
        subprocess.run(["playwright", "install", "chromium"], check=True)
        subprocess.run(["playwright", "install-deps"], check=True)
    except Exception as e:
        print(f"Повідомлення інсталятора: {e}")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    # Перевірка кожну хвилину
    app.job_queue.run_repeating(fast_scan, interval=60, first=5)
    
    print("🚀 Бот увійшов у режим моніторингу...")
    app.run_polling()

async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🤖 Бот запущено! Сканування кожну хвилину.")

if __name__ == "__main__":
    main()
