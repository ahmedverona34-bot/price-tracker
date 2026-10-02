# pip install requests beautifulsoup4 openpyxl pywebview
# Optional, only for a storefront that truly renders in the browser:
#   pip install playwright
# Sites fetch over plain HTTP by default. Set "use_playwright": true per site in
# sites.json to opt into a real browser (installed Edge first, then Chrome,
# then a one-time headless Chromium download).
# Build exe (single folder, WebView2 is NOT bundled - it ships with Windows):
# .\.venv\Scripts\pyinstaller.exe --noconfirm --onedir --windowed --name PriceTracker --exclude-module tkinter --add-data "ui;ui" --add-data "sites.json;." price_tracker.py
# (the 5 Thmanyah Sans OTF files ship inside ui/fonts/)
"""Desktop price tracker: Python core + local HTML UI in a native window.

Run: python price_tracker.py            (opens the desktop window)
Self-test (no window): python price_tracker.py --selftest [query]
"""
import asyncio
import contextlib
import io
import json
import logging
import os
import random
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from urllib.parse import quote_plus
from urllib.parse import urljoin

import requests
import requests.adapters
from bs4 import BeautifulSoup

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from concurrent.futures import ThreadPoolExecutor, as_completed


def app_dir():
    """Folder holding the program (exe dir when frozen, script dir in dev)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """Folder the app WRITES to: settings, log, cache, saved prices.

    Kept apart from app_dir() because the program folder may be read-only
    (installed under Program Files). Settings live in the user's roaming
    profile so they follow the user; caches and the log go next to them.

    In development nothing is moved: the project folder is already writable
    and dev expects to find its files there.
    """
    if not getattr(sys, "frozen", False):
        return app_dir()
    root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if not root:
        return app_dir()
    path = os.path.join(root, "PriceTracker")
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        logging.exception("data dir not creatable, falling back to program dir")
        return app_dir()


def _local_run_dir():
    """Local folder for update executables (installer + relaunch helper).

    data_dir() may be a UNC path (``\\\\server\\...``) when %APPDATA% is
    folder-redirected on a domain. Windows cannot run an .exe from UNC
    reliably and cmd.exe cannot use UNC as its working directory -- both
    surface as a visible "The network path was not found." MessageBox, which
    is exactly what the in-app updater hit. So executables always go to a
    local dir: %LOCALAPPDATA% first, then TEMP/TMP, never a UNC path.
    """
    candidates = []
    for key in ("LOCALAPPDATA", "TEMP", "TMP"):
        val = os.environ.get(key)
        if val and not val.startswith("\\\\"):
            candidates.append(os.path.join(val, "PriceTrackerUpdate"))
    try:
        tmp = tempfile.gettempdir()
        if tmp and not tmp.startswith("\\\\"):
            candidates.append(os.path.join(tmp, "PriceTrackerUpdate"))
    except Exception:
        pass
    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            continue
    # Last resort: data_dir() even if it is UNC (caller still works,
    # it just risks the old MessageBox on redirected profiles).
    try:
        return data_dir()
    except Exception:
        return os.path.abspath(".")


logging.basicConfig(filename=os.path.join(data_dir(), "app.log"),
                    level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    encoding="utf-8")


# Sent on every plain HTTP request. These are the exact headers Chrome sends
# for a top-level page navigation. Stores behind bot protection (Vercel BotID,
# Cloudflare) score the header set as well as the TLS fingerprint, so a request
# that looks like a plain HTTP client is the one that gets challenged.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
TIMEOUT = 20
AUTOSAVE_FILE = "prices.xlsx"
AUTO_REFRESH_SEC = 10 * 60  # 10 minutes
SEARCH_TIMEOUT_SEC = 8 * 60  # watchdog: free a stuck search
# How long the app waits for the update installer to exit before closing
# its own window. The setup writes ~26 MB, so it needs real slack; the wait
# is a ceiling, not a delay, because the close happens the moment the
# installer is actually done.
UPDATE_EXIT_WAIT_SEC = 45
SITES_FILE = "sites.json"
SETTINGS_FILE = "settings.json"
PREVRUN_FILE = "prevrun.json"
SLOW_AFTER_SEC = 45  # a site slower than this gets an amber status dot

# ---- in-app update ----
# Keep APP_VERSION in step with AppVersion in installer/installer.iss. The app
# never reads the installed version from the registry: the registry can hold a
# newer one after an update, and a mismatch there would make the button offer
# the same build forever.
APP_VERSION = "1.2.4"
# A manifest file served over HTTPS. Two lines:
#
#   1.1.0
#   https://github.com/ahmedverona34-bot/price-tracker/releases/download/v1.1.0/PriceTracker-Setup-1.1.0.exe
#
# It is published from the gh-pages branch of this repo, so a release is:
#   build_setup.bat                       -> dist-installer\PriceTracker-Setup-<ver>.exe
#   gh release create v<ver> <setup>      -> the download URL in line 2
#   edit gh-pages/latest.txt with both lines, then push that branch
#   raise APP_VERSION here and AppVersion in installer/installer.iss
#
# The download URL has to be listed rather than guessed: the build number is
# not known until the file exists, and a wrong guess would silently
# re-download the build the user already has. _update_manifest rejects any
# URL that is not https or does not end in .exe, so a mistyped manifest
# means "no update offered" rather than a download that gets run.
UPDATE_URL = "https://ahmedverona34-bot.github.io/price-tracker/latest.txt"

# What a manifest's first line must look like: digits and dots only, at least
# one digit. Compared against exactly (no $ anchor beyond the match) so a
# trailing note or stray character is rejected rather than silently trimmed.
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")

KIND_DEVICES = "أجهزة فقط"
KIND_ACCESSORIES = "إكسسوارات فقط"
KIND_ALL = "الكل"
KIND_CHOICES = (KIND_DEVICES, KIND_ACCESSORIES, KIND_ALL)

# How many product pages are opened at once when checking for coupons. Over
# plain HTTP this is a small worker pool (capped, see load_sites) and it is
# paced with a gap between batches, because a burst of parallel requests is
# what triggers store rate limits in the first place.
_DETAIL_BATCH = {}

LAST_BROWSER_CHANNEL = "?"

# Table columns (keys used by the UI). The thumbnail column is opt-in.
ALL_COLUMNS = ("image", "site", "title", "before", "after", "discount",
               "link", "time", "note")
DEFAULT_COLUMNS = ["site", "title", "before", "after", "discount",
                   "link", "time", "note"]

# Columns stay exactly as specified (Excel keeps these seven).
EXCEL_HEADER = ["Site", "Product Title", "Price Before", "Price After",
                "Discount %", "Product Link", "Scraped At"]

# Default accessory words for the advanced exclude box (comma separated).
# Matched case-insensitively against every title (English + Arabic).
DEFAULT_EXCLUDE_WORDS = (
    "case, cover, glass, protector, charger, cable, adapter, holder, strap, "
    "bumper, pouch, skin, wallet, stand, mount, dock, sleeve, speaker, "
    "earbuds, earphone, headphone, headset, watch, stylus, "
    "جراب, كفر, حماية, شاحن, شواحن, كابل, وصلة, سلك, لاصق, سكرين, "
    "حافظة, حامل, ستاند, سماعة, سماعات, ساعة"
)

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# Last Playwright browser channel that worked ("msedge"/"chrome"/"chromium").
LAST_BROWSER_CHANNEL = None


def version_tuple(v):
    """'1.10.2' -> (1, 10, 2) so versions compare numerically, not as text."""
    parts = re.findall(r"\d+", str(v or ""))
    return tuple(int(p) for p in parts[:3]) or (0,)


def parse_exclude_words(text):
    """Split a comma-separated exclude box into casefolded words."""
    return [w.strip().casefold() for w in (text or "").split(",") if w.strip()]


def parse_min_price(text):
    """Parse the manual min-price box; None means no minimum."""
    t = (text or "").strip().translate(_AR_DIGITS).replace(",", "").replace(" ", "")
    if not t:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v > 0 else None


def split_accessories(rows, exclude_words):
    """Split rows into (devices, accessories) by exclude-word title match."""
    dev, acc = [], []
    for r in rows:
        title = (r.get("title") or "").casefold()
        (acc if any(w in title for w in exclude_words) else dev).append(r)
    return dev, acc


def median_after(rows):
    """Median of after-prices, or None when there are none."""
    prices = sorted(r["after"] for r in rows
                    if isinstance(r.get("after"), (int, float)))
    return statistics.median(prices) if prices else None


def apply_kind_filter(rows, kind, exclude_words, manual_min):
    """Filter pipeline: manual min-price floor -> accessory split -> kind.

    - أجهزة فقط: devices kept, dropping anything below ~30% of the median
      after-price of that site's devices for the current search.
    - إكسسوارات فقط: only the rows the accessory rules removed.
    - الكل: everything (after the manual floor), original order kept.
    """
    floored = [r for r in rows
               if manual_min is None
               or (isinstance(r.get("after"), (int, float))
                   and r["after"] >= manual_min)]
    dev, acc = split_accessories(floored, exclude_words)
    if kind == KIND_ACCESSORIES:
        return acc
    if kind == KIND_ALL:
        return list(floored)
    kept_keys = set()
    by_site = {}
    for r in dev:
        by_site.setdefault(r.get("site", ""), []).append(r)
    for site_rows in by_site.values():
        med = median_after(site_rows)
        thr = 0.3 * med if med else None
        for r in site_rows:
            if thr is None or r["after"] >= thr:
                kept_keys.add((r.get("site"), r.get("link")))
    return [r for r in floored if (r.get("site"), r.get("link")) in kept_keys]


def classify_rows(rows, exclude_words):
    """Tag every row with kind = "device" | "accessory", once per search.

    The page filters on this tag, so switching النوع is instant and never
    needs a re-scrape. Rules are the original ones: an exclude-word match in
    the title means accessory, and anything under 30% of its site's median
    after-price among the non-accessories goes with it.
    """
    for r in rows:
        r["kind"] = "device"
    _dev, acc = split_accessories(rows, exclude_words)
    for r in acc:
        r["kind"] = "accessory"
    by_site = {}
    for r in rows:
        if r.get("kind") == "device":
            by_site.setdefault(r.get("site", ""), []).append(r)
    for site_rows in by_site.values():
        med = median_after(site_rows)
        if not med:
            continue
        floor = 0.3 * med
        for r in site_rows:
            if (isinstance(r.get("after"), (int, float))
                    and r["after"] < floor):
                r["kind"] = "accessory"
    return rows


def kind_filtered_view(rows, kind, manual_min=None):
    """Server-side mirror of the النوع choice, used for the autosave file and
    the status line. The page owns the visible filtering; this keeps the
    saved prices.xlsx identical to what the user was looking at."""
    if kind == KIND_ACCESSORIES:
        out = [r for r in rows if r.get("kind") == "accessory"]
    elif kind == KIND_DEVICES:
        out = [r for r in rows if r.get("kind") == "device"]
    else:
        out = list(rows)
    if manual_min is not None:
        out = [r for r in out
               if isinstance(r.get("after"), (int, float))
               and r["after"] >= manual_min]
    return out


def fmt_price(v):
    """Whole-number display (128799.08 -> '128799')."""
    return "" if v is None else f"{v:.0f}"


def parse_price(text):
    text = text.replace("\xa0", " ")
    m = re.search(r"\d[\d,\.\s]*", text)
    if not m:
        return None
    s = m.group().strip().replace(" ", "").rstrip(".,")
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", "") if len(s.split(",")[-1]) == 3 else s.replace(",", ".")
    return float(s)


def parse_coupon_badge(text):
    """Extract (percent, code) from the coupon line of a product page.

    Some stores show a single price plus a coupon instead of a compare-at
    price. Dubai Phone writes it as one sentence, e.g.
    "استخدم كود DP8 واحصل على خصم إضافي 8% بدون حد أقصى" - a code and an
    extra-discount percent that belong together.

    The code and the percent are matched as a pair first, because a product
    page also mentions unrelated codes and discounts further down (bundle
    offers, related products); matching each on its own happily pairs a code
    with somebody else's percentage. Returns (None, None) when nothing looks
    like a coupon.
    """
    t = text.translate(_AR_DIGITS)

    # \b keeps "code" from matching inside "zipcode"/"barcode".
    code_pat = r"(?:بروموكود|كود|promo\s*code|\bcoupon\b|\bcode\b)"
    # Words that introduce the percentage itself: Arabic "extra discount"
    # (either spelling of the alef), English "extra 10% off" / "discount".
    pct_intro = r"(?:خصم\s*إضاف[يى]|خصم|discount|additional|extra|off)"
    pct_pat = r"(\d+(?:\.\d+)?)\s*%"
    # "code DP8 ... extra discount 8%": pair them inside one short sentence.
    pair = re.search(
        code_pat + r"\W{0,12}?([A-Za-z0-9]{2,})"
        r".{0,140}?" + pct_intro + r"\D{0,20}?" + pct_pat,
        t, re.IGNORECASE)
    if pair:
        return float(pair.group(2)), pair.group(1)

    m = re.search(pct_intro + r"\D{0,20}?" + pct_pat, t, re.IGNORECASE)
    if not m:
        # English often puts the number first: "10% off", "Save 30% discount".
        m = re.search(pct_pat + r"\s*(?:off|discount|خصم)", t, re.IGNORECASE)
    if not m:
        return None, None
    pct = float(m.group(1))
    # A bare percentage: take the nearest code, preferring one close by.
    code = None
    best = None
    for mc in re.finditer(
            r"(?:بروموكود|كود|promo\s*code|\bcoupon\b|\bcode\b)\s*:?\s*([A-Za-z0-9]{2,})",
            t, re.IGNORECASE):
        dist = abs(mc.start() - m.start())
        if best is None or dist < best[0]:
            best = (dist, mc.group(1))
    if best:
        code = best[1]
    return pct, code


# ---------------------------------------------------------------------------
# Timing instrumentation
# ---------------------------------------------------------------------------
# Lightweight: every expensive stage wraps itself in PERF.stage() and the run
# prints one breakdown at the end (also written to app.log). Nothing about the
# scraping behaviour depends on it.
class Perf:
    def __init__(self):
        self._lock = threading.Lock()
        self.run = None
        self.history = []

    def begin(self, label):
        with self._lock:
            self.run = {"label": label, "t0": time.perf_counter(),
                        "stages": [], "first_rows": None,
                        "rows_complete": None, "notes": []}
            return self.run

    @contextlib.contextmanager
    def stage(self, name, site=""):
        t0 = time.perf_counter()
        with self._lock:
            run = self.run
        try:
            yield
        finally:
            sec = time.perf_counter() - t0
            if run is not None:
                with self._lock:
                    run["stages"].append({"site": site, "stage": name,
                                          "sec": sec})

    def note(self, text):
        with self._lock:
            if self.run is not None:
                self.run["notes"].append(text)

    def mark_first_rows(self):
        with self._lock:
            if self.run is not None and self.run["first_rows"] is None:
                self.run["first_rows"] = time.perf_counter() - self.run["t0"]

    def mark_rows_complete(self):
        with self._lock:
            if self.run is not None and self.run["rows_complete"] is None:
                self.run["rows_complete"] = time.perf_counter() - self.run["t0"]

    def end(self):
        with self._lock:
            if self.run is None:
                return None
            self.run["total"] = time.perf_counter() - self.run["t0"]
            done, self.run = self.run, None
            self.history.append(done)
            self.history = self.history[-5:]
        return done


PERF = Perf()


def format_timings(run):
    """Human readable breakdown: total, time to first rows, per stage."""
    if not run:
        return "(no timings)"
    lines = ["", "=== TIMING BREAKDOWN: %s ===" % run["label"]]
    lines.append("  total refresh   %6.2fs" % run["total"])
    fr = run.get("first_rows")
    lines.append("  first rows      %6.2fs"
                 % (fr if fr is not None else float("nan")))
    rc = run.get("rows_complete")
    lines.append("  rows complete   %6.2fs"
                 % (rc if rc is not None else float("nan")))
    lines.append("  %-22s %-10s %s" % ("stage", "site", "seconds"))
    for st in sorted(run["stages"], key=lambda x: -x["sec"]):
        lines.append("  %-22s %-10s %6.2f"
                     % (st["stage"], st.get("site") or "-", st["sec"]))
    if run.get("notes"):
        lines.append("  notes:")
        for n in run["notes"]:
            lines.append("    - %s" % n)
    lines.append("=== end breakdown ===")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shared HTTP sessions (connection pooling + compression, one per site)
# ---------------------------------------------------------------------------
_SESSIONS = {}
_SESSION_LOCK = threading.Lock()


def get_session(key):
    """One pooled requests.Session per site so refreshes reuse connections."""
    key = key or "default"
    with _SESSION_LOCK:
        s = _SESSIONS.get(key)
        if s is None:
            s = requests.Session()
            s.headers.update(HEADERS)
            s.headers["Accept-Encoding"] = "gzip, deflate"
            # Pool sized for the parallel detail fetch (see detail_workers)
            # so coupon lookups open sockets instead of waiting on the pool.
            adapter = requests.adapters.HTTPAdapter(pool_connections=8,
                                                    pool_maxsize=12)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            _SESSIONS[key] = s
            logging.info("http session created for %s", key)
        return s


# ---------------------------------------------------------------------------
# Persistent browser: launched lazily, reused across refreshes, idle-closed
# ---------------------------------------------------------------------------
# Only the search page and coupon detail pages need a real browser, and both
# need DOM text only. Images, fonts, media and analytics are aborted through
# route interception: the request never leaves the machine, which cuts both
# the page weight and the load on the store's servers.
_BLOCK_TYPES = {"image", "media", "font"}
_BLOCK_HOSTS = (
    "google-analytics", "googletagmanager", "doubleclick", "facebook.net",
    "hotjar", "clarity.ms", "segment.io", "segment.com", "amplitude",
    "yandex", "mc.yandex", "tiktok", "snapchat", "pinterest", "bing.com/bat",
    "scorecardresearch", "criteo", "taboola", "outbrain", "adnxs",
)


def _make_route_guard():
    async def guard(route):
        try:
            req = route.request
            if req.resource_type in _BLOCK_TYPES:
                return await route.abort()
            url = req.url.lower()
            if any(h in url for h in _BLOCK_HOSTS):
                return await route.abort()
            return await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass
    return guard


class CheckpointError(RuntimeError):
    """The store answered with a bot-checkpoint or a rate-limit, not content."""


_CHECKPOINT_MARKERS = (
    # Cloudflare
    "just a moment", "checking your browser", "attention required",
    "cf-challenge", "cdn-cgi/challenge-platform", "enable javascript and cookies",
    # Vercel BotID / Security Challenge (the protection on Dubai Phone)
    "vercel security", ".well-known/vercel/security", "request-challenge",
    "challenge.v2.wasm", "cdn-cgi/challenge", "access denied",
    # generic
    "security checkpoint", "unusual traffic", "you have been blocked",
)

# Statuses that mean "a wall, not content". 708 is Vercel's own code for a
# bot challenge that was served instead of the page.
CHECKPOINT_STATUS = (401, 403, 407, 408, 429, 708)


def looks_like_checkpoint(html):
    """True when the response is a checkpoint wall instead of the shop."""
    low = (html or "")[:200000].lower()
    return any(m in low for m in _CHECKPOINT_MARKERS)


class _Browser:
    """Keeps one Playwright browser alive between refreshes.

    - Started on the first job that needs it, closed after IDLE_CLOSE_SEC of
      no work so an idle app holds no browser process.
    - One context shared by all pages; each job gets its own tab.
    - All calls happen on a private asyncio loop thread, so the search worker
      threads can submit jobs safely (Playwright objects are not thread safe).
    """

    IDLE_CLOSE_SEC = 240

    def __init__(self):
        self._loop = None
        self._thread = None
        self._pw = None
        self._browser = None
        self._context = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._site_label = ""
        self.launches = 0

    # -- lifecycle -------------------------------------------------
    def _ensure_loop(self):
        with self._lock:
            if self._loop is not None:
                return self._loop
            ready = threading.Event()

            def run_loop():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ready.set()
                loop.run_forever()

            self._thread = threading.Thread(target=run_loop,
                                            name="browser-loop", daemon=True)
            self._thread.start()
            ready.wait(5)
            self._last_used = time.time()
            threading.Thread(target=self._idle_watch, daemon=True).start()
            return self._loop

    def _idle_watch(self):
        while True:
            time.sleep(30)
            try:
                with self._lock:
                    if self._browser is None or self._loop is None:
                        continue
                    idle = time.time() - self._last_used > self.IDLE_CLOSE_SEC
                if idle:
                    self.close()
                    logging.info("browser closed after idle period")
            except Exception:
                logging.exception("browser idle watch failed")

    def submit(self, coro_factory, timeout=180):
        """Run an async job on the browser loop from any thread."""
        loop = self._ensure_loop()
        self._last_used = time.time()
        fut = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        try:
            return fut.result(timeout=timeout)
        finally:
            self._last_used = time.time()

    def warm(self, progress=None):
        """Start the browser ahead of the first search.

        Launching costs seconds; doing it while the app is idle means the
        first search only pays for the page itself.
        """
        try:
            self.submit(lambda: self._ensure_browser(), timeout=120)
            logging.info("browser ready via %s", LAST_BROWSER_CHANNEL)
            return True
        except Exception:
            logging.exception("browser warm failed")
            if progress:
                progress("جاري تحميل المتصفح لأول مرة، قد يستغرق بضع دقائق...")
            try:
                self.submit(lambda: self._ensure_browser(), timeout=180)
                return True
            except Exception:
                return False

    # -- jobs ------------------------------------------------------
    async def _ensure_browser(self):
        global LAST_BROWSER_CHANNEL
        if self._browser is not None:
            return self._browser
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        last = None
        for channel in ("msedge", "chrome"):
            try:
                self._browser = await self._pw.chromium.launch(
                    headless=True, timeout=15000, channel=channel)
                LAST_BROWSER_CHANNEL = channel
                break
            except Exception as e:
                last = e
        if self._browser is None:
            self._browser = await self._pw.chromium.launch(headless=True,
                                                          timeout=60000)
            LAST_BROWSER_CHANNEL = "chromium"
        self._context = await self._browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            java_script_enabled=True)
        await self._context.route("**/*", _make_route_guard())
        self.launches += 1
        logging.info("browser launched via %s (launch #%d)",
                     LAST_BROWSER_CHANNEL, self.launches)
        return self._browser

    async def fetch_search(self, url, wait_selector=None, scroll_selector=None,
                           max_scrolls=0, timeout_ms=60000):
        """Rendered HTML of a search page in a fresh tab (resources blocked).

        A store answering with a security checkpoint or 429 is detected from
        the response itself, so a blocked site costs seconds instead of two
        full selector timeouts.
        """
        await self._ensure_browser()
        page = await self._context.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded",
                                   timeout=timeout_ms)
            status = resp.status if resp else 0
            if status in CHECKPOINT_STATUS or status >= 500:
                raise CheckpointError("HTTP %s from %s" % (status, url))
            if wait_selector:
                # First a short probe: a checkpoint page never grows cards,
                # and waiting the full timeout on it is pure dead time.
                try:
                    await page.wait_for_selector(wait_selector,
                                                 timeout=CHECKPOINT_PROBE_MS)
                except Exception:
                    html = await page.content()
                    if looks_like_checkpoint(html):
                        raise CheckpointError(
                            "security checkpoint page for %s" % url)
                    try:
                        # Slow but healthy: keep waiting, then one reload
                        # retry (same as pressing F5).
                        await page.wait_for_selector(wait_selector,
                                                     timeout=timeout_ms)
                    except Exception:
                        logging.info("retrying %s after reload", url)
                        await page.reload(wait_until="domcontentloaded",
                                          timeout=timeout_ms)
                        await page.wait_for_selector(wait_selector,
                                                     timeout=timeout_ms)
            if scroll_selector and max_scrolls > 0:
                await self._scroll_cards(page, scroll_selector, max_scrolls)
            return await page.content()
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _scroll_cards(self, page, selector, max_scrolls):
        """Adaptive scroll: stop as soon as a scroll adds no new cards."""
        count = "(s) => document.querySelectorAll(s).length"
        last = -1
        for i in range(max_scrolls):
            with PERF.stage("scroll:%d" % (i + 1), self._site_label):
                n = await page.evaluate(count, selector)
                if n <= last:
                    PERF.note("scroll %d added no cards (still %d), stopping"
                              % (i + 1, n))
                    return          # no new cards -> stop immediately
                last = n
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)")
                # Wait for the card count to change instead of a fixed sleep.
                try:
                    await page.wait_for_function(
                        "([s, n]) => document.querySelectorAll(s).length > n",
                        arg=[selector, n], timeout=SCROLL_SETTLE_MS)
                except Exception:
                    PERF.note("scroll %d: no new cards within %dms, stopping"
                              % (i + 1, SCROLL_SETTLE_MS))
                    return          # timed out waiting for more cards
                PERF.note("scroll %d: cards now %d" % (i + 1, n))

    async def fetch_details(self, urls, batch=3, page_timeout_ms=30000):
        """Rendered body text of detail pages, `batch` tabs at a time."""
        await self._ensure_browser()
        out = [""] * len(urls)

        async def one(url):
            page = await self._context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=page_timeout_ms)
                await page.wait_for_timeout(600)
                return await page.evaluate("document.body.innerText")
            except Exception:
                return ""
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

        i = 0
        while i < len(urls):
            chunk = urls[i:i + batch]
            # A small gap between batches only: never hammer the store.
            if i:
                await asyncio.sleep(random.uniform(0.3, 0.9))
            try:
                texts = await asyncio.gather(*[one(u) for u in chunk])
            except Exception:
                texts = [""] * len(chunk)
            for t in texts:
                out[i] = t
                i += 1
        return out

    def close(self):
        with self._lock:
            loop, pw = self._loop, self._pw
            self._browser = self._context = self._pw = None

        async def _close():
            try:
                if self._context is not None:
                    await self._context.close()
            except Exception:
                pass
            try:
                if self._browser is not None:
                    await self._browser.close()
            except Exception:
                pass
            if pw is not None:
                try:
                    await pw.stop()
                except Exception:
                    pass

        try:
            if loop is not None and not loop.is_closed():
                asyncio.run_coroutine_threadsafe(_close(), loop).result(30)
        except Exception:
            logging.exception("browser close failed")


BROWSER = _Browser()


# NOTE on JavaScript-rendered sites:
# requests only downloads the initial HTML. Before reaching for a browser, check
# whether the store renders its cards server-side and just paginates them -
# Dubai Phone does (Next.js on Vercel, `?page=N`, 16 cards each), and walking
# those pages keeps the search on plain HTTP, which is both faster and far less
# likely to trip the store's bot protection. A real browser stays available as
# a per-site opt-in with "use_playwright": true for storefronts that genuinely
# need it.
def fetch_with_playwright(url, wait_until="domcontentloaded", timeout_ms=60000,
                            wait_selector=None, scroll_selector=None,
                            max_scrolls=0, site_key="default"):
    """Rendered HTML for a JS site, reusing the shared browser."""
    return BROWSER.submit(lambda: BROWSER.fetch_search(
        url, wait_selector=wait_selector, scroll_selector=scroll_selector,
        max_scrolls=max_scrolls, timeout_ms=timeout_ms))


def fetch_html(url, use_playwright=False, wait_selector=None,
               scroll_selector=None, max_scrolls=0, site_key="default"):
    """GET with up to 2 attempts on network errors. Returns (html, final_url).

    A bot-checkpoint answer (Cloudflare or Vercel BotID) is raised as
    CheckpointError on the first try: retrying the same blocked request only
    deepens the rate limit, and the caller already knows how to cool a site
    down and tell the user in Arabic.
    """
    if use_playwright:
        if not playwright_available():
            raise RuntimeError(
                "هذا الموقع يحتاج إلى متصفح، وهذه النسخة غير مزوَّدة به")
        return fetch_with_playwright(
            url, wait_selector=wait_selector, scroll_selector=scroll_selector,
            max_scrolls=max_scrolls, site_key=site_key), url
    session = get_session(site_key)
    last_exc = None
    for _ in range(2):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code in CHECKPOINT_STATUS:
                raise CheckpointError("HTTP %s from %s"
                                      % (r.status_code, url))
            r.raise_for_status()
            # A 200 that is really a challenge page still has to be caught,
            # otherwise it parses into zero rows and looks like "no results".
            if looks_like_checkpoint(r.text):
                raise CheckpointError("bot checkpoint page for %s" % url)
            return r.text, r.url
        except CheckpointError:
            raise
        except requests.RequestException as e:
            last_exc = e
            time.sleep(1.0)
    raise last_exc


def fetch_page_texts_playwright(urls, delay_range=(0.8, 2.0), timeout_ms=60000,
                               page_timeout=30000, site_key="default"):
    """Rendered body text of detail pages in the shared browser.

    Detail pages are fetched `batch` tabs at a time with a short gap between
    batches: much faster than one page at a time, still a light load.
    """
    if not urls:
        return []
    batch = int(site_detail_batch(site_key))
    return BROWSER.submit(lambda: BROWSER.fetch_details(
        urls, batch=batch, page_timeout_ms=page_timeout))


def site_detail_batch(site_key):
    """Pages fetched at once for coupons (workers over HTTP, tabs in a browser)."""
    try:
        return int(_DETAIL_BATCH.get(site_key, 6))
    except (TypeError, ValueError):
        return 6


def detail_workers(site):
    """Parallel plain-HTTP workers for coupon detail pages.

    Only for sites fetched over plain HTTP: those answers are served from the
    store's own cache, so a small parallel fan-out is cheap for the store and
    turns a 20-second coupon pass into a 3-second one. Capped low on purpose.
    """
    try:
        n = int(site.get("detail_workers", 6))
    except (TypeError, ValueError):
        n = 6
    return max(1, min(8, n))


def fetch_detail_texts_http(urls, delay_range=(0.8, 2.0), workers=4,
                            site_key="default"):
    """Visible text of each product page over plain HTTP.

    Fetches run in small parallel batches with a gap between batches, the same
    pacing idea as the browser path but with sockets instead of tabs. A single
    checkpointed page answers empty instead of failing the whole batch, so one
    blocked product never costs the other coupons.
    """
    if not urls:
        return []
    workers = max(1, min(8, int(workers or 1)))
    lo, hi = (list(delay_range) + [0.8, 2.0])[:2]
    session = get_session(site_key)
    out = [""] * len(urls)

    def one(url):
        try:
            resp = session.get(url, timeout=TIMEOUT)
            if resp.status_code in CHECKPOINT_STATUS:
                logging.info("detail page %s hit a checkpoint: HTTP %s",
                             url, resp.status_code)
                return ""
            resp.raise_for_status()
            if looks_like_checkpoint(resp.text):
                return ""
            return BeautifulSoup(resp.text, "html.parser").get_text(" ")
        except requests.RequestException as e:
            logging.info("detail page failed %s: %s", url, e)
            return ""

    i = 0
    with ThreadPoolExecutor(max_workers=workers,
                             thread_name_prefix="detail") as pool:
        while i < len(urls):
            chunk = urls[i:i + workers]
            if i:
                time.sleep(random.uniform(lo, hi))
            try:
                texts = list(pool.map(one, chunk))
            except Exception:
                logging.exception("detail batch failed for %s", site_key)
                texts = [""] * len(chunk)
            for j, t in enumerate(texts):
                out[i + j] = t
            i += len(chunk)
    return out


# ---------------------------------------------------------------------------
# Product detail cache: coupons change rarely, so a fresh entry means the
# product page never has to be opened again (TTL 6h).
# ---------------------------------------------------------------------------
DETAIL_CACHE_FILE = "detail_cache.json"
DETAIL_TTL_SEC = 6 * 3600
CHECKPOINT_PROBE_MS = 4000   # how long to wait for cards before suspecting a wall
SCROLL_SETTLE_MS = 1500      # how long a scroll may take to add new cards
SITE_BACKOFF_SEC = 30 * 60   # skip a rate-limited site for this long
_site_backoff = {}           # site name -> epoch when it may be tried again
_detail_cache = None
_detail_cache_lock = threading.Lock()


def detail_cache_path():
    return os.path.join(data_dir(), DETAIL_CACHE_FILE)


def load_detail_cache():
    global _detail_cache
    with _detail_cache_lock:
        if _detail_cache is None:
            try:
                with open(detail_cache_path(), encoding="utf-8") as f:
                    data = json.load(f)
                _detail_cache = data if isinstance(data, dict) else {}
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                _detail_cache = {}
        return _detail_cache


def save_detail_cache(cache):
    with _detail_cache_lock:
        try:
            with open(detail_cache_path(), "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
        except OSError:
            logging.exception("detail cache save failed")


def cached_detail(link):
    """(pct, code) from cache when still fresh, else (None, None)."""
    entry = load_detail_cache().get(link)
    if not isinstance(entry, dict):
        return None, None
    if time.time() - float(entry.get("t", 0)) > DETAIL_TTL_SEC:
        return None, None
    return entry.get("pct"), entry.get("code")


def store_detail(link, pct, code):
    cache = load_detail_cache()
    cache[link] = {"t": time.time(), "pct": pct, "code": code}
    # Keep the file small: drop anything already expired.
    now = time.time()
    for k in [k for k, v in cache.items()
              if not isinstance(v, dict)
              or now - float(v.get("t", 0)) > DETAIL_TTL_SEC * 4]:
        cache.pop(k, None)
    save_detail_cache(cache)


def enrich_from_detail_pages(site, rows):
    """Opt-in per site ("detail_pages": True): visit product pages (capped,
    small random delay) to pick up coupon badges that exist only there.

    Fresh cache entries skip the visit entirely; only uncached rows are
    fetched, in small parallel batches inside the shared browser.
    """
    cap = int(site.get("detail_cap", 10))
    delay = tuple(site.get("detail_delay", [0.8, 2.0]))
    targets = [r for r in rows if not (r["discount"] or 0)][:cap]
    if not targets:
        return rows
    with PERF.stage("coupon:cache-lookup", site.get("name", "?")):
        pending, cached = [], []
        for r in targets:
            pct, code = cached_detail(r["link"])
            if pct:
                cached.append((r, pct, code))
            else:
                pending.append(r)
        for r, pct, code in cached:
            apply_coupon(r, pct, code)
    if not pending:
        PERF.note("coupon cache hit for all %d rows of %s"
                  % (len(cached), site.get("name")))
        return rows
    PERF.note("coupon cache: %d hit / %d pages to open (%s)"
              % (len(cached), len(pending), site.get("name")))
    with PERF.stage("coupon:fetch", site.get("name", "?")):
        try:
            if site.get("use_playwright"):
                texts = fetch_page_texts_playwright(
                    [r["link"] for r in pending], delay,
                    page_timeout=site.get("detail_timeout_ms", 30000),
                    site_key=site.get("name", "default"))
            else:
                texts = fetch_detail_texts_http(
                    [r["link"] for r in pending], delay,
                    workers=detail_workers(site),
                    site_key=site.get("name", "default"))
        except Exception:
            logging.exception("detail enrichment failed")
            return rows
    for r, text in zip(pending, texts):
        if not text:
            continue
        pct, code = parse_coupon_badge(text)
        store_detail(r["link"], pct, code)
        if pct:
            apply_coupon(r, pct, code)
    return rows


def apply_coupon(row, pct, code):
    """Write a coupon into a row: note, price after, discount percent."""
    row["discount"] = pct
    row["after"] = round(row["before"] * (1 - pct / 100), 2)
    row["note"] = (f"coupon {code} -{pct:g}%" if code else f"coupon -{pct:g}%")


def _walk_jsonld(node, out):
    """Recursively collect dicts that look like Product/Offer data."""
    if isinstance(node, dict):
        t = node.get("@type", "")
        types = t if isinstance(t, list) else [t]
        if any(x in ("Product", "Offer", "AggregateOffer") for x in types) or (
            "price" in node or "offers" in node
        ):
            out.append(node)
        for v in node.values():
            _walk_jsonld(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_jsonld(v, out)


def extract_jsonld_products(soup, base_url):
    """Fallback: pull (title, price, url) from application/ld+json blocks."""
    found = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        nodes = []
        _walk_jsonld(data, nodes)
        for n in nodes:
            title = n.get("name")
            url = n.get("url")
            price = n.get("price")
            offers = n.get("offers")
            if price is None and isinstance(offers, dict):
                price = offers.get("price", offers.get("lowPrice"))
            elif price is None and isinstance(offers, list) and offers:
                o = offers[0] if isinstance(offers[0], dict) else {}
                price = o.get("price", o.get("lowPrice"))
            try:
                price_f = float(str(price).replace(",", "")) if price is not None else None
            except (ValueError, TypeError):
                price_f = None
            if title and price_f is not None:
                link = urljoin(base_url, url) if url else base_url
                found.append({"title": str(title).strip(), "price": price_f, "link": link})
    # de-dupe by (title, link)
    seen, uniq = set(), []
    for p in found:
        key = (p["title"], p["link"])
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def site_in_backoff(name):
    """True while a site that answered with a checkpoint/429 is cooling down."""
    until = _site_backoff.get(name, 0)
    if until and time.time() < until:
        return True
    if until:
        _site_backoff.pop(name, None)
    return False


def note_backoff(name):
    _site_backoff[name] = time.time() + SITE_BACKOFF_SEC
    logging.info("site %s cooling down for %d min after a checkpoint/429",
                 name, SITE_BACKOFF_SEC // 60)


def clear_backoff(name):
    _site_backoff.pop(name, None)


def page_delay_range(site):
    """Random gap between paginated result pages (polite, never zero)."""
    d = site.get("page_delay") or [0.4, 1.0]
    try:
        lo, hi = float(d[0]), float(d[1])
    except (TypeError, ValueError, IndexError):
        return (0.4, 1.0)
    if hi < lo:
        lo, hi = hi, lo
    return (max(0.0, lo), max(0.0, hi))


def paginated_urls(site, url):
    """Every result-page URL to walk, or just [url] when the site has one page.

    A store that renders its result list server-side (Dubai Phone: Next.js on
    Vercel, 16 cards per page, `?page=N`) hands over the whole list without a
    browser. Walking those pages replaces the old scroll-and-wait loop, so the
    search no longer needs a real browser at all.
    """
    pages = site.get("paginate") or site.get("pages") or {}
    if not isinstance(pages, dict) or not pages:
        return [url]
    try:
        max_pages = int(pages.get("max_pages") or 1)
    except (TypeError, ValueError):
        max_pages = 1
    param = str(pages.get("param") or "page")
    sep = "&" if "?" in url else "?"
    # First page is the bare search URL (it carries no ?page=), then 2..N.
    return [url] + ["%s%s%s=%d" % (url, sep, param, i)
                    for i in range(2, max_pages + 1)]


def scrape_site(site, query, progress=None, enrich=True):
    """Scrape one configured site. Raises on network failure; never kills others.

    Every stage is timed through PERF so a slow site can be attributed to the
    network fetch, the browser, the page walk or the parse instead of guessed.
    `enrich=False` returns the parsed cards only, so the caller can show them
    and fill coupons in afterwards.
    """
    name = site.get("name", "?")
    key = site.get("name", "default")
    BROWSER._site_label = name
    url = site["search_url"].replace("{q}", quote_plus(query))
    pages = paginated_urls(site, url)
    delay = page_delay_range(site)
    rows, seen, empty_pages = [], set(), 0
    for i, page_url in enumerate(pages):
        if i:
            time.sleep(random.uniform(*delay))
        stage = "fetch:search-page" if i == 0 else "fetch:page%d" % (i + 1)
        with PERF.stage(stage, name):
            html, final_url = fetch_html(
                page_url, use_playwright=site.get("use_playwright", False),
                wait_selector=site.get("wait_selector"),
                scroll_selector=site.get("scroll_selector"),
                max_scrolls=site.get("max_scrolls", 0), site_key=key)
        with PERF.stage("parse", name):
            fresh = []
            for r in _parse_cards(site, html, final_url):
                # De-dupe across pages: JS storefronts can render each product
                # card twice (visible + hidden carousel copies), and a
                # pagination walk can repeat a card at a page boundary.
                k = (r["site"], r["link"])
                if k not in seen:
                    seen.add(k)
                    fresh.append(r)
        rows.extend(fresh)
        if not fresh:
            # Past the last page the store repeats or drops the tail; either
            # way there is nothing left to collect.
            empty_pages += 1
            if empty_pages >= 2:
                PERF.note("%s: page %d added nothing new, stopping" % (name, i + 1))
                break
        else:
            empty_pages = 0
        if progress and len(pages) > 1 and i + 1 < len(pages):
            progress("جاري جلب المنتجات (%d/%d)..." % (i + 2, len(pages)))
    if not rows:
        PERF.note("%s: page had no product cards" % name)
    if enrich and site.get("detail_pages"):
        if progress:
            progress("جاري فحص صفحات المنتجات (حتى %d)..."
                     % int(site.get("detail_cap", 10)))
        rows = enrich_from_detail_pages(site, rows)
    return rows


def _parse_cards(site, html, final_url):
    """Turn fetched HTML into rows using the site's selectors."""
    soup = BeautifulSoup(html, "html.parser")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []

    cards = soup.select(site["item"]) if site.get("item") else []
    for card in cards:
        title_el = card.select_one(site["title"]) if site.get("title") else None
        now_el = card.select_one(site["price_now"]) if site.get("price_now") else None
        old_el = card.select_one(site["price_old"]) if site.get("price_old") else None
        link_el = card.select_one(site["link"]) if site.get("link") else None
        img_el = card.select_one(site["image"]) if site.get("image") else None

        title = title_el.get_text(strip=True) if title_el else ""
        price_after = parse_price(now_el.get_text(" ", strip=True)) if now_el else None
        if price_after is None:
            # Generic fallback for single-price (non-discounted) Magento cards,
            # e.g. 2B items listed with one price and no .special-price block.
            # [data-price-type="finalPrice"] is a real attribute observed on 2B.
            fb = card.select_one('[data-price-type="finalPrice"] .price')
            if fb is None:
                fb = card.select_one('.price-box .price')
            price_after = parse_price(fb.get_text(" ", strip=True)) if fb else None
        price_before = parse_price(old_el.get_text(" ", strip=True)) if old_el else None
        if price_after is None:
            continue  # skip cards with no usable current price
        if price_before is None:
            price_before = price_after
            discount = 0.0
        else:
            discount = round((price_before - price_after) / price_before * 100, 2) if price_before else 0.0
        note = ""
        if site.get("coupon_badge") and discount == 0.0:
            # Generic coupon-badge handling (opt-in per site): the displayed
            # price is the pre-coupon price, e.g. Dubai Phone shows 18,290
            # with "خصم 5% / بروموكود DP5", and its product page confirms
            # "use code DP5 for an extra 5% off". So before = displayed,
            # after = before * (1 - pct/100).
            pct, code = parse_coupon_badge(card.get_text(" ", strip=True))
            if pct:
                discount = pct
                price_after = round(price_before * (1 - pct / 100), 2)
                note = f"coupon {code} -{pct:g}%" if code else f"coupon -{pct:g}%"

        href = link_el.get("href") if link_el and link_el.has_attr("href") else ""
        if not href:
            # Generic fallback for cards wrapped in a link (e.g. Dubai Phone:
            # <a href="/shop/..."><article class="nf-product-card">...).
            if card.name == "a" and card.has_attr("href"):
                href = card["href"]
            else:
                parent_a = card.find_parent("a", href=True)
                if parent_a:
                    href = parent_a["href"]
        link = urljoin(final_url, href) if href else final_url
        img = ""
        if img_el is not None:
            img = img_el.get("src") or img_el.get("data-src") or ""
            if img:
                img = urljoin(final_url, img)
        rows.append({
            "site": site["name"],
            "title": title or "(no title)",
            "before": round(price_before, 2),
            "after": round(price_after, 2),
            "discount": max(discount, 0.0),
            "link": link,
            "image": img,
            "timestamp": ts,
            "note": note,
        })

    if not rows:
        # JSON-LD fallback for stores exposing price/offers in script tags.
        for p in extract_jsonld_products(soup, final_url):
            rows.append({
                "site": site["name"],
                "title": p["title"],
                "before": round(p["price"], 2),
                "after": round(p["price"], 2),
                "discount": 0.0,
                "link": p["link"],
                "image": "",
                "timestamp": ts,
            })
    return rows


