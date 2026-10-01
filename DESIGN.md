# Design System

<!-- impeccable:design-schema 1 -->

The layout and colours are a port of the **"Price Tracker Dark RTL"** system
authored in Google Stitch. The one typeface stays Thmanyah Sans, as required.

## Where the values come from

Every token was extracted mechanically from the `tailwind.config` object inside
the design system's own `_2/code.html`, not retyped and not read off the
screens. That matters because the project's `DESIGN.md` prose section describes
a *different* palette from the one its screens render — canvas `#0B0D11`,
accent `#2563EB`, green `#10B981` — while the theme in the markup specifies
canvas `#0C0E12`, primary `#b4c5ff` on `#2563EB`, secondary `#4edea3`.
Pixel-sampling the reference screenshots confirms the markup theme is the one
actually rendered. This port follows the markup theme.

`ui/tokens.css` holds the 47 colours, the 4 radii, the spacing steps and the
type scale, all verbatim. `ui/styles.css` holds the layout.

## Tokens

- Surfaces: `--surface-container-lowest #0c0e12` (page ground, inputs) ·
  `--surface-container-low #1a1c20` (rail, metric ribbon) ·
  `--surface-container #1e2024` (panels, the table card) ·
  `--surface-container-high #282a2e` · `--surface-container-highest #333539`.
- Lines: `--outline-variant #434655` with `/15 /20 /30 /40 /50 /60` variants.
- Text: `--on-surface #e2e2e8` · `--on-surface-variant #c3c6d7` ·
  `--outline #8d90a0`.
- Accents: `--primary-container #2563eb` for solid fills (CTA, active rail, page
  number), `--primary #b4c5ff` for tinted text (countdown, selection counter),
  `--inverse-primary #0053db` for CTA hover.
- Semantic: `--secondary #4edea3`, `--secondary-container #00a572`,
  `--tertiary #ffb95f`, `--error #ffb4ab`.
- Radii: 4 / 8 / 12 / 9999px. The theme states these in `rem`; they are pinned
  in `px` so the port is exact regardless of the root font size.
- Spacing: 4 / 8 / 12 / 16 / 24px, gutters 16 and 24px.

## Typography

Thmanyah Sans, self-hosted, weights 300/400/500/700/900.

| Role | Size / line | Reference weight | Here |
|---|---|---|---|
| page title | 24 / 32 | 700 | 700 |
| section + button | 16 / 24 | 600 | 700 |
| body / input | 14 / 20 | 400 | 400 |
| table cell | 13 / 18 | 400 | 400 |
| metric number | 28 / 36 | 700 | 700 |
| price, counts | 13 / 18 | 500 | 500 |
| label | 12 / 16 | 600 | 500 |
| label-sm | 11 / 14 | 500 | 500 |

Two substitutions, both forced by this project:

1. **No 600.** The system asks for 600 in several roles; Thmanyah ships no such
   face, so the browser would synthesise or round it. Bold intent takes 700,
   semibold labels take 500 so labels stay subordinate to the numbers.
2. **No letter-spacing.** The system sets `0.02em`; tracking breaks Arabic
   letter joining, so it is 0 throughout.

Icons are inline SVG rather than Material Symbols ligatures, so no icon font
ships with the executable.

## Layout

The shell mirrors the reference exactly: a 68px rail fixed to the physical
right, a 56px translucent top bar inset by the rail width, and content padded
from the right by 68px. Above the table, four stacked blocks in this order:

1. **Extraction surface** — `p-3.5` card. Row one: search field (flexible, icon
   at the right, column-menu button nested at the left), then the segmented
   kind control, then the blue `استخراج الأسعار` CTA. Row two, split by a top
   hairline: source pills with live dots and counts on one side, min / max /
   discount bounds and the quick table filter on the other.
2. **Metric ribbon** — six label/value pairs divided by 1px rules, with the
   countdown and auto-refresh toggle at the far end.
3. **Table toolbar** — heading, live badge, counters; column menu and the joined
   export group opposite.
4. **Table card** — sticky header on the canvas tone, 53px rows, hairline
   separators, footer with a page-size select and numbered pagination.

The kind control's button order follows the reference: الكل, أجهزة فقط,
إكسسوارات فقط, with the saved kind marked active.

## Components

- **Table** — leading checkbox column with a header select-all bound to the
  visible page, then thumbnail, store pill, name, previous price (struck,
  muted), current price over a green saving line, discount pill, coupon chip,
  source link, pull time. A selected row carries a 3px cobalt marker on the
  right edge over a `rgba(180,197,255,.10)` tint. Verified by pixel probe: a
  *negative* inset x-offset is what leaves the band on the right.
- **Coupon chip** — outlined, LTR so codes read correctly.
- **Relative time** — `منذ 6 دقيقة`, full stamp in the tooltip. Python stamps
  rows with local wall-clock time, so the string is parsed as local time;
  reading it as UTC shifts every row by the machine's offset.
- **Metric card** — label and icon on top, value and delta beneath, then a
  progress rule tinted by health.
- **Sources page** — four metric cards, then the store table with the search
  URL pattern as an LTR code chip with `{q}` picked out in green.

## What was left out, and why

Every reference feature the program has no equivalent for was dropped rather
than stubbed: the product picker, "add a store", per-store test and run
buttons, proxy and browser-engine settings, request timeout, Super Deals. A
dead control reads as broken; an absent one does not. The five pages keep this
project's real functions in the reference's visual language.

## Verification

`probe_fidelity.py` measures 63 computed properties on both pages at the same
viewport: **61 match exactly**, including every colour, radius, height, padding
and font size. The two that differ are the toast's shadow layer list (the
reference carries two extra transparent layers) and row height, which is now
pinned to the reference's 53px. `probe_pixels.py` compares rendered pixels by
region: rail 97%, top bar 93%, gutters 96% identical. `verify_ui2.py` drives
every control against a stubbed bridge and reports no console errors.

## Responsive

The extraction row stacks below 1024px as the reference's `lg:` breakpoint
does, then returns to a row. Gutters tighten, the rail narrows to 60px, the
export group scrolls horizontally instead of running off the edge, and the
table scrolls inside its container. Verified at 1440×900 and 390×844.
