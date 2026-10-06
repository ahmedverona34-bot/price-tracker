Price Tracker

A Windows desktop app that searches one keyword across Egyptian phone stores
(2B Egypt, Dubai Phone, Dream 2000, Kimo Store, Compumarts, Miami Centers)
and shows real prices with before/after values,
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
`use_playwright`, `platform` (free-form hint: `shopify`, `woocommerce`,
`magento` — documents which quirk set the entry was verified against).

### Per-platform cheat sheet

Every new store so far turned out to be one of two platforms, each with its
own search/pagination shape. Match the platform first, then verify the
selectors live — class names drift between themes.

| Platform | Stores here | Search URL | Pages | Price quirk |
|---|---|---|---|---|
| Shopify | Dream 2000, Kimo, Compumarts | `/search?q={q}` | `?page=N` (`{"param": "page"}`) | Sale cards split current/compare into separate spans; single-price cards have no compare node (`before = after`) |
| WooCommerce (Woodmart) | Miami Centers | `/?s={q}&post_type=product` (the `post_type` filter keeps blog posts out) | `/page/N/` (`{"path": "/page/{n}/"}`) | Discounted price is `<del>old</del> <ins>new</ins>` inside one `.price` — the parser prefers `<ins>` when both numbers are present |
| Magento 2 | 2B Egypt | `/catalogsearch/result/?q={q}` (locale prefix matters: `/en/…`) | `?p=N` | Compare-at and current share `span.price`, split only by `.old-price` / `.special-price` wrappers |
| Next.js (custom) | Dubai Phone | site-specific (`/search-results?q=`) | `?page=N` | Coupon badges (`coupon_badge: true`): displayed price is pre-coupon |

Two parser fallbacks cover stores whose cards hydrate late or rename classes:
JSON-LD (`@graph` / `ItemList` / `priceSpecification` aware) and lazy-image
`srcset` / `data-srcset` URLs.

### Adding a store from its URL

The Data Sources page (مصادر البيانات) can add a store the user has never
configured: they paste the shop's link and the app works out how to read it.
Pasting either the home page or a working search URL works — a search URL is
easier, because the hard part (the path) is then already supplied.

1. **Find the search URL.** The store's own search form is read first, because
   that is where the parameter name is written down. `miamicenters.com` 404s on
   every conventional pattern (`?q=`, `/search?q=`, `/catalogsearch/result/?q=`)
   and answers on `?s=`, which nothing in the markup would have revealed. The
   conventional patterns follow, for stores that ship no form. Up to
   `_MAX_SEARCH_PROBES` requests, with a short pause between each.

   A WordPress storefront needs the product filter as well: bare `?s=iphone`
   matches every post type and returns blog posts with no prices in them, while
   `?s=iphone&post_type=product` returns the listing. The form's other params
   are carried over, which is how that filter is picked up.
2. **Find how the results paginate.** Page one is not the answer — it is the
   difference between 12 products and 598. The count and the *style* are read
   off the result page's own pager: WordPress permalinks put the number in the
   path (`/ar/page/2/`) while Shopify uses `?page=2`, and guessing the wrong one
   is invisible — every request answers with page 1 again and the search
   reports a full-looking result. Auto-detected walks are capped at
   `_MAX_AUTO_PAGES`.

   **`"max_pages": 0` means unlimited.** The walk then ends on evidence rather
   than on a number: two pages in a row adding nothing new, which is what every
   store does at the end of its results, plus a 404 past the last page. Note
   that a 404 past the end now ends the walk *with what was collected* rather
   than discarding the whole scrape — an unbounded walk always asks for one page
   too many, and 598 good rows should not be lost over page 51.
2. **Guess the selectors.** The listing is the markup structure that repeats on
   the page and carries both a price and a product link, so card candidates are
   ranked on exactly that. Within a card, the title, prices and link are read
   the same way.
3. **Verify by parsing.** Each candidate goes through `_parse_cards` — the same
   code a real search uses — and must produce a believable listing: several
   rows, each with its own distinct link, a title, and prices that add up. Up
   to `_MAX_SELECTOR_TRIES` candidates are tried against the page already in
   hand; nothing is re-requested, because this loop repairs *our* guess, it does
   not ask the store again.
4. **Check the keyword is real.** The winning URL is requested once with a word
   no shop stocks. A page returning the same listing for nonsense is rejected:
   adding it would look healthy while answering every future search identically.
