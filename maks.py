import asyncio
import os
import time
import requests
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- НАЛАШТУВАННЯ ---
TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"
IMMOMIO_EMAIL = "maksymsheveliuk@gmail.com"
IMMOMIO_PASSWORD = "Maksoplot2007"

# База переглянутих
seen = set()
if os.path.exists("seen.txt"):
    with open("seen.txt") as f:
        seen = {line.strip() for line in f if line.strip()}

# Налаштування браузера для Railway
BROWSER_ARGS = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]

# --- ФУНКЦІЯ 1: АВТО-ВІДГУК (ТІЛЬКИ ПРИ НОВІЙ КВАРТИРІ) ---
async def perform_auto_apply(link):
    print(f"🚀 ЗАПУСК БРАУЗЕРА для відгуку: {link}")
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, args=BROWSER_ARGS)
            context = await browser.new_context()
            page = await context.new_page()
            
            # Логін
            await page.goto("https://app.immomio.com", timeout=60000)
            await page.fill('input[type="email"]', IMMOMIO_EMAIL)
            await page.fill('input[type="password"]', IMMOMIO_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(5000)

            # Перехід до квартири
            await page.goto(link, timeout=60000)
            await page.wait_for_load_state("networkidle")

            # Клік по кнопці
            apply_btn = page.get_by_text("Interesse bekunden", exact=False)
            if await apply_btn.is_visible():
                await apply_btn.click()
                await page.wait_for_timeout(2000)
                print(f"✅ ЗАЯВКУ НАДІСЛАНО!")
                return True
            else:
                print("⚠️ Кнопку не знайдено (вже подано?)")
                return False
        except Exception as e:
            print(f"❌ Помилка браузера: {e}")
            return False
        finally:
            await browser.close()

# --- ФУНКЦІЯ 2: ШВИДКИЙ СКАНЕР (ЛЕГКИЙ ЗАПИТ) ---
async def fast_scan(context: ContextTypes.DEFAULT_TYPE):
    print(f"🔎 [{time.strftime('%H:%M:%S')}] Швидка перевірка списку...")
    
    # --- ТЕСТОВИЙ ЗАПУСК (Видаліть цей блок після перевірки) ---
    # Вставте сюди посилання на будь-яку квартиру, яку зараз бачите на сайті
    test_link = "https://www.saga.hamburg/immobiliensuche/immo-detail/6424/schone-2-zimmer-wohnung-in-allermohe" 
    if test_link not in seen:
        print(f"🧪 ЗАПУСК ТЕСТУ НА: {test_link}")
        seen.add(test_link) # Щоб не крутилося по колу
        await context.bot.send_message(chat_id=CHAT_ID, text=f"🧪 Тест: пробую подати на {test_link}")
        status = await perform_auto_apply(test_link)
        msg = "✅ ТЕСТ УСПІШНИЙ!" if status else "❌ ТЕСТ ПРОВАЛЕНО (див. логи)"
        await context.bot.send_message(chat_id=CHAT_ID, text=msg)
    # --- КІНЕЦЬ ТЕСТОВОГО БЛОКУ ---

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        # Обов'язково вказуйте повну адресу пошуку Гамбурга
        r = requests.get("https://www.immomio.com", headers=headers, timeout=10)
        
        if r.status_code != 200:
            print(f"Помилка сайту: {r.status_code}")
            return

        # Виправлена логіка збору посилань
        parts = r.text.split("/expose/")[1:]
        for part in parts:
            expose_id = part.split('"')[0]
            link = f"https://www.immomio.com{expose_id}"

            if link not in seen:
                print(f"✨ ЗНАЙДЕНО НОВУ КВАРТИРУ: {link}")
                seen.add(link)
                with open("seen.txt", "a") as f: f.write(link + "\n")

                await context.bot.send_message(chat_id=CHAT_ID, text=f"🏠 **Нова квартира!**\n{link}\n⏳ Подаю заявку...")

                # ЗАПУСКАЄМО АВТО-ВІДГУК
                status = await perform_auto_apply(link)
                
                result_text = "✅ Заявку подано успішно!" if status else "❌ Помилка авто-подачі. Зробіть це вручну!"
                await context.bot.send_message(chat_id=CHAT_ID, text=result_text)

    except Exception as e:
        print(f"Помилка сканера: {e}")


# --- СТАРТ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Бот активний. Сканування кожні 45 секунд.")

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    
    # Інтервал: 45 секунд (оптимально для швидкості та безпеки)
    app.job_queue.run_repeating(fast_scan, interval=45, first=5)
    
    print("🚀 Бот увійшов у режим моніторингу...")
    app.run_polling()

if __name__ == "__main__":
    main()