def write_excel(rows, path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Prices"
    ws.append(EXCEL_HEADER)
    for r in rows:
        ws.append([r["site"], r["title"], r["before"], r["after"],
                   r["discount"], r["link"], r["timestamp"]])
    # Bold dark header row
    dark = PatternFill("solid", fgColor="1F4E78")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = dark
        cell.alignment = Alignment(vertical="center")
    widths = [18, 55, 14, 14, 12, 60, 20]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)


def sites_path():
    """sites.json next to the program (editable), else the bundled copy."""
    here = os.path.join(app_dir(), SITES_FILE)
    if os.path.exists(here):
        return here
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        bundled = os.path.join(bundle, SITES_FILE)
        if os.path.exists(bundled):
            return bundled
    return here


def load_sites():
    """Read sites.json next to the program. Returns (sites, error_msg)."""
    path = sites_path()
    if not os.path.exists(path):
        return [], ("ملف sites.json غير موجود بجوار البرنامج. "
                     "يُرجى نسخ ملف المواقع بجوار ملف التشغيل والمحاولة مرة أخرى.")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        sites = data.get("sites", data) if isinstance(data, dict) else data
        if not isinstance(sites, list) or not sites:
            return [], "ملف sites.json فارغ أو غير صالح. يُرجى التأكد من احتوائه على قائمة المواقع."
        for s in sites:
            if not isinstance(s, dict):
                return [], "ملف sites.json غير صالح. يُرجى مراجعة الملف والمحاولة مرة أخرى."
            for k in ("name", "search_url", "item", "title", "price_now", "link"):
                if not s.get(k):
                    return [], ("موقع ينقصه بيانات (%s). يُرجى مراجعة ملف sites.json."
                                 % s.get("name", "?"))
            # How many product pages may be opened at once for coupons.
            # Over plain HTTP this is a worker count, not browser tabs, so it
            # may go higher; the hard cap keeps a burst polite either way.
            try:
                _DETAIL_BATCH[s["name"]] = max(1, min(8, int(
                    s.get("detail_batch", s.get("detail_workers", 6)))))
            except (TypeError, ValueError):
                _DETAIL_BATCH[s["name"]] = 6
        return sites, None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        logging.exception("sites.json load failed")
        return [], "ملف sites.json غير صالح (ليست بيانات JSON سليمة). يُرجى مراجعة الملف والمحاولة مرة أخرى."


