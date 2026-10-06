"""Multi-method store extraction race.

When the user pastes a store link, every extraction method runs at once and
the first one returning *valid* rows wins; the rest are cancelled on the
spot, including hidden browser windows. The winning method and its settings
(endpoint, selectors, search-URL pattern) are stored with the site entry so
later searches use them directly, with automatic fallback when they fail.

Each method is one function with the same shape:
    run_<id>(ctx, token, report) -> dict | None
taking the race context, a CancelToken checked often, and a thread-safe
report(status, detail) callback. A method never raises out: anything it
cannot handle becomes {"ok": False, ...} and the race goes on.
"""

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

METHODS = (
    ("html_search", "نتائج البحث في HTML"),
    ("structured", "البيانات المنظمة (JSON-LD)"),
    ("platform_api", "API المنصة"),
    ("embedded", "JSON مدفون في الصفحة"),
    ("searchurl", "اكتشاف رابط البحث"),
    ("browser", "متصفح حقيقي"),
    ("xhr", "التقاط طلبات الشبكة"),
    ("sitemap", "خريطة الموقع"),
)

METHOD_TIMEOUTS = {
    "html_search": 100,
    "structured": 70,
    "platform_api": 70,
    "embedded": 70,
    "searchurl": 60,
    "browser": 150,
    "xhr": 150,
    "sitemap": 150,
}

MIN_ROWS = 3
KEYWORD_MIN_RATIO = 0.4


class Cancelled(Exception):
    pass


class CancelToken:
    """Cooperative cancellation, checked between requests - never mid-parse."""

    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    @property
    def cancelled(self):
        return self._event.is_set()

    def check(self):
        if self._event.is_set():
            raise Cancelled()

    def sleep(self, seconds):
        if self._event.wait(timeout=max(0.0, seconds)):
            raise Cancelled()


def _pt():
    import price_tracker
    return price_tracker


def polite_sleep(token, seconds):
    token.sleep(seconds)


