Price Tracker

A Windows desktop app that searches one keyword across Egyptian phone stores
(2B Egypt, Dubai Phone) and shows real prices with before/after values,
discount percentages, and coupon math — then keeps an Excel record and flags
price drops over time.

Built for people who don't read English and don't edit config files: double-click,
type one word, read prices.

---

## Running from source

Requires **Python 3.12** and **Microsoft Edge WebView2** (ships with Windows 10/11).

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python price_tracker.py
```

Or just double-click **`start.bat`**, which finds the venv (or a system Python) and
launches it.

### Two entry points

| Command | What it does |
|---|---|
| `python price_tracker.py` | Opens the app window. |
| `python price_tracker.py --selftest iphone` | No window. Scrapes every site concurrently, writes `prices.xlsx`, prints a PASS/FAIL and a timing breakdown. This is the build's smoke test. |
| `python price_tracker.py --profile iphone` | No window. One real search with per-stage timings printed. |

## Architecture

Python backend + a local HTML/CSS/vanilla-JS interface, with no frontend
framework and no build step for the UI.

```
price_tracker.py     backend: scraping, filtering, Excel, updates, pywebview bridge
ui/                  the interface (index.html, app.js, styles.css, tokens.css)
ui/fonts/            Thmanyah Sans, self-hosted (5 OTF weights + LICENSE.pdf)
sites.json           store definitions: CSS selectors per site
settings.json        local user state (gitignored; see settings.example.json)
installer/           Inno Setup script for the installable setup
```

- **Scraping runs in background threads.** The window never blocks; rows stream in
  per site as each store finishes.
- **Plain HTTP by default.** Every store page is requested with a browser-shaped
  header set and parsed from server-rendered HTML. Playwright is opt-in per site
  via `"use_playwright": true`.
- **The UI polls a `js_api` bridge.** `Api` (in `price_tracker.py`) is the only
  surface the interface can reach.

### Adding a store

Edit `sites.json`. No Python changes. Each entry needs a `search_url` containing
`{q}`, plus the CSS selectors `item`, `title`, `price_now`, `link` — and
optionally `price_old` and `image`.

```json
{
  "name": "Store Name",
  "search_url": "https://example.com/search?q={q}",
  "item": "div.product-card",
  "title": "h3.title",
  "price_now": "span.price",
  "price_old": "span.old-price",
  "link": "a.product-link"
}
```

Optional keys: `paginate` (`{"param": "page", "max_pages": 5}`),
`page_delay`, `refresh_sec`, `coupon_badge`, `detail_pages`, `detail_cap`,
`use_playwright`.

## Building a release

```bat
build_exe.bat     :: dist\PriceTracker\PriceTracker.exe   (~25 MB, onedir)
build_setup.bat   :: dist-installer\PriceTracker-Setup-1.0.0.exe  (~12 MB)
```

Needs the build deps and Inno Setup 6:

```bat
pip install -r requirements.txt -r requirements-build.txt
winget install JRSoftware.InnoSetup
```

`PriceTracker.spec` is the single source of truth for the build.

**Two deliberate choices in the build**, both worth not undoing:

1. **Playwright is not bundled.** It carries a private copy of node (~89 MB) and
   no configured site needs it. Excluding it took the build from ~128 MB to
   ~25 MB. A site asking for `use_playwright` reports a plain Arabic notice
   instead of crashing.
2. **`pythonnet` and `clr_loader` must stay.** They aren't Playwright's
   dependencies — pywebview loads WebView2 through pythonnet's .NET bridge.
   Excluding them breaks the window entirely.

WebView2 isn't bundled either: Windows 10/11 already ships it, and the app shows a
download page if it's ever missing.

## Where the app writes

The program folder and the data folder are deliberately separate, so the app can
run read-only from Program Files and an in-app update can replace files without a
UAC prompt.

| | Location |
|---|---|
| Program | wherever it's installed (default `%LOCALAPPDATA%\PriceTracker`) |
| `settings.json`, `prices.xlsx`, `detail_cache.json`, `app.log` | `%APPDATA%\PriceTracker` |
| `sites.json` | next to the exe, so users can add a store |

From source, nothing is moved — everything stays in the project folder.

## Design

`DESIGN.md` is the design system: tokens, typography, layout, components, and the
reasoning behind them. `PRODUCT.md` describes the product constraints. Both are
worth reading before changing anything visual.

## In-app updates

Not enabled yet. `UPDATE_URL` in `price_tracker.py` is still a placeholder, and
the app says so in plain Arabic rather than failing. To enable it, point that at a
two-line plain-text manifest you host:

```
1.1.0
https://example.com/PriceTracker-Setup-1.1.0.exe
```

The download URL must be listed, not guessed — the build number isn't known until
the file exists, and a guess would re-download the build the user already has.

Keep `APP_VERSION` in step with `AppVersion` in `installer/installer.iss`.

## License

**No license has been chosen yet.** The repository is public but carries no
stated terms, so "all rights reserved" applies by default and nobody may legally
reuse the code. Add a `LICENSE` file before sharing it beyond this account.

**Thmanyah Sans** (bundled in `ui/fonts/`) is by Boutros Fonts and is licensed
separately — its terms travel with it in `ui/fonts/LICENSE.pdf`.
