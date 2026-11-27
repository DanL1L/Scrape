
# ============================================================
# LINELLA.MD SCRAPER
# Romanian-only | curl_cffi | Chrome 124 impersonation
# Compatible with original 7-field JSON
# Google Colab
# ============================================================

from __future__ import annotations

# ============================================================
# 1. INSTALL DEPENDENCIES
# ============================================================

import sys
import subprocess

packages = ["curl_cffi", "parsel", "tqdm"]

for package in packages:
    try:
        __import__(package)
    except ImportError:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "-q", package
        ])

# ============================================================
# 2. IMPORTS
# ============================================================

import json
import re
import threading
import time

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from curl_cffi import requests
from parsel import Selector
from tqdm.auto import tqdm

print("Dependencies installed successfully!")

# ============================================================
# 3. CONFIGURATION
# ============================================================

SITEMAP_URLS = [
    "https://linella.md/products1.xml",
    "https://linella.md/products2.xml"
]

OUTPUT_DIRECTORY = Path("Linella")
TIMEZONE = ZoneInfo("Europe/Chisinau")

MAX_WORKERS = 10
PRODUCT_LIMIT = 100       # 0 = all products

MIN_SUCCESS_RATE = 0.85
REQUEST_TIMEOUT = 20

FIELDS = (
    "url",
    "product_name",
    "product_price",
    "discount_price",
    "product_sku",
    "product_desc",
    "product_meas"
)

OUTPUT_DIRECTORY.mkdir(exist_ok=True)

# ============================================================
# 4. COOKIES AND HEADERS
# ============================================================

COOKIES = {
    "region": (
        "O%3A8%3A%22stdClass%22%3A6%3A%7B"
        "s%3A2%3A%22id%22%3Bs%3A1%3A%223%22%3B"
        "s%3A5%3A%22title%22%3Bs%3A8%3A%22Botanica%22%3B"
        "s%3A8%3A%22delivery%22%3Bs%3A2%3A%2260%22%3B"
        "s%3A8%3A%22id_shops%22%3Bs%3A3%3A%22502%22%3B"
        "s%3A12%3A%22regions_type%22%3Bs%3A1%3A%221%22%3B"
        "s%3A13%3A%22delivery_free%22%3Bs%3A4%3A%221400%22%3B%7D"
    )
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ro-RO,ro;q=0.9",
    "Referer": "https://linella.md/"
}

# ============================================================
# 5. THREAD-LOCAL SESSION
# ============================================================

_thread_local = threading.local()


def create_session():

    session = requests.Session(
        impersonate="chrome124"
    )

    session.headers.update(HEADERS)
    session.cookies.update(COOKIES)

    return session


def get_session():

    if not hasattr(_thread_local, "session"):
        _thread_local.session = create_session()

    return _thread_local.session


# ============================================================
# 6. TEXT CLEANING
# ============================================================

def clean_text(value):

    if value is None:
        return None

    value = re.sub(r"\s+", " ", str(value)).strip()

    return value if value else None


def first(selector, paths):

    for path in paths:

        value = clean_text(selector.xpath(path).get())

        if value:
            return value

    return None


# ============================================================
# 7. URL CLEANING
# ============================================================

def clean_url(url):

    p = urlsplit(url.strip())

    if p.hostname not in {"linella.md", "www.linella.md"}:
        return None

    if "/ru/" in p.path.lower() or "/en/" in p.path.lower():
        return None

    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in {"sl", "lang", "language"}
    ]

    return urlunsplit((
        "https",
        "linella.md",
        p.path,
        urlencode(query),
        ""
    ))


def romanian_url(url):

    p = urlsplit(url)

    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in {"sl", "lang", "language"}
    ]

    query.append(("sl", "ro"))

    return urlunsplit((
        p.scheme,
        p.netloc,
        p.path,
        urlencode(query),
        ""
    ))


# ============================================================
# 8. GET PRODUCT LINKS FROM BOTH SITEMAPS
# ============================================================

def get_product_links():

    links = []

    with create_session() as session:

        for sitemap in SITEMAP_URLS:

            try:

                response = session.get(
                    sitemap,
                    timeout=REQUEST_TIMEOUT
                )

                print(
                    f"Sitemap: {sitemap} | "
                    f"HTTP: {response.status_code}"
                )

                response.raise_for_status()

                selector = Selector(
                    text=response.text,
                    type="xml"
                )

                locs = selector.xpath(
                    "//*[local-name()='loc']/text()"
                ).getall()

                sitemap_links = [
                    clean_url(url)
                    for url in locs
                ]

                links.extend(
                    url for url in sitemap_links if url
                )

            except Exception as exc:

                print(f"Sitemap error: {exc}")

    # Remove duplicates
    links = list(dict.fromkeys(links))

    print(f"\nUnique Romanian URLs: {len(links)}")

    if not links:
        raise RuntimeError(
            "No product URLs found. Check sitemap responses."
        )

    if PRODUCT_LIMIT:
        links = links[:PRODUCT_LIMIT]

    print(f"Products selected: {len(links)}")

    return links


# ============================================================
# 9. PRICE NORMALIZATION
# ============================================================