def settings_path():
    return os.path.join(data_dir(), SETTINGS_FILE)


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def save_settings(d):
    try:
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except OSError:
        logging.exception("settings save failed")


def prevrun_path():
    return os.path.join(data_dir(), PREVRUN_FILE)


def load_prevrun():
    """Previous run for price-change arrows: {"prices": {...}, "sites": {...}}."""
    try:
        with open(prevrun_path(), encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {"prices": {}, "sites": {}}
        prices = d.get("prices", {})
        sites = d.get("sites", {})
        return {"prices": prices if isinstance(prices, dict) else {},
                "sites": sites if isinstance(sites, dict) else {}}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"prices": {}, "sites": {}}


def save_prevrun(prices, sites):
    try:
        with open(prevrun_path(), "w", encoding="utf-8") as f:
            json.dump({"prices": prices, "sites": sites}, f,
                      ensure_ascii=False)
    except OSError:
        logging.exception("prevrun save failed")


def playwright_available():
    """True when this build can drive a real browser.

    Playwright is not bundled: it ships about 100 MB (mostly a bundled node
    runtime) and no configured site needs it, so a site that asks for
    "use_playwright" on a build without it gets a plain Arabic notice
    instead of a crash.
    """
    try:
        import playwright  # noqa: F401
    except Exception:
        return False
    return True