def fetch_text(session, url, token, timeout=20):
    """One polite GET returning (html, final_url); raises on block/failure."""
    pt = _pt()
    token.check()
    try:
        resp = session.get(url, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError("تعذّر الوصول (%s)" % e.__class__.__name__)
    if resp.status_code in pt.CHECKPOINT_STATUS:
        raise RuntimeError("المتجر يحجب الاستخراج الآلي (HTTP %s)"
                           % resp.status_code)
    try:
        resp.raise_for_status()
    except requests.RequestException:
        raise RuntimeError("الصفحة ردت بخطأ HTTP %s" % resp.status_code)
    html = resp.text or ""
    if pt.looks_like_checkpoint(html):
        raise RuntimeError("المتجر يحجب الاستخراج الآلي (صفحة تحقق)")
    return html, resp.url


def normalize_url(raw):
    pt = _pt()
    return pt.normalize_store_url(raw)


def valid_rows(rows, keyword):
    """A result counts only with real products matching the test keyword."""
    if not rows or len(rows) < MIN_ROWS:
        return False, "عدد النتائج أقل من %d" % MIN_ROWS
    distinct = {r.get("link") for r in rows if r.get("link")}
    if len(distinct) < MIN_ROWS:
        return False, "كل النتائج تشير لرابط واحد"
    sane = [r for r in rows
            if (r.get("title") and r["title"] != "(no title)")
            and (r.get("after") or 0) > 0
            and (r.get("before") or 0) >= (r.get("after") or 0)]
    if len(sane) < len(rows) * 0.8:
        return False, "أسعار غير مفهومة في معظم النتائج"
    if keyword:
        key = keyword.strip().lower()
        hits = 0
        for r in sane:
            title = (r.get("title") or "").lower()
            link = (r.get("link") or "").lower()
            # The keyword can live in the URL even when titles are
            # transliterated (e.g. "آيفون" vs "iphone", while every product
            # link carries search=iphone).
            if key in title or key in link:
                hits += 1
        if hits < max(1, int(len(sane) * KEYWORD_MIN_RATIO)):
            return False, "النتائج لا تتعلق بكلمة الاختبار"
    return True, ""


def keyword_variants(keyword):
    """The keyword plus a generic fallback when the field is empty."""
    word = (keyword or "").strip()
    if word:
        return [word]
    return ["iphone", "موبايل"]


# ---------------------------------------------------------------------------
# Phase A: search-URL discovery (method "searchurl" reports this)
# ---------------------------------------------------------------------------

OPENSEARCH_NS = {"os": "http://a9.com/-/spec/opensearch/1.1/"}

EXTRA_SEARCH_PATTERNS = (
    # OpenCart (verified live on elshennawy.com).
    "/index.php?route=product/search&search={q}",
    "?route=product/search&search={q}",
    # PrestaShop 1.7+/8.
    "/search?controller=search&s={q}",
    # BigCommerce (Stencil).
    "/search.php?search_query={q}",
    # Salla / Zid style storefronts.
    "/search?q={q}&search_for={q}",
)


def coerce_search_template(url, keyword):
    """A pasted search-results URL becomes a reusable {q} template."""
    if "{q}" in url:
        return url
    parsed = urlparse(url)
    query = parsed.query
    if not query or not keyword:
        return None
    key = keyword.strip()
    if key and quote_plus(key) in url:
        return url.replace(quote_plus(key), "{q}")
    if key and key in url:
        return url.replace(key, "{q}")
    return None


def discover_search_urls(ctx, token, report=None):
    """Real search addresses for this store, best evidence first.

    Reads the home page's own search form, <link rel="search"> / OpenSearch
    descriptors, then platform-first and generic patterns - every candidate is
    fetched once with the test keyword and kept only when the answer looks
    like results. Returns ordered template URLs containing {q}.
    """
    pt = _pt()
    found = []

    def note(message):
        if report is not None:
            report("running", message)

    token.check()
    note("قراءة الصفحة الرئيسية…")
    try:
        home_html, _ = fetch_text(ctx.session, ctx.base, token)
    except Exception as e:
        return [], "تعذّر فتح المتجر: %s" % e
    ctx.home_html = home_html
    ctx.platform = pt.detect_platform(home_html, ctx.base)
    soup = BeautifulSoup(home_html, "html.parser")

    # 1. The store's own search form - the only reliable parameter source.
    note("قراءة فورم البحث…")
    try:
        for pattern in pt.form_search_patterns(ctx.base, home_html, ctx.host):
            url = pt._join(ctx.base, pattern)
            if url not in found:
                found.append(url)
    except Exception:
        pass

    # 2. <link rel="search"> / OpenSearch descriptor.
    note("البحث عن OpenSearch…")
    try:
        for link in soup.find_all("link", rel="search"):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            desc_url = urljoin(ctx.base + "/", href)
            try:
                xml_text, _ = fetch_text(ctx.session, desc_url, token,
                                         timeout=12)
            except Exception:
                continue
            token.sleep(0.4)
            match = re.search(
                r"<Url[^>]*type=\"text/html\"[^>]*template=\"([^\"]+)\"",
                xml_text)
            if match:
                template = match.group(1).replace("{searchTerms}", "{q}")
                if "{q}" in template:
                    full = urljoin(ctx.base + "/", template)
                    if full not in found:
                        found.append(full)
    except Cancelled:
        raise
    except Exception:
        pass

    # 3. Platform-first, then generic patterns.
    patterns = list(pt.PLATFORM_FIRST_PATTERNS.get(ctx.platform, ()))
    patterns.extend(EXTRA_SEARCH_PATTERNS)
    patterns.extend(p for p in pt._SEARCH_URL_PATTERNS if p not in patterns)
    for pattern in patterns:
        token.check()
        url = pt._join(ctx.base, pattern)
        if url in found:
            continue
        found.append(url)
        if len([u for u in found]) >= 14:
            break

    # 4. A pasted results URL is evidence already - try it first.
    pasted = coerce_search_template(ctx.raw_url, ctx.keyword)
    ordered = ([pasted] if pasted else []) + [u for u in found
                                              if u != pasted]
    usable = []
    probe_word = keyword_variants(ctx.keyword)[0]
    for i, template in enumerate(ordered[:12]):
        token.check()
        note("تجربة عنوان البحث (%d/%d)…" % (i + 1, min(12, len(ordered))))
        url = template.replace("{q}", quote_plus(probe_word))
        try:
            html, final = fetch_text(ctx.session, url, token, timeout=18)
        except Exception:
            token.sleep(0.5)
            continue
        token.sleep(0.5)
        low = html.lower()
        signals = sum((
            "product" in low,
            "price" in low or "egp" in low or "جنيه" in low or "ر.س" in low,
            "/product" in low or "/products/" in low,
        ))
        if len(html) > 20000 and signals >= 2:
            usable.append(template.replace("{q}", "{q}"))
        if len(usable) >= 2:
            break
    if not usable:
        return [], "لم يتم العثور على صفحة نتائج"
    ctx.search_urls = usable
    return usable, ""


# ---------------------------------------------------------------------------
# Method runners (all share one context + cancel token)
# ---------------------------------------------------------------------------

class RaceContext:
    def __init__(self, raw_url, base, keyword, session):
        self.raw_url = raw_url
        self.base = base
        parsed = urlparse(base)
        self.origin = "%s://%s" % (parsed.scheme, parsed.netloc)
        self.host = parsed.netloc
        self.name = parsed.netloc.split(":")[0].replace("www.", "") or \
            parsed.netloc
        self.keyword = (keyword or "").strip()
        self.session = session
        self.home_html = ""
        self.platform = "unknown"
        self.search_urls = []


def _paginate_entry(ctx, search_url, html, final_url):
    """Detect the walk style off the result page's own pager (capped)."""
    pt = _pt()
    soup = BeautifulSoup(html, "html.parser")
    try:
        found = pt.detect_pagination(None, search_url, html)
    except Exception:
        found = None
    if found and found > 1:
        cap = min(found, pt._MAX_AUTO_PAGES, 8)
        try:
            template = pt.detect_page_path(soup, final_url)
        except Exception:
            template = None
        if template:
            return {"path": template, "max_pages": cap}
        try:
            param = pt.detect_page_param(soup, final_url) or "page"
        except Exception:
            param = "page"
        return {"param": param, "max_pages": cap}
    return None


def run_html_search(ctx, token, report):
    """Method 1: visible search-result cards parsed with guessed selectors."""
    pt = _pt()
    probe_word = keyword_variants(ctx.keyword)[0]
    tried = 0
    for template in ctx.search_urls:
        token.check()
        report("running", "قراءة صفحة البحث…")
        url = template.replace("{q}", quote_plus(probe_word))
        try:
            html, final_url = fetch_text(ctx.session, url, token, timeout=25)
        except Exception as e:
            report("running", "عنوان لا يجيب، نجرب التالي…")
            token.sleep(0.5)
            continue
        token.sleep(0.5)
        try:
            _site, rows, _score, tried_n = pt._probe_page(
                html, final_url, ctx.base, template, ctx.name, ctx.host)
        except Exception:
            continue
        tried += 1
        ok, reason = valid_rows(rows, probe_word)
        if not ok:
            continue
        # The keyword check: a page ignoring the keyword is not a search page.
        try:
            responds = pt._responds_to_keyword(
                ctx.base, template, ctx.name, ctx.host, len(rows))
        except Exception:
            responds = True
        if not responds:
            report("running", "الصفحة تتجاهل الكلمة، نجرب التالي…")
            continue
        entry = dict(_site)
        entry["search_url"] = template
        entry["name"] = ctx.name
        page_rule = _paginate_entry(ctx, template, html, final_url)
        if page_rule:
            entry["paginate"] = page_rule
            entry["page_delay"] = [0.5, 1.1]
        return {"ok": True, "entry": entry, "rows": rows,
                "count": len(rows)}
    return {"ok": False,
            "reason": "لم تُقرأ بطاقات منتجات من صفحات البحث (%d محاولة)"
                      % tried}


def _rows_from_structured(soup, final_url):
    pt = _pt()
    rows = []
    for func in (pt.extract_jsonld_products, pt.extract_next_data_products):
        try:
            found = func(soup, final_url)
        except Exception:
            continue
        for p in found:
            rows.append({"title": p.get("title", ""),
                         "before": p.get("price"), "after": p.get("price"),
                         "link": p.get("link", ""), "image": ""})
    seen, uniq = set(), []
    for r in rows:
        key = (r["title"], r["link"])
        if key not in seen and r["title"] and r["after"]:
            seen.add(key)
            uniq.append(r)
    return uniq


def _microdata_products(soup, final_url):
    """schema.org microdata (itemscope) product offers."""
    rows = []
    for scope in soup.find_all(attrs={"itemscope": True}):
        itemtype = (scope.get("itemtype") or "").lower()
        if "product" not in itemtype and "offer" not in itemtype:
            continue
        name_el = scope.find(attrs={"itemprop": "name"})
        price_el = scope.find(attrs={"itemprop": "price"})
        url_el = scope.find("a", href=True)
        if not name_el or not price_el:
            continue
        try:
            price = float(str(price_el.get("content")
                              or price_el.get_text()).replace(",", ""))
        except (TypeError, ValueError):
            continue
        link = urljoin(final_url, url_el["href"]) if url_el else final_url
        rows.append({"title": name_el.get_text(strip=True),
                     "before": price, "after": price,
                     "link": link, "image": ""})
    return rows


def _og_product(soup, final_url):
    """Open Graph product tags (single-product pages)."""
    def meta(prop):
        tag = soup.find("meta", property=prop)
        return (tag.get("content") or "").strip() if tag else ""

    title = meta("og:title")
    price = meta("product:price:amount")
    if not title or not price:
        return []
    try:
        price_f = float(price.replace(",", ""))
    except ValueError:
        return []
    image = meta("og:image")
    url = meta("og:url") or final_url
    return [{"title": title, "before": price_f, "after": price_f,
             "link": urljoin(final_url, url), "image": image}]


def run_structured(ctx, token, report):
    """Method 2: JSON-LD / microdata / Open Graph inside the page."""
    probe_word = keyword_variants(ctx.keyword)[0]
    for template in ctx.search_urls:
        token.check()
        report("running", "قراءة البيانات المنظمة…")
        url = template.replace("{q}", quote_plus(probe_word))
        try:
            html, final_url = fetch_text(ctx.session, url, token, timeout=25)
        except Exception:
            token.sleep(0.5)
            continue
        token.sleep(0.5)
        soup = BeautifulSoup(html, "html.parser")
        rows = _rows_from_structured(soup, final_url)
        if len(rows) < MIN_ROWS:
            rows = _microdata_products(soup, final_url)
        if len(rows) < MIN_ROWS:
            rows = _og_product(soup, final_url)
        ok, _reason = valid_rows(rows, probe_word)
        if not ok:
            continue
        entry = {"name": ctx.name, "search_url": template,
                 "method": "structured"}
        return {"ok": True, "entry": entry, "rows": rows,
                "count": len(rows)}
    return {"ok": False, "reason": "لا توجد بيانات منظمة كافية في الصفحات"}


def _shopify_suggest_rows(session, origin, keyword, token):
    rows = []
    for prefix in ("/en", "", "/ar"):
        token.check()
        url = ("%s%s/search/suggest.json?q=%s"
               "&resources[type]=product&resources[limit]=10"
               % (origin, prefix, quote_plus(keyword)))
        try:
            html, _ = fetch_text(session, url, token, timeout=18)
        except Exception:
            token.sleep(0.4)
            continue
        token.sleep(0.4)
        try:
            data = json.loads(html)
        except ValueError:
            continue
        try:
            products = ((data.get("resources") or {}).get("results")
                        or {}).get("products") or []
        except AttributeError:
            continue
        if not products:
            continue
        for p in products:
            title = p.get("title")
            handle = p.get("handle")
            if not title or not handle:
                continue
            try:
                after = float(str(p.get("price")).replace(",", ""))
            except (TypeError, ValueError):
                continue
            try:
                before = float(str(p.get("compare_at_price_max")
                                   or p.get("price")).replace(",", ""))
            except (TypeError, ValueError):
                before = after
            rows.append({"title": str(title).strip(),
                         "before": before if before > after else after,
                         "after": after,
                         "link": urljoin(origin, "/products/" + str(handle)),
                         "image": p.get("image") or ""})
        if rows:
            return rows, prefix or "/"
    return rows, ""


def _shopify_products_rows(session, origin, keyword, token):
    """Full /products.json with client-side keyword filter."""
    token.check()
    url = origin + "/products.json?limit=250"
    try:
        html, _ = fetch_text(session, url, token, timeout=25)
    except Exception as e:
        return [], "تعذّر قراءة products.json"
    try:
        data = json.loads(html)
    except ValueError:
        return [], "رد غير متوقع من products.json"
    products = data.get("products") if isinstance(data, dict) else None
    if not products:
        return [], "لا منتجات في products.json"
    key = keyword.lower()
    rows = []
    for p in products:
        title = str(p.get("title") or "")
        if key and key not in title.lower():
            continue
        variants = p.get("variants") or []
        variant = variants[0] if variants else {}
        try:
            after = float(str(variant.get("price")).replace(",", ""))
        except (TypeError, ValueError):
            continue
        images = p.get("images") or []
        rows.append({"title": title.strip(), "before": after, "after": after,
                     "link": urljoin(origin, "/products/"
                                     + str(p.get("handle") or "")),
                     "image": (images[0].get("src") if images else "") or ""})
    if not rows:
        return [], "لا منتجات مطابقة للكلمة"
    return rows, ""


def _woo_store_rows(session, origin, keyword, token):
    rows = []
    for page_num in (1, 2):
        token.check()
        url = ("%s/wp-json/wc/store/v1/products?search=%s&per_page=20&page=%d"
               % (origin, quote_plus(keyword), page_num))
        try:
            html, _ = fetch_text(session, url, token, timeout=18)
        except Exception:
            break
        token.sleep(0.4)
        try:
            items = json.loads(html)
        except ValueError:
            break
        if not isinstance(items, list) or not items:
            break
        for p in items:
            prices = p.get("prices") or {}
            try:
                minor = int(prices.get("currency_minor_unit") or 0)
                after = float(prices.get("price")) / (10 ** minor)
                regular = float(prices.get("regular_price")) / (10 ** minor)
            except (TypeError, ValueError):
                continue
            images = p.get("images") or []
            title = re.sub(r"&#?\w+;", "", str(p.get("name") or "")).strip()
            rows.append({"title": title, "before": regular
                         if regular > after else after, "after": after,
                         "link": p.get("permalink") or origin,
                         "image": (images[0].get("src") if images else "")
                         or ""})
    return rows


def _magento_graphql_rows(session, origin, keyword, token):
    token.check()
    query = {"query": "query($s: String!) { products(search: $s, pageSize: 12)"
                      " { items { name sku url_key url_suffix price_range {"
                      " minimum_price { final_price { value currency } } } } } }",
             "variables": {"s": keyword}}
    try:
        token.check()
        resp = session.post(origin + "/graphql", json=query, timeout=25)
    except requests.RequestException:
        return []
    if resp.status_code != 200:
        return []
    try:
        items = resp.json()["data"]["products"]["items"]
    except (ValueError, KeyError, TypeError):
        return []
    rows = []
    for item in items or []:
        try:
            final = item["price_range"]["minimum_price"]["final_price"]
            after = float(final["value"])
        except (KeyError, TypeError, ValueError):
            continue
        slug = item.get("url_key") or ""
        suffix = item.get("url_suffix") or ".html"
        rows.append({"title": str(item.get("name") or "").strip(),
                     "before": after, "after": after,
                     "link": urljoin(origin + "/", slug + suffix
                                     if slug else ""),
                     "image": ""})
    return rows


def run_platform_api(ctx, token, report):
    """Method 3: the platform's own JSON API (no HTML parsing)."""
    keyword = keyword_variants(ctx.keyword)[0]
    platform = ctx.platform
    report("running", "فحص منصة المتجر (%s)…" % platform)
    tried = []
    if platform in ("shopify", "unknown"):
        rows, _prefix = _shopify_suggest_rows(ctx.session, ctx.origin,
                                              keyword, token)
        tried.append("suggest")
        ok, _r = valid_rows(rows, keyword)
        if ok:
            entry = {"name": ctx.name,
                     "search_url": ctx.origin + "/search?q={q}",
                     "method": "api", "api": "shopify_suggest",
                     "api_url": ctx.origin
                     + "/search/suggest.json?q={q}"
                       "&resources[type]=product&resources[limit]=10"}
            return {"ok": True, "entry": entry, "rows": rows,
                    "count": len(rows)}
        rows2, _r2 = _shopify_products_rows(ctx.session, ctx.origin,
                                           keyword, token)
        tried.append("products.json")
        ok, _r = valid_rows(rows2, keyword)
        if ok:
            entry = {"name": ctx.name,
                     "search_url": ctx.origin + "/search?q={q}",
                     "method": "api", "api": "shopify_products",
                     "api_url": ctx.origin + "/products.json?limit=250"}
            return {"ok": True, "entry": entry, "rows": rows2,
                    "count": len(rows2)}
    if platform in ("woocommerce", "unknown") or "wp-" in (ctx.home_html
                                                           or "")[:20000]:
        token.check()
        rows = _woo_store_rows(ctx.session, ctx.origin, keyword, token)
        tried.append("wc-store-api")
        ok, _r = valid_rows(rows, keyword)
        if ok:
            entry = {"name": ctx.name,
                     "search_url": ctx.origin + "/?s={q}&post_type=product",
                     "method": "api", "api": "woo_store_api",
                     "api_url": ctx.origin
                     + "/wp-json/wc/store/v1/products"
                       "?search={q}&per_page=20&page={page}"}
            return {"ok": True, "entry": entry, "rows": rows,
                    "count": len(rows)}
    if platform in ("magento", "unknown"):
        token.check()
        rows = _magento_graphql_rows(ctx.session, ctx.origin, keyword, token)
        tried.append("graphql")
        ok, _r = valid_rows(rows, keyword)
        if ok:
            entry = {"name": ctx.name,
                     "search_url": ctx.origin
                     + "/catalogsearch/result/?q={q}",
                     "method": "api", "api": "magento_graphql",
                     "api_url": ctx.origin + "/graphql"}
            return {"ok": True, "entry": entry, "rows": rows,
                    "count": len(rows)}
    return {"ok": False, "reason": "لا API معروف لمنصة %s" % platform
            if not tried else "لا API متاح (%s)" % ", ".join(tried)}


def _walk_state(node, out):
    if isinstance(node, dict):
        keys = set(node.keys())
        if (("name" in keys or "title" in keys)
                and ("price" in keys or "finalPrice" in keys
                     or "salePrice" in keys or "regularPrice" in keys
                     or "amount" in keys)
                and ("url" in keys or "slug" in keys or "handle" in keys
                     or "link" in keys or "id" in keys)):
            out.append(node)
        for value in node.values():
            _walk_state(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_state(value, out)


def _embedded_products(soup, final_url):
    payloads = []
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag:
        raw = tag.string or tag.get_text() or ""
        if raw.strip():
            payloads.append(raw.strip())
    for tag in soup.find_all("script"):
        raw = tag.string or tag.get_text() or ""
        raw = (raw or "").strip()
        if not raw or len(raw) > 2000000:
            continue
        if raw.startswith("window.__NUXT__"):
            payloads.append(raw.split("=", 1)[-1].rstrip(" ;"))
        elif raw.startswith("window.__INITIAL_STATE__"):
            payloads.append(raw.split("=", 1)[-1].rstrip(" ;"))
        elif (tag.get("type") == "application/json"
                and ("price" in raw[:2000] or "Price" in raw[:2000])):
            payloads.append(raw)
    found = []
    for raw in payloads:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        nodes = []
        _walk_state(data, nodes)
        for node in nodes:
            title = node.get("name") or node.get("title")
            url = (node.get("url") or node.get("link")
                   or ("/products/" + str(node["handle"])
                       if node.get("handle") else None)
                   or ("/product/" + str(node["slug"])
                       if node.get("slug") else None))
            price = (node.get("price") or node.get("finalPrice")
                     or node.get("salePrice") or node.get("regularPrice")
                     or node.get("amount"))
            if isinstance(price, dict):
                price = price.get("value", price.get("amount"))
            try:
                price_f = float(str(price).replace(",", "")) \
                    if price is not None else None
            except (TypeError, ValueError):
                price_f = None
            if title and price_f and price_f > 0:
                found.append({"title": str(title).strip(),
                              "before": price_f, "after": price_f,
                              "link": urljoin(final_url, str(url))
                              if url else final_url, "image": ""})
    seen, uniq = set(), []
    for row in found:
        key = (row["title"], row["link"])
        if key not in seen:
            seen.add(key)
            uniq.append(row)
    return uniq


def run_embedded(ctx, token, report):
    """Method 4: JSON buried in the page (Next/Nuxt/initial-state)."""
    probe_word = keyword_variants(ctx.keyword)[0]
    for template in ctx.search_urls:
        token.check()
        report("running", "قراءة JSON المدفون…")
        url = template.replace("{q}", quote_plus(probe_word))
        try:
            html, final_url = fetch_text(ctx.session, url, token, timeout=25)
        except Exception:
            token.sleep(0.5)
            continue
        token.sleep(0.5)
        rows = _embedded_products(BeautifulSoup(html, "html.parser"),
                                  final_url)
        ok, _reason = valid_rows(rows, probe_word)
        if not ok:
            continue
        entry = {"name": ctx.name, "search_url": template,
                 "method": "embedded"}
        return {"ok": True, "entry": entry, "rows": rows,
                "count": len(rows)}
    return {"ok": False, "reason": "لا JSON منتجات مدفون في الصفحات"}


def run_searchurl(ctx, token, report):
    """Method 5: the discovery phase itself, shown as its own row."""
    report("running", "اكتشاف عناوين البحث…")
    urls, reason = discover_search_urls(ctx, token, report=report)
    if not urls:
        return {"ok": False, "reason": reason}
    return {"ok": True, "entry": None, "rows": [],
            "count": len(urls), "urls": urls}


# ---------------------------------------------------------------------------
# Hidden WebView2 window (methods 6 and 7 share one window per race)
# ---------------------------------------------------------------------------

XHR_HOOK_JS = """
(function () {
  if (window.__raceXhrInstalled) return;
  window.__raceXhrInstalled = true;
  window.__raceXhrCaught = [];
  function keep(url, body) {
    try {
      if (typeof body !== "string") return;
      if (body.length < 200 || body.length > 1500000) return;
      if (window.__raceXhrCaught.length >= 40) return;
      window.__raceXhrCaught.push({url: String(url).slice(0, 500),
                                  body: body});
    } catch (e) {}
  }
  var open_fetch = window.fetch;
  window.fetch = function (url, opts) {
    return open_fetch.apply(this, arguments).then(function (resp) {
      try {
        var clone = resp.clone();
        clone.text().then(function (text) { keep(url, text); });
      } catch (e) {}
      return resp;
    });
  };
  var open_send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__raceUrl = url;
    return XMLHttpRequest.prototype.open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    var self = this;
    this.addEventListener("load", function () {
      try { keep(self.__raceUrl, self.responseText); } catch (e) {}
    });
    return open_send.apply(this, arguments);
  };
})();
"""


class WebViewPool:
    """One hidden WebView2 window shared by the browser-based methods."""

    def __init__(self):
        self._lock = threading.Lock()
        self._window = None
        self._users = 0

    def acquire(self, token):
        import webview
        token.check()
        with self._lock:
            if self._window is None:
                try:
                    self._window = webview.create_window(
                        "فحص متجر", hidden=True, width=1280, height=800)
                except Exception as e:
                    raise RuntimeError("تعذّر فتح نافذة المتصفح (%s)"
                                       % e.__class__.__name__)
            self._users += 1
            return self._window

    def release(self):
        with self._lock:
            self._users = max(0, self._users - 1)

    def destroy(self):
        with self._lock:
            window, self._window = self._window, None
            self._users = 0
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass


def _webview_eval(window, token, js, timeout=20):
    token.check()
    box = {}
    def run():
        try:
            box["value"] = window.evaluate_js(js)
        except Exception as e:
            box["error"] = e
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise RuntimeError("المتصفح لا يرد")
    if "error" in box:
        raise RuntimeError("تعذّر قراءة الصفحة")
    return box.get("value")


def webview_load(window, token, url, settle_sec=6, timeout=60):
    """Navigate the shared window and wait for JS to settle."""
    token.check()
    try:
        window.load_url(url)
    except Exception:
        raise RuntimeError("تعذّر تحميل الصفحة في المتصفح")
    waited = 0.0
    last_len = -1
    stable = 0
    while waited < timeout:
        token.check()
        try:
            state = window.evaluate_js(
                "JSON.stringify({ready: document.readyState, "
                "len: document.documentElement.outerHTML.length})")
            info = json.loads(state or "{}")
        except Exception:
            token.sleep(1.0)
            waited += 1.0
            continue
        if info.get("ready") == "complete":
            if info.get("len") == last_len:
                stable += 1
            else:
                stable = 0
            last_len = info.get("len")
            if stable >= 2 and waited >= settle_sec:
                return
        token.sleep(1.0)
        waited += 1.0
    raise RuntimeError("الصفحة لم تستقر (انتهت المهلة)")


def _checkpoint_in_dom(html):
    pt = _pt()
    try:
        return pt.looks_like_checkpoint(html)
    except Exception:
        return False


def _window_alive(window, token):
    """Fast fail when no GUI loop drives the window (headless test runs)."""
    try:
        _webview_eval(window, token, "1", timeout=12)
        return True
    except Exception:
        return False


def run_browser(ctx, token, report, pool):
    """Method 6: real browser renders the search page, DOM is parsed."""
    pt = _pt()
    probe_word = keyword_variants(ctx.keyword)[0]
    try:
        window = pool.acquire(token)
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    if not _window_alive(window, token):
        pool.release()
        return {"ok": False,
                "reason": "نافذة المتصفح لا تستجيب (يعمل داخل البرنامج فقط)"}
    try:
        for template in ctx.search_urls:
            token.check()
            report("running", "تحميل صفحة البحث في المتصفح…")
            url = template.replace("{q}", quote_plus(probe_word))
            try:
                webview_load(window, token, url)
                dom = _webview_eval(
                    window, token,
                    "document.documentElement.outerHTML", timeout=25)
            except Exception as e:
                report("running", "صفحة لم تتحمل، نجرب التالي…")
                continue
            if not dom or len(dom) < 5000:
                continue
            if _checkpoint_in_dom(dom):
                report("running",
                       "المتصفح ظاهر الآن - حل التحقق بيدك والفحص سيكمل…")
                try:
                    window.show()
                except Exception:
                    pass
                try:
                    webview_load(window, token, url, timeout=120)
                    dom = _webview_eval(
                        window, token,
                        "document.documentElement.outerHTML", timeout=25)
                except Exception as e:
                    return {"ok": False, "reason": str(e)}
                finally:
                    try:
                        window.hide()
                    except Exception:
                        pass
                if _checkpoint_in_dom(dom or ""):
                    continue
            soup = BeautifulSoup(dom, "html.parser")
            found_site = None
            try:
                found_site, rows, _score, _t = pt._probe_page(
                    dom, url, ctx.base, template, ctx.name, ctx.host)
            except Exception:
                rows = []
            ok, _reason = valid_rows(rows, probe_word)
            if not ok:
                rows = _rows_from_structured(soup, url)
                ok, _reason = valid_rows(rows, probe_word)
            if not ok:
                continue
            entry = dict(found_site) if found_site else {}
            entry.update({"search_url": template, "name": ctx.name,
                          "use_webview": True})
            if not entry.get("item"):
                entry["method"] = "structured"
            page_rule = _paginate_entry(ctx, template, dom, url)
            if page_rule:
                entry["paginate"] = page_rule
                entry["page_delay"] = [0.5, 1.1]
            return {"ok": True, "entry": entry, "rows": rows,
                    "count": len(rows)}
        return {"ok": False, "reason": "المتصفح لم يجد بطاقات منتجات"}
    finally:
        pool.release()


def _product_dicts(node, out):
    if isinstance(node, dict):
        keys = set(node.keys())
        title = node.get("title") or node.get("name")
        price = (node.get("price") or node.get("final_price")
                 or node.get("sale_price") or node.get("regular_price"))
        link = (node.get("url") or node.get("link") or node.get("permalink")
                or node.get("handle") or node.get("slug"))
        if title and price is not None and link:
            out.append(node)
        for value in node.values():
            _product_dicts(value, out)
    elif isinstance(node, list):
        for value in node:
            _product_dicts(value, out)
        if len(node) >= 3 and all(isinstance(v, dict) for v in node):
            titles = sum(1 for v in node if v.get("title") or v.get("name"))
            prices = sum(1 for v in node if v.get("price") is not None)
            if titles >= 3 and prices >= 3:
                out.append({"__list__": node})


def _rows_from_captured(captured, keyword):
    rows = []
    for item in captured:
        body = item.get("body") or ""
        try:
            data = json.loads(body)
        except ValueError:
            continue
        dicts = []
        _product_dicts(data, dicts)
        for node in dicts:
            if "__list__" in node:
                for entry in node["__list__"]:
                    one = []
                    _product_dicts(entry, one)
                    for single in one:
                        rows.append((single, item.get("url", "")))
            else:
                rows.append((node, item.get("url", "")))
    normalized = []
    for node, endpoint in rows:
        title = str(node.get("title") or node.get("name") or "").strip()
        price = node.get("price")
        if isinstance(price, dict):
            price = price.get("value", price.get("amount"))
        try:
            after = float(str(price).replace(",", ""))
        except (TypeError, ValueError):
            continue
        link = node.get("url") or node.get("link") or node.get("permalink")
        if isinstance(link, str) and link.startswith("/"):
            link = link
        if not title or not link or after <= 0:
            continue
        normalized.append({"title": title, "before": after, "after": after,
                           "link": str(link),
                           "image": node.get("image") or "",
                           "_endpoint": endpoint})
    seen, uniq = set(), []
    for row in normalized:
        key = (row["title"], row["link"])
        if key not in seen:
            seen.add(key)
            uniq.append(row)
    return uniq


def run_xhr(ctx, token, report, pool):
    """Method 7: capture the page's own JSON (fetch/XHR) in the browser."""
    probe_word = keyword_variants(ctx.keyword)[0]
    try:
        window = pool.acquire(token)
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    if not _window_alive(window, token):
        pool.release()
        return {"ok": False,
                "reason": "نافذة المتصفح لا تستجيب (يعمل داخل البرنامج فقط)"}
    try:
        for template in ctx.search_urls[:3]:
            token.check()
            report("running", "مراقبة طلبات الشبكة…")
            url = template.replace("{q}", quote_plus(probe_word))
            try:
                webview_load(window, token, url, timeout=45)
                _webview_eval(window, token, XHR_HOOK_JS, timeout=15)
                token.sleep(10)
                raw = _webview_eval(
                    window, token,
                    "JSON.stringify(window.__raceXhrCaught || [])",
                    timeout=20)
            except Exception:
                continue
            try:
                captured = json.loads(raw or "[]")
            except ValueError:
                captured = []
            if not captured:
                continue
            rows = _rows_from_captured(captured, probe_word)
            ok, _reason = valid_rows(
                [{"title": r["title"], "before": r["after"],
                  "after": r["after"], "link": r["link"],
                  "image": r["image"]} for r in rows], probe_word)
            if not ok:
                continue
            endpoint = rows[0].pop("_endpoint", "")
            generalized = endpoint.replace(quote_plus(probe_word), "{q}")
            if "{q}" not in generalized:
                generalized = endpoint.split("?")[0] + "?q={q}"
            entry = {"name": ctx.name, "search_url": template,
                     "method": "api", "api": "captured",
                     "api_url": generalized}
            clean = []
            for r in rows:
                r = dict(r)
                r.pop("_endpoint", None)
                clean.append(r)
            return {"ok": True, "entry": entry, "rows": clean,
                    "count": len(clean)}
        return {"ok": False, "reason": "لا JSON منتجات في طلبات الشبكة"}
    finally:
        pool.release()


def _sitemap_locs(session, sitemap_url, token, depth=0):
    try:
        html, _ = fetch_text(session, sitemap_url, token, timeout=18)
    except Exception:
        return []
    locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", html)
    if depth < 2 and locs and ("sitemap" in locs[0]
                               or locs[0].strip().endswith(".xml")):
        nested = []
        for loc in locs[:10]:
            token.check()
            nested.extend(_sitemap_locs(session, loc.strip(), token,
                                        depth + 1))
            token.sleep(0.3)
        return nested
    return [loc.strip() for loc in locs]


def run_sitemap(ctx, token, report):
    """Method 8 (last resort): product links out of sitemap.xml."""
    pt = _pt()
    keyword = keyword_variants(ctx.keyword)[0]
    report("running", "قراءة خريطة الموقع…")
    candidates = [ctx.origin + "/sitemap.xml",
                  ctx.origin + "/sitemap_index.xml",
                  ctx.origin + "/sitemap-index.xml"]
    try:
        robots, _ = fetch_text(ctx.session, ctx.origin + "/robots.txt",
                               token, timeout=12)
        for match in re.findall(r"(?im)^sitemap:\s*(\S+)", robots):
            if match not in candidates:
                candidates.append(match)
    except Exception:
        pass
    locs = []
    for url in candidates[:3]:
        token.check()
        locs = _sitemap_locs(ctx.session, url, token)
        if locs:
            break
        token.sleep(0.4)
    if not locs:
        return {"ok": False, "reason": "لا خريطة موقع متاحة"}
    key = keyword.lower()
    product_locs = [u for u in locs if "/product" in u.lower()
                    or "/products/" in u.lower() or "/p/" in u.lower()
                    or "product" in u.lower()]
    pool_urls = product_locs or locs
    matching = [u for u in pool_urls if key in u.lower()]
    targets = (matching or pool_urls)[:10]
    rows = []
    for i, product_url in enumerate(targets):
        token.check()
        report("running", "قراءة صفحة منتج (%d/%d)…"
               % (i + 1, len(targets)))
        try:
            html, final_url = fetch_text(ctx.session, product_url, token,
                                         timeout=18)
        except Exception:
            token.sleep(0.4)
            continue
        token.sleep(0.4)
        soup = BeautifulSoup(html, "html.parser")
        page_rows = _rows_from_structured(soup, final_url)
        if not page_rows:
            page_rows = _microdata_products(soup, final_url)
        rows.extend(page_rows)
        if len(rows) >= 12:
            break
    ok, reason = valid_rows(rows, "" if not matching else keyword)
    if not ok:
        return {"ok": False,
                "reason": "صفحات المنتجات بلا أسعار (%s)" % reason}
    entry = {"name": ctx.name, "search_url": targets[0],
             "method": "sitemap", "sitemap_url": candidates[0],
             "keyword_hint": keyword}
    return {"ok": True, "entry": entry, "rows": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Race orchestration
# ---------------------------------------------------------------------------

RUNNERS = {
    "html_search": run_html_search,
    "structured": run_structured,
    "platform_api": run_platform_api,
    "embedded": run_embedded,
    "searchurl": run_searchurl,
    "browser": run_browser,
    "xhr": run_xhr,
    "sitemap": run_sitemap,
}


class Race:
    """One add-store race: discovery, then every method at once."""

    def __init__(self, raw_url, keyword):
        self.raw_url = raw_url
        self.keyword = keyword
        self.token = CancelToken()
        self.pool = WebViewPool()
        self.lock = threading.Lock()
        self.methods = {mid: {"id": mid, "name": name, "state": "waiting",
                              "detail": "", "count": 0, "ms": 0}
                        for mid, name in METHODS}
        self.message = "جاري البدء…"
        self.winner = None
        self.done = threading.Event()

    def report(self, method_id):
        def update(state, detail="", count=0):
            with self.lock:
                row = self.methods.get(method_id)
                if row is not None and row["state"] not in ("won", "lost"):
                    row["state"] = state
                    row["detail"] = detail
                    if count:
                        row["count"] = count
        return update

    def set_message(self, message):
        with self.lock:
            self.message = message

    def snapshot(self):
        with self.lock:
            return {"message": self.message,
                    "methods": [dict(self.methods[mid]) for mid, _ in METHODS],
                    "winner": dict(self.winner) if self.winner else None}

    def _run_method(self, method_id, ctx, extra):
        t0 = time.perf_counter()
        report = self.report(method_id)
        try:
            if method_id == "searchurl":
                result = run_searchurl(ctx, self.token, report)
            elif method_id in ("browser", "xhr"):
                result = RUNNERS[method_id](ctx, self.token, report,
                                            self.pool)
            else:
                result = RUNNERS[method_id](ctx, self.token, report)
        except Cancelled:
            with self.lock:
                row = self.methods.get(method_id)
                if row is not None and row["state"] not in ("won", "lost"):
                    row["state"] = "lost"
                    row["detail"] = "أُلغيت بعد فوز طريقة أخرى"
            return None
        except Exception as e:
            logging.exception("race method %s crashed", method_id)
            result = {"ok": False, "reason": "عطل داخلي"}
        ms = int((time.perf_counter() - t0) * 1000)
        with self.lock:
            self.methods[method_id]["ms"] = ms
            if result is None:
                self.methods[method_id]["state"] = "lost"
                return None
            if result.get("ok") and method_id != "searchurl":
                if self.winner is None:
                    self.winner = {"method": method_id,
                                   "name": dict(METHODS)[method_id],
                                   "entry": result.get("entry"),
                                   "rows": result.get("rows", []),
                                   "count": result.get("count", 0),
                                   "ms": ms}
                    self.methods[method_id]["state"] = "won"
                    self.methods[method_id]["count"] = result.get(
                        "count", 0)
                    self.token.cancel()
                    return result
                self.methods[method_id]["state"] = "lost"
                self.methods[method_id]["detail"] = "طريقة أخرى فازت أولًا"
                return None
            if result.get("ok"):
                self.methods[method_id]["state"] = "done"
                self.methods[method_id]["count"] = result.get("count", 0)
                self.methods[method_id]["detail"] = "تم"
            else:
                self.methods[method_id]["state"] = "failed"
                self.methods[method_id]["detail"] = result.get(
                    "reason", "فشلت")
            return None

    def run(self, single=None):
        """Full race (single=None) or one method alone for retry."""
        pt = _pt()
        base = normalize_url(self.raw_url)
        if not base:
            self.set_message("الرابط غير صالح. اكتب رابط المتجر كاملًا.")
            self.done.set()
            return {"ok": False, "message": self.message}
        try:
            session = pt.get_session("race")
        except Exception:
            session = requests.Session()
            session.headers.update(pt.HEADERS)
        ctx = RaceContext(self.raw_url, base, self.keyword, session)
        # Phase A: discovery first - every method needs real search URLs.
        self.set_message("اكتشاف عناوين البحث…")
        try:
            urls, reason = discover_search_urls(
                ctx, self.token, report=self.report("searchurl"))
        except Cancelled:
            self.done.set()
            return {"ok": False, "message": "تم الإلغاء."}
        with self.lock:
            if urls:
                self.methods["searchurl"]["state"] = "done"
                self.methods["searchurl"]["detail"] = \
                    "%d عناوين (%s…)" % (len(urls), ctx.platform)
                self.methods["searchurl"]["count"] = len(urls)
            else:
                self.methods["searchurl"]["state"] = "failed"
                self.methods["searchurl"]["detail"] = reason
        if not urls:
            ctx.search_urls = []
        if single == "searchurl":
            self.done.set()
            return {"ok": bool(urls), "message": reason or "تم"}

        racers = [mid for mid, _ in METHODS if mid != "searchurl"]
        if single is not None:
            racers = [single] if single in RUNNERS else []
        self.set_message("تجربة طرق الاستخراج…")
        with ThreadPoolExecutor(max_workers=len(racers),
                               thread_name_prefix="race") as pool:
            futures = {pool.submit(self._run_method, mid, ctx, None): mid
                       for mid in racers}
            deadline = time.perf_counter() + max(
                METHOD_TIMEOUTS.get(mid, 90) for mid in racers)
            while futures:
                if self.token.cancelled and self.winner is not None:
                    break
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                finished, _ = wait(futures, timeout=min(1.0, remaining),
                                   return_when=FIRST_COMPLETED)
                for future in finished:
                    del futures[future]
                    try:
                        future.result(timeout=0)
                    except Exception:
                        logging.exception("race future crashed")
                if self.winner is not None:
                    break
            self.token.cancel()
            for future in list(futures):
                future.cancel()
        self.pool.destroy()
        self.done.set()
        if self.winner is not None:
            return {"ok": True, "winner": self.winner}
        reasons = "; ".join(
            "%s: %s" % (dict(METHODS)[mid], self.methods[mid]["detail"])
            for mid, _ in METHODS if self.methods[mid]["state"] == "failed")
        return {"ok": False, "message": reasons or "كل الطرق فشلت"}


# ---------------------------------------------------------------------------
# Saved-method searches (later refreshes reuse the winner directly)
# ---------------------------------------------------------------------------

def _row(site_name, title, before, after, link, image):
    before = before if before and before >= after else after
    discount = round((before - after) / before * 100, 2) if before else 0.0
    return {"site": site_name, "title": title or "(no title)",
            "before": round(before, 2), "after": round(after, 2),
            "discount": max(discount, 0.0), "link": link, "image": image or "",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def _fetch_api_rows(site, query, token, session):
    kind = site.get("api")
    parsed = urlparse(site.get("api_url") or site.get("search_url") or "")
    origin = "%s://%s" % (parsed.scheme or "https", parsed.netloc)
    if kind == "shopify_suggest":
        rows, _p = _shopify_suggest_rows(session, origin, query, token)
        return rows
    if kind == "shopify_products":
        rows, _r = _shopify_products_rows(session, origin, query, token)
        return rows
    if kind == "woo_store_api":
        return _woo_store_rows(session, origin, query, token)
    if kind == "magento_graphql":
        return _magento_graphql_rows(session, origin, query, token)
    if kind == "captured":
        template = site.get("api_url") or ""
        url = template.replace("{q}", quote_plus(query))
        html, _ = fetch_text(session, url, token, timeout=20)
        try:
            data = json.loads(html)
        except ValueError:
            return []
        dicts = []
        _product_dicts(data, dicts)
        rows = []
        for node in dicts:
            if "__list__" in node:
                continue
            title = str(node.get("title") or node.get("name") or "")
            price = node.get("price")
            if isinstance(price, dict):
                price = price.get("value", price.get("amount"))
            try:
                after = float(str(price).replace(",", ""))
            except (TypeError, ValueError):
                continue
            link = node.get("url") or node.get("link") or ""
            if link.startswith("/"):
                link = urljoin(origin, link)
            if title and link and after > 0:
                rows.append({"title": title.strip(), "before": after,
                             "after": after, "link": str(link),
                             "image": node.get("image") or ""})
        return rows
    return []


def scrape_saved_site(site, query, token=None, session=None):
    """Refresh rows for an entry saved with a winning race method."""
    pt = _pt()
    name = site.get("name", "?")
    token = token or CancelToken()
    own_session = False
    if session is None:
        try:
            session = pt.get_session("race-refresh")
        except Exception:
            session = requests.Session()
            session.headers.update(pt.HEADERS)
        own_session = True
    method = site.get("method")
    if method == "api":
        if site.get("api") == "woo_store_api":
            rows = []
            for page_num in (1, 2, 3):
                token.check()
                template = site.get("api_url", "")
                url = template.replace("{q}", quote_plus(query)).replace(
                    "{page}", str(page_num))
                html, _ = fetch_text(session, url, token, timeout=18)
                try:
                    items = json.loads(html)
                except ValueError:
                    break
                if not isinstance(items, list) or not items:
                    break
                rows.extend(_map_woo_products(items, urlparse(url).scheme
                                              + "://"
                                              + urlparse(url).netloc))
                token.sleep(0.5)
                if len(rows) >= 60:
                    break
        else:
            rows = _fetch_api_rows(site, query, token, session)
        return [_row(name, r["title"], r["before"], r["after"], r["link"],
                     r["image"]) for r in rows]
    if method in ("structured", "embedded"):
        url = (site.get("search_url") or "").replace("{q}",
                                                     quote_plus(query))
        html, final_url = fetch_text(session, url, token, timeout=25)
        soup = BeautifulSoup(html, "html.parser")
        if method == "structured":
            rows = _rows_from_structured(soup, final_url)
        else:
            rows = _embedded_products(soup, final_url)
        return [_row(name, r["title"], r["before"], r["after"], r["link"],
                     r["image"]) for r in rows]
    if method == "sitemap":
        rows = _sitemap_search_rows(site, query, token, session)
        return [_row(name, r["title"], r["before"], r["after"], r["link"],
                     r["image"]) for r in rows]
    raise RuntimeError("طريقة غير معروفة")


def _sitemap_search_rows(site, query, token, session):
    """Re-run the sitemap method filtered by the new query."""
    key = query.strip().lower()
    locs = _sitemap_locs(session, site.get("sitemap_url", ""), token)
    matching = [u for u in locs if key and key in u.lower()]
    targets = (matching or locs)[:12]
    rows = []
    for product_url in targets:
        token.check()
        try:
            html, final_url = fetch_text(session, product_url, token,
                                         timeout=15)
        except Exception:
            continue
        token.sleep(0.3)
        soup = BeautifulSoup(html, "html.parser")
        rows.extend(_rows_from_structured(soup, final_url))
        if len(rows) >= 30:
            break
    return rows


def webview_fetch_dom(search_url, token, timeout=70):
    """Load one page in a throwaway hidden window, return final DOM."""
    import webview
    box = {}
    error = {}

    def open_window():
        try:
            box["window"] = webview.create_window("فحص متجر", hidden=True,
                                                  width=1280, height=800)
        except Exception as e:
            error["error"] = e
    opener = threading.Thread(target=open_window, daemon=True)
    opener.start()
    opener.join(timeout=20)
    window = box.get("window")
    if window is None:
        raise RuntimeError("تعذّر فتح نافذة المتصفح")
    try:
        pool = WebViewPool()
        pool._window = window
        pool._users = 1
        webview_load(window, token, search_url, timeout=timeout)
        return _webview_eval(window, token,
                             "document.documentElement.outerHTML",
                             timeout=25)
    finally:
        try:
            window.destroy()
        except Exception:
            pass


def scrape_race_site(site, query, progress=None, enrich=True):
    """Search entry point for race-saved entries (all methods)."""
    pt = _pt()
    if site.get("use_webview"):
        template = site.get("search_url") or ""
        url = template.replace("{q}", quote_plus(query))
        pages = pt.paginated_urls(site, url)
        delay = pt.page_delay_range(site)
        rows, seen, empty_pages = [], set(), 0
        token = CancelToken()
        for i, page_url in enumerate(pages):
            if i:
                token.sleep(delay[0] + (delay[1] - delay[0]) / 2)
            dom = webview_fetch_dom(page_url, token)
            if progress and len(pages) > 1:
                progress("جاري جلب المنتجات (%d/%d)..." % (i + 1, len(pages)))
            for row in pt._parse_cards(site, dom, page_url):
                key = (row["site"], row["link"])
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
            if not rows:
                empty_pages += 1
                if empty_pages >= 2:
                    break
            else:
                empty_pages = 0
        if enrich and site.get("detail_pages"):
            rows = pt.enrich_from_detail_pages(site, rows)
        return rows
    rows = scrape_saved_site(site, query)
    if enrich and site.get("detail_pages"):
        rows = pt.enrich_from_detail_pages(site, rows)
    return rows


def quick_refresh(site, query):
    """One automatic fallback when the saved method fails: re-race the
    cheap methods (no browser) and return a fresh entry, or None."""
    pt = _pt()
    base = normalize_url(site.get("search_url") or "")
    if not base:
        return None
    try:
        session = pt.get_session("race-refresh")
    except Exception:
        session = requests.Session()
        session.headers.update(pt.HEADERS)
    ctx = RaceContext(site.get("search_url") or "", base,
                      query, session)
    token = CancelToken()
    try:
        urls, _reason = discover_search_urls(ctx, token)
    except Cancelled:
        return None
    if not urls:
        return None
    for mid in ("platform_api", "structured", "embedded", "html_search"):
        token.check()
        try:
            if mid == "platform_api":
                result = run_platform_api(
                    ctx, token, lambda *a: None)
            elif mid == "structured":
                result = run_structured(ctx, token, lambda *a: None)
            elif mid == "embedded":
                result = run_embedded(ctx, token, lambda *a: None)
            else:
                result = run_html_search(ctx, token, lambda *a: None)
        except Cancelled:
            return None
        except Exception:
            continue
        if result and result.get("ok") and result.get("entry"):
            entry = dict(result["entry"])
            entry["name"] = site.get("name")
            return entry
    return None