5. **Save atomically.** The entry is appended to `sites.json` through a temp
   file and `os.replace`, so an interrupted write cannot leave a store list that
   fails to parse — which would take every configured store down, not just the
   new one.

A store already present is refused **by host**, not by name. The discovered name
is the domain while an existing entry carries whatever its owner called it
("2B Egypt"), so matching on name alone would add 2B a second time and show
every product twice.

**A store that blocks extraction is reported, not retried.** The probe stops at
the first bot checkpoint (Cloudflare, Vercel BotID — see `looks_like_checkpoint`)
and says so in Arabic, for the reason `fetch_html` already documents: asking a
store that has blocked you again only deepens the block. Likewise a listing that
only exists after JavaScript runs — there is no honest way to read it over plain
HTTP, so the message says results were not found rather than retrying a page
that will never answer. This is a deliberate limit: a loop that retries until a
block lifts cannot converge (a challenge needs real JS execution and a browser
fingerprint, not more HTTP requests), and its only remaining function would be
circumventing an access control the store's owner put in place.

**Two things worth not undoing:**

- **A price must be a leaf.** 2B renders the compare-at and the current price as
  `span.price` *in the same card*, separated only by `.old-price` and
  `.special-price` wrappers. A selector naming the price container matches both,
  and `parse_price` takes the first number — always the higher — so every
  discounted product would silently report its pre-discount price.
  `_is_price_leaf` and `_price_path` exist for exactly this.
- **`parse_price` folds Arabic-Indic digits and Arabic separators** (`٬` U+066C,
  `٫` U+066B). 2B's Arabic pages write "٦٧٬٧٩٩ ج.م."; without the fold that
  reads as `67.0` instead of `67799.0` — wrong by a factor of a thousand, with
  nothing reporting an error.
- **A price is not a number with words attached.** `dream2000`'s title link
  reads "أبل آيفون 15" and `parse_price` returns 15 from the tail of it, so an
  iPhone 15 priced at £69,400 was showing as 15. `_looks_pricey` requires the
  text to be digits, separators and a currency mark; a category tile reading
  "1035 products" is rejected the same way.
- **A link is not an action.** `miamicenters`' add-to-cart button is an `<a>`
  whose href is `…?add-to-cart=<id>` on some pages and a real product URL on
  others, so a selector built from it collapsed 598 products to 460 — every card
  past the 38th page resolved to the same address and was dropped as a
  duplicate. `_usable_href` rejects action-only hrefs, and the image link
  (`/product/<slug>`) is what a card's real link is read from.

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

Enabled. `UPDATE_URL` in `price_tracker.py` points at a two-line plain-text
manifest published from this repo's `gh-pages` branch:

```
1.1.1
https://github.com/ahmedverona34-bot/price-tracker/releases/download/v1.1.1/PriceTracker-Setup-1.1.1.exe
```

The check is **on demand only**: the settings page has a "تحديث البرنامج"
section with the installed version, a *البحث عن تحديث* button, and an install
button that appears only when a newer build is actually found. Nothing is
fetched on launch. A rail dot lights up when an update is pending.

Every failure — no network, empty manifest, malformed line — is reported as a
plain Arabic sentence. The detail goes to `app.log`.

### What the manifest may contain

`_update_manifest` rejects the whole thing unless all three hold, before either
value can reach `subprocess.Popen`:

| Line | Requirement |
|---|---|
| 1 | digits and dots only (`^\d+(?:\.\d+)*$`) — matched, not parsed-and-trimmed, so a trailing note is rejected instead of silently accepted |
| 2 | must start with `https://` — a manifest cannot move the download to plain http where the setup could be swapped in transit |
| 2 | path must end in `.exe` (query and fragment ignored) — this URL is downloaded **and run** |

The download URL must be listed, not guessed: the build number isn't known until
the file exists, and a guess would re-download the build the user already has.
Rejecting a bad manifest costs the user one update; accepting one would execute
an arbitrary URL with their rights, so the worst case is deliberately "no update
offered".

### Installing

`install_update` downloads the setup into a `PriceTrackerUpdate` folder under
`%LOCALAPPDATA%`, falling back to `TEMP`/`TMP` (`_local_run_dir`). It is never
allowed to land on a UNC path: a redirected roaming profile would otherwise
make `cmd` fail with a visible "The network path was not found." MessageBox.
Then it runs:

