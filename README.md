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
launched detached through `wscript.exe`, which polls until the installer PID
exits and only then starts the new build. Polling for the PID is what orders the
two — starting on a timer would race the install and could reopen the old files.

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

## License

**No license has been chosen yet.** The repository is public but carries no
stated terms, so "all rights reserved" applies by default and nobody may legally
reuse the code. Add a `LICENSE` file before sharing it beyond this account.

**Thmanyah Sans** (bundled in `ui/fonts/`) is by Boutros Fonts and is licensed
separately — its terms travel with it in `ui/fonts/LICENSE.pdf`.
