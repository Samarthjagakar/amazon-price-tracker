import os
import re
import json
import time
import random
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
HISTORY_FILE = "history.json"

HEADERS_LIST = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    },
]

def send_telegram_alert(message):
    url = f"https://telegram.org{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": message})

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
        response = requests.get(url, headers=random.choice(HEADERS_LIST), timeout=15)
        if response.status_code != 200 or "captcha" in response.text.lower():
            print(f"Blocked or unavailable ({response.status_code}): {url}")
            return None, None

        soup = BeautifulSoup(response.text, "html.parser")
        
        # Extract main price
        price_el = soup.select_one("span.a-price-whole")
        if not price_el:
            return None, None
        clean_price = re.sub(r"[^\d]", "", price_el.get_text())
        price = int(clean_price) if clean_price else None

        # Check for extra clip coupon text
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
            # Alert only if we haven't alerted this exact price (or lower) yet
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
            # Reset history if price goes back above target so future drops alert again
            history.pop(url, None)

        time.sleep(random.uniform(5, 10))

    save_json(HISTORY_FILE, history)

if __name__ == "__main__":
    main()