def find_playwright_driver():
    """Locate the Playwright node driver (frozen exe or dev venv)."""
    import playwright
    bundle = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle:
        pkg = os.path.join(bundle, "playwright")
    else:
        pkg = os.path.dirname(playwright.__file__)
    node = os.path.join(pkg, "driver", "node.exe")
    cli = os.path.join(pkg, "driver", "package", "lib", "cli", "cli.js")
    return node, cli


def ensure_chromium(progress=None):
    """Make sure a browser exists for Playwright: installed Edge first,
    then Chrome, else download headless Chromium once (first launch).

    Returns True when a browser launches. progress(msg) reports Arabic
    status updates. Technical details go to app.log.
    """
    if not playwright_available():
        logging.info("playwright is not bundled with this build")
        if progress:
            progress("المتصفح غير متاح في هذه النسخة")
        return False
    if BROWSER.warm(progress=progress):
        return True
    return ensure_chromium_download(progress)


def ensure_chromium_download(progress=None):
    """Download headless Chromium once, when neither Edge nor Chrome exists."""
    msg = "يجري تحميل المتصفح للمرة الأولى، وقد يستغرق بضع دقائق..."
    logging.info("no Edge/Chrome found, downloading headless chromium")
    if progress:
        progress(msg)
    try:
        node, cli = find_playwright_driver()
        subprocess.run([node, cli, "install", "chromium", "--only-shell"],
                       check=True, timeout=900)
        logging.info("chromium download finished")
        return True
    except Exception:
        logging.exception("chromium download failed")
        return False


