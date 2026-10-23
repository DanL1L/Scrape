"""Linella.md scraper, Romanian-only, compatible with original 7-field JSON.
Run in Google Colab after: !pip -q install requests parsel tqdm
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from parsel import Selector
from requests.adapters import HTTPAdapter
from tqdm.auto import tqdm
from urllib3.util.retry import Retry

SITEMAP_URLS = ["https://linella.md/products1.xml", "https://linella.md/products2.xml"]
OUTPUT_DIRECTORY = Path("Linella")
TIMEZONE = ZoneInfo("Europe/Chisinau")
MAX_WORKERS = 6
PRODUCT_LIMIT = 100  # 0 = toate produsele
MIN_SUCCESS_RATE = 0.85

COOKIES = {"region": (
    "O%3A8%3A%22stdClass%22%3A6%3A%7B"
    "s%3A2%3A%22id%22%3Bs%3A1%3A%223%22%3B"
    "s%3A5%3A%22title%22%3Bs%3A8%3A%22Botanica%22%3B"
    "s%3A8%3A%22delivery%22%3Bs%3A2%3A%2260%22%3B"
    "s%3A8%3A%22id_shops%22%3Bs%3A3%3A%22502%22%3B"
    "s%3A12%3A%22regions_type%22%3Bs%3A1%3A%221%22%3B"
    "s%3A13%3A%22delivery_free%22%3Bs%3A4%3A%221400%22%3B%7D"
)}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept-Language": "ro-RO,ro;q=0.9",
    "Referer": "https://linella.md/",
}
FIELDS = ("url", "product_name", "product_price", "discount_price", "product_sku", "product_desc", "product_meas")
_thread_local = threading.local()


def create_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.update(COOKIES)
    retry = Retry(total=3, connect=3, read=3, backoff_factor=1,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def get_session():
    if not hasattr(_thread_local, "session"):
        _thread_local.session = create_session()
    return _thread_local.session


def clean_text(s):
    return re.sub(r"\s+", " ", str(s)).strip() if s is not None and str(s).strip() else None


def first(sel, paths):
    for path in paths:
        v = clean_text(sel.xpath(path).get())
        if v:
            return v
    return None


def romanian_url(url):
    p = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in {"sl", "lang", "language"}]
    query.append(("sl", "ro"))
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ""))


def clean_url(url):
    p = urlsplit(url)
    if p.hostname not in {"linella.md", "www.linella.md"}:
        return None
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in {"sl", "lang", "language"}]
    return urlunsplit(("https", "linella.md", p.path, urlencode(query), ""))


def get_product_links():
    links = []
    with create_session() as s:
        for sitemap in SITEMAP_URLS:
            try:
                r = s.get(sitemap, timeout=30)
                print(f"Sitemap {sitemap}: HTTP {r.status_code}, {r.headers.get('Content-Type')}")
                r.raise_for_status()
                sel = Selector(text=r.text, type="xml")
                locs = sel.xpath("//*[local-name()='url']/*[local-name()='loc']/text()").getall()
                if not locs:
                    locs = sel.xpath("//*[local-name()='loc']/text()").getall()
                links.extend(filter(None, (clean_url(u.strip()) for u in locs if not u.strip().lower().endswith('.xml'))))
            except requests.RequestException as exc:
                print(f"Eroare sitemap {sitemap}: {exc}")
    links = list(dict.fromkeys(links))
    print(f"URL-uri unice: {len(links)}")
    if not links:
        raise RuntimeError("Sitemap-urile nu au furnizat URL-uri; verifică răspunsurile HTTP și XML.")
    return links[:PRODUCT_LIMIT] if PRODUCT_LIMIT else links


def price_number(text):
    if not text:
        return None
    s = re.sub(r"[^\d,.]", "", str(text))
    if not s:
        return None
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.') if s.rfind(',') > s.rfind('.') else s.replace(',', '')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return f"{float(s):.2f}"
    except ValueError:
        return None
