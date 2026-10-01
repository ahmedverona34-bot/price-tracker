# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Python desktop app (pywebview shell, Edge WebView2) rendering local HTML/CSS/vanilla JS from a `ui/` folder; Python exposed via a `js_api` class. No frontend framework.

## Users

Non-technical Arabic-speaking shoppers in Egypt who track mobile phone prices across local e-commerce stores (2B Egypt, Dubai Phone). They double-click an app, type one keyword, and read prices — they never edit configs, selectors, or code.

## Product Purpose

Watch product prices across several Egyptian stores from one window: search once, see before/after prices with discount percents and coupon notes, get Excel exports, and catch price drops via automatic 10-minute refreshes. Success means a non-technical user finds the cheapest current price within seconds of opening the app.

## Positioning

One keyword searches every configured store at once through each store's own search, normalizes before/after/coupon pricing (including coupon-badge math no store shows directly), and keeps a persistent Excel record with drop highlighting — configured添加新店 by editing `sites.json`, never Python.

## Operating Context

Runs on any Windows 10/11 machine by double-clicking `PriceTracker-Setup-<ver>.exe` (Arabic wizard, no admin rights, uninstaller included). The program lands in a folder the user picks — by default `%LOCALAPPDATA%\PriceTracker` — and data lives apart from it in `%APPDATA%\PriceTracker`: `settings.json` (keyword, kind choice, auto-refresh, advanced values), `prices.xlsx` (results), `detail_cache.json`, `app.log` (technical log). This split is what lets the app run read-only and lets a later in-app update replace files without a UAC prompt. `sites.json` (stores) stays editable next to the exe. Scraping runs in a background thread; the UI never freezes. Status is plain Arabic sentences; tracebacks stay in `app.log`.

## Capabilities and Constraints

- Multi-site search with per-site CSS selectors; one failing site never stops the others.
- Before/after/discount per row; coupon badges computed as before × (1 − pct), labeled with code.
- Kind filter: أجهزة فقط (auto 30%-of-median floor per site), إكسسوارات فقط, الكل; advanced exclude words + manual min price.
- Excel export with fixed seven columns; auto-overwrite `prices.xlsx` every refresh; open-file button only (never auto-open).
- Auto-refresh every 10 minutes with countdown; drop highlighting vs previous run.
- Plain HTTP first: every store page is fetched with a browser-shaped request and parsed from the server-rendered HTML. Dubai Phone (Next.js on Vercel, 16 cards per page) is walked page by page with `?page=N`, so no browser is needed for it and Vercel BotID never sees a headless client.
- Playwright remains only as an opt-in per site (`"use_playwright": true`) for storefronts that truly render in the browser; when used it prefers installed Edge (msedge), then Chrome, then downloads headless Chromium once with Arabic progress. WebView2 required for the window (ships with Windows 10/11; friendly screen + download link if missing).
- A store answering with a bot checkpoint (Cloudflare or Vercel's 708) is detected from the response itself, cools that site down, and reports plain Arabic — never a technical error, never a silent zero-result.
- Thmanyah Sans is the only typeface, self-hosted from the official OTF files in `ui/fonts/` (weights 300/400/500/700/900, plus the font's `LICENSE.pdf`). They ship inside the bundle, so the app needs no font download at runtime.
- Packaged onedir build must stay under 60 MB excluding any later-downloaded browser. **Met: 25.4 MB** — Playwright is not bundled (it carries a private copy of node, ~89 MB, and no configured site needs it); the setup is 12.3 MB.

## Brand Commitments

Product lane, not brand lane. No logo or brand system exists. Voice: plain Egyptian Arabic, no technical terms on screen. Binding user constraints: solid dark surfaces only (no glass/blur/gradients/neon), exactly one cobalt-blue accent, green reserved for discounts, 150 ms transitions, Thmanyah Sans weights 300/400/500/700/900 (never 600).

## Evidence on Hand

- Live verified selectors and coupon model for 2B Egypt and Dubai Phone (see `price_tracker.py` history); `sites.json`, `settings.json` examples in repo.
- Real search output samples: `prices.xlsx`, written on every refresh.
- No product images are guaranteed; thumbnails are optional per site.

## Product Principles

1. Zero setup: double-click, type one word, read prices.
2. Never show a technical error on screen; log it, speak plain Arabic.
3. The window never freezes; every wait has a visible state.
4. Honest numbers only: displayed prices are displayed prices; computed coupon prices are labeled as coupons.

## Accessibility & Inclusion

Large fonts (12–14pt) and buttons for non-technical users; Arabic RTL layout; keyboard-focusable controls; status messages readable without technical knowledge.