class TrackerCore:
    """All app logic with no GUI toolkit dependency (testable headless)."""

    def __init__(self):
        self.sites, self.sites_error = load_sites()
        self.settings = load_settings()
        self.all_rows = []       # raw rows from the last search
        self.results = []        # filtered rows (shown + saved)
        _prev = load_prevrun()
        self.prev = _prev.get("prices", {})  # "site||link" -> after price
        # Per-site status from the previous run is deliberately NOT loaded
        # here. On launch the window must describe what has actually happened
        # in this session, and nothing has run yet: showing "45 صف • 0.7ث"
        # before the user has searched reads as this session's result. It is
        # still on disk for the price-delta baseline above and for the
        # Sources page, which is explicitly about history.
        self.site_stats = {}
        # Manifest cache. The page re-checks for updates while the window
        # stays open, and a repeating network fetch on every check is a
        # cost the user can feel. The manifest only changes when a new
        # build is published, which happens between launches, so a short
        # freshness window makes the repeating check free.
        self._manifest_cache = None
        self._manifest_time = 0.0
        self.last_query = ""   # armed by the first search, see below
        self.kind = self.settings.get("kind", KIND_DEVICES)
        self.auto_refresh = self.settings.get("auto_refresh", True)
        _theme = self.settings.get("theme", "light")
        self.theme = _theme if _theme in ("light", "dark") else "light"
        self.searching = False
        self.search_gen = 0
        self.search_count = 0
        self.rows_token = 0   # bumped whenever the row set changes
        self.status_msg = "اكتب كلمة البحث ثم اضغط على زر البحث"
        self.failed_sites = []
        self.last_saved_path = None
        self.last_update = ""
        self.view = []          # kind-filtered rows (autosave + headline count)
        self.site_rows = {}     # site name -> rows that streamed in so far
        self.pending_coupon = set()  # links whose coupon page is open now
        self.site_next_at = {}  # per-site refresh clock
        self.site_refresh = {}  # site name -> its own interval in seconds
        try:
            self.refresh_every = int(self.settings.get("refresh_sec",
                                                        AUTO_REFRESH_SEC))
        except (TypeError, ValueError):
            self.refresh_every = AUTO_REFRESH_SEC
        if self.refresh_every < 60:
            self.refresh_every = AUTO_REFRESH_SEC
        self.next_refresh_at = 0.0
        # The field is pre-filled with the last keyword so the user can simply
        # press the button again, but that is a saved preference, not a search:
        # it must not arm the scheduler or label the (still empty) table.
        self.saved_query = self.settings.get("keyword", "")
        # No refresh is armed at launch. last_query stays empty until the user
        # searches, so the countdown reads as nothing rather than promising an
        # automatic refresh that has not been started.
        self._lock = threading.Lock()
        self._window = None  # set by the GUI layer for evaluate_js pushes
        threading.Thread(target=self._scheduler_loop, daemon=True).start()

    # ----- settings -----
    def current_settings(self):
        # saved_query, not last_query: the field is pre-filled with the last
        # keyword on launch, but last_query is empty until a search runs.
        return {"keyword": self.saved_query,
                "kind": self.kind,
                "theme": self.theme,
                "auto_refresh": bool(self.auto_refresh),
                "refresh_sec": self.refresh_every,
                "exclude_words": self.settings.get("exclude_words",
                                                   DEFAULT_EXCLUDE_WORDS),
                "min_price": self.settings.get("min_price", ""),
                "disabled_sites": self.settings.get("disabled_sites", []),
                "visible_columns": self.settings.get(
                    "visible_columns", DEFAULT_COLUMNS),
                "f_min": self.settings.get("f_min", ""),
                "f_max": self.settings.get("f_max", ""),
                "f_disc": self.settings.get("f_disc", "")}

    def save_settings(self):
        self.settings.update(self.current_settings())
        save_settings(self.settings)

    def disabled_sites(self):
        return self.settings.get("disabled_sites", []) or []

    def set_kind(self, kind):
        if kind in KIND_CHOICES:
            self.kind = kind
            self.save_settings()
            self.apply_filters()
            return True
        return False

    def set_auto(self, on):
        self.auto_refresh = bool(on)
        self.save_settings()

    def set_theme(self, theme):
        """Light/dark choice from the topbar toggle; persisted like kind."""
        if theme in ("light", "dark"):
            self.theme = theme
            self.save_settings()
            return True
        return False

    def set_advanced(self, exclude_words, min_price):
        self.settings["exclude_words"] = exclude_words or ""
        self.settings["min_price"] = min_price or ""
        self.save_settings()
        self.apply_filters()
        return True

    def set_refresh_sec(self, sec):
        """Refresh cadence from the settings page (Stitch interval control).

        0 means manual only. Anything under a minute falls back to 10 min.
        """
        try:
            sec = int(sec)
        except (TypeError, ValueError):
            return False
        if sec == 0:
            self.refresh_every = 0
            self.next_refresh_at = 0.0
            self.settings["refresh_sec"] = 0
            self.save_settings()
            return True
        if sec < 60:
            sec = AUTO_REFRESH_SEC
        self.refresh_every = sec
        self.settings["refresh_sec"] = sec
        self.save_settings()
        if self.last_query and self.auto_refresh:
            self.next_refresh_at = time.time() + sec
        return True

    def get_logs(self, limit=60):
        """Last lines of app.log for the status page (technical, untranslated)."""
        try:
            path = os.path.join(data_dir(), "app.log")
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            return lines[-limit:]
        except OSError:
            return []

    # ----- filtering -----
    def _manual_min(self):
        return parse_min_price(self.settings.get("min_price", ""))

    def _kind_filtered(self, rows):
        """Rows for the saved prices.xlsx and the status line.

        The page does the visible filtering (instantly, from its own state),
        so this is only the persisted artefact plus the headline count.
        """
        return kind_filtered_view(rows, self.kind, self._manual_min())

    def apply_filters(self):
        """Re-filter raw results instantly (no re-scrape)."""
        if not self.all_rows:
            return self.results
        classify_rows(self.all_rows, parse_exclude_words(self.settings.get(
            "exclude_words", DEFAULT_EXCLUDE_WORDS)))
        self.view = self._kind_filtered(self.all_rows)
        return self.view

    # ----- search -----
    def start_search(self, query=None, sites=None):
        q = (query if query is not None else self.last_query).strip()
        if not q:
            self._set_status("يُرجى كتابة كلمة البحث أولًا")
            return False
        if self.searching:
            self._set_status("البحث قيد التنفيذ بالفعل، يُرجى الانتظار")
            return False
        if not self.sites:
            self._set_status("لا توجد مواقع، يُرجى مراجعة ملف sites.json")
            return False
        self.last_query = q
        self.saved_query = q
        self.save_settings()
        self.searching = True
        # The countdown only starts once a real search has run, so it always
        # describes work the user has actually asked for.
        self.next_refresh_at = time.time() + self.refresh_every
        self.search_gen += 1
        gen = self.search_gen
        self._set_status('يجري البحث عن "%s"...' % q)
        logging.info("search started: %s (sites=%s)", q,
                     [s.get("name") for s in sites] if sites else "all")
        threading.Thread(target=self._worker, args=(q, gen, sites),
                         daemon=True).start()
        _wd = threading.Timer(SEARCH_TIMEOUT_SEC,
                              lambda: self._watchdog(gen))
        _wd.daemon = True
        _wd.start()
        return True

    def _set_status(self, msg):
        with self._lock:
            self.status_msg = msg

    def _post_status(self, gen, msg):
        if gen == self.search_gen:
            self._set_status(msg)

    def _push_results(self):
        win = self._window
        if win is None:
            return
        try:
            win.evaluate_js("window.__pushResults && window.__pushResults()")
        except Exception:
            pass

    def _push_status(self):
        """Repaint the status line without re-fetching rows."""
        win = self._window
        if win is None:
            return
        try:
            win.evaluate_js("window.__pushStatus && window.__pushStatus()")
        except Exception:
            pass

    def _worker(self, query, gen, sites=None):
        """Every enabled site at the same time.

        Sites run on their own threads, so a refresh costs the slowest site
        instead of the sum of all of them. Each site streams its rows to the
        table the moment it finishes, and coupon enrichment fills in
        progressively afterwards so the slowest page never blocks the view.
        """
        PERF.begin("search:%s" % query)
        try:
            disabled = set(self.disabled_sites())
            active = [s for s in self.sites
                      if s.get("name") not in disabled]
            self._reset_run(gen)
            stats = {}
            for s in self.sites:
                if s.get("name") in disabled:
                    stats[s.get("name")] = {"status": "off", "rows": 0,
                                            "duration_sec": 0}
            if not active:
                self._on_results([], [], gen)
                return
            collected, failed = {}, []
            pool = ThreadPoolExecutor(max_workers=min(4, len(active)),
                                      thread_name_prefix="scrape")
            try:
                futures = {pool.submit(self._scrape_one, site, query, gen):
                           site for site in active}
                # Stream each site as soon as IT is ready, not when the
                # slowest one is.
                for fut in as_completed(futures):
                    site = futures[fut]
                    name = site.get("name", "?")
                    try:
                        rows, dt, blocked = fut.result()
                    except Exception:
                        logging.exception("site worker crashed: %s", name)
                        rows, dt, blocked = None, 0.0, False
                    if rows is None:
                        failed.append(name)
                        stats[name] = {"status": "blocked" if blocked
                                       else "failed", "rows": 0,
                                       "duration_sec": dt}
                        self.site_stats[name] = stats[name]
                        self._push_status()
                        continue
                    collected[name] = rows
                    stats[name] = {
                        "status": "slow" if dt > SLOW_AFTER_SEC else "ok",
                        "rows": len(rows), "duration_sec": dt}
                    self.site_stats[name] = stats[name]
                    self._stream_site(name, rows, gen)
            finally:
                pool.shutdown(wait=True)
            self.site_stats.update(stats)
            # Rows are on screen now; coupons keep filling in behind them.
            self._on_results([], failed, gen)
            PERF.mark_rows_complete()
            self._enrich_progressive(gen, query, collected)
        except Exception:
            logging.exception("search worker crashed")
            self.searching = False
            self._set_status("حدث خطأ غير متوقع، يُرجى إعادة البحث")
        finally:
            run = PERF.end()
            if run:
                logging.info("TIMINGS %s", format_timings(run))
            save_prevrun(self.prev, self.site_stats)

    def _reset_run(self, gen):
        """Clear per-search state and snapshot the previous prices."""
        self.site_rows = {}
        self.failed_sites = []
        self.pending_coupon = set()
        self._baseline = dict(self.prev)
        self._baseline_empty = not self._baseline

    def _scrape_one(self, site, query, gen):
        """Fetch one site and return (rows, seconds, blocked)."""
        name = site.get("name", "?")
        self.site_stats[name] = {"status": "loading", "rows": 0,
                                 "duration_sec": 0}
        self._post_status(gen, "جاري جلب موقع %s..." % name)
        self._push_status()
        t0 = time.perf_counter()
        try:
            rows = scrape_site(site, query,
                               progress=lambda m: self._post_status(gen, m),
                               enrich=False)
        except CheckpointError as e:
            # Rate limited / checkpointed: cool this site down instead of
            # hammering it on the next cycle.
            logging.info("site %s is behind a checkpoint: %s", name, e)
            note_backoff(name)
            return None, round(time.perf_counter() - t0, 1), True
        except Exception:
            logging.exception("site failed: %s", name)
            return None, round(time.perf_counter() - t0, 1), False
        clear_backoff(name)
        return rows, round(time.perf_counter() - t0, 1), False

    def _stream_site(self, name, rows, gen):
        """Publish one site's rows immediately, with change/new markers."""
        if gen != self.search_gen:
            return
        classify_rows(rows, parse_exclude_words(self.settings.get(
            "exclude_words", DEFAULT_EXCLUDE_WORDS)))
        self._apply_deltas(rows)
        self.site_rows[name] = rows
        self.results = [r for rs in self.site_rows.values() for r in rs]
        self.all_rows = self.results
        self.view = self._kind_filtered(self.results)
        self.rows_token += 1
        PERF.mark_first_rows()
        done = sum(len(v) for v in self.site_rows.values())
        self._post_status(gen, "تم جلب %s: %d منتج (إجمالي %d)"
                          % (name, len(rows), done))
        self._push_results()

    def _apply_deltas(self, rows):
        """Price change vs the previous run (from prevrun.json)."""
        baseline = getattr(self, "_baseline", {}) or {}
        empty = getattr(self, "_baseline_empty", True)
        for r in rows:
            key = "%s||%s" % (r.get("site"), r.get("link"))
            old = baseline.get(key)
            if old is None:
                r["change"] = None
                r["is_new"] = not empty
            else:
                diff = round(r["after"] - old, 2)
                r["change"] = diff if diff != 0 else None
                r["is_new"] = False
            r["note"] = r.get("note") or ""
            self.prev[key] = r["after"]

    def _enrich_progressive(self, gen, query, collected):
        """Fill in coupons after the rows are already visible.

        Only rows the النوع filter would actually show are visited, capped per
        site, fresh cache entries are skipped, and each finished batch is
        pushed to the table as it lands.
        """
        for name, rows in list(collected.items()):
            if gen != self.search_gen:
                return
            site = next((s for s in self.sites
                         if s.get("name") == name), None)
            if site is None or not site.get("detail_pages"):
                continue
            cap = int(site.get("detail_cap", 10))
            self._enrich_site(site, rows, cap, gen)

    def _enrich_site(self, site, rows, cap, gen):
        name = site.get("name", "?")
        # Only rows the النوع filter will actually show are worth opening a
        # page for; a row that stays hidden never triggers a visit.
        if self.kind == KIND_ACCESSORIES:
            visible = [r for r in rows if r.get("kind") == "accessory"]
        elif self.kind == KIND_DEVICES:
            visible = [r for r in rows if r.get("kind") == "device"]
        else:
            visible = list(rows)
        # 1) cache: free, so it runs over every row
        with PERF.stage("coupon:cache-lookup", name):
            pending = []
            for r in rows:
                if (r.get("discount") or 0) > 0:
                    continue
                pct, code = cached_detail(r["link"])
                if pct:
                    apply_coupon(r, pct, code)
                    self._republish(gen)
                elif r in visible:
                    pending.append(r)
        if not pending:
            PERF.note("%s: coupon cache covered every target" % name)
            return
        PERF.note("%s: %d coupon pages to open (cap %d)"
                  % (name, len(pending), cap))
        self._post_status(gen, "جاري فحص %d كوبون في %s..."
                          % (len(pending), name))
        self._push_status()
        urls = [r["link"] for r in pending[:cap]]
        batch = int(site_detail_batch(name))
        targets = pending[:cap]
        done = 0
        # Flag the rows so the table can show a quiet "updating" mark while
        # their product page is open. Rows are already visible.
        self.pending_coupon.update(urls)
        self._republish(gen)
        with PERF.stage("coupon:fetch", name):
            for i in range(0, len(urls), batch):
                if gen != self.search_gen:
                    self.pending_coupon.difference_update(urls)
                    return
                chunk = urls[i:i + batch]
                try:
                    if site.get("use_playwright"):
                        texts = fetch_page_texts_playwright(
                            chunk,
                            page_timeout=site.get("detail_timeout_ms", 30000),
                            site_key=name)
                    else:
                        texts = fetch_detail_texts_http(
                            chunk,
                            workers=min(batch, detail_workers(site)),
                            site_key=name)
                except Exception:
                    logging.exception("coupon batch failed: %s", name)
                    texts = [""] * len(chunk)
                for r, text in zip(targets[i:i + batch], texts):
                    pct, code = (None, None)
                    if text:
                        pct, code = parse_coupon_badge(text)
                    store_detail(r["link"], pct, code)
                    if pct:
                        apply_coupon(r, pct, code)
                    done += 1
                # each finished batch clears its own rows and repaints
                self.pending_coupon.difference_update(chunk)
                self._republish(gen)
                if i + batch < len(urls):
                    time.sleep(random.uniform(0.3, 0.9))
        PERF.note("%s: %d coupon pages visited" % (name, done))

    def _republish(self, gen):
        """Push updated rows without touching the status line."""
        if gen != self.search_gen:
            return
        self.results = [r for rs in self.site_rows.values() for r in rs]
        self.all_rows = self.results
        self.rows_token += 1
        self.view = self._kind_filtered(self.results)
        self._push_results()

    def _watchdog(self, gen):
        # Last-resort reset for a stuck search; a late worker is still
        # accepted (same gen).
        if self.searching and gen == self.search_gen:
            logging.warning("search watchdog fired")
            self.searching = False
            self._set_status("استغرق البحث وقتًا أطول من المتوقع، يُرجى إعادة البحث")

    def _on_results(self, rows, failed, gen=None):
        if gen is not None and gen != self.search_gen:
            logging.info("ignoring stale results (gen %s != %s)",
                         gen, self.search_gen)
            return
        self.searching = False
        rows = [r for rs in self.site_rows.values() for r in rs]
        self.all_rows = rows  # raw, before user filters
        # Tag every row once; the page filters on the tag instantly.
        classify_rows(rows, parse_exclude_words(self.settings.get(
            "exclude_words", DEFAULT_EXCLUDE_WORDS)))
        filtered = self._kind_filtered(rows)
        # Price changes vs the previous run were applied per site as its rows
        # streamed in (kept in prevrun.json across restarts).
        self.results = rows
        self.rows_token += 1
        save_prevrun(self.prev, self.site_stats)
        self.failed_sites = list(failed)
        self.search_count += 1
        self.last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._schedule_next(gen)
        with PERF.stage("write:excel", "(autosave)"):
            try:
                if filtered:
                    path = os.path.join(data_dir(), AUTOSAVE_FILE)
                    write_excel(filtered, path)
                    self.last_saved_path = path
                autosave_ok = bool(filtered)
            except Exception:
                logging.exception("autosave failed")
                autosave_ok = False
        msg = "تم تحديث %d منتج" % len(filtered)
        if not filtered and not failed:
            msg = "لا توجد نتائج، يُرجى تجربة كلمة بحث أخرى"
        hidden = len(rows) - len(filtered)
        if hidden:
            msg += " (%d مستبعد)" % hidden
        ndrop = sum(1 for r in filtered if (r.get("change") or 0) < 0)
        if ndrop:
            msg += "، %d منتج سعره نزل" % ndrop
        if failed:
            msg += "، " + "، ".join("موقع %s لا يستجيب حاليًا" % n for n in failed)
        if not autosave_ok:
            msg += "، تعذر حفظ الملف"
        self._set_status(msg)
        logging.info("search done: %d shown / %d scraped, failed=%s",
                     len(filtered), len(rows), failed)
        self._push_results()

    # ----- scheduler (no toolkit needed) -----
    def site_interval(self, site):
        """A site may declare its own refresh cadence ("refresh_sec"):
        Dubai Phone is heavy and rate limited, 2B follows the global 10 min.
        """
        try:
            return int(site.get("refresh_sec") or AUTO_REFRESH_SEC)
        except (TypeError, ValueError):
            return AUTO_REFRESH_SEC

    def _schedule_next(self, gen=None):
        """Arm each site's next refresh and the countdown the page shows."""
        now = time.time()
        disabled = set(self.disabled_sites())
        soonest = None
        for s in self.sites:
            name = s.get("name", "?")
            if name in disabled:
                self.site_next_at.pop(name, None)
                continue
            if site_in_backoff(name):
                # Cooling down: wake up when the cooldown ends, never sooner
                # (no hammering a rate-limited store).
                due = _site_backoff.get(name, now + self.site_interval(s))
                self.site_next_at[name] = due
                soonest = due if soonest is None else min(soonest, due)
                continue
            due = now + self.site_interval(s)
            self.site_next_at[name] = due
            soonest = due if soonest is None else min(soonest, due)
        self.next_refresh_at = soonest or 0.0

    def _due_sites(self, only_due=False):
        """Sites to scrape now: all enabled ones, or only the ones whose own
        interval has elapsed (automatic refresh). Sites cooling down after a
        checkpoint are skipped until the cooldown ends."""
        disabled = set(self.disabled_sites())
        enabled = [s for s in self.sites if s.get("name") not in disabled]
        if not only_due:
            return enabled
        now = time.time()
        due = []
        for s in enabled:
            name = s.get("name", "?")
            if site_in_backoff(name):
                continue
            if now >= self.site_next_at.get(name, 0):
                due.append(s)
        return due

    def _scheduler_loop(self):
        while True:
            try:
                if (self.auto_refresh and self.last_query
                        and not self.searching
                        and self.next_refresh_at > 0
                        and time.time() >= self.next_refresh_at):
                    due = self._due_sites(only_due=True)
                    if due:
                        self.start_search(self.last_query, sites=due)
                    else:
                        self._schedule_next()
            except Exception:
                logging.exception("scheduler tick failed")
            time.sleep(1.0)

    def countdown_sec(self):
        if not (self.auto_refresh and self.last_query
                and self.next_refresh_at > 0):
            return None
        return max(0, int(self.next_refresh_at - time.time()))

    # ----- snapshot for the UI (JSON-safe) -----
    def get_status(self):
        with self._lock:
            msg = self.status_msg
        return {"message": msg,
                "searching": self.searching,
                "countdown_sec": self.countdown_sec(),
                "auto_refresh": bool(self.auto_refresh),
                "saved_query": self.saved_query,
                "kind": self.kind,
                "last_query": self.last_query,
                "shown": len(self.view or self.results),
                "scraped": len(self.all_rows),
                "discounted": sum(1 for r in self.results
                                  if (r.get("discount") or 0) > 0),
                "updated_at": self.last_update,
                "rows_token": self.rows_token,
                "failed": list(self.failed_sites),
                "site_stats": {k: dict(v) for k, v in self.site_stats.items()},
                "has_file": bool(self.last_saved_path
                                 and os.path.exists(self.last_saved_path)),
                "search_count": self.search_count}

    def get_results(self):
        """Every scraped row for the page.

        The page owns all filtering (kind, sites, price range, discount,
        text, sort) so the controls respond instantly; `kind` and `enriching`
        are the only hints it needs from here.
        """
        pending = self.pending_coupon
        out = []
        for r in self.results:
            out.append({"site": r.get("site", ""),
                        "title": r.get("title", ""),
                        "before": r.get("before"),
                        "after": r.get("after"),
                        "discount": r.get("discount", 0.0),
                        "link": r.get("link", ""),
                        "image": r.get("image", ""),
                        "timestamp": r.get("timestamp", ""),
                        "note": r.get("note", ""),
                        "change": r.get("change"),
                        "is_new": bool(r.get("is_new")),
                        "kind": r.get("kind", "device"),
                        "enriching": r.get("link") in pending})
        return out

    def get_sites(self):
        stats = self.site_stats
        out = []
        for s in self.sites:
            name = s.get("name", "?")
            st = stats.get(name, {})
            out.append({"name": name,
                        "pattern": s.get("search_url", ""),
                        "enabled": name not in self.disabled_sites(),
                        "status": st.get("status", "idle"),
                        "rows": st.get("rows", 0),
                        "duration_sec": st.get("duration_sec", 0)})
        return out

    def set_site_enabled(self, name, on):
        dis = set(self.disabled_sites())
        if on:
            dis.discard(name)
        else:
            dis.add(name)
        self.settings["disabled_sites"] = sorted(dis)
        self.save_settings()
        return True

    def set_columns(self, cols):
        cols = [c for c in (cols or []) if c in ALL_COLUMNS]
        if not cols:
            cols = list(DEFAULT_COLUMNS)
        self.settings["visible_columns"] = cols
        self.save_settings()
        return True

    def set_view_filters(self, f_min, f_max, f_disc):
        self.settings["f_min"] = f_min or ""
        self.settings["f_max"] = f_max or ""
        self.settings["f_disc"] = f_disc or ""
        self.save_settings()
        return True

    # ----- files -----
    def rows_for_export(self, mode, links=None):
        """Rows for an export.

        "all"   -> everything that was scraped.
        "links" -> exactly the product links the page is currently showing,
                   so the file matches the filtered view on screen.
        """
        if mode == "all":
            return list(self.all_rows)
        if links:
            order = list(dict.fromkeys(links))
            by_link = {}
            for r in self.results:
                by_link.setdefault(r.get("link"), r)
            return [by_link[l] for l in order if l in by_link]
        return list(self.view or self.results)

    def export_excel(self, path, rows=None):
        data = self.results if rows is None else rows
        try:
            write_excel(data, path)
            self.last_saved_path = path
            logging.info("exported %d rows to %s", len(data), path)
            return {"ok": True, "message": "تم حفظ ملف Excel بنجاح"}
        except Exception:
            logging.exception("export failed")
            return {"ok": False, "message": "تعذّر حفظ الملف"}

    def export_csv(self, path, rows=None):
        try:
            import csv
            data = self.results if rows is None else rows
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(EXCEL_HEADER)
                for r in data:
                    w.writerow([r.get("site", ""), r.get("title", ""),
                                r.get("before"), r.get("after"),
                                r.get("discount", 0.0), r.get("link", ""),
                                r.get("timestamp", "")])
            self.last_saved_path = path
            logging.info("exported %d rows to %s", len(data), path)
            return {"ok": True, "message": "تم حفظ ملف CSV بنجاح"}
        except Exception:
            logging.exception("csv export failed")
            return {"ok": False, "message": "تعذّر حفظ الملف"}

    # ----- in-app update -----
    def _update_manifest(self, max_age=None):
        """(latest_version, download_url) from the manifest, or (None, None).

        Never raises: the caller turns None into a plain Arabic sentence.

        Both fields are checked here, before either reaches subprocess.Popen
        in install_update(). The manifest is only a file on a web server, so a
        typo, a half-finished upload or a hijacked host would otherwise end
        with an arbitrary URL being downloaded and run with the user's rights.
        Refusing a malformed manifest makes the worst case "no update
        offered" rather than "ran something".
        """
        if not UPDATE_URL or "YOUR-ACCOUNT" in UPDATE_URL:
            return None, None
        # Served from memory when it is still fresh, so a repeating check
        # costs no request. Only a manifest that parsed and validated is
        # ever remembered: a rejected one must be re-fetched, not cached.
        if (max_age is not None and self._manifest_cache is not None
                and self._manifest_time + max_age > time.time()):
            return self._manifest_cache
        try:
            r = get_session("update").get(UPDATE_URL, timeout=10)
            r.raise_for_status()
            lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
        except Exception:
            logging.exception("update manifest fetch failed")
            return None, None
        if not lines:
            logging.warning("update manifest is empty")
            return None, None
        version = lines[0]
        if not _VERSION_RE.match(version):
            # version_tuple() is the right tool for *comparing* two versions,
            # but it cannot validate one: with no digits it falls back to (0,),
            # which is still a usable tuple. So check the shape directly.
            logging.warning("update manifest has no usable version: %r",
                            version[:40])
            return None, None
        url = lines[1] if len(lines) > 1 else ""
        if not url.startswith("https://"):
            # https only, so a manifest cannot move the download to plain http
            # where the setup could be swapped in transit.
            logging.warning("update manifest url is not https: %r", url[:80])
            return None, None
        # install_update downloads this and runs it as an executable, so
        # anything that is not a setup is a mistake rather than an update. The
        # query is dropped first: a signed or token-bearing release URL is
        # still a .exe behind the "?", and install_update already splits on it
        # to name the file.
        path = url.split("?")[0].split("#")[0]
        if not path.lower().endswith(".exe"):
            logging.warning("update manifest url is not a .exe: %r", url[:80])
            return None, None
        self._manifest_cache = (version, url)
        self._manifest_time = time.time()
        return version, url

    def check_update(self, max_age=None):
        """Ask the manifest whether a newer build exists.

        Returns a dict the page renders directly. Nothing technical reaches
        the screen: a network problem is reported as a plain Arabic sentence,
        the detail goes to app.log.
        """
        out = {"ok": False, "current": APP_VERSION, "latest": APP_VERSION,
               "has_update": False, "message": ""}
        if not UPDATE_URL or "YOUR-ACCOUNT" in UPDATE_URL:
            out["message"] = "خدمة التحديث غير مُفعَّلة بعد"
            return out
        latest, _url = self._update_manifest(max_age=max_age)
        if not latest:
            out["message"] = "تعذّر التحقق من وجود تحديث، يُرجى المحاولة لاحقًا"
            return out
        out["latest"] = latest
        if version_tuple(latest) > version_tuple(APP_VERSION):
            out["ok"] = True
            out["has_update"] = True
            out["message"] = "يتوفر إصدار جديد: %s" % latest
        else:
            out["message"] = "البرنامج محدّث"
        return out

    def install_update(self):
        """Download the new setup named in the manifest and run it silently.

        Inno Setup does the rest: the same AppId replaces the installed files,
        and because the app lives in %LOCALAPPDATA% the user is never asked
        for administrator rights. The user's settings live in %APPDATA% and
        are untouched.
        """
        if not UPDATE_URL or "YOUR-ACCOUNT" in UPDATE_URL:
            return {"ok": False, "message": "خدمة التحديث غير مُفعَّلة بعد"}
        latest, url = self._update_manifest()
        if not url:
            return {"ok": False, "message": "تعذّر التحقق من وجود تحديث، يُرجى المحاولة لاحقًا"}
        if version_tuple(latest) <= version_tuple(APP_VERSION):
            return {"ok": False, "message": "البرنامج محدّث"}
        name = os.path.basename(url.split("?")[0]) or "PriceTracker-Setup.exe"
        # Never download/run the setup from a UNC path (roaming %APPDATA%
        # on a domain): cmd/start show "The network path was not found."
        run_dir = _local_run_dir()
        dest = os.path.join(run_dir, name)
        logging.info("update download dir: %s (data_dir=%s app_dir=%s)",
                     run_dir, data_dir(), app_dir())
        try:
            with get_session("update").get(url, timeout=60, stream=True) as r:
                r.raise_for_status()
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        if chunk:
                            f.write(chunk)
            os.replace(tmp, dest)
        except Exception:
            logging.exception("update download failed")
            return {"ok": False, "message": "فشل تحميل التحديث، يُرجى المحاولة لاحقًا"}
        # The setup overwrites the files this process is running from, so this
        # process has to be gone before the installer reaches them. /VERYSILENT
        # keeps the user from seeing a wizard, and /DIR pins it to this install
        # so the update lands in place.
        args = [dest, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                "/CURRENTUSER", "/DIR=%s" % app_dir()]
        logging.info("launching update installer: %s", dest)
        try:
            inst = subprocess.Popen(args, close_fds=True, cwd=run_dir)
        except OSError:
            logging.exception("update installer failed to start")
            return {"ok": False, "message": "مقدرنا نشغل التحديث"}

        # Closing the window is not the end of the update: the user asked for a
        # new build, not for a closed program, so the app has to come back.
        #
        # It cannot be done from here. This process is destroyed seconds later
        # (os._exit below), so a thread waiting on the installer dies with it.
        # It is not done by the installer either: the [Run] entry in
        # installer.iss carries skipifsilent, and a silent run is what an update
        # must be, so it is skipped by design. Adding an /AUTOREL entry and
        # passing /MERGETASKS was tried and does not fire, so it is not relied on.
        #
        # What works is a small helper script in a process that outlives this
        # one: it polls until the installer has exited, then starts the new
        # build. Polling for the PID is what orders the two correctly. Starting
        # the app on a timer instead would race a 26 MB install and could open
        # the old files again.
        exe = os.path.join(app_dir(), "PriceTracker.exe")
        if os.path.exists(exe) and not exe.startswith("\\\\"):
            helper = os.path.join(run_dir, "relaunch_after_update.vbs")
            try:
                _write_relaunch_helper(helper, inst.pid,
                                       os.path.basename(dest), exe)
                # wscript takes the script path as one plain argument and
                # shows no console, so there is no `cmd /c start` quoting
                # layer left to mangle it.
                #
                # The path is passed plain, with no `//?` prefix. wscript
                # cannot open an extended-length path: it ignores the prefix,
                # reports "The system cannot find the path specified." in its
                # own modal "Windows Script Host" box, and never runs the
                # script. That box is invisible to us, so the app would just
                # never reopen. The prefix is also not needed here -- the
                # helper lives in %LOCALAPPDATA%\PriceTrackerUpdate, which is
                # far below the 260 character limit it exists for.
                subprocess.Popen(
                    ["wscript.exe", "//nologo", helper],
                    close_fds=True,
                    env=_relaunch_env(exe, os.path.basename(dest), helper),
                    cwd=run_dir,
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logging.info("update will relaunch %s once pid %s exits",
                             exe, inst.pid)
            except OSError:
                logging.exception("could not schedule the relaunch")
        elif os.path.exists(exe):
            logging.warning("app runs from a network path (%s), "
                            "skipping auto-relaunch", exe)
        else:
            # Running from source, or a build that names its exe differently:
            # there is nothing to reopen, which is the right outcome.
            logging.info("no exe at %s, not scheduling a relaunch", exe)

        # Wait for the installer, then close. The window has to go either way,
        # so a failed wait must not keep it on screen.
        def _close_when_done():
            try:
                inst.wait(timeout=UPDATE_EXIT_WAIT_SEC)
            except Exception:
                logging.warning("installer wait ended early", exc_info=True)
            win = self._window
            try:
                if win is not None:
                    win.destroy()
            except Exception:
                logging.exception("could not close the window for the update")
            BROWSER.close()
            os._exit(0)

        threading.Thread(target=_close_when_done, daemon=True).start()
        return {"ok": True,
                "message": "يجري التحديث، وسيُفتح البرنامج تلقائيًا بعد قليل"}

    def open_excel_file(self):
        if not self.last_saved_path or not os.path.exists(self.last_saved_path):
            return {"ok": False, "message": "لا يوجد ملف Excel بعد، يُرجى إجراء البحث أولًا"}
        try:
            os.startfile(self.last_saved_path)
            return {"ok": True, "message": ""}
        except OSError:
            logging.exception("open excel failed")
            return {"ok": False, "message": "تعذر فتح الملف"}


class Api:
    """js_api bridge: plain-JSON methods the page calls."""

    def __init__(self, core):
        self._core = core
        self._window = None

    def log_message(self, level, msg):
        """Page-side log sink: JS errors and failed bridge calls land here."""
        getattr(logging, level if level in ("info", "warning", "error")
                else "info")("page: %s", msg)
        return True

    def get_status(self):
        return self._core.get_status()

    def get_results(self):
        return self._core.get_results()

    def get_sites(self):
        return self._core.get_sites()

    def get_settings(self):
        st = self._core.settings
        cols = st.get("visible_columns", DEFAULT_COLUMNS)
        cols = [c for c in cols if c in ALL_COLUMNS] or list(DEFAULT_COLUMNS)
        return {"kind": self._core.kind,
                "theme": self._core.settings.get("theme", ""),
                "auto_refresh": bool(self._core.auto_refresh),
                "refresh_sec": self._core.refresh_every,
                "exclude_words": st.get("exclude_words", DEFAULT_EXCLUDE_WORDS),
                "min_price": st.get("min_price", ""),
                "disabled_sites": st.get("disabled_sites", []),
                "visible_columns": cols,
                "f_min": st.get("f_min", ""),
                "f_max": st.get("f_max", ""),
                "f_disc": st.get("f_disc", "")}

    def set_refresh_sec(self, sec):
        return {"ok": self._core.set_refresh_sec(sec)}

    def check_update(self, max_age=None):
        # max_age comes from the page so a repeating check is answered
        # from the manifest cache instead of the network.
        return self._core.check_update(max_age=max_age)

    def install_update(self):
        """The page closes the window after this returns ok, so the installer
        can replace the running files."""
        return self._core.install_update()

    def app_version(self):
        return {"version": APP_VERSION}

    def get_logs(self, limit=60):
        try:
            return self._core.get_logs(int(limit or 60))
        except (TypeError, ValueError):
            return []

    def search(self, keyword):
        ok = self._core.start_search(keyword or "")
        return {"ok": ok, "message": self._core.get_status()["message"]}

    def set_kind(self, kind):
        """Persists the choice; the page re-filters its own rows instantly."""
        return {"ok": self._core.set_kind(kind or "")}

    def set_auto(self, on):
        self._core.set_auto(bool(on))
        return {"ok": True}

    def set_theme(self, theme):
        return {"ok": self._core.set_theme(theme or "")}

    def set_advanced(self, exclude_words, min_price):
        """Saves the advanced filters; the page picks up the new minimum."""
        self._core.set_advanced(exclude_words or "", min_price or "")
        return {"ok": True}

    def set_site_enabled(self, name, on):
        return {"ok": self._core.set_site_enabled(name or "", bool(on))}

    def set_columns(self, cols):
        return {"ok": self._core.set_columns(list(cols or []))}

    def set_view_filters(self, f_min, f_max, f_disc):
        self._core.set_view_filters(f_min or "", f_max or "", f_disc or "")
        return {"ok": True}

    def open_link(self, url):
        try:
            webbrowser.open(url or "")
            return {"ok": True}
        except Exception:
            logging.exception("open link failed")
            return {"ok": False}

    def _save_dialog(self, filename, file_types):
        if self._window is None:
            return ""
        try:
            import webview
            path = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=filename,
                file_types=file_types)
        except Exception:
            logging.exception("save dialog failed")
            return ""
        if not path:
            return ""
        if isinstance(path, (list, tuple)):
            path = path[0] if path else ""
        return path or ""

    def _rows_or_empty(self, mode, links):
        rows = self._core.rows_for_export(mode or "filtered", links or [])
        if not rows:
            return None, {"ok": False,
                          "message": "لا توجد صفوف للتصدير"}
        return rows, None

    def export_excel(self, mode="filtered", links=None):
        rows, err = self._rows_or_empty(mode, links)
        if err:
            return err
        path = self._save_dialog("prices.xlsx", ("Excel (*.xlsx)",))
        if not path:
            return {"ok": False, "message": ""}
        return self._core.export_excel(path, rows)

    def export_csv(self, mode="filtered", links=None):
        rows, err = self._rows_or_empty(mode, links)
        if err:
            return err
        path = self._save_dialog("prices.csv", ("CSV (*.csv)",))
        if not path:
            return {"ok": False, "message": ""}
        return self._core.export_csv(path, rows)

    def open_excel(self):
        return self._core.open_excel_file()


