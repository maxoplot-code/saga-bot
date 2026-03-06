import asyncio
import requests
from bs4 import BeautifulSoup

from telegram.ext import Application, ContextTypes

TOKEN = "8652232123:AAFOD4BUpETqOHdb3qxq1SI9jAKR7Rnxebc"
CHAT_ID = "8349459166"

seen = set()

# -----------------------
# SEND MESSAGE
# -----------------------

async def send_if_new(context, title, link):

    key = title + link

    if key in seen:
        return

    seen.add(key)

    text = f"🏠 Нова квартира\n\n{title}\n{link}"

    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=text
    )


# -----------------------
# KLEINANZEIGEN
# -----------------------

async def scan_kleinanzeigen(context):

    url = "https://www.kleinanzeigen.de/s-wohnung-mieten/hamburg/c203l9409"

    try:

        r = requests.get(url, timeout=10)

        soup = BeautifulSoup(r.text, "html.parser")

        ads = soup.select(".aditem")

        for ad in ads[:10]:

            title = ad.select_one(".ellipsis")

            link = ad.select_one("a")

            if title and link:

                t = title.get_text(strip=True)

                l = "https://www.kleinanzeigen.de" + link["href"]

                await send_if_new(context, t, l)

    except Exception as e:

        print("Kleinanzeigen error:", e)


# -----------------------
# IMMOWELT
# -----------------------

async def scan_immowelt(context):

    url = "https://www.immowelt.de/suche/hamburg/wohnungen/mieten"

    try:

        r = requests.get(url, timeout=10)

        soup = BeautifulSoup(r.text, "html.parser")

        ads = soup.select(".SearchList-Entry")

        for ad in ads[:10]:

            title = ad.get_text(strip=True)[:150]

            link = ad.select_one("a")

            if link:

                l = "https://www.immowelt.de" + link["href"]

                await send_if_new(context, title, l)

    except Exception as e:

        print("Immowelt error:", e)


# -----------------------
# WG-GESUCHT
# -----------------------

async def scan_wg(context):

    url = "https://www.wg-gesucht.de/wohnungen-in-Hamburg.55.2.1.0.html"

    try:

        r = requests.get(url, timeout=10)

        soup = BeautifulSoup(r.text, "html.parser")

        ads = soup.select(".wgg_card")

        for ad in ads[:10]:

            title = ad.get_text(strip=True)[:150]

            link = ad.select_one("a")

            if link:

                l = "https://www.wg-gesucht.de" + link["href"]

                await send_if_new(context, title, l)

    except Exception as e:

        print("WG error:", e)


# -----------------------
# MAIN SCAN
# -----------------------

async def scan(context: ContextTypes.DEFAULT_TYPE):

    print("SCAN STARTED")

    await scan_kleinanzeigen(context)

    await scan_immowelt(context)

    await scan_wg(context)

    print("SCAN FINISHED")


# -----------------------
# MAIN
# -----------------------

def main():

    app = Application.builder().token(TOKEN).build()

    app.job_queue.run_repeating(
        scan,
        interval=5,
        first=5
    )

    print("BOT STARTED")

    app.run_polling()


if __name__ == "__main__":
    main()
