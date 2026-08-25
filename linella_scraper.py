"""Scrape Linella product data and save a dated JSON snapshot."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from parsel import Selector
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry


SITEMAP_URL = "https://linella.md/sitemap-products-en.xml"
OUTPUT_DIRECTORY = Path("Linella")
TIMEZONE = ZoneInfo("Europe/Chisinau")
MAX_WORKERS = 20

# Linella region cookie for Botanica, Chișinău.
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
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://linella.md/",
}

_thread_local = threading.local()


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = create_session()
    return _thread_local.session


def get_product_links() -> list[str]:
    with create_session() as session:
        response = session.get(SITEMAP_URL, timeout=30)
        response.raise_for_status()

    selector = Selector(text=response.text)
    links = selector.xpath("//*[local-name()='loc']/text()").getall()
    if not links:
        raise RuntimeError("Sitemap-ul Linella nu conține linkuri de produse.")
    return links


def clean_text(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None


def scrape_product(link: str) -> dict[str, str | None] | None:
    try:
        response = get_session().get(link, cookies=COOKIES, timeout=20)
        response.raise_for_status()
        selector = Selector(text=response.text)

        product_name = clean_text(
            selector.xpath("//div[@class='product__rht']/h1/text()").get()
        )
        product_sku = clean_text(
            selector.xpath("//p[contains(text(), 'SKU')]/span/text()").get()
        )

        description_parts = selector.xpath(
            "//div[contains(@class, 'products_text_1')]//text()"
        ).getall()
        product_description = " ".join(
            text.strip() for text in description_parts if text.strip()
        ) or None

        price_block = selector.xpath("//div[@class='rht__block_3']")
        discount_price = clean_text(
            price_block.xpath(".//span[@class='price__real']/text()").get()
        )
        if discount_price:
            product_price = clean_text(
                price_block.xpath(".//span[@class='price__past']/text()").get()
            )
        else:
            product_price = clean_text(
                price_block.xpath(".//div[contains(@class, 'price')]/text()").get()
            )

        product_measure = clean_text(
            price_block.xpath(
                ".//div[contains(@class, 'price')]/span[not(@class)]/text()"
            ).get()
        )

        time.sleep(0.1)
        return {
            "url": link,
            "product_name": product_name,
            "product_price": product_price,
            "discount_price": discount_price,
            "product_sku": product_sku,
            "product_desc": product_description,
            "product_meas": product_measure,
        }
    except requests.RequestException as exc:
        print(f"Eroare pentru {link}: {exc}")
        return None


def main() -> None:
    product_links = get_product_links()
    scraped_data: list[dict[str, str | None]] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(scrape_product, link) for link in product_links]
        for future in tqdm(as_completed(futures), total=len(futures)):
            result = future.result()
            if result:
                scraped_data.append(result)

    if not scraped_data:
        raise RuntimeError("Nu a fost extras niciun produs; fișierul nu va fi salvat.")

    # Stable ordering avoids unnecessary differences on repeated runs.
    scraped_data.sort(key=lambda item: item["url"] or "")

    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    date_stamp = datetime.now(TIMEZONE).strftime("%Y%m%d")
    output_path = OUTPUT_DIRECTORY / f"linella_scrape_{date_stamp}.json"
    with output_path.open("w", encoding="utf-8") as output_file:
        json.dump(scraped_data, output_file, ensure_ascii=False, indent=2)

    success_rate = len(scraped_data) / len(product_links) * 100
    print(
        f"Salvat: {output_path} | "
        f"{len(scraped_data)}/{len(product_links)} produse ({success_rate:.1f}%)."
    )


if __name__ == "__main__":
    main()