def _write_relaunch_helper(path, installer_pid, setup_image, exe):
    """Write the script that reopens the app after an update.

    This is a .vbs run by wscript.exe, not a .cmd, and that is the whole fix
    for the failure it replaces. The batch version had to be started through
    `cmd /c start "" "<path>"`, and cmd's quoting rules cut that argument in
    half: the user saw "Windows cannot find '\\\\'" and the app never came
    back. VBScript takes the path as a plain string and never re-parses it,
    and wscript runs it with no console at all. It must also be started with
    a plain path: see the `//?` note at the call site.

    It polls for the installer's PID rather than sleeping a fixed time: the
    install writes roughly 26 MB and the machine's speed is not ours to
    assume, so starting the app on a timer risks opening the old files.
    """
    # The script does not delete itself: the host holds it open while it
    # runs. Instead the next update removes it -- see the unlink below. The
    # path never changes, so at most one stale helper exists and it is
    # replaced before use.
    # The paths are handed over in environment variables, not written into
    # the script. The audience installs under an Arabic user name, and this
    # file cannot carry that: wscript reads a .vbs as ANSI, so a path written
    # as UTF-8 turns to mojibake inside the string literal and FileExists then
    # never matches; written as the ANSI codepage instead, it raises
    # UnicodeEncodeError outright on a Western machine. Environment variables
    # have neither problem -- they are Unicode end to end -- so the script
    # stays pure ASCII and still gets the real path. See the call site for the
    # env this reads.
    body = (
        "' Written by price_tracker.py. Relaunches the app after an update.\r\n"
        "' Pure ASCII on purpose: the paths arrive in environment variables\r\n"
        "' because this file is read as ANSI and cannot hold an Arabic path.\r\n"
        "'\r\n"
        "' Process checks go through tasklist, not WMI: on the machines this\r\n"
        "' has to survive, connecting to winmgmts:\\\\.\\root\\cimv2 fails with\r\n"
        "' \"Generic failure\" and every check would then read as \"gone\".\r\n"
        "Option Explicit\r\n"
        "\r\n"
        "Dim fso, log, pid, waited, tries, sh, err, text\r\n"
        "Dim exe, exeImage, logPath, setupImage\r\n"
        "Set fso = CreateObject(\"Scripting.FileSystemObject\")\r\n"
        "Set sh = CreateObject(\"WScript.Shell\")\r\n"
        "exe = sh.ExpandEnvironmentStrings(\"%%PRICE_TRACKER_EXE%%\")\r\n"
        "logPath = sh.ExpandEnvironmentStrings(\"%%PRICE_TRACKER_RELAUNCH_LOG%%\")\r\n"
        "setupImage = sh.ExpandEnvironmentStrings(\"%%PRICE_TRACKER_SETUP_IMAGE%%\")\r\n"
        "exeImage = fso.GetFileName(exe)\r\n"
        "pid = %d\r\n"
        "Set log = fso.CreateTextFile(logPath, True)\r\n"
        "log.WriteLine Now & \" waiting for installer pid \" & pid\r\n"
        "\r\n"
        "' A PID alone is not proof: Windows recycles PIDs, so the process\r\n"
        "' image name must match the setup that was just run as well.\r\n"
        "waited = 0\r\n"
        "Do While IsRunning(\"PID eq \" & pid, setupImage)\r\n"
        "  waited = waited + 1\r\n"
        "  If waited > 300 Then Exit Do\r\n"
        "  WScript.Sleep 2000\r\n"
        "Loop\r\n"
        "log.WriteLine Now & \" installer gone, launching the app\"\r\n"
        "\r\n"
        "' A short pause lets the installer release the exe.\r\n"
        "WScript.Sleep 2000\r\n"
        "\r\n"
        "' Guard the relaunch: running a missing path pops an error box.\r\n"
        "If Len(exe) = 0 Or Not fso.FileExists(exe) Then\r\n"
        "  log.WriteLine Now & \" exe missing, giving up\"\r\n"
        "  log.Close\r\n"
        "  WScript.Quit 0\r\n"
        "End If\r\n"
        "\r\n"
        "' Retry: a launch against half-replaced files dies silently.\r\n"
        "tries = 0\r\n"
        "Do\r\n"
        "  sh.CurrentDirectory = fso.GetParentFolderName(exe)\r\n"
        "  err = \"\"\r\n"
        "  On Error Resume Next\r\n"
        "  sh.Run \"\"\" & exe & \"\"\", 1, False\r\n"
        "  If Err.Number <> 0 Then err = Err.Description\r\n"
        "  Err.Clear\r\n"
        "  On Error GoTo 0\r\n"
        "  If err = \"\" Then\r\n"
        "    log.WriteLine Now & \" launch attempt \" & tries + 1 & \" ok\"\r\n"
        "  Else\r\n"
        "    log.WriteLine Now & \" launch attempt \" & tries + 1 & \" \" & err\r\n"
        "  End If\r\n"
        "  WScript.Sleep 4000\r\n"
        "  If IsRunning(\"IMAGENAME eq \" & exeImage, exeImage) Then\r\n"
        "    log.WriteLine Now & \" app is running\"\r\n"
        "    log.Close\r\n"
        "    WScript.Quit 0\r\n"
        "  End If\r\n"
        "  tries = tries + 1\r\n"
        "  If tries > 5 Then\r\n"
        "    log.WriteLine Now & \" FAILED to start the app\"\r\n"
        "    log.Close\r\n"
        "    WScript.Quit 1\r\n"
        "  End If\r\n"
        "Loop\r\n"
        "\r\n"
        "\r\n"
        "' True when tasklist reports a process matching the filter AND the\r\n"
        "' image name. Two conditions on purpose: a bare PID can be recycled\r\n"
        "' by an unrelated process while we wait.\r\n"
        "Function IsRunning(filter, image)\r\n"
        "  Dim cmd, out, f, line, found\r\n"
        "  out = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), _\r\n"
        "    \"tasklist.tmp\")\r\n"
        "  cmd = \"%%comspec%% /d /c tasklist /fi \"\"\" & filter & _\r\n"
        "    \"\"\" /fo csv /nh > \"\"\" & out & \"\"\" 2>nul\"\r\n"
        "  sh.Run cmd, 0, True\r\n"
        "  found = False\r\n"
        "  If fso.FileExists(out) Then\r\n"
        "    Set f = fso.OpenTextFile(out, 1)\r\n"
        "    Do Until f.AtEndOfStream\r\n"
        "      line = f.ReadLine\r\n"
        "      If InStr(1, line, image, vbTextCompare) > 0 Then found = True\r\n"
        "    Loop\r\n"
        "    f.Close\r\n"
        "    fso.DeleteFile out, True\r\n"
        "  End If\r\n"
        "  IsRunning = found\r\n"
        "End Function\r\n"
    ) % installer_pid
    # Remove the previous helper first. It is dead by now: the app it was
    # waiting on has exited, and its host process is gone with it. Best
    # effort, because a locked file is not worth failing an update over.
    try:
        os.remove(path)
    except OSError:
        pass
    # Every path now travels in an environment variable, so this file is pure
    # ASCII and can be written as ASCII. That is what keeps an Arabic install
    # path working: wscript reads the script as ANSI, so anything non-ASCII
    # written here would be mangled or rejected. Write it strictly, and fail
    # loudly in app.log if a future edit reintroduces a non-ASCII character,
    # rather than shipping a helper that dies silently on launch.
    try:
        with io.open(path, "w", encoding="ascii", newline="") as f:
            f.write(body)
    except UnicodeEncodeError:
        logging.exception("relaunch helper is not ASCII; an Arabic install "
                          "path would break the relaunch")
        raise
    logging.info("relaunch helper written to %s (exe=%s)", path, exe)


