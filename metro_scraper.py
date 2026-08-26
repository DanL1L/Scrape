"""Scrape Metro Moldova product data and save a dated JSON snapshot."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry


SEARCH_URL = "https://sortiment.metro.md/searchdiscover/articlesearch/search"
DETAIL_URL = "https://sortiment.metro.md/evaluate.article.v1/betty-variants"
OUTPUT_DIRECTORY = Path("Metro")
TIMEZONE = ZoneInfo("Europe/Chisinau")
STORE_ID = "00001"
LOCALE = "ro-MD"
COUNTRY = "MD"
PAGE_SIZE = 24
CHUNK_SIZE = 10
MAX_WORKERS = 5

CATEGORIES = [
    "bacanie-conserve-dulciuri",
    "produse-nealimentare",
    "produse-proaspete",
    "detergenti-si-produse-cosmetice",
    "bauturi",
]

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ro-MD,ro;q=0.9,en;q=0.8",
    "Referer": "https://sortiment.metro.md/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
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


def request_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    response = get_session().get(url, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Răspuns JSON neașteptat de la {url}.")
    return payload


def collect_product_ids(category: str, timestamp: str) -> list[str]:
    product_ids: list[str] = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        payload = request_json(
            SEARCH_URL,
            {
                "storeId": STORE_ID,
                "language": LOCALE,
                "country": COUNTRY,
                "query": "*",
                "rows": str(PAGE_SIZE),
                "page": str(page),
                "filter": f"category:{category}",
                "facets": "true",
                "categories": "true",
                "__t": timestamp,
            },
        )

        if page == 1:
            total_pages = int(payload.get("totalPages", 1))

        result_ids = payload.get("resultIds", [])
        if not isinstance(result_ids, list):
            raise ValueError(f"Lista de produse lipsește pentru categoria {category}.")
        product_ids.extend(str(product_id) for product_id in result_ids)
        page += 1
        time.sleep(0.05)

    # Preserve API order while removing accidental duplicates.
    return list(dict.fromkeys(product_ids))


def parse_product_results(
    category: str, results: dict[str, Any]
) -> list[dict[str, Any]]:
    category_data: list[dict[str, Any]] = []

    for product_id, product_info in results.items():
        brand = product_info.get("brandName")
        variants = product_info.get("variants", {})

        for variant_data in variants.values():
            categories = variant_data.get("categories", [])
            category_path = categories[0].get("name") if categories else None

            bundle_selector = variant_data.get("bundleSelector", {})
            packaging = next(iter(bundle_selector.values()), None)

            for bundle_data in variant_data.get("bundles", {}).values():
                store_data = bundle_data.get("stores", {}).get(STORE_ID, {})
                delivery_data = store_data.get("possibleDeliveryModes", {}).get(
                    "STORE", {}
                )
                fulfillment_data = delivery_data.get(
                    "possibleFulfillmentTypes", {}
                ).get("STORE", {})
                price_info = fulfillment_data.get("sellingPriceInfo", {})

                bulk_discounts: dict[str, Any] = {}
                for discount_data in price_info.get("dnrInfo", {}).values():
                    for quantity, level_data in discount_data.get(
                        "levels", {}
                    ).items():
                        bulk_discounts[f"price_for_{quantity}"] = level_data.get(
                            "finalSingleNetPrice"
                        ) or level_data.get("value")

                category_data.append(
                    {
                        "category_search": category,
                        "category_path": category_path,
                        "product_id": product_id,
                        "brand": brand,
                        "name": variant_data.get("description"),
                        "packaging": packaging,
                        "gross_weight_kg": bundle_data.get("grossWeight"),
                        "final_price": price_info.get("finalPrice"),
                        "regular_shelf_price": price_info.get("shelfPrice"),
                        "base_price_no_vat": price_info.get("basePrice"),
                        "vat_amount": price_info.get("vat"),
                        "bulk_discounts": (
                            str(bulk_discounts) if bulk_discounts else None
                        ),
                    }
                )

    return category_data


def scrape_category(category: str) -> list[dict[str, Any]]:
    now = datetime.now(TIMEZONE)
    date_stamp = now.strftime("%Y%m%d")
    timestamp = str(int(time.time() * 1000))
    product_ids = collect_product_ids(category, timestamp)
    if not product_ids:
        raise RuntimeError(f"Categoria {category} nu conține produse.")

    category_data: list[dict[str, Any]] = []
    chunks = [
        product_ids[index : index + CHUNK_SIZE]
        for index in range(0, len(product_ids), CHUNK_SIZE)
    ]

    for chunk in chunks:
        payload = request_json(
            DETAIL_URL,
            {
                "storeIds": STORE_ID,
                "ids": chunk,
                "country": COUNTRY,
                "locale": LOCALE,
                "deliveryDate": date_stamp,
                "__t": timestamp,
            },
        )
        results = payload.get("result", {})
        if not isinstance(results, dict):
            raise ValueError(f"Detaliile produselor lipsesc pentru {category}.")
        category_data.extend(parse_product_results(category, results))
        time.sleep(0.05)

    return category_data


def main() -> None:
    scraped_data: list[dict[str, Any]] = []
    errors: list[str] = []

    print(f"Începe extragerea pentru {len(CATEGORIES)} categorii...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(scrape_category, category): category
            for category in CATEGORIES
        }
        for future in tqdm(
            as_completed(futures), total=len(futures), desc="Categorii"
        ):
            category = futures[future]
            try:
                scraped_data.extend(future.result())
            except Exception as exc:  # Report all failed categories together.
                errors.append(f"{category}: {exc}")

    if errors:
        raise RuntimeError(
            "Extragerea Metro a eșuat pentru: " + " | ".join(errors)
        )
    if not scraped_data:
        raise RuntimeError("Nu a fost extras niciun produs Metro.")

    scraped_data.sort(
        key=lambda item: (
            str(item.get("category_search") or ""),
            str(item.get("product_id") or ""),
            str(item.get("name") or ""),
            str(item.get("packaging") or ""),
        )
    )

    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    date_stamp = datetime.now(TIMEZONE).strftime("%Y%m%d")
    output_path = OUTPUT_DIRECTORY / f"metro_scrape_{date_stamp}.json"
    with output_path.open("w", encoding="utf-8") as output_file:
        json.dump(scraped_data, output_file, ensure_ascii=False, indent=2)

    print(f"Salvat: {output_path} | {len(scraped_data):,} rânduri.")


if __name__ == "__main__":
    main()