def price_number(value):

    if not value:
        return None

    s = re.sub(r"[^\d,.]", "", str(value))

    if not s:
        return None

    if "," in s and "." in s:

        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")

    elif "," in s:

        s = s.replace(",", ".")

    try:

        return f"{float(s):.2f}"

    except ValueError:

        return None


# ============================================================
# 10. SCRAPE ONE PRODUCT
# ============================================================

def scrape_single_link(link):

    session = get_session()

    try:

        url = romanian_url(link)

        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code != 200:

            print(
                f"HTTP {response.status_code}: {url}"
            )

            return None

        selector = Selector(
            text=response.text
        )

        # ----------------------------------------------------
        # PRODUCT NAME
        # ----------------------------------------------------

        product_name = first(
            selector,
            [
                "//h1//text()"
            ]
        )

        # ----------------------------------------------------
        # SKU
        # ----------------------------------------------------

        product_sku = first(
            selector,
            [
                "//div[contains(@class,'ut2--sku-text')]/text()"
            ]
        )

        # ----------------------------------------------------
        # PRODUCT DESCRIPTION
        # ----------------------------------------------------

        raw_desc = selector.xpath(
            "//div[@class='ty-product-feature']//text()"
        ).getall()

        product_desc = clean_text(
            " ".join(
                t.strip()
                for t in raw_desc
                if t.strip()
            )
        )

        # ----------------------------------------------------
        # CURRENT PRICE
        # ----------------------------------------------------

        cur_parts = selector.xpath(
            "(//span[contains(@class,'ty-price')])[1]"
            "//span[contains(@class,'ty-price-num')][1]"
            "//text()"
        ).getall()

        cur_price = price_number(
            "".join(
                p.strip()
                for p in cur_parts
                if p.strip()
            )
        )

        # ----------------------------------------------------
        # OLD PRICE
        # ----------------------------------------------------

        old_parts = selector.xpath(
            "(//span[contains(@class,'ty-strike')])[1]"
            "//span[contains(@class,'ty-list-price')][1]"
            "//text()"
        ).getall()

        old_price = price_number(
            "".join(
                p.strip()
                for p in old_parts
                if p.strip()
            )
        )

        # ----------------------------------------------------
        # PRICE LOGIC
        # ----------------------------------------------------

        if old_price:

            product_price = old_price
            discount_price = cur_price

        else:

            product_price = cur_price
            discount_price = None

        # ----------------------------------------------------
        # PRODUCT MEASUREMENT
        # ----------------------------------------------------

        raw_meas = selector.xpath(
            "//div[contains(@class,'linella-unit-price')]//text()"
        ).getall()

        product_meas = clean_text(
            " ".join(
                m.strip()
                for m in raw_meas
                if m.strip()
            )
        )

        # ----------------------------------------------------
        # FINAL JSON OBJECT - 7 FIELDS
        # ----------------------------------------------------

        result = {
            "url": link,
            "product_name": product_name,
            "product_price": product_price,
            "discount_price": discount_price,
            "product_sku": product_sku,
            "product_desc": product_desc,
            "product_meas": product_meas
        }

        return result

    except Exception as exc:

        print(f"Product error: {link} | {exc}")

        return None


# ============================================================
# 11. SCRAPE ALL PRODUCTS IN PARALLEL
# ============================================================

def scrape_all_products(product_links):

    scraped_data = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(scrape_single_link, link): link
            for link in product_links
        }

        for future in tqdm(
            as_completed(futures),
            total=len(futures),
            desc="Scraping Linella"
        ):

            try:

                result = future.result()

                if result:
                    scraped_data.append(result)

            except Exception as exc:

                print(f"Worker error: {exc}")

    return scraped_data


# ============================================================
# 12. SAVE JSON
# ============================================================

def save_json(data):

    timestamp = datetime.now(
        TIMEZONE
    ).strftime("%Y%m%d_%H%M%S")

    filename = (
        OUTPUT_DIRECTORY /
        f"linella_products_{timestamp}.json"
    )

    with open(
        filename,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=4
        )

    print(f"\nJSON saved: {filename}")

    return filename


# ============================================================
# 13. MAIN EXECUTION
# ============================================================

def main():

    start_time = time.time()

    print("\nStarting Linella scraper...\n")

    # Get product links
    product_links = get_product_links()

    # Scrape products
    scraped_data = scrape_all_products(
        product_links
    )

    # Statistics
    total = len(product_links)
    successful = len(scraped_data)

    success_rate = (
        successful / total if total else 0
    )

    print("\n" + "=" * 50)
    print("SCRAPING RESULTS")
    print("=" * 50)

    print(f"Products found: {total}")
    print(f"Successfully scraped: {successful}")
    print(f"Failed: {total - successful}")
    print(f"Success rate: {success_rate:.2%}")

    print(
        f"Execution time: "
        f"{(time.time() - start_time):.2f} seconds"
    )

    # Save JSON
    filename = save_json(scraped_data)

    if success_rate < MIN_SUCCESS_RATE:

        print(
            "\nWARNING: Success rate below "
            f"{MIN_SUCCESS_RATE:.0%}"
        )

    return scraped_data, filename


# ============================================================
# 14. RUN
# ============================================================

scraped_data, output_file = main()

print("\nFirst 3 products:")

for product in scraped_data[:3]:
    print(json.dumps(product, ensure_ascii=False, indent=2))
