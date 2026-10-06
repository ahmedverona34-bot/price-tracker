"""Collect structured product data from e-commerce stores via Scrapling.

Simple interface for the rest of the project::

    from scrapling_client import collect_products, crawl_catalog

    products = collect_products("https://dream2000.com", query="iphone")
    # [{"title": ..., "price": ..., "currency": "EGP", "old_price": ...,
    #   "image_urls": [...], "availability": ..., "link": ...}, ...]

Fetcher ladder (lightest first, escalate only when needed):

1. Structured JSON endpoints (Shopify ``/search/suggest.json``,
   WooCommerce Store API) - no HTML parsing at all.
2. Plain ``Fetcher`` for static server-rendered pages.
3. ``DynamicFetcher`` (real installed Chrome) when products load via JS.
4. ``StealthyFetcher`` only behind Cloudflare/Turnstile-style protection.

Politeness is on by default: robots.txt is checked before single-page
fetches, spiders run with ``robots_txt_obey=True``, per-domain concurrency
of 1 and a download delay. Proxy support exists but is off by default.
"""

import argparse
import json
import logging
import re
import time
from dataclasses import dataclass, field
from html import unescape as _unescape
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.robotparser import RobotFileParser

logger = logging.getLogger("scrapling_client")

PRODUCT_FIELDS = ("title", "price", "currency", "old_price", "image_urls",
                  "availability", "link", "brand")

# Cheap platform markers (raw-HTML substrings, not selectors).
_PLATFORM_MARKERS = (
    ("shopify", ("cdn/shop", "myshopify.com", "Shopify.shop")),
    ("woocommerce", ("woocommerce", "/wp-json/", "wp-content")),
    ("magento", ("catalogsearch", "static/frontend", "data-price-type")),
)

_BLOCKED_STATUS = {401, 403, 407, 429, 444, 500, 502, 503, 504}
_CHECKPOINT_MARKERS = ("just a moment", "turnstile", "cf-challenge",
                       "attention required", "access denied",
                       "verify you are human")

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_CURRENCY_RE = re.compile(r"(EGP|LE|L\.E\.?|£|\$|€|ج\.?\s?م\.?|جنيه|ريال|درهم)",
                          re.IGNORECASE)
_PRICE_RE = re.compile(r"\d[\d,]*\.?\d*")
# A number with a currency token right beside it - the only numbers we trust
# inside a card. Bare numbers are model names ("64GB", "iPhone 15").
_CURRENCY_PRICE_RE = re.compile(
    r"(EGP|LE|L\.E\.?|£|\$|€|ج\.?\s?م\.?|جنيه|ريال|درهم)\s*(\d[\d,]*\.?\d*)"
    r"|(\d[\d,]*\.?\d*)\s*(EGP|LE|L\.E\.?|£|\$|€|ج\.?\s?م\.?|جنيه|ريال|درهم)",
    re.IGNORECASE)


def _currency_prices(text):
    """All currency-qualified numbers in text, in document order."""
    out = []
    for match in _CURRENCY_PRICE_RE.finditer(text or ""):
        num = match.group(2) or match.group(3) or ""
        value = parse_price(num)
        if value is not None:
            out.append(value)
    return out


@dataclass
class CollectConfig:
    """Knobs for collection. Proxy stays off unless explicitly configured."""
    timeout: int = 30
    retries: int = 3
    retry_delay: float = 1.0
    delay: float = 1.0          # politeness gap between requests (seconds)
    max_pages: int = 3          # catalog-walk page cap for collect_products
    headless: bool = True
    real_chrome: bool = True    # reuse installed Chrome, no browser download
    solve_cloudflare: bool = False
    proxies: list = field(default_factory=list)  # off by default; enables rotation
    proxy: str | None = None    # single proxy override (takes precedence)


def detect_platform(html, url=""):
    """Best-guess storefront platform, or 'unknown'."""
    try:
        blob = "%s\n%s" % (url or "", (html or "")[:60000].lower())
    except Exception:
        return "unknown"
    for platform, markers in _PLATFORM_MARKERS:
        if any(m.lower() in blob for m in markers):
            return platform
    return "unknown"


