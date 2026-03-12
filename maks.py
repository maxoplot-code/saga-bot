import asyncio
import os
from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# --- ФУНКЦІЯ АВТО-ВІДГУКУ ---
async def apply_to_apartment(link):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True) # headless=True для сервера
        context = await browser.new_context()
        page = await context.new_page()
        
        try:
            print(f"🚀 Спроба подачі на: {link}")
            # 1. Логін
            await page.goto("https://app.immomio.com", timeout=60000)
            await page.fill('input[type="email"]', IMMOMIO_EMAIL)
            await page.fill('input[type="password"]', IMMOMIO_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(5000) # Чекаємо завантаження профілю

            # 2. Перехід до оголошення
            await page.goto(link, timeout=60000)
            await page.wait_for_timeout(3000)

            # 3. Пошук кнопки (текст може бути "Interesse bekunden" або "Bewerben")
            # Використовуємо універсальний пошук за текстом
            apply_button = page.get_by_text("Interesse bekunden", exact=False)
            
            if await apply_button.is_visible():
                await apply_button.click()
                await page.wait_for_timeout(2000)
                print(f"✅ Успішно подано!")
                return True
            else:
                print("⚠️ Кнопку не знайдено (можливо, вже подано або інший формат)")
                return False

        except Exception as e:
            print(f"❌ Помилка Playwright: {e}")
            return False
        finally:
            await browser.close()

# --- СКАНЕР ---
async def check_immomio(context: ContextTypes.DEFAULT_TYPE):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto("https://www.immomio.com", timeout=60000)
            # Чекаємо появи оголошень
            content = await page.content()
            parts = content.split("/expose/")[1:]
            
            for part in parts:
                expose_id = part.split('"')[0]
                link = f"https://www.immomio.com{expose_id}"

                if link not in seen:
                    seen.add(link)
                    with open("seen.txt", "a") as f: f.write(link + "\n")

                    # Одразу пробуємо подати заявку
                    status = await apply_to_apartment(link)
                    
                    msg = f"🏠 **НОВЕ ОГОЛОШЕННЯ!**\n🔗 {link}\n"
                    msg += "✅ **Заявку подано автоматично!**" if status else "⚠️ Не вдалося подати авто-заявку."
                    
                    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
        
        except Exception as e:
            print(f"Помилка сканера: {e}")
        finally:
            await browser.close()

# --- СТАРТ БОТА ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Бот з авто-відгуком запущений!")

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    
    # Скануємо кожні 10 хвилин (безпечно для акаунта)
    app.job_queue.run_repeating(check_immomio, interval=600, first=10)
    
    print("🚀 Бот працює...")
    app.run_polling()

if __name__ == "__main__":
    main()
