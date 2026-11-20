import html
import json
import os
import re
import sys
import random
import time
from pathlib import Path

import requests
from playwright_stealth import Stealth
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

WATCHLIST_FILE = Path("watchlist.json")
SEARCHES_FILE = Path("searches.json")
HISTORY_FILE = Path("history.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SOCKS5_PROXY = "socks5://127.0.0.1:40000"
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


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


def parse_rupee_value(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def extract_search_card(card):
    asin = card.get_attribute("data-asin")
    if not asin:
        return None

    whole_card_text = card.inner_text(timeout=5000)
    if re.search(r"\bsponsored\b", whole_card_text, re.I):
        return None

    title_element = card.locator("h2").first
    title = (
        title_element.inner_text(timeout=3000).strip()
        if title_element.count()
        else asin
    )

    selling_elements = card.locator(
        "span.a-price:not(.a-text-price) span.a-offscreen"
    )
    price = None
    if selling_elements.count():
        price = parse_rupee_value(
            selling_elements.first.text_content(timeout=3000)
        )

    if price is None:
        whole_price = card.locator("span.a-price-whole")
        if whole_price.count():
            price = parse_rupee_value(
                whole_price.first.text_content(timeout=3000)
            )

    mrp_element = card.locator("span.a-price.a-text-price span.a-offscreen")
    mrp = None
    if mrp_element.count():
        mrp = parse_rupee_value(mrp_element.first.text_content(timeout=3000))

    review_element = card.locator("span.a-size-base.s-underline-text")
    reviews = 0
    if review_element.count():
        review_text = review_element.first.inner_text(timeout=3000)
        review_digits = re.sub(r"[^0-9.,]", "", review_text).strip()
        if review_digits:
            review_digits = review_digits.replace(",", "")
            try:
                reviews = int(float(review_digits))
                if "k" in review_text.lower():
                    reviews *= 1000
            except ValueError:
                reviews = 0

    return {
        "asin": asin.upper(),
        "title": title,
        "price": price,
        "mrp": mrp,
        "reviews": reviews,
    }


def scan_search(page, search, history):
    name = str(search.get("name", "Unknown category"))
    url = str(search.get("url", "")).strip()

    try:
        min_discount = float(search["min_discount_percent"])
        max_price = int(search["max_price"])
        min_reviews = int(search["min_reviews"])
    except (KeyError, TypeError, ValueError):
        print(f"Skipping invalid search category: {name}", file=sys.stderr)
        return

    if not url:
        print(f"Skipping search category without a URL: {name}", file=sys.stderr)
        return

    print(f"Scanning: {name}")

    try:
        page_count = int(search.get("pages", 5))
    except (TypeError, ValueError):
        page_count = 5
    page_count = max(1, page_count)

    separator = "&" if "?" in url else "?"

    for page_number in range(1, page_count + 1):
        page_url = f"{url}{separator}page={page_number}"
        try:
            response = page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            if response is not None and response.status == 503:
                print(
                    f"{name} page {page_number}: HTTP 503; backing off for 60 seconds",
                    file=sys.stderr,
                )
                time.sleep(60)
                response = page.goto(
                    page_url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

            if response is not None and response.status >= 400:
                print(
                    f"{name} page {page_number}: Amazon returned HTTP "
                    f"{response.status}",
                    file=sys.stderr,
                )
            else:
                cards = page.locator("div[data-component-type='s-search-result']")
                for index in range(cards.count()):
                    card = cards.nth(index)
                    try:
                        product = extract_search_card(card)
                        if product is None:
                            continue

                        asin = product["asin"]
                        price = product["price"]
                        mrp = product["mrp"]
                        reviews = product["reviews"]

                        if price is None or mrp is None or mrp <= 0:
                            continue

                        discount = ((mrp - price) / mrp) * 100
                        if (
                            discount < min_discount
                            or price > max_price
                            or reviews < min_reviews
                        ):
                            continue

                        previous = history.get(asin)
                        if isinstance(previous, (int, float)):
                            if previous <= price:
                                continue

                        product_url = f"https://amazon.in/dp/{asin}"
                        message = (
                            f"<b>Amazon deal error / price drop</b>\n"
                            f"<b>{html.escape(product['title'])}</b>\n"
                            f"Category: {html.escape(name)}\n"
                            f"ASIN: <code>{html.escape(asin)}</code>\n"
                            f"Price: <b>₹{price:,}</b>\n"
                            f"MRP: ₹{mrp:,}\n"
                            f"Discount: <b>{discount:.1f}% off</b>\n"
                            f"Reviews: {reviews:,}\n\n"
                            f'<a href="{html.escape(product_url, quote=True)}">'
                            "View on Amazon.in</a>"
                        )

                        send_telegram(message)
                        history[asin] = price
                        print(
                            f"{asin}: alert sent at ₹{price:,} "
                            f"({discount:.1f}% off, {reviews} reviews)"
                        )
                    except Exception as error:
                        print(
                            f"Could not process a result in {name}: {error}",
                            file=sys.stderr,
                        )

            if page_number < page_count:
                page.wait_for_timeout(random.uniform(3, 6) * 1000)
        except Exception as error:
            print(
                f"Could not scan {name} page {page_number}: {error}",
                file=sys.stderr,
            )
            if page_number < page_count:
                page.wait_for_timeout(random.uniform(3, 6) * 1000)


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
    searches = load_json(SEARCHES_FILE, [])
    history = load_json(HISTORY_FILE, {})

    if not isinstance(watchlist, list):
        print("watchlist.json must contain a JSON array", file=sys.stderr)
        sys.exit(1)

    if not isinstance(searches, list):
        print("searches.json must contain a JSON array", file=sys.stderr)
        sys.exit(1)

    if not isinstance(history, dict):
        history = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            proxy={"server": SOCKS5_PROXY},
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
        Stealth().apply_stealth_sync(page)

        def block_heavy_resources(route):
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                route.abort()
            else:
                route.continue_()

        page.route("**/*", block_heavy_resources)

        try:
            # Load Amazon first so the browser has an established session and cookies.
            try:
                page.goto(
                    "https://amazon.in",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_timeout(3000)
            except Exception as error:
                print(f"Could not load Amazon homepage: {error}", file=sys.stderr)

            for search in searches:
                if isinstance(search, dict):
                    scan_search(page, search, history)

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

                    if response is not None and response.status == 503:
                        print(
                            f"{asin}: HTTP 503; backing off for 60 seconds",
                            file=sys.stderr,
                        )
                        time.sleep(60)
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

                    page.wait_for_timeout(random.uniform(3, 6) * 1000)
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