def robots_allowed(url, user_agent="*"):
    """True when robots.txt permits fetching `url` (fail-open on error)."""
    try:
        parts = urlparse(url)
        robots_url = "%s://%s/robots.txt" % (parts.scheme, parts.netloc)
        rp = RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(user_agent, url)
    except Exception:
        return True


def parse_price(text):
    """First plausible price number in text (Arabic-Indic digits folded)."""
    text = (text or "").translate(_AR_DIGITS).replace("\xa0", " ")
    match = _PRICE_RE.search(text)
    if not match:
        return None
    num = match.group().strip().replace(" ", "").rstrip(".,")
    if "," in num and "." in num:
        num = num.replace(",", "")
    elif "," in num:
        num = num.replace(",", "") if len(num.split(",")[-1]) == 3 \
            else num.replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def detect_currency(text, default="EGP"):
    """Currency hint from surrounding text."""
    match = _CURRENCY_RE.search(text or "")
    if not match:
        return default
    token = match.group(1).upper()
    if token in ("£", "LE", "L.E", "L.E."):
        return "EGP"
    if "ج" in match.group(1) or "جنيه" in match.group(1):
        return "EGP"
    return {"$": "USD", "€": "EUR"}.get(token, default)


# ---------------------------------------------------------------------------
# Fetching (lightest fetcher that works)
# ---------------------------------------------------------------------------

def _proxy_kwargs(config):
    if config.proxies:
        from scrapling.fetchers import ProxyRotator
        return {"proxy_rotator": ProxyRotator(list(config.proxies))}
    if config.proxy:
        return {"proxy": config.proxy}
    return {}


def fetch_page(url, config=None, level="static", **kwargs):
    """Fetch one page. level: 'static' | 'dynamic' | 'stealth'."""
    from scrapling.fetchers import (DynamicFetcher, Fetcher, StealthyFetcher)
    config = config or CollectConfig()
    extra = _proxy_kwargs(config)
    # Adaptive element tracking needs `adaptive` on at parse time; without it
    # `auto_save` on css() calls is silently ignored.
    adaptive = {"selector_config": {"adaptive": True}}
    if level == "static":
        return Fetcher.get(url, timeout=config.timeout, retries=config.retries,
                           retry_delay=config.retry_delay,
                           stealthy_headers=True, **extra, **adaptive,
                           **kwargs)
    if level == "dynamic":
        return DynamicFetcher.fetch(
            url, headless=config.headless, real_chrome=config.real_chrome,
            network_idle=True, timeout=config.timeout * 1000,
            retries=config.retries, retry_delay=config.retry_delay,
            **extra, **adaptive, **kwargs)
    return StealthyFetcher.fetch(
        url, headless=config.headless, real_chrome=config.real_chrome,
        solve_cloudflare=config.solve_cloudflare or True,
        network_idle=True, timeout=max(config.timeout * 1000, 60000),
        retries=config.retries, retry_delay=config.retry_delay,
        **extra, **adaptive, **kwargs)


def looks_blocked(page):
    """Status/marker check for bot walls and rate limits."""
    try:
        if getattr(page, "status", 200) in _BLOCKED_STATUS:
            return True
        body = (getattr(page, "body", b"") or b"").decode("utf-8",
                                                          errors="ignore")
        blob = body[:60000].lower()
        return any(m in blob for m in _CHECKPOINT_MARKERS)
    except Exception:
        return False


def looks_js_shell(page):
    """True when the HTML is a shell whose products hydrate in the browser."""
    try:
        body = (getattr(page, "body", b"") or b"").decode("utf-8",
                                                          errors="ignore")
        return ("__NEXT_DATA__" in body or "__NUXT__" in body
                or "id=\"root\"" in body or "id=\"app\"" in body) \
            and not page.css("img").getall()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Structured JSON endpoints (fastest, most stable - tried before any HTML)
# ---------------------------------------------------------------------------

def _json_rows_shopify_suggest(origin, query, config):
    from scrapling.fetchers import Fetcher
    # Locale-prefixed paths first: unprefixed suggest.json answers 417
    # ("Unsupported buyer locale") on single-locale stores like Dream2000.
    for prefix in ("/en", "", "/ar"):
        url = ("%s%s/search/suggest.json?q=%s"
               "&resources[type]=product&resources[limit]=10"
               % (origin, prefix, quote_plus(query)))
        try:
            page = Fetcher.get(url, timeout=config.timeout,
                               retries=config.retries,
                               retry_delay=config.retry_delay,
                               **_proxy_kwargs(config))
        except Exception:
            logger.debug("shopify suggest failed for %s", url, exc_info=True)
            continue
        if getattr(page, "status", 0) != 200:
            continue
        try:
            data = page.json()
        except Exception:
            continue
        products = ((data.get("resources") or {}).get("results") or {}).get(
            "products") or []
        if products:
            return _map_shopify_products(products, origin)
    return []