def _relaunch_env(exe, setup_image, helper_path):
    """Environment for the helper's wscript process.

    The paths go here rather than into the script because environment
    variables are Unicode, and the script file cannot be: see
    _write_relaunch_helper.
    """
    env = dict(os.environ)
    env["PRICE_TRACKER_EXE"] = exe
    env["PRICE_TRACKER_SETUP_IMAGE"] = setup_image
    env["PRICE_TRACKER_RELAUNCH_LOG"] = os.path.join(
        os.path.dirname(helper_path), "relaunch.log")
    return env


def _resource(*parts):
    """Resolve a bundled file: beside the program first, else the frozen
    bundle dir (PyInstaller onedir puts --add-data under _MEIPASS/_internal)."""
    here = os.path.join(app_dir(), *parts)
    if os.path.exists(here):
        return here
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        bundled = os.path.join(bundle, *parts)
        if os.path.exists(bundled):
            return bundled
    return here


def ui_entry():
    return _resource("ui", "index.html")


def launch_gui(test_close_sec=None):
    """Open the native desktop window (Edge WebView2 on Windows)."""
    import webview

    core = TrackerCore()
    api = Api(core)
    if core.sites_error:
        logging.error("sites problem: %s", core.sites_error)
    window = webview.create_window("متتبع الأسعار", url=ui_entry(),
                                   js_api=api, width=1280, height=800,
                                   min_size=(900, 600))
    api._window = window
    core._window = window

    def _ensure_bg():
        ok = ensure_chromium(
            progress=lambda m: core._set_status(m))
        if not ok:
            core._set_status("تعذّر تجهيز المتصفح، يُرجى مراجعة ملف app.log")

    if any(s.get("use_playwright") for s in core.sites):
        core._set_status("يجري تجهيز المتصفح للمرة الأولى...")
        threading.Thread(target=_ensure_bg, daemon=True).start()

    if test_close_sec:
        def _killer():
            time.sleep(test_close_sec)
            try:
                window.destroy()
            except Exception:
                pass
        threading.Thread(target=_killer, daemon=True).start()
    try:
        webview.start(debug=False)
    except Exception as e:
        logging.exception("webview failed to start")
        if "WebView2" in str(e) or "Edge" in str(e):
            page = _resource("ui", "webview2-missing.html")
            try:
                webbrowser.open("file://" + page.replace(os.sep, "/"))
            except Exception:
                pass
            print("WebView2 is missing. Install it from the page that opened, "
                  "then restart the app.")
        raise SystemExit(1)
    finally:
        # Never leave a headless browser behind after the window closes.
        BROWSER.close()


