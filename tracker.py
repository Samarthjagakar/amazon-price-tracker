import os
import re
import json
import time
import random
from curl_cffi import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
HISTORY_FILE = "history.json"

def send_telegram_alert(message):
    url = f"https://telegram.org{BOT_TOKEN}/sendMessage"
    res = requests.post(url, data={"chat_id": CHAT_ID, "text": message})
    print("Telegram status:", res.status_code)

def load_json(filename, default):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def check_product(url):
    try:
        # Impersonate real Chrome browser to bypass Amazon bot detection
        response = requests.get(
            url,
            impersonate="chrome124",
            headers={"Accept-Language": "en-IN,en;q=0.9"},
            timeout=20,
        )
        if response.status_code != 200 or "Type the characters you see in this image" in response.text:
            print(f"Blocked or CAPTCHA ({response.status_code}): {url}")
            return None, None

        soup = BeautifulSoup(response.text, "html.parser")

        # Check multiple Amazon price selectors
        price_el = (
            soup.select_one("span.a-price-whole")
            or soup.select_one("#corePriceDisplay_desktop_feature_div .a-price-whole")
            or soup.select_one("#corePrice_feature_div .a-price-whole")
        )
        if not price_el:
            print(f"Price element not found on page (item may be out of stock): {url}")
            return None, None

        clean_price = re.sub(r"[^\d]", "", price_el.get_text())
        price = int(clean_price) if clean_price else None

        coupon_el = soup.select_one("#vpcButton, .promoPriceBlockMessage")
        coupon_text = coupon_el.get_text(strip=True) if coupon_el else None

        return price, coupon_text
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None, None

def main():
    watchlist = load_json("watchlist.json", [])
    history = load_json(HISTORY_FILE, {})

    for item in watchlist:
        url = item["url"]
        name = item["name"]
        target = item["max_price"]

        price, coupon = check_product(url)
        print(f"{name}: ₹{price} (Target: ₹{target})")

        if price and price <= target:
            last_alerted_price = history.get(url)
            if last_alerted_price is None or price < last_alerted_price:
                coupon_note = f"\n🎟️ Extra Offer: {coupon}" if coupon else ""
                msg = (
                    f"🚨 AMAZON PRICE ALERT!\n\n"
                    f"📦 {name}\n"
                    f"💰 Current Price: ₹{price:,}\n"
                    f"🎯 Your Target: ₹{target:,}"
                    f"{coupon_note}\n\n"
                    f"🔗 {url}"
                )
                send_telegram_alert(msg)
                history[url] = price
        elif price and price > target:
            history.pop(url, None)

        time.sleep(random.uniform(5, 10))

    save_json(HISTORY_FILE, history)

if __name__ == "__main__":
    main()