def _map_shopify_products(products, origin):
    rows = []
    for product in products:
        title = product.get("title")
        handle = product.get("handle")
        if not title or not handle:
            continue
        after = parse_price(str(product.get("price")))
        before = parse_price(str(product.get("compare_at_price_max")
                                 or product.get("price")))
        if after is None:
            continue
        rows.append({
            "title": _unescape(str(title)).strip(),
            "price": after,
            "currency": "EGP",
            "old_price": before if before and before > after else after,
            "image_urls": [product["image"]] if product.get("image") else [],
            "availability": "in_stock" if product.get("available") else None,
            "link": urljoin(origin, "/products/" + str(handle)),
            "brand": None,
        })
    return rows


def _json_rows_woo_store_api(origin, query, config, page_num=1):
    from scrapling.fetchers import Fetcher
    url = ("%s/wp-json/wc/store/v1/products?search=%s&per_page=20&page=%d"
           % (origin, quote_plus(query), page_num))
    page = Fetcher.get(url, timeout=config.timeout, retries=config.retries,
                       retry_delay=config.retry_delay, **_proxy_kwargs(config))
    items = page.json()
    if not isinstance(items, list):
        return []
    rows = []
    for product in items:
        prices = product.get("prices") or {}
        try:
            minor = int(prices.get("currency_minor_unit") or 0)
        except (TypeError, ValueError):
            minor = 0
        divisor = 10 ** minor
        try:
            after = float(prices.get("price")) / divisor
            regular = float(prices.get("regular_price")) / divisor
        except (TypeError, ValueError):
            continue
        images = product.get("images") or []
        rows.append({
            "title": _unescape(str(product.get("name") or "")).strip(),
            "price": after,
            "currency": prices.get("currency_code") or "EGP",
            "old_price": regular if regular > after else after,
            "image_urls": [images[0].get("src")] if images and images[0].get("src") else [],
            "availability": "in_stock" if product.get("is_in_stock") else "out_of_stock",
            "link": product.get("permalink") or origin,
            "brand": None,
        })
    return rows


def try_json_endpoint(origin, query, platform, config):
    """Structured-data fast path. Returns (rows, method) or ([], None)."""
    if platform == "shopify" or platform == "unknown":
        try:
            rows = _json_rows_shopify_suggest(origin, query, config)
            if len(rows) >= 3:
                return rows, "shopify_suggest"
        except Exception:
            logger.debug("shopify suggest failed for %s", origin,
                         exc_info=True)
    if platform == "woocommerce" or platform == "unknown":
        try:
            rows = _json_rows_woo_store_api(origin, query, config)
            if len(rows) >= 3:
                return rows, "woo_store_api"
        except Exception:
            logger.debug("woo store api failed for %s", origin, exc_info=True)
    return [], None


# ---------------------------------------------------------------------------
# HTML extraction (JSON-LD first, then adaptive card discovery)
# ---------------------------------------------------------------------------

