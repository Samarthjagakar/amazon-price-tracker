import html
import json
import os
import re
import sys
from pathlib import Path

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

WATCHLIST_FILE = Path("watchlist.json")
HISTORY_FILE = Path("history.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def load_json(path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError) as error:
        print(f"Could not read {path}: {error}", file=sys.stderr)
        return default


def save_history(history):
    with HISTORY_FILE.open("w", encoding="utf-8") as file:
        json.dump(history, file, indent=2, sort_keys=True)
        file.write("\n")


def extract_rupee_price(page):
    price_selectors = [
        "span.a-price-whole",
        "span.a-price .a-offscreen",
        ".apexPriceToPay",
        "#corePrice_feature_div .a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#priceblock_saleprice",
    ]

    for selector in price_selectors:
        try:
            elements = page.locator(selector)
            for index in range(elements.count()):
                text = elements.nth(index).inner_text(timeout=3000).strip()
                digits = re.sub(r"[^\d]", "", text)
                if digits:
                    return int(digits)
        except PlaywrightTimeoutError:
            continue
        except Exception as error:
            print(f"Price extraction failed for {selector}: {error}", file=sys.stderr)

    # Amazon also exposes structured product metadata on some page variants.
    try:
        scripts = page.locator('script[type="application/ld+json"]')
        for index in range(scripts.count()):
            raw = scripts.nth(index).text_content(timeout=3000)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            records = data if isinstance(data, list) else [data]
            for record in records:
                if not isinstance(record, dict):
                    continue
                offers = record.get("offers")
                if isinstance(offers, list):
                    candidates = offers
                else:
                    candidates = [offers] if isinstance(offers, dict) else []
                for offer in candidates:
                    for key in ("price", "lowPrice", "highPrice"):
                        digits = re.sub(r"[^\d]", "", str(offer.get(key, "")))
                        if digits:
                            return int(digits)
    except Exception as error:
        print(f"Structured price extraction failed: {error}", file=sys.stderr)

    return None


def extract_coupon_text(page):
    candidates = []

    try:
        body_text = page.locator("body").inner_text(timeout=5000)
        for line in body_text.splitlines():
            line = re.sub(r"\s+", " ", line).strip()
            if re.search(r"\b(?:clip|coupon|save)\b", line, re.I):
                if 0 < len(line) <= 180:
                    candidates.append(line)
    except Exception:
        pass

    try:
        coupon_elements = page.locator(
            "#couponText, "
            "[data-action='coupon'], "
            "[class*='coupon' i], "
            "[id*='coupon' i]"
        )
        for index in range(coupon_elements.count()):
            text = coupon_elements.nth(index).inner_text(timeout=2000).strip()
            text = re.sub(r"\s+", " ", text)
            if text and 0 < len(text) <= 180:
                candidates.append(text)
    except Exception:
        pass

    unique = []
    seen = set()

    for candidate in candidates:
        normalized = candidate.lower()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(candidate)

    return unique[:5]


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables are required"
        )

    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    response.raise_for_status()


def main():
    watchlist = load_json(WATCHLIST_FILE, [])
    history = load_json(HISTORY_FILE, {})

    if not isinstance(watchlist, list):
        print("watchlist.json must contain a JSON array", file=sys.stderr)
        sys.exit(1)

    if not isinstance(history, dict):
        history = {}

    if not watchlist:
        print("Watchlist is empty")
        save_history(history)
        return

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        context = browser.new_context(
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        try:
            # Load Amazon first so the browser has an established session and cookies.
            try:
                page.goto(
                    "https://www.amazon.in/",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_timeout(3000)
            except Exception as error:
                print(f"Could not load Amazon homepage: {error}", file=sys.stderr)

            for item in watchlist:
                asin = str(item.get("asin", "")).strip().upper()
                title = str(item.get("title", asin)).strip()
                max_price = item.get("max_price")

                if not asin:
                    print("Skipping watchlist entry without an ASIN", file=sys.stderr)
                    continue

                try:
                    max_price = int(max_price)
                except (TypeError, ValueError):
                    print(
                        f"Skipping {asin}: max_price must be an integer",
                        file=sys.stderr,
                    )
                    continue

                product_url = f"https://www.amazon.in/dp/{asin}"

                try:
                    response = page.goto(
                        product_url,
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )

                    if response is not None and response.status >= 400:
                        print(
                            f"Amazon returned HTTP {response.status} for {asin}",
                            file=sys.stderr,
                        )
                        continue

                    page.wait_for_timeout(2500)
                    price = extract_rupee_price(page)

                    if price is None:
                        print(
                            f"Could not extract a price for {title} ({asin})",
                            file=sys.stderr,
                        )
                        continue

                    print(f"{title}: ₹{price:,}")

                    if price > max_price:
                        if asin in history:
                            del history[asin]
                            print(f"{asin}: price returned above target; history reset")
                        continue

                    previous_alert = history.get(asin)
                    if (
                        isinstance(previous_alert, (int, float))
                        and previous_alert <= price
                    ):
                        print(
                            f"{asin}: already alerted at ₹{previous_alert:,} "
                            "or lower"
                        )
                        continue

                    coupons = extract_coupon_text(page)
                    coupon_message = ""

                    if coupons:
                        coupon_message = "\n\n<b>Coupon / offer:</b>\n" + "\n".join(
                            f"- {html.escape(coupon)}" for coupon in coupons
                        )

                    message = (
                        f"<b>Amazon price drop</b>\n"
                        f"{html.escape(title)}\n"
                        f"ASIN: <code>{html.escape(asin)}</code>\n"
                        f"Current price: <b>₹{price:,}</b>\n"
                        f"Target price: ₹{max_price:,}"
                        f"{coupon_message}\n\n"
                        f'<a href="{html.escape(product_url, quote=True)}">'
                        "View on Amazon.in</a>"
                    )

                    send_telegram(message)
                    history[asin] = price
                    print(f"{asin}: Telegram alert sent at ₹{price:,}")

                except requests.RequestException as error:
                    print(
                        f"Telegram request failed for {asin}: {error}",
                        file=sys.stderr,
                    )
                except Exception as error:
                    print(
                        f"Could not process {asin}: {error}",
                        file=sys.stderr,
                    )

        finally:
            context.close()
            browser.close()

    save_history(history)


if __name__ == "__main__":
    main()