def run_selftest(query="iphone"):
    """Headless check used to test the build: scrape every site in
    sites.json concurrently with the saved (or default) kind pipeline and
    write prices.xlsx next to the program. Returns a process exit code."""
    sites, err = load_sites()
    if err:
        print("SITES ERROR:", err)
        return 1
    settings = load_settings()
    kind = settings.get("kind", KIND_DEVICES)
    excl = parse_exclude_words(settings.get("exclude_words",
                                            DEFAULT_EXCLUDE_WORDS))
    minp = parse_min_price(settings.get("min_price", ""))
    print("selftest query=%s kind=%s" % (query, kind))
    all_rows = []
    ok = True
    PERF.begin("selftest:%s" % query)

    def one(site):
        t0 = time.perf_counter()
        try:
            raw = scrape_site(site, query)
        except CheckpointError as e:
            # Rate limited right now: the app shows the Arabic notice and
            # retries later, so this is not a broken build.
            logging.info("selftest site %s is rate limited: %s",
                         site.get("name"), e)
            note_backoff(site.get("name", "?"))
            return site, None, round(time.perf_counter() - t0, 1), e
        except Exception as e:
            logging.exception("selftest site failed: %s", site.get("name"))
            return site, None, round(time.perf_counter() - t0, 1), e
        classify_rows(raw, excl)
        return site, raw, round(time.perf_counter() - t0, 1), None

    results = []
    pool = ThreadPoolExecutor(max_workers=min(4, len(sites)))
    try:
        for res in pool.map(one, sites):
            results.append(res)
    finally:
        pool.shutdown(wait=True)
    for site, raw, dt, err in results:
        name = site.get("name")
        if raw is None:
            if isinstance(err, CheckpointError):
                print("%s: rate limited after %.1fs (%s) - will retry later"
                      % (name, dt, err))
                PERF.note("%s: rate limited, skipped" % name)
            else:
                print("%s: FAILED in %.1fs (%s) - see app.log" % (name, dt, err))
                ok = False
            continue
        rows = kind_filtered_view(raw, kind, minp)
        print("%s: scraped=%d kept=%d time=%.1fs channel=%s"
              % (name, len(raw), len(rows), dt, LAST_BROWSER_CHANNEL))
        PERF.note("%s: scraped=%d kept=%d" % (name, len(raw), len(rows)))
        all_rows.extend(rows)
    try:
        path = os.path.join(data_dir(), AUTOSAVE_FILE)
        with PERF.stage("write:excel", "(selftest)"):
            write_excel(all_rows, path)
        print("wrote %s with %d rows" % (path, len(all_rows)))
    except Exception:
        logging.exception("selftest save failed")
        print("SAVE FAILED (see app.log)")
        return 1
    run = PERF.end()
    print(format_timings(run))
    BROWSER.close()
    print("SELFTEST:", "PASS" if (ok and all_rows) else "FAIL")
    return 0 if (ok and all_rows) else 1


def run_profile(query="iphone", limit=300):
    """One real search with the timing breakdown printed (no window)."""
    PERF.begin("search:%s" % query)
    core = TrackerCore()
    print("sites:", [s.get("name") for s in core.sites])
    t0 = time.perf_counter()
    start_count = core.search_count
    core.start_search(query)
    while time.perf_counter() - t0 < limit:
        st = core.get_status()
        if st["searching"] is False and core.search_count > start_count:
            break
        time.sleep(0.2)
    run = PERF.run or (PERF.history[-1] if PERF.history else None)
    if PERF.run:
        run = PERF.end()
    st = core.get_status()
    print()
    print("rows scraped: %d | kept(kind=%s): %d | failed: %s"
          % (st["scraped"], st["kind"], st["shown"], st["failed"]))
    print("site stats:", st.get("site_stats"))
    print("status message:", st["message"])
    print("wall clock total: %.2fs" % (time.perf_counter() - t0))
    print("browser channel:", LAST_BROWSER_CHANNEL,
          "| launches:", BROWSER.launches)
    print(format_timings(run))
    BROWSER.close()
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        q = sys.argv[i + 1] if len(sys.argv) > i + 1 else "iphone"
        raise SystemExit(run_selftest(q))
    if "--profile" in sys.argv:
        i = sys.argv.index("--profile")
        q = sys.argv[i + 1] if len(sys.argv) > i + 1 else "iphone"
        raise SystemExit(run_profile(q))
    launch_gui()