def _jsonld_products(page, base_url):
    """Products from application/ld+json blocks (stable, SEO-driven)."""
    rows = []
    try:
        scripts = page.css('script[type="application/ld+json"]::text').getall()
    except Exception:
        return rows
    for raw in scripts:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        nodes = []
        _walk_jsonld(data, nodes)
        for node in nodes:
            title = node.get("name")
            url = node.get("url")
            price = _offer_price(node.get("offers"))
            if price is None:
                price = node.get("price")
            try:
                price_f = float(str(price).replace(",", "")) \
                    if price is not None else None
            except (TypeError, ValueError):
                price_f = None
            if title and price_f:
                rows.append({
                    "title": _unescape(str(title)).strip(),
                    "price": price_f,
                    "currency": detect_currency(json.dumps(node)),
                    "old_price": price_f,
                    "image_urls": [node["image"]] if node.get("image") and isinstance(node.get("image"), str) else [],
                    "availability": node.get("availability", "").split("/")[-1] or None,
                    "link": urljoin(base_url, str(url)) if url else base_url,
                    "brand": (node.get("brand") or {}).get("name") if isinstance(node.get("brand"), dict) else node.get("brand"),
                })
    seen, unique = set(), []
    for row in rows:
        key = (row["title"], row["link"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def _walk_jsonld(node, out):
    if isinstance(node, dict):
        node_type = node.get("@type", "")
        types = node_type if isinstance(node_type, list) else [node_type]
        if any(t in ("Product", "Offer", "AggregateOffer") for t in types) \
                or ("price" in node or "offers" in node):
            out.append(node)
        for value in node.values():
            _walk_jsonld(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_jsonld(value, out)


def _offer_price(offers):
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list):
        return None
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        for key in ("price", "lowPrice"):
            if offer.get(key) is not None:
                return offer.get(key)
        spec = offer.get("priceSpecification")
        specs = spec if isinstance(spec, list) else [spec]
        for entry in specs:
            if isinstance(entry, dict) and entry.get("price") is not None:
                return entry.get("price")
    return None


def _card_ancestor(element):
    """Largest ancestor holding exactly one distinct product URL.

    Climbing one level too few strands the images in a sibling block;
    climbing into a multi-product container merges cards. A single distinct
    ``/product`` link is the boundary in both directions.
    """
    node = element
    best = None
    for _ in range(10):
        try:
            node = node.parent
        except Exception:
            return best
        if node is None or getattr(node, "tag", "") in ("body", "html"):
            break
        try:
            links = {href.split("?")[0]
                     for href in node.css("a::attr(href)").getall()
                     if href and "/product" in href}
        except Exception:
            continue
        if len(links) == 1:
            best = node
        elif len(links) > 1:
            break
    return best


def _extract_card(card, base_url):
    """One product dict out of a card element. None when unusable."""
    try:
        link = ""
        for href in card.css("a::attr(href)").getall():
            if href and "/product" in href:
                link = urljoin(base_url, href)
                break
        texts = " ".join(card.css("::text").getall())
        candidates = _currency_prices(texts)
        if not candidates:
            # No currency-qualified number: guessing from bare digits turns
            # model names ("64GB", "iPhone 15") into prices. Skip instead.
            return None
        after = candidates[0]
        title = (card.css("h2::text, h3::text").get()
                 or card.css("a::text").get() or "").strip()
        images = [urljoin(base_url, src) for src in
                  card.css("img::attr(src)").getall() if src]
        images += [urljoin(base_url, src) for src in
                   card.css("img::attr(data-src)").getall() if src]
        lowered = texts.lower()
        if any(word in lowered for word in ("out of stock", "sold out",
                                            "غير متوفر", "نفد")):
            availability = "out_of_stock"
        elif any(word in lowered for word in ("in stock", "متوفر", "in_stock",
                                              "add to cart", "أضف")):
            availability = "in_stock"
        else:
            availability = None
        return {
            "title": _unescape(title) or "(no title)",
            "price": after,
            "currency": detect_currency(texts),
            "old_price": after,
            "image_urls": images,
            "availability": availability,
            "link": link or base_url,
            "brand": None,
        }
    except Exception:
        return None


def extract_cards_adaptive(page, base_url):
    """Card discovery that survives layout changes.

    Anchors on price text (regex), climbs to the owning card, then uses
    ``find_similar`` to collect sibling cards regardless of class names.
    The anchor query is saved (``auto_save``) so later ``adaptive=True``
    reads can relocate it after a redesign.
    """
    anchor = page.find_by_regex(
        r"\d[\d,]*\.?\d*\s*(EGP|LE|L\.E\.?|£|\$|€|ج\.?\s?م\.?)",
        first_match=True)
    if anchor is None:
        return []
    if not hasattr(anchor, "parent"):
        anchor = anchor[0]
    card = _card_ancestor(anchor)
    if card is None:
        return []
    try:
        siblings = [card] + list(card.find_similar())
    except Exception:
        siblings = [card]
    rows, seen = [], set()
    for sibling in siblings:
        row = _extract_card(sibling, base_url)
        if row and row["link"] not in seen:
            seen.add(row["link"])
            rows.append(row)
    return rows


def extract_products_from_html(page, base_url, selectors=None):
    """HTML -> product rows. JSON-LD first, caller selectors, then adaptive."""
    rows = _jsonld_products(page, base_url)
    if len(rows) >= 3:
        return rows
    if selectors:
        try:
            cards = page.css(selectors["item"], auto_save=True,
                             identifier="item")
            scraped = []
            title_sel = selectors.get("title", "a")
            if "::" not in title_sel:
                title_sel += "::text"
            link_sel = selectors.get("link", "a")
            if "::attr" not in link_sel:
                link_sel += "::attr(href)"
            img_sel = selectors.get("image", "img")
            for card in cards:
                title = card.css(title_sel).get()
                price_text = " ".join(
                    card.css(selectors.get("price_now", ".price")).css(
                        "::text").getall())
                after = parse_price(price_text)
                if after is None:
                    continue
                link_el = card.css(link_sel).get()
                if "::attr" in img_sel:
                    img_srcs = card.css(img_sel).getall()
                else:
                    img_srcs = [el.attrib.get("src")
                                or el.attrib.get("data-src") or ""
                                for el in card.css(img_sel)]
                scraped.append({
                    "title": (_unescape(title).strip() if title else "(no title)"),
                    "price": after,
                    "currency": detect_currency(price_text),
                    "old_price": after,
                    "image_urls": [urljoin(base_url, src) for src in img_srcs
                                   if src and not src.startswith("data:")],
                    "availability": None,
                    "link": urljoin(base_url, link_el) if link_el else base_url,
                    "brand": None,
                })
            if scraped:
                return scraped
        except Exception:
            logger.debug("caller selectors failed", exc_info=True)
    return extract_cards_adaptive(page, base_url)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

_PLATFORM_SEARCH = {
    "shopify": "/search?q={q}",
    "woocommerce": "/?s={q}&post_type=product",
    "magento": "/catalogsearch/result/?q={q}",
}


def _build_search_url(origin, platform, query):
    """Search-page URL for a bare domain + keyword."""
    pattern = _PLATFORM_SEARCH.get(platform, "/search?q={q}")
    return origin + pattern.replace("{q}", quote_plus(query))


def collect_products(store_url, query=None, config=None, selectors=None,
                     fetcher="auto", max_pages=None):
    """Collect product rows for one store search/listing page.

    :param store_url: store home, search, or category URL.
    :param query: keyword appended for JSON endpoints (``{q}``-style URLs
        are used as-is when they already contain the keyword).
    :param fetcher: 'auto' (escalate static->dynamic->stealth as needed),
        or pin 'static' / 'dynamic' / 'stealth'.
    :returns: list of dicts with PRODUCT_FIELDS keys.
    """
    from scrapling.fetchers import Fetcher
    config = config or CollectConfig()
    if max_pages is not None:
        config.max_pages = max_pages
    parts = urlparse(store_url if "://" in store_url else "https://" + store_url)
    origin = "%s://%s" % (parts.scheme or "https", parts.netloc)
    if not robots_allowed(store_url):
        logger.warning("robots.txt disallows %s", store_url)
        return []

    if query:
        platform_probe = Fetcher.get(origin, timeout=config.timeout,
                                     retries=1,
                                     selector_config={"adaptive": True})
        platform = detect_platform(
            (getattr(platform_probe, "body", b"") or b"").decode(
                "utf-8", errors="ignore"), origin)
        rows, _method = try_json_endpoint(origin, query, platform, config)
        if len(rows) >= 3:
            return rows
        if not parts.query and parts.path in ("", "/"):
            # Bare domain + keyword: point the HTML pass at the search page.
            store_url = _build_search_url(origin, platform, query)
            time.sleep(config.delay)

    levels = {"static": ("static",), "dynamic": ("dynamic",),
              "stealth": ("stealth",)}.get(
        fetcher, ("static", "dynamic", "stealth"))
    last_error = None
    for level in levels:
        try:
            page = fetch_page(store_url, config, level=level)
        except Exception as exc:
            last_error = exc
            logger.debug("%s fetch failed for %s", level, store_url,
                         exc_info=True)
            continue
        if looks_blocked(page):
            last_error = RuntimeError("blocked (%s)" % getattr(
                page, "status", "?"))
            if fetcher == "auto" and level != "stealth":
                time.sleep(config.delay)
                continue
            return []
        rows = extract_products_from_html(page, page.url if getattr(
            page, "url", None) else store_url, selectors)
        if len(rows) >= 3:
            return rows
        if fetcher == "auto" and level == "static" and looks_js_shell(page):
            time.sleep(config.delay)
            continue
        if rows:
            return rows
        time.sleep(config.delay)
    if last_error:
        logger.warning("collect failed for %s: %r", store_url, last_error)
    return []


def crawl_catalog(start_urls, parse_callback=None, out_path="products.json",
                  next_page_css="a.next::attr(href), li.next a::attr(href)",
                  card_css=None, config=None, crawldir="crawl_data/catalog",
                  export_format="json"):
    """Multi-page catalog crawl with pagination, pause/resume and export.

    :param start_urls: catalog/search pages to start from.
    :param parse_callback: ``fn(response) -> dict | None`` per product card;
        defaults to the adaptive card extractor.
    :param out_path: export destination (parent dirs auto-created).
    :param export_format: 'json' | 'jsonl' | 'csv' | 'xml'.
    """
    from scrapling.spiders import Request, Response, Spider
    config = config or CollectConfig()
    if isinstance(start_urls, str):
        start_urls = [start_urls]
    domains = {urlparse(url).netloc for url in start_urls}
    card_selector = card_css
    delay = config.delay

    class CatalogSpider(Spider):
        name = "catalog"
        concurrent_requests = 2
        concurrent_requests_per_domain = 1
        download_delay = delay
        robots_txt_obey = True
        allowed_domains = domains

        async def parse(self, response: Response):
            base = response.url
            if parse_callback is not None:
                item = parse_callback(response)
                if item:
                    yield item
            else:
                for row in extract_products_from_html(response, base):
                    yield row
            if card_selector:
                cards = response.css(card_selector)
                for card in cards:
                    row = _extract_card(card, base)
                    if row:
                        yield row
            next_href = response.css(next_page_css).get()
            if next_href:
                yield response.follow(next_href, callback=self.parse)

    CatalogSpider.start_urls = list(start_urls)
    spider = CatalogSpider(crawldir=crawldir)
    result = spider.start()
    exporters = {"json": "to_json", "jsonl": "to_jsonl", "csv": "to_csv",
                 "xml": "to_xml"}
    getattr(result.items, exporters.get(export_format, "to_json"))(out_path)
    return result


def crawl_shopify_catalog(domain, out_path="products.json", config=None,
                          crawldir="crawl_data/shopify"):
    """Full Shopify catalog via the store JSON API (one item per variant)."""
    from scrapling.spiders import ShopifySpider
    config = config or CollectConfig()

    class StoreSpider(ShopifySpider):
        name = "shopify_store"
        target_website = domain
        concurrent_requests = 2
        concurrent_requests_per_domain = 1
        download_delay = config.delay
        robots_txt_obey = True

    result = StoreSpider(crawldir=crawldir).start()
    result.items.to_json(out_path)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Collect product data from an e-commerce store.")
    parser.add_argument("--url", required=True, help="Store/search/category URL")
    parser.add_argument("--query", default=None, help="Search keyword")
    parser.add_argument("--out", default=None, help="Write rows as JSON here")
    parser.add_argument("--fetcher", default="auto",
                        choices=["auto", "static", "dynamic", "stealth"])
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--crawl", action="store_true",
                        help="Full catalog crawl instead of one page")
    parser.add_argument("--proxies", action="append", default=[],
                        help="Proxy URL (repeatable; off by default)")
    args = parser.parse_args(argv)

    config = CollectConfig(proxies=args.proxies)
    if args.crawl:
        result = crawl_catalog(args.url, out_path=args.out or "products.json",
                               config=config)
        print("crawled %d items -> %s" % (len(result.items),
                                          args.out or "products.json"))
        return
    rows = collect_products(args.url, query=args.query, config=config,
                            fetcher=args.fetcher, max_pages=args.max_pages)
    print("collected %d products" % len(rows))
    for row in rows[:10]:
        print(" - %s | %s %s | %s" % (row["title"][:60], row["price"],
                                      row["currency"], row["link"][:70]))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
        print("wrote %s" % args.out)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