```
/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /DIR=<app_dir>
```

Inno Setup's matching `AppId` replaces the files in place, and because the app
lives under `%LOCALAPPDATA%` no UAC prompt appears. `settings.json`,
`prices.xlsx` and everything else in `%APPDATA%\PriceTracker` are untouched.

The window then closes and the app comes back by itself. It cannot be done
in-process: this process is destroyed seconds after the installer starts, and
`installer.iss`'s `[Run]` entry carries `skipifsilent`, so a silent run is
skipped by design. What works is a VBS helper written next to the download and
launched detached through `wscript.exe`.

**The installer PID is not the signal that the install finished.** `setup.exe`
is only a launcher: it starts the real installer as a child process and exits
while that child is still copying files — measured at four seconds for a 13 MB
setup, with the app folder still being written at the moment the launcher PID
disappeared. So the helper polls the PID only to get a small head start, then
keeps starting the app until **the app is actually running**, backing off from 5
to 30 seconds between attempts over about three minutes. A start against
half-replaced files dies on its own, and each doomed attempt costs only a hidden
window that closes by itself.

If the app still does not come back, the helper says so in Arabic instead of
exiting silently — this audience cannot tell a failed update from a program
that was closed. The message travels in an environment variable for the same
reason the paths do: `wscript` reads a `.vbs` as ANSI, so it cannot hold Arabic
text. `relaunch.log` next to the download is **appended**, never truncated, so a
failure stays readable after the next attempt.

From source there is no `PriceTracker.exe` beside the script, so no relaunch is
scheduled at all, which is the right outcome.

### Publishing a release

1. `build_setup.bat` → `dist-installer\PriceTracker-Setup-<ver>.exe`
2. `gh release create v<ver> <setup>` → produces the download URL for line 2
3. Edit `gh-pages/latest.txt` with both lines, then push that branch
4. Raise `APP_VERSION` in `price_tracker.py` **and** `AppVersion` in
   `installer/installer.iss`

The app never reads the installed version from the registry: the registry can
hold a newer one after an update, and a mismatch there would make the button
offer the same build forever.

`gh-pages/` in this folder is a working copy of that branch, which is why it
carries its own `.git` and is never picked up as a submodule. Editing
`latest.txt` through it and pushing from here is the intended workflow.

## The download button on the site

The marketing site (**price-tracker-site**, deployed on Vercel) has a "حمّل
البرنامج" button that serves the installer **from the site itself**, not from
GitHub Releases. A visitor clicks once and the setup file comes down; they never
land on a release page and have to pick the right file themselves.

The site keeps its own copy of the newest setup and pulls it straight out of
this repo's `dist-installer/`. Publishing a new build is a file copy:

```bat
cd <path>\price-tracker-site
npm run stage                                  :: newest setup -> public/downloads\PriceTracker-Setup-<ver>.exe
git add public/downloads                       :: the previous versioned copy is removed by the stage
git commit -m "Stage installer <ver>"
git push                                       :: Vercel redeploys automatically
```

`npm run stage` picks the highest `PriceTracker-Setup-<ver>.exe` by **version
number, not file time** (so 1.2.11 beats 1.2.8), and warns when that version and
`APP_VERSION` here disagree — the same mismatch that once shipped a 1.2.10
installer wrapping a 1.2.9-era binary.

The file is served **under its versioned name**, and that is deliberate. A
stable `latest.exe` sounds tidier, but the browser takes the save name from the
URL: a `download` attribute only renames the file after the response has already
started, and a `Content-Disposition` from the host outranks it entirely. Both
were tried and the dialog still offered `latest.exe`. Naming the file for its
version makes the path itself the filename, so it is right the first time.

The version and size printed under the button are read from the staged file at
build time, so they always describe the file actually being served.

**The 12 MB binary is committed to the site repo on purpose.** Deployment builds
from a git clone, so an installer that was git-ignored would leave the deployed
site carrying the button and not the file, and the download would 404. The
stage script checks for that rule and warns rather than leaving it to be
discovered in production.

## License

**No license has been chosen yet.** The repository is public but carries no
stated terms, so "all rights reserved" applies by default and nobody may legally
reuse the code. Add a `LICENSE` file before sharing it beyond this account.

**Thmanyah Sans** (bundled in `ui/fonts/`) is by Boutros Fonts and is licensed
separately — its terms travel with it in `ui/fonts/LICENSE.pdf`.
