/* Price Tracker table UI.
 *
 * One state object, one pure filter function, one render function.
 * Every control writes to `state` and calls `apply()`; nothing else draws.
 * Filtering is entirely local: no control ever triggers a re-scrape.
 */
(function () {
  "use strict";

  var KIND_DEVICES = "أجهزة فقط";
  var KIND_ACCESSORIES = "إكسسوارات فقط";
  var KIND_ALL = "الكل";

  /* Column order and headers follow the design system. `image` keeps an empty
     label because it is a thumbnail well, not a word. */
  var COLUMNS = [
    { key: "image", label: "صورة" },
    { key: "site", label: "المتجر" },
    { key: "title", label: "اسم المنتج والمواصفات" },
    { key: "before", label: "السعر السابق", num: true },
    { key: "after", label: "السعر الحالي", num: true },
    { key: "discount", label: "الخصم", num: true },
    { key: "note", label: "الكوبون المتاح" },
    { key: "link", label: "المصدر" },
    { key: "time", label: "وقت السحب" }
  ];
  var DEFAULT_COLS = ["site", "title", "before", "after", "discount",
                      "note", "link", "time"];
  var NO_SORT = { image: 1, link: 1 };

  /* Canonical display order. A saved settings.json may list columns in any
     order, so they are normalised into this one for display; which columns are
     visible is never changed here. */
  var COLUMN_ORDER = {};
  COLUMNS.forEach(function (c, i) { COLUMN_ORDER[c.key] = i; });

  var SITE_STATUS = { ok: "شغال", slow: "بطيء", failed: "واقع",
                      blocked: "محجوب", off: "مغلق", idle: "—" };

  /* ------------------------------------------------------------------ *
   * State: the single source of truth for what the table shows.
   * ------------------------------------------------------------------ */
  var state = {
    rows: [],            // every row Python sent, already kind-tagged
    kind: KIND_DEVICES,
    sites: [],           // site metadata (name, enabled, status, rows)
    siteOn: {},          // site name -> included in the view
    fMin: "",            // view minimum price
    fMax: "",            // view maximum price
    fDisc: "",           // view minimum discount %
    minPrice: "",        // advanced minimum from settings.json
    query: "",           // live text filter
    sort: { key: "discount", dir: -1 },
    cols: DEFAULT_COLS.slice(),
    selected: {},        // link -> true
    anchor: -1,
    status: null,
    rowsToken: -1,
    pinned: false,
    wasSearching: false,
    lastMsg: "",
    lastUpdatedAt: ""
  };

  /* ------------------------------------------------------------------ *
   * Bridge
   * ------------------------------------------------------------------ */
  function bridge() {
    try {
      if (typeof window.pywebview !== "undefined"
        && window.pywebview && window.pywebview.api) {
        return window.pywebview.api;
      }
    } catch (e) { /* not ready */ }
    return null;
  }

  function reportJsError(msg) {
    try {
      var b = bridge();
      if (b && b.log_message) b.log_message("error", String(msg).slice(0, 500));
    } catch (e) { /* logging must never break the page */ }
  }

  window.addEventListener("error", function (e) {
    reportJsError("window.onerror: " + (e.message || e.type));
  });

  function api(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    var b = bridge();
    if (!b || typeof b[name] !== "function") {
      reportJsError("bridge missing for api." + name);
      return Promise.resolve(null);
    }
    try {
      return Promise.resolve(b[name].apply(b, args)).catch(function (err) {
        reportJsError("api." + name + " rejected: " + err);
        return null;
      });
    } catch (err) {
      reportJsError("api." + name + " threw: " + err);
      return null;
    }
  }

  /* ------------------------------------------------------------------ *
   * Helpers
   * ------------------------------------------------------------------ */
  function $(id) { return document.getElementById(id); }

  /* ------------------------------------------------------------------ *
   * Theme (light / dark).
   * ------------------------------------------------------------------ *
   * The whole window repaints from tokens.css: every rule reads var(--…),
   * so switching data-theme on <html> is the entire theme change. The
   * choice is kept in two places: localStorage paints the first frame
   * before the bridge exists (see the inline script in index.html), and
   * settings.json carries it across machines via the backend. */
  var THEME_KEY = "pt-theme";

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "dark"
      ? "dark" : "light";
  }

  function applyTheme(t) {
    t = (t === "dark") ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* private mode */ }
    var moon = $("themeIconMoon"), sun = $("themeIconSun"), btn = $("themeBtn");
    if (moon) moon.classList.toggle("hidden", t !== "light");
    if (sun) sun.classList.toggle("hidden", t !== "dark");
    if (btn) btn.title = (t === "light")
      ? "التبديل إلى الوضع الداكن" : "التبديل إلى الوضع الفاتح";
  }

  function readLocalTheme() {
    try {
      var t = localStorage.getItem(THEME_KEY);
      return (t === "dark" || t === "light") ? t : "";
    } catch (e) { return ""; }
  }

  /* ------------------------------------------------------------------ *
   * Appearance presets (terminal palettes) + font scale.
   * ------------------------------------------------------------------ *
   * Same two-place rule as theme: data-appearance / data-font-scale on
   * <html> repaint from tokens.css, localStorage pre-paints the first
   * frame, settings.json carries it via the backend. */
  var APPEARANCES = [
    { id: "default", label: "الافتراضي", sw: "#2563eb" },
    { id: "dracula", label: "دراكولا", sw: "#bd93f9" },
    { id: "nord", label: "نورد", sw: "#88c0d0" },
    { id: "gruvbox", label: "جروفبوكس", sw: "#fabd2f" },
    { id: "everforest", label: "إيفر فورست", sw: "#a7c080" },
    { id: "rose-pine", label: "روز باين", sw: "#ebbcba" },
    { id: "ayu", label: "آيو", sw: "#ff8f40" },
    { id: "kanagawa", label: "كاناجاوا", sw: "#d27e99" }
  ];
  var FONT_SCALES = ["small", "medium", "large"];
  var FONT_LABELS = { small: "صغير", medium: "متوسط", large: "كبير" };
  var APPEAR_KEY = "pt-appearance";
  var FONT_KEY = "pt-font-scale";

  function applyAppearance(a) {
    var ok = APPEARANCES.some(function (x) { return x.id === a; });
    a = ok ? a : "default";
    if (a === "default") document.documentElement.removeAttribute("data-appearance");
    else document.documentElement.setAttribute("data-appearance", a);
    try { localStorage.setItem(APPEAR_KEY, a); } catch (e) {}
    var grid = $("appearGrid");
    if (grid) Array.prototype.forEach.call(
      grid.querySelectorAll("[data-appear]"), function (b) {
        b.classList.toggle("active", b.dataset.appear === a);
      });
    return a;
  }

  function applyFontScale(s) {
    if (FONT_SCALES.indexOf(s) < 0) s = "medium";
    document.documentElement.setAttribute("data-font-scale", s);
    try { localStorage.setItem(FONT_KEY, s); } catch (e) {}
    var r = $("fontScale"), v = $("fontScaleVal");
    if (r) r.value = String(FONT_SCALES.indexOf(s));
    if (v) v.textContent = FONT_LABELS[s] || s;
    return s;
  }

  function renderAppearGrid(current) {
    var grid = $("appearGrid");
    if (!grid) return;
    setHTML(grid, APPEARANCES.map(function (x) {
      return '<button class="appear-opt' + (x.id === current ? " active" : "")
        + '" data-appear="' + x.id + '" type="button">'
        + '<span class="appear-sw" style="background:' + x.sw + '"></span>'
        + "<span>" + esc(x.label) + "</span></button>";
    }).join(""));
    Array.prototype.forEach.call(
      grid.querySelectorAll("[data-appear]"), function (b) {
        b.addEventListener("click", function () {
          var next = applyAppearance(b.dataset.appear);
          api("set_appearance", next);
        });
      });
  }

  /* Circular theme reveal (View Transitions API).
   * Same idea as the viral light/dark clip-path demo: the new theme
   * expands as a circle from the toggle click point instead of swapping
   * instantly. Falls back to an instant switch when the API is missing
   * (older WebView2), on keyboard activation without coordinates, or when
   * the user prefers reduced motion. */
  function toggleThemeAnimated(e) {
    var next = (currentTheme() === "light") ? "dark" : "light";
    function doSwitch() {
      applyTheme(next);
      api("set_theme", next);
    }
    try {
      if (!document.startViewTransition) { doSwitch(); return; }
      if (window.matchMedia
        && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        doSwitch();
        return;
      }
      var x = (e && typeof e.clientX === "number") ? e.clientX : -1;
      var y = (e && typeof e.clientY === "number") ? e.clientY : -1;
      if (x < 0 || y < 0) {
        var btn = $("themeBtn");
        if (btn && btn.getBoundingClientRect) {
          var r = btn.getBoundingClientRect();
          x = r.left + r.width / 2;
          y = r.top + r.height / 2;
        } else {
          x = window.innerWidth / 2;
          y = window.innerHeight / 2;
        }
      }
      var endR = Math.hypot(
        Math.max(x, window.innerWidth - x),
        Math.max(y, window.innerHeight - y));
      var transition = document.startViewTransition(doSwitch);
      if (!transition || !transition.ready) return;
      transition.ready.then(function () {
        document.documentElement.animate(
          { clipPath: ["circle(0px at " + x + "px " + y + "px)",
            "circle(" + endR + "px at " + x + "px " + y + "px)"] },
          { duration: 500, easing: "ease-out",
            pseudoElement: "::view-transition-new(root)" });
      }).catch(function () { /* instant switch already applied */ });
    } catch (err) {
      doSwitch();
    }
  }

  /* Rewrite an element only when the markup actually changed.
     Rebuilding identical HTML would drop text selection, hover states and any
     pointer event already in flight, so it is skipped. */
  function setHTML(el, html) {
    if (!el) return;
    if (el.__html === html) return;
    el.innerHTML = html;
    el.__html = html;
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  /* Arabic-aware normalisation for matching only (never for display):
     strip tashkeel and tatweel, unify alef/ya/ta-marbuta, fold Arabic
     digits to ASCII, so "حافظه" matches "حافظة" and "٥٠٠٠٠" matches 5000. */
  function norm(s) {
    return String(s == null ? "" : s)
      .replace(/[\u064B-\u0652\u0670\u0640]/g, "")
      .replace(/[\u0622\u0623\u0625\u0671\u0649\u064A\u0629]/g,
        function (ch) { return ch === "\u0629" ? "\u0647" : "\u0627"; })
      .replace(/[\u0660-\u0669]/g, function (d) {
        return String(d.charCodeAt(0) - 0x0660);
      })
      .replace(/[\u06F0-\u06F9]/g, function (d) {
        return String(d.charCodeAt(0) - 0x06F0);
      })
      .toLowerCase().trim();
  }

  function toNum(v) {
    if (typeof v === "number") return v;
    if (v == null || v === "") return null;
    var n = parseFloat(norm(v).replace(/,/g, ""));
    return isNaN(n) ? null : n;
  }

  function money(v) {
    var n = toNum(v);
    return n == null ? "" : Math.round(n).toLocaleString("en-US");
  }

  /* Currency suffix shown on the current price and its saving line, matching
     the reference ("62,499 د.إ" over "▼ 6,500 د.إ"). The previous price carries
     no suffix there, so it is left bare. */
  var CURRENCY = "ج.م";

  /* "منذ 6 دقائق" — the design shows relative age, not a wall-clock stamp.
     Python stamps rows with LOCAL wall-clock time, so the string is parsed as
     local time too; reading it as UTC would shift every row by the machine's
     offset and a fresh scrape would claim to be from the future. Display only:
     the full timestamp stays in the cell's tooltip. Anything unparseable falls
     back to the raw string so no row ever goes blank. */
  function timeAgo(stamp) {
    var raw = String(stamp == null ? "" : stamp).trim();
    if (!raw) return "";
    var m = /(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/
          .exec(raw);
    if (!m) return raw;
    var t = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)).getTime();
    if (isNaN(t)) return raw;
    var mins = Math.floor((Date.now() - t) / 60000);
    if (mins < 1) return "منذ لحظات";
    if (mins < 60) return "منذ " + mins + " دقيقة";
    var hrs = Math.floor(mins / 60);
    if (hrs < 24) return "منذ " + hrs + " ساعة";
    var days = Math.floor(hrs / 24);
    if (days < 30) return "منذ " + days + " يوم";
    return raw;
  }

  /* Column display order, without touching which columns are shown. */
  function orderCols(list) {
    return (list || []).slice().sort(function (a, b) {
      var x = COLUMN_ORDER[a], y = COLUMN_ORDER[b];
      return (x == null ? 99 : x) - (y == null ? 99 : y);
    });
  }

  /* Per-column width classes, taken from the reference table's <th> widths so
     the columns land in the same proportions. */
  var COL_WIDTH = {
    image: "w12", site: "w28", before: "w28", after: "w36",
    discount: "w24", note: "w36", link: "w14", time: "w28"
  };
  /* Alignment per the reference <th>s: the two price columns are text-left, the
     discount, coupon, thumbnail and source columns are centred, and everything
     else follows the RTL default (right). */
  var COL_ALIGN = {
    image: "center", discount: "center", note: "center", link: "center",
    before: "left", after: "left"
  };

  /* ------------------------------------------------------------------ *
   * THE pipeline: pure function, no DOM, no bridge.
   * rows -> { rows, bySite, beforeSites }
   * ------------------------------------------------------------------ */
  function computeView(rows, st) {
    var minP = toNum(st.minPrice);
    var min = toNum(st.fMin);
    var max = toNum(st.fMax);
    var minD = toNum(st.fDisc);
    var q = norm(st.query);
    var out = [];
    var bySite = {};

    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var kind = r.kind || "device";

      // 1) النوع
      if (st.kind === KIND_DEVICES && kind !== "device") continue;
      if (st.kind === KIND_ACCESSORIES && kind !== "accessory") continue;

      // 2) advanced minimum price (settings.json)
      if (minP != null) {
        var ap = toNum(r.after);
        if (ap == null || ap < minP) continue;
      }
      // 3) price range
      if (min != null || max != null) {
        var p = toNum(r.after);
        if (p == null) continue;
        if (min != null && p < min) continue;
        if (max != null && p > max) continue;
      }
      // 4) minimum discount
      if (minD != null) {
        var d = toNum(r.discount) || 0;
        if (d < minD) continue;
      }
      // 5) site chips
      if (st.siteOn && st.siteOn[r.site] === false) continue;
      // 6) live text filter (title, site, note)
      if (q && norm(r.title + " " + r.site + " " + (r.note || ""))
          .indexOf(q) < 0) continue;

      out.push(r);
      bySite[r.site] = (bySite[r.site] || 0) + 1;
    }

    // 7) sort
    var k = st.sort.key, dir = st.sort.dir;
    out.sort(function (a, b) {
      var av = sortVal(a, k), bv = sortVal(b, k);
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * dir;
      return String(av).localeCompare(String(bv), "ar") * dir;
    });
    return { rows: out, bySite: bySite };
  }

  function sortVal(r, k) {
    if (k === "before") return toNum(r.before);
    if (k === "after") return toNum(r.after);
    if (k === "discount") return toNum(r.discount) || 0;
    if (k === "site") return r.site;
    if (k === "title") return r.title;
    if (k === "note") return r.note;
    if (k === "time") return r.timestamp;
    return null;
  }

  function view() { return computeView(state.rows, state); }

  /* Every state change funnels through here: one render, no exceptions
     swallowed, so a broken control can never look like a working one. */
  function apply(opts) {
    try {
      render();
    } catch (e) {
      reportJsError("render failed: " + e);
      toast("توجد مشكلة في عرض الجدول، يُرجى مراجعة ملف app.log");
    }
  }

  function setState(patch, opts) {
    Object.keys(patch).forEach(function (k) { state[k] = patch[k]; });
    apply(opts);
  }

  /* ------------------------------------------------------------------ *
   * Toasts
   * ------------------------------------------------------------------ */
  function toast(msg, actionLabel, actionFn) {
    if (!msg) return;
    var box = $("toasts");
    var el = document.createElement("div");
    el.className = "toast";
    var sp = document.createElement("span");
    sp.textContent = msg;
    el.appendChild(sp);
    if (actionLabel && actionFn) {
      var b = document.createElement("button");
      b.textContent = actionLabel;
      b.addEventListener("click", function () { actionFn(); el.remove(); });
      el.appendChild(b);
    }
    box.appendChild(el);
    // The slide-and-fade transition runs off the .is-visible class (see
    // animations.css). It is added on the next frame so the entrance
    // transition actually plays instead of painting the final state.
    function show() { el.classList.add("is-visible"); }
    if (window.requestAnimationFrame) {
      window.requestAnimationFrame(function () {
        window.requestAnimationFrame(show);
      });
    } else {
      show();
    }
    while (box.children.length > 3) box.removeChild(box.firstChild);
    setTimeout(function () {
      el.classList.remove("is-visible");
      setTimeout(function () { el.remove(); }, 300);
    }, 4500);
  }

  function toastSaved(res) {
    if (!res || !res.message) return;
    if (res.ok) toast(res.message, "فتح الملف", function () { api("open_excel"); });
    else toast(res.message);
  }

  /* ------------------------------------------------------------------ *
   * Sidebar pages
   * ------------------------------------------------------------------ */
  var railBtns = document.querySelectorAll(".rail-btn");
  Array.prototype.forEach.call(railBtns, function (btn) {
    btn.addEventListener("click", function () {
      Array.prototype.forEach.call(railBtns, function (b) {
        b.classList.toggle("active", b === btn);
      });
      Array.prototype.forEach.call(
        document.querySelectorAll(".page"), function (p) {
          p.classList.toggle("active", p.id === "page-" + btn.dataset.page);
        });
      if (btn.dataset.page === "sites") loadSites();
      if (btn.dataset.page === "settings") {
        loadSettings();
        renderUpdateCard(updateInfo);   // the update card lives here
      }
    });
  });

  function exclWords() {
    return ($("excludeBox").value || "").split(",")
      .map(function (w) { return w.trim(); })
      .filter(function (w) { return w; });
  }

  function renderExclChips() {
    var words = exclWords();
    setHTML($("exclChips"), words.map(function (w) {
      return '<span class="chip excl-chip"><span>' + esc(w) + "</span>"
        + '<button data-rm="' + esc(w) + '" type="button"'
        + ' aria-label="حذف ' + esc(w) + '" title="إزالة">'
        + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
        + ' stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/>'
        + "</svg></button></span>";
    }).join("") || '<span class="muted">لا توجد كلمات مستبعدة.</span>');
    Array.prototype.forEach.call(
      $("exclChips").querySelectorAll("[data-rm]"), function (b) {
        b.addEventListener("click", function () {
          var next = exclWords().filter(function (w) { return w !== b.dataset.rm; });
          $("excludeBox").value = next.join(", ");
          renderExclChips();
        });
      });
  }

  $("exclAddBtn").addEventListener("click", function () {
    var w = $("exclAdd").value.trim();
    if (!w) return;
    var words = exclWords();
    if (words.indexOf(w) < 0) words.push(w);
    $("excludeBox").value = words.join(", ");
    $("exclAdd").value = "";
    renderExclChips();
  });
  $("exclAdd").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); $("exclAddBtn").click(); }
  });
  $("exclClearBtn").addEventListener("click", function () {
    $("excludeBox").value = "";
    renderExclChips();
  });
  $("excludeBox").addEventListener("input", renderExclChips);

  function renderRefreshSeg(sec) {
    Array.prototype.forEach.call(
      document.querySelectorAll("#refreshSeg button"), function (b) {
        b.classList.toggle("active", Number(b.dataset.sec) === Number(sec));
      });
  }
  Array.prototype.forEach.call(
    document.querySelectorAll("#refreshSeg button"), function (b) {
      b.addEventListener("click", function () {
        renderRefreshSeg(b.dataset.sec);
        api("set_refresh_sec", Number(b.dataset.sec)).then(function () {
          poll();
        });
      });
    });

  function renderDefaultCols() {
    setHTML($("defaultCols"), COLUMNS.map(function (c) {
      return '<label class="col-opt"><input type="checkbox" class="custom-checkbox" data-col="' + c.key + '"'
        + (state.cols.indexOf(c.key) >= 0 ? " checked" : "")
        + ' aria-label="' + esc(c.label) + '">'
        + "<span>" + esc(c.label) + "</span></label>";
    }).join(""));
    Array.prototype.forEach.call(
      $("defaultCols").querySelectorAll("input"), function (cb) {
        cb.addEventListener("change", function () {
          var list = Array.prototype.map.call(
            $("defaultCols").querySelectorAll("input:checked"),
            function (x) { return x.dataset.col; });
          if (!list.length) { cb.checked = true; return; }
          setState({ cols: list });
          api("set_columns", list).then(function () { renderColMenu(); });
        });
      });
  }

  function loadSettings() {
    api("get_settings").then(function (s) {
      if (!s) return;
      if (document.activeElement !== $("excludeBox"))
        $("excludeBox").value = s.exclude_words || "";
      if (document.activeElement !== $("minBox"))
        $("minBox").value = s.min_price || "";
      renderExclChips();
      renderRefreshSeg(s.refresh_sec == null ? 600 : s.refresh_sec);
      renderDefaultCols();
      renderAppearGrid(applyAppearance(s.appearance || "default"));
      applyFontScale(s.font_scale || "medium");
    });
  }

  $("saveAdvBtn").addEventListener("click", function () {
    api("set_advanced", $("excludeBox").value, $("minBox").value)
      .then(function () {
        api("get_settings").then(function (s) {
          // The advanced minimum is part of the same filter pipeline.
          state.minPrice = (s && s.min_price) || "";
          if (document.activeElement !== $("minBox"))
            $("minBox").value = state.minPrice;
          $("settingsSavedAt").textContent = new Date().toLocaleString("ar-EG");
          toast("تم حفظ الإعدادات");
          apply();
        });
      });
  });

  /* ---- in-app update ----
     There is no permanent update control anywhere. On launch the app asks
     the version file once, quietly, and only then does a rail icon appear in
     the sidebar. If the build is current, nothing is shown at all. */
  (function updateRail() {
    var railBtn = $("updateRailBtn");
    if (!railBtn) return;

    api("app_version").then(function (v) {
      if (v && v.version) $("railVer").textContent = "v" + v.version;
    });

    function show(msg) { toast(msg); }

    /* Checked after the first paint so a slow or dead server can never hold
       up the window the user is trying to work in. Then re-checked on a slow
       timer, because a new build can be published while the window is open:
       requiring a restart to learn about one defeats the point of an in-app
       updater. The backend serves this from a short-lived cache, so the
       repeating check costs no request most of the time.

       Visibility is respected: a window in the background does not need to
       poll, and a laptop waking from sleep checks straight away. */
    var UPDATE_EVERY_MS = 15 * 60 * 1000;

    function check() {
      // max_age matches the re-check interval: if the last manifest was read
      // less than that long ago it is still considered current, so a repeat
      // check is answered without touching the network.
      api("check_update", UPDATE_EVERY_MS / 1000).then(function (r) {
        if (!r) return;
        updateInfo = r;
        updateInfo.checked = true;
        renderUpdateCard(updateInfo);
        if (!r.has_update) return;   // current, or offline: stay silent
        if (railBtn.classList.contains("hidden")) {
          show("في نسخة جديدة: " + r.latest);
        }
        applyUpdateRail(r);
      });
    }

    setInterval(function () {
      if (!document.hidden) check();
    }, UPDATE_EVERY_MS);

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) check();
    });

    railBtn.addEventListener("click", runUpdateInstall);

    setTimeout(check, 2500);
  })();

  function siteQuery() {
    return ($("siteSearch").value || "").trim().toLowerCase();
  }
  $("siteSearch").addEventListener("input", function () { loadSites(); });
  $("sitesRefreshBtn").addEventListener("click", function () {
    loadSites(); poll();
  });

  function renderSiteStats(sites) {
    var on = sites.filter(function (s) { return s.enabled !== false; });
    var ok = sites.filter(function (s) { return s.status === "ok"; }).length;
    var durs = sites.map(function (s) { return s.duration_sec || 0; })
      .filter(function (d) { return d > 0; });
    var avg = durs.length
      ? (durs.reduce(function (a, b) { return a + b; }, 0) / durs.length).toFixed(1)
      : null;
    var rate = on.length ? Math.round(100 * ok / on.length) : null;
    // Four tiles in the design's arrangement: volume, how many are live, how
    // fast, how reliable. All four come out of the same get_sites payload.
    var speed = avg == null ? null : Math.max(10, Math.min(100,
      Math.round(100 - avg * 4)));
    setHTML($("siteStats"), [
      statCard({ label: "إجمالي المتاجر الممسوحة", value: String(sites.length),
        foot: "100% مفعّلة", icon: "stack", tone: "blue", pct: 100 }),
      statCard({ label: "المصادر النشطة في العمل", value: String(on.length),
        foot: on.length ? Math.round(100 * on.length / sites.length) + "% مفعّلة"
                        : "لا يوجد مفعّل",
        icon: "check", tone: "green", green: true,
        pct: sites.length ? Math.round(100 * on.length / sites.length) : 0 }),
      statCard({ label: "متوسط سرعة الاستخراج",
        value: avg == null ? "—" : avg + " ثانية",
        foot: avg == null ? "بانتظار أول بحث" : "لكل موقع",
        icon: "speed", tone: speed == null ? "" : speed >= 80 ? "green"
              : speed >= 50 ? "amber" : "",
        pct: speed, footTone: speed == null ? "" : speed >= 50 ? "" : "amber" }),
      statCard({ label: "نسبة نجاح البحث والمطابقة",
        value: rate == null ? "—" : rate + "%",
        foot: ok + " موقع شغال الآن", icon: "check",
        tone: rate == null ? "" : rate >= 80 ? "green" : rate >= 50 ? "amber" : "",
        pct: rate == null ? 0 : rate })
    ].join(""));
  }

  /* The search URL pattern as a code chip with the keyword placeholder picked
     out, exactly as the design shows it. */
  function patternHtml(p) {
    var s = String(p || "");
    if (!s) return '<span class="muted">—</span>';
    return esc(s).replace(/(\{[a-z0-9_]+\})/gi, "<b>$1</b>");
  }

  /* Column set mirrors the reference sources table: pattern, then the numbers.
     The per-store "test" and "run" buttons that design shows are deliberately
     absent: this program has no such action. */
  function renderSitesHead() {
    setHTML($("sitesHead"),
      '<th class="w12"></th>'
      + '<th class="w28">المتجر</th>'
      + '<th class="min280">نمط رابط البحث (Search URL Pattern)</th>'
      + '<th class="w36">الحالة</th>'
      + '<th class="w28 center">عدد الصفوف المستخرجة</th>'
      + '<th class="w28">آخر زمن استخراج</th>'
      + '<th class="w24 center">مفعّل</th>');
  }
  renderSitesHead();

  function loadSites() {
    api("get_sites").then(function (sites) {
      if (!sites) return;
      state.sites = sites;
      renderSiteStats(sites);
      var q = siteQuery();
      var shown = q ? sites.filter(function (s) {
        return (s.name + " " + (s.pattern || "")).toLowerCase().indexOf(q) >= 0;
      }) : sites;
      setHTML($("sitesBody"), shown.map(function (s) {
        var dot = s.status || (s.enabled ? "idle" : "off");
        return "<tr>"
          + '<td class="c-pick"><span class="site-badge" aria-hidden="true">'
          + esc(s.name.slice(0, 2)) + "</span></td>"
          + '<td class="c-site"><b>' + esc(s.name) + "</b></td>"
          + '<td class="c-time"><span class="pattern">'
          + patternHtml(s.pattern) + "</span></td>"
          + '<td><span class="pill pill-site pill-lift"><span class="dot-wrap"><span class="dot ' + esc(dot)
          + '"></span><span class="ping-ring" aria-hidden="true"></span></span>' + esc(SITE_STATUS[s.status] || "—") + "</span></td>"
          + '<td class="num center">' + (s.rows || 0) + "</td>"
          + '<td class="num">' + (s.duration_sec || 0) + " ث</td>"
          + '<td class="center"><input type="checkbox" class="custom-checkbox site-toggle"'
          + ' data-site="' + esc(s.name) + '"' + (s.enabled ? " checked" : "")
          + ' aria-label="تفعيل ' + esc(s.name) + '"></td></tr>';
      }).join(""));
      apply();
    });
  }

  /* Delegated on purpose. setHTML() leaves the markup alone when it is
     unchanged, so rebinding per element on every loadSites() would stack a new
     listener on the same checkbox each time and fire set_site_enabled several
     times for one click. */
  $("sitesBody").addEventListener("change", function (e) {
    var cb = e.target.closest(".site-toggle");
    if (!cb) return;
    api("set_site_enabled", cb.dataset.site, cb.checked)
      .then(function () { loadSites(); poll(true); });
  });

  /* ------------------------------------------------------------------ *
   * Search
   * ------------------------------------------------------------------ */
  function showSearching() {
    state.wasSearching = true;
    renderSkeletons();
    $("searchBtn").disabled = true;
  }

  function renderSkeletons() {
    var tb = $("tbody");
    // +1 for the leading checkbox column the header now renders.
    var cols = state.cols.length + 1;
    var html = "";
    for (var i = 0; i < 10; i++) {
      html += '<tr class="skel-row"><td colspan="' + cols
        + '"><div class="bar"></div></td></tr>';
    }
    tb.innerHTML = html;
    tb.__html = html;
    $("empty").classList.add("hidden");
    $("noMatch").classList.add("hidden");
    $("rowCount").textContent = "…";
    $("rowTotal").textContent = "";
  }

  function doSearch() {
    var q = $("q").value.trim();
    showSearching();
    toast('يجري البحث عن "' + (q || "…") + '"...');
    api("search", q).then(function (res) {
      if (res && res.message) toast(res.message);
      poll(true);
    });
  }
  $("searchBtn").addEventListener("click", doSearch);
  $("q").addEventListener("keydown", function (e) {
    if (e.key === "Enter") doSearch();
  });

  /* ------------------------------------------------------------------ *
   * Controls: each writes state, then re-renders. Active marks are drawn
   * from state in render(), never toggled by the click handler.
   * ------------------------------------------------------------------ */
  Array.prototype.forEach.call(
    document.querySelectorAll("#kindSeg button"), function (btn) {
      btn.addEventListener("click", function () {
        setState({ kind: btn.dataset.kind });
        api("set_kind", btn.dataset.kind);
      });
    });

  $("autoChk").addEventListener("change", function () {
    api("set_auto", $("autoChk").checked);
  });
  ["fMin", "fMax", "fDisc"].forEach(function (id) {
    $(id).addEventListener("input", function () {
      setState({ fMin: $("fMin").value, fMax: $("fMax").value,
                 fDisc: $("fDisc").value });
    });
    $(id).addEventListener("change", function () {
      api("set_view_filters", $("fMin").value, $("fMax").value,
        $("fDisc").value);
    });
  });

  $("liveFilter").addEventListener("input", function () {
    setState({ query: $("liveFilter").value });
  });

  function resetFilters() {
    setState({
      kind: KIND_DEVICES,
      fMin: "", fMax: "", fDisc: "", minPrice: "", query: "",
      siteOn: enabledMap(),
      cols: DEFAULT_COLS.slice(),
      sort: { key: "discount", dir: -1 },
      selected: {}, anchor: -1
    });
    $("fMin").value = ""; $("fMax").value = ""; $("fDisc").value = "";
    $("liveFilter").value = "";
    api("set_kind", state.kind);
    api("set_view_filters", "", "", "");
    renderColMenu();
    toast("رجعت كل الفلاتر");
  }
  $("resetFiltersBtn").addEventListener("click", resetFilters);

  /* ------------------------------------------------------------------ *
   * Column menu
   * ------------------------------------------------------------------ */
  /* The trigger lives inside the search field, so the menu is placed under it
     with inline-start/end rather than left, to stay on the right in RTL. */
  function closeColMenu() { $("colMenu").classList.add("hidden"); }

  $("colBtn").addEventListener("click", function (e) {
    e.stopPropagation();
    var menu = $("colMenu");
    if (!menu.classList.contains("hidden")) { closeColMenu(); return; }
    var r = $("colBtn").getBoundingClientRect();
    menu.style.top = (r.bottom + 6) + "px";
    menu.style.insetInlineEnd = Math.max(8, window.innerWidth - r.right) + "px";
    menu.classList.remove("hidden");
  });
  window.addEventListener("resize", closeColMenu);
  document.addEventListener("click", function (e) {
    if (!$("colMenu").classList.contains("hidden")
      && !e.target.closest(".colmenu-wrap")) {
      closeColMenu();
    }
  });

  function renderColMenu() {
    setHTML($("colMenu"), COLUMNS.map(function (c) {
      return '<label><input type="checkbox" class="custom-checkbox" data-col="' + c.key + '"'
        + (state.cols.indexOf(c.key) >= 0 ? " checked" : "")
        + "> " + (c.label || "صورة") + "</label>";
    }).join(""));
    Array.prototype.forEach.call(
      $("colMenu").querySelectorAll("input"), function (cb) {
        cb.addEventListener("change", function () {
          var list = Array.prototype.map.call(
            $("colMenu").querySelectorAll("input:checked"),
            function (x) { return x.dataset.col; });
          if (!list.length) {          // never allow an empty table
            cb.checked = true;
            toast("يجب إبقاء عمود واحد على الأقل");
            return;
          }
          setState({ cols: list });
          api("set_columns", list);
        });
      });
  }

  /* ------------------------------------------------------------------ *
   * Exports: always the rows currently on screen.
   * ------------------------------------------------------------------ */
  function selectedLinks() {
    return Object.keys(state.selected).filter(function (k) {
      return state.selected[k];
    });
  }

  /* Every link currently drawn, in display order. The header checkbox selects
     across the whole result set, since all of it is on one scrollable page now. */
  function pageLinks() {
    return computeView(state.rows, state).rows.map(function (r) {
      return r.link;
    });
  }
  function pageAllSelected() {
    var ls = pageLinks();
    return ls.length > 0 && ls.every(function (l) { return state.selected[l]; });
  }
  function pageSomeSelected() {
    var ls = pageLinks(), n = 0;
    ls.forEach(function (l) { if (state.selected[l]) n++; });
    return n > 0 && n < ls.length;
  }
  function togglePageSelect(on) {
    pageLinks().forEach(function (l) {
      if (on) state.selected[l] = true;
      else delete state.selected[l];
    });
    apply();
  }
  function toggleOne(link, on) {
    if (on) state.selected[link] = true;
    else delete state.selected[link];
    apply();
  }

  function exportView(mode) {
    var links = mode === "selected" ? selectedLinks() : viewLinks();
    if (!links.length) {
      toast("لا توجد صفوف للتصدير");
      return;
    }
    api("export_excel", "links", links).then(toastSaved);
  }
  function viewLinks() {
    return view().rows.map(function (r) { return r.link; });
  }

  $("expAllBtn").addEventListener("click", function () {
    api("export_excel", "all", []).then(toastSaved);
  });
  $("expViewBtn").addEventListener("click", function () {
    exportView("view");
  });
  $("expSelBtn").addEventListener("click", function () {
    exportView("selected");
  });
  $("expCsvBtn").addEventListener("click", function () {
    api("export_csv", "links", viewLinks()).then(toastSaved);
  });
  $("openBtn").addEventListener("click", openFile);
  function openFile() {
    api("open_excel").then(function (res) {
      if (res && !res.ok && res.message) toast(res.message);
    });
  }

  $("copySelBtn").addEventListener("click", function () {
    var links = selectedLinks();
    if (!links.length) { toast("يُرجى تحديد الصفوف أولًا"); return; }
    var byLink = {};
    state.rows.forEach(function (r) { byLink[r.link] = r; });
    var head = ["Site", "Product Title", "Price Before", "Price After",
      "Discount %", "Product Link", "Scraped At"];
    var lines = [head.join("\t")];
    links.forEach(function (k) {
      var r = byLink[k];
      if (!r) return;
      lines.push([r.site, r.title, r.before, r.after, r.discount, r.link,
        r.timestamp].map(function (v) {
          return String(v == null ? "" : v).replace(/\t/g, " ");
        }).join("\t"));
    });
    var text = lines.join("\n");
    function done() { toast("تم نسخ " + (lines.length - 1) + " صف"); }
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); } catch (e) { /* noop */ }
      ta.remove();
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  });

  /* ------------------------------------------------------------------ *
   * Render
   * ------------------------------------------------------------------ */
  function renderHead() {
    var pick = '<th class="c-pick" scope="col">'
      + '<input type="checkbox" class="custom-checkbox" id="pickAll" aria-label="تحديد كل صفوف الصفحة">'
      + "</th>";
    setHTML($("headRow"), pick + orderCols(state.cols).map(function (key) {
      var col = COLUMNS.filter(function (c) { return c.key === key; })[0];
      var arrow = state.sort.key === key
        ? '<span class="arrow">' + (state.sort.dir === 1 ? "▲" : "▼") + "</span>"
        : "";
      var cls = COL_WIDTH[key] || "";
      if (COL_ALIGN[key]) cls += " " + COL_ALIGN[key];
      if (key === "title") cls += " min280";
      var sortable = !NO_SORT[key];
      return '<th class="' + cls.trim() + (sortable ? " sortable" : "") + '"'
        + (sortable ? ' data-sortkey="' + key + '" tabindex="0"' : "")
        + ' scope="col">' + (col ? col.label : "") + arrow + "</th>";
    }).join(""));
    // Mirror the page selection onto the header box. The listener itself is
    // delegated below, because the header row is rebuilt on every render and a
    // per-element listener would be attached again each time.
    var all = $("pickAll");
    if (all) {
      all.checked = pageAllSelected();
      all.indeterminate = pageSomeSelected();
    }
  }

  /* One delegated handler on the stable container: sorting survives any
     re-render of the header row. */
  $("headRow").addEventListener("click", function (e) {
    var th = e.target.closest("[data-sortkey]");
    if (!th) return;
    toggleSort(th.dataset.sortkey);
  });
  $("headRow").addEventListener("keydown", function (e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    var th = e.target.closest("[data-sortkey]");
    if (!th) return;
    e.preventDefault();
    toggleSort(th.dataset.sortkey);
  });
  /* Select-all lives on the header checkbox, so its handler is delegated for
     the same reason the sort handlers are. */
  $("headRow").addEventListener("change", function (e) {
    if (!e.target.matches("#pickAll")) return;
    togglePageSelect(e.target.checked);
  });

  function toggleSort(k) {
    // First click on a column sorts ascending, the next one reverses it.
    var dir = state.sort.key === k ? -state.sort.dir : 1;
    setState({ sort: { key: k, dir: dir } });
  }

  function renderKind() {
    Array.prototype.forEach.call(
      document.querySelectorAll("#kindSeg button"), function (b) {
        b.classList.toggle("active", b.dataset.kind === state.kind);
      });
  }

  function renderChips(v) {
    var box = $("siteChips");
    if (!state.sites.length) { setHTML(box, ""); return; }
    var stats = (state.status && state.status.site_stats) || {};
    setHTML(box, state.sites.map(function (s) {
      var st = stats[s.name] || {};
      var dot = st.status || (s.enabled ? "idle" : "off");
      var on = state.siteOn[s.name] !== false;
      var count = v.bySite[s.name] || 0;
      // A site that has not run in this session shows its name and nothing
      // else. Row counts and timings belong to a search the user watched
      // happen; carrying the previous run's numbers onto an empty window
      // reads as this session's result.
      var ran = st.status === "ok" || st.status === "slow";
      var meta = ran ? st.rows + " صف • " + st.duration_sec + "ث" : "";
      return '<button class="chip pill-lift' + (on ? "" : " off") + '"'
        + ' data-site="' + esc(s.name) + '"'
        + ' aria-pressed="' + on + '"'
        + (s.enabled ? "" : ' disabled title="مقفول من صفحة المصادر"') + ">"
        + '<span class="dot-wrap"><span class="dot ' + esc(dot) + '"></span><span class="ping-ring" aria-hidden="true"></span></span>' + esc(s.name)
        + ' <span class="meta">' + count + (meta ? " • " + meta : "")
        + "</span></button>";
    }).join(""));
  }

  $("siteChips").addEventListener("click", function (e) {
    var chip = e.target.closest(".chip");
    if (!chip || chip.disabled) return;
    var name = chip.dataset.site;
    var site = state.sites.filter(function (s) {
      return s.name === name;
    })[0];
    if (!site || site.enabled === false) return;
    var next = {};
    state.sites.forEach(function (s) {
      next[s.name] = state.siteOn[s.name] !== false;
    });
    next[name] = !next[name];
    setState({ siteOn: next });
  });

  function changeHtml(r) {
    if (r.enriching) {
      return ' <span class="chg-busy" title="يجري التأكد من الكوبون">'
        + "…</span>";
    }
    var c = r.change;
    if (c != null && c !== 0) {
      var down = c < 0;
      return ' <span class="' + (down ? "chg-down" : "chg-up") + '">'
        + (down ? "↓ " : "↑ ") + Math.abs(c).toLocaleString("en-US") + "</span>";
    }
    return "";
  }

  function couponChip(note) {
    var m = /coupon\s+(\S+)\s+(-[\d.]+%)/.exec(note || "");
    if (m) {
      return '<span class="pill-coupon">' + esc(m[1]) + " " + esc(m[2])
        + "</span>";
    }
    if ((note || "").indexOf("coupon") >= 0) {
      return '<span class="pill-coupon">كوبون</span>';
    }
    return '<span class="c-disc none">—</span>';
  }

  function cellHtml(r, key) {
    if (key === "image") {
      return r.image
        ? '<img src="' + esc(r.image) + '" alt="" loading="lazy"'
          + ' onerror="this.remove()">'
        : '<span class="thumb-empty"></span>';
    }
    if (key === "site") return '<span class="pill pill-site pill-lift">'
      + '<span class="dot ok"></span>' + esc(r.site) + "</span>";
    if (key === "title") return esc(r.title);
    if (key === "before") {
      // Struck through and muted, as in the design: it is history, not a price.
      return '<span class="was">' + money(r.before) + "</span>";
    }
    if (key === "after") {
      // Main price on top, the money saved underneath. The saving is the gap
      // between the two prices; the change-vs-last-run marker stays beside it
      // so a real price drop is still visible.
      var b = toNum(r.before), a = toNum(r.after);
      var sub = "";
      if (b != null && a != null && b - a > 0.5) {
        sub = '<div class="after-sub">▼ ' + money(b - a) + " " + CURRENCY + "</div>";
      }
      return '<div class="after-main">' + money(r.after) + " " + CURRENCY + "</div>"
        + sub + changeHtml(r);
    }
    if (key === "discount") {
      return (toNum(r.discount) || 0) > 0
        ? '<span class="c-disc has discount-pulse">' + r.discount + "%-</span>"
        : '<span class="c-disc none">—</span>';
    }
    if (key === "link") {
      return '<button class="icon-btn hover-pop" data-open="' + esc(r.link)
        + '" aria-label="فتح الرابط">'
        + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
        + ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
        + '<path d="M14 4h6v6"/><path d="M20 4L10 14"/>'
        + '<path d="M20 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h5"/>'
        + "</svg></button>";
    }
    if (key === "time") {
      var raw = esc(r.timestamp);
      return '<span title="' + raw + '">' + esc(timeAgo(r.timestamp))
        + "</span>";
    }
    if (key === "note") return couponChip(r.note);
    return "";
  }

  function cellClass(key) {
    var cls = [];
    if (key === "image") cls.push("c-thumb");
    if (key === "site") cls.push("c-site");
    if (key === "title") cls.push("c-title");
    if (key === "before") cls.push("c-before");
    if (key === "before" || key === "after" || key === "discount") {
      cls.push("num");
    }
    if (key === "after") cls.push("c-after");
    if (key === "discount") cls.push("c-disc");
    if (key === "link") cls.push("c-link");
    if (key === "time") cls.push("c-time");
    if (key === "note") cls.push("c-note");
    return cls.join(" ");
  }

  function render() {
    var st = state.status || {};
    renderHead();
    renderKind();
    var v = computeView(state.rows, state);
    var tb = $("tbody");

    if (st.searching && !state.rows.length) {
      renderSkeletons();
      return;
    }
    if (!state.rows.length) {
      setHTML(tb, "");
      $("noMatch").classList.add("hidden");
      $("empty").classList.remove("hidden");
      $("empty").querySelector("h3").textContent = st.last_query
        ? "لا توجد نتائج — يُرجى تجربة كلمة بحث أخرى"
        : "اكتب كلمة البحث في الحقل أعلاه ثم اضغط على زر البحث";
      $("rowCount").textContent = "";
      $("rowTotal").textContent = "";
      $("summary").innerHTML = "";
      $("sectionTitle").textContent = st.last_query
        ? 'نتائج "' + st.last_query + '"' : "نتائج البحث";
      renderChips(v);
      renderSel();
      return;
    }

    $("empty").classList.add("hidden");
    $("sectionTitle").textContent = st.last_query
      ? 'نتائج "' + st.last_query + '"' : "نتائج البحث";

    if (!v.rows.length) {
      // Filters left nothing: name the active filters so it is obvious which
      // one to relax, and offer one click back to all rows.
      setHTML(tb, "");
      $("noMatch").classList.remove("hidden");
      $("rowCount").textContent = "0 صف";
      $("rowTotal").textContent = "";
      $("summary").textContent = "";
      var why = [];
      if (state.query) why.push("نص: " + state.query);
      if (toNum(state.fMin) != null) why.push("الأدنى ≥ " + money(state.fMin));
      if (toNum(state.fMax) != null) why.push("الأقصى ≤ " + money(state.fMax));
      if (toNum(state.fDisc) != null) why.push("الخصم ≥ " + state.fDisc + "%");
      if (toNum(state.minPrice) != null)
        why.push("الحد اليدوي ≥ " + money(state.minPrice));
      var offSites = Object.keys(state.siteOn).filter(function (n) {
        return state.siteOn[n] === false;
      });
      if (offSites.length) why.push("متاجر مقفولة: " + offSites.join("، "));
      var w = $("noMatchWhy");
      if (w) {
        w.textContent = why.length
          ? "الفلاتر النشطة: " + why.join(" • ")
          : "يُرجى تجربة كلمة بحث أخرى أو اختيار الكل";
      }
      renderChips(v);
      renderSel();
      return;
    }
    $("noMatch").classList.add("hidden");

    // Selection can only hold rows that are actually visible.
    var visible = {};
    v.rows.forEach(function (r) { visible[r.link] = true; });
    Object.keys(state.selected).forEach(function (k) {
      if (!visible[k]) delete state.selected[k];
    });

    /* Every matching row is drawn at once; the table scrolls instead of
       paging. Paging was hiding results behind numbered buttons, which is the
       opposite of what a price comparison is for — you want to scan the whole
       market, then sort or filter it down. */
    var slice = v.rows;
    var cols = orderCols(state.cols);

    setHTML(tb, slice.map(function (r) {
      var tds = '<td class="c-pick">'
        + '<input type="checkbox" class="custom-checkbox" data-pick="' + esc(r.link) + '"'
        + (state.selected[r.link] ? " checked" : "")
        + ' aria-label="تحديد الصف"></td>' + cols.map(function (key) {
        var extra = key === "title" ? ' title="' + esc(r.title) + '"' : "";
        var cls = cellClass(key);
        if (COL_ALIGN[key]) cls += " " + COL_ALIGN[key];
        return '<td class="' + cls.trim() + '"' + extra + ">"
          + cellHtml(r, key) + "</td>";
      }).join("");
      return '<tr data-link="' + esc(r.link) + '"'
        + (state.selected[r.link] ? ' class="selected"' : "")
        + ">" + tds + "</tr>";
    }).join(""));

    /* Summary numbers come from the same computed view as the table. */
    var disc = v.rows.filter(function (r) { return (toNum(r.discount) || 0) > 0; });
    var sum = disc.reduce(function (s, r) { return s + (toNum(r.discount) || 0); }, 0);
    var prices = v.rows.map(function (r) { return toNum(r.after); })
      .filter(function (x) { return x != null; });
    /* The ribbon above already carries every number, so these two lines only
       state how many rows are drawn versus how many came back — no duplicated
       statistics. */
    $("rowCount").textContent = "عرض " + v.rows.length + " من "
      + state.rows.length;
    $("rowTotal").textContent = v.rows.length === state.rows.length
      ? "كل المنتجات المعروضة (" + v.rows.length + ")"
      : "المعروض " + v.rows.length + " من " + state.rows.length + " منتج";
    /* The ribbon and the previous-price column are the places a min/max is
       read, so the suffix belongs on the current price and its saving line —
       the same two lines the reference puts it on. */
    $("summary").textContent = "";

    renderChips(v);
    renderStatCards(v, st);
    renderSel();
  }


  function renderEngine(st) {
    var dot = $("engineDot"), txt = $("engineText");
    if (!dot || !txt) return;
    if (st.searching) {
      dot.className = "dot slow";
      txt.textContent = "محرك الاستخراج: شغال…";
    } else if (st.failed && st.failed.length) {
      dot.className = "dot slow";
      txt.textContent = "محرك الاستخراج: متصل (موقع واقع)";
    } else if (st.search_count > 0) {
      dot.className = "dot ok";
      txt.textContent = "محرك الاستخراج: متصل";
    } else {
      dot.className = "dot idle";
      txt.textContent = "محرك الاستخراج: جاهز";
    }
  }

  function renderStatCards(v, st) {
    function set(id, val) { var el = $(id); if (el) el.textContent = val; }
    var disc = v.rows.filter(function (r) { return (toNum(r.discount) || 0) > 0; });
    var sum = disc.reduce(function (s, r) { return s + (toNum(r.discount) || 0); }, 0);
    var prices = v.rows.map(function (r) { return toNum(r.after); })
      .filter(function (x) { return x != null; });
    set("statTotal", v.rows.length ? v.rows.length + " نتيجة" : "—");
    set("statDisc", disc.length ? disc.length + " نتيجة" : "—");
    set("statAvg", disc.length ? (sum / disc.length).toFixed(1) + "%" : "—");
    set("statMin", prices.length ? money(Math.min.apply(null, prices)) + " " + CURRENCY : "—");
    set("statMax", prices.length ? money(Math.max.apply(null, prices)) + " " + CURRENCY : "—");
    // Relative age with the clock time kept alongside, as the design shows it.
    var upd = st.updated_at || "";
    var clock = /(\d{2}:\d{2}:\d{2})/.exec(upd);
    set("statUpdated", upd
      ? timeAgo(upd) + (clock ? " (" + clock[1] + ")" : "")
      : "—");
  }

  /* One metric tile, built the way the design builds it: label and icon on the
     top line, the big value and a delta underneath, then a progress rule.
     `tone` colours the icon and the rule. */
  function statCard(opt) {
    var bar = opt.pct == null ? ""
      : '<span class="stat-bar"><i class="' + (opt.tone || "") + '" style="width:'
        + opt.pct + '%"></i></span>';
    return '<div class="stat-card' + (opt.flat ? " flat" : "") + '">'
      + '<div class="stat-top"><span class="stat-label">' + esc(opt.label)
      + '</span><span class="stat-icon ' + (opt.tone || "") + '">'
      + ICON[opt.icon] + "</span></div>"
      + '<div class="stat-bot"><b class="' + (opt.green ? "green" : "") + '">'
      + esc(opt.value) + '</b><span class="stat-foot '
      + (opt.footTone || "") + '">' + esc(opt.foot || "") + "</span></div>"
      + bar + "</div>";
  }

  /* Inline SVG in place of the reference's Material Symbols ligatures. Same
     glyph set, same sizes, no icon font to ship. */
  var ICON = {
    radar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 12l5-3"/><circle cx="12" cy="12" r="1.5"/></svg>',
    stack: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 10h16"/></svg>',
    speed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 12l4-4"/><path d="M12 3v2M21 12h-2M12 21v-2M3 12h2"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9.5"/></svg>',
    box: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    pulse: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l2-5 4 10 2-5h6"/></svg>',
    tag: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12l9-9h9v9l-9 9z"/><circle cx="16.5" cy="7.5" r="1.5"/></svg>'
  };

  /* ---- manual update check, on the settings page ----
     The rail icon only ever appears when a new build is found, so there is
     nowhere to click when nothing is wrong and nothing is published either.
     This card is that place: it always shows the installed version, says
     plainly what the last check found, and lets the user look right now
     instead of waiting for the background timer. */
  function renderUpdateCard(info) {
    var verLine = $("updateVerLine"), stateLine = $("updateStateLine");
    var checkBtn = $("checkUpdateBtn"), installBtn = $("installUpdateBtn");
    if (!verLine || !stateLine || !checkBtn) return;
    if (!info) return;
    if (info.current) verLine.textContent = "\u0627\u0644\u0625\u0635\u062f\u0627\u0631 \u0627\u0644\u0645\u062b\u0628\u0651\u062a: " + info.current;
    if (info.checked && info.has_update) {
      stateLine.textContent = "\u0641\u064a \u0646\u0633\u062e\u0629 \u062c\u062f\u064a\u062f\u0629: " + info.latest;
      if (installBtn) installBtn.classList.remove("hidden");
    } else if (info.checked) {
      stateLine.textContent = info.message || "\u0627\u0644\u0628\u0631\u0646\u0627\u0645\u062c \u0645\u062d\u062f\u0651\u062b.";
      if (installBtn) installBtn.classList.add("hidden");
    } else {
      stateLine.textContent = "\u0644\u0645 \u064a\u064f\u0645 \u0627\u0644\u0641\u062d\u0635 \u0628\u0639\u062f.";
    }
  }

  var updateInfo = null;

  function runUpdateCheck(manual) {
    var checkBtn = $("checkUpdateBtn"), label = $("checkUpdateLabel");
    if (checkBtn) checkBtn.disabled = true;
    if (label) label.textContent = "\u062c\u0627\u0631\u064a \u0627\u0644\u0641\u062d\u0635\u2026";
    // No max_age: this is either an explicit user request or the automatic
    // pass, and both are answered from the live manifest rather than a cache
    // that may predate a freshly published build.
    return api("check_update").then(function (r) {
      if (checkBtn) checkBtn.disabled = false;
      if (label) label.textContent = "\u0627\u0644\u0628\u062d\u062b \u0639\u0646 \u062a\u062d\u062f\u064a\u062b";
      if (!r) return;
      updateInfo = r;
      updateInfo.checked = true;
      renderUpdateCard(updateInfo);
      applyUpdateRail(r);
      if (manual) toast(r.message || "");
    });
  }

  /* Shared by the rail icon and the status-page card so the two cannot drift. */
  function applyUpdateRail(r) {
    var railBtn = $("updateRailBtn");
    if (!railBtn || !r) return;
    if (r.has_update) {
      railBtn.classList.remove("hidden");
      railBtn.title = "\u0641\u064a \u0646\u0633\u062e\u0629 \u062c\u062f\u064a\u062f\u0629: " + r.latest;
    }
  }

  /* Download, install, and come back up on their own. The backend closes this
     window and relaunches once the installer has finished, so there is
     deliberately nothing further to do here. */
  function runUpdateInstall() {
    if (!window.confirm("\u0633\u064a\u0642\u0644 \u0627\u0644\u0628\u0631\u0646\u0627\u0645\u062c \u0627\u0644\u0622\u0646 \u062d\u062a\u0649 \u064a\u062a\u0645 \u0627\u0644\u062a\u062d\u062f\u064a\u062b \u0648\u064a\u0641\u062a\u062d \u0645\u062a\u0637\u0628\u0642\u0627\u064b \u0646\u0641\u0633\u0647. \u0647\u0644 \u062a\u0631\u063a\u0628 \u0628\u0627\u0644\u0645\u062a\u0627\u0628\u0639\u0629\u061f"))
      return;
    var installBtn = $("installUpdateBtn");
    if (installBtn) installBtn.disabled = true;
    toast("\u064a\u062c\u0631\u064a \u062a\u062d\u0645\u064a\u0644 \u0627\u0644\u062a\u062d\u062f\u064a\u062a\u2026");
    api("install_update").then(function (r) {
      if (r && r.ok) {
        toast(r.message || "\u064a\u062c\u0631\u064a \u0627\u0644\u062a\u062d\u062f\u064a\u062a\u2026");
        return;   // the backend closes and relaunches the app
      }
      if (installBtn) installBtn.disabled = false;
      toast((r && r.message) || "\u0641\u0634\u0644 \u0627\u0644\u062a\u062d\u062f\u064a\u062a\u060c \u064a\u064f\u0631\u062c\u0649 \u0627\u0644\u0645\u062d\u0627\u0648\u0644\u0629 \u0644\u0627\u062d\u0642\u0627");
    });
  }

  (function wireUpdateCard() {
    var checkBtn = $("checkUpdateBtn"), installBtn = $("installUpdateBtn");
    if (checkBtn) checkBtn.addEventListener("click", function () { runUpdateCheck(true); });
    if (installBtn) installBtn.addEventListener("click", runUpdateInstall);
    api("app_version").then(function (v) {
      if (v && v.version && updateInfo) updateInfo.current = v.version;
      renderUpdateCard(updateInfo);
    });
  })();

  function renderSel() {
    renderEngine(state.status || {});
    var lv = $("liveBadge");
    if (lv) {
      var st = state.status || {};
      lv.classList.toggle("hidden",
        !(st.search_count > 0 && !st.searching && state.rows.length > 0));
    }
    var n = selectedLinks().length;
    $("selCount").textContent = n ? "المحدد: " + n : "";
    $("copySelBtn").disabled = !n;
    $("expSelBtn").disabled = !n;
    Array.prototype.forEach.call(
      document.querySelectorAll("#tbody tr[data-link]"), function (tr) {
        tr.classList.toggle("selected", !!state.selected[tr.dataset.link]);
      });
  }

  /* Row selection and the open-link button, delegated on the tbody so the
     handlers keep working whenever the rows are redrawn. */
  $("tbody").addEventListener("click", function (e) {
    var pick = e.target.closest("[data-pick]");
    if (pick) {
      // A checkbox drives selection only; it must not also fire the row click,
      // which would clear the selection it just made.
      e.stopPropagation();
      toggleOne(pick.dataset.pick, pick.checked);
      return;
    }
    var open = e.target.closest("[data-open]");
    if (open) {
      e.stopPropagation();
      api("open_link", open.dataset.open);
      return;
    }
    var tr = e.target.closest("tr[data-link]");
    if (!tr) return;
    var link = tr.dataset.link;
    var rows = document.querySelectorAll("#tbody tr[data-link]");
    var gi = Array.prototype.indexOf.call(rows, tr);
    if (e.ctrlKey || e.metaKey) {
      if (state.selected[link]) delete state.selected[link];
      else state.selected[link] = true;
      state.anchor = gi;
    } else if (e.shiftKey && state.anchor >= 0) {
      var v = computeView(state.rows, state);
      var a = Math.min(state.anchor, gi), b = Math.max(state.anchor, gi);
      for (var i = a; i <= b && i < v.rows.length; i++) {
        state.selected[v.rows[i].link] = true;
      }
    } else {
      state.selected = {};
      state.selected[link] = true;
      state.anchor = gi;
    }
    apply();
  });

  /* ------------------------------------------------------------------ *
   * Polling: cheap status every second, rows only when they changed.
   * ------------------------------------------------------------------ */
  function fmtCountdown(sec) {
    if (sec == null) return "";
    var m = Math.floor(sec / 60), s = sec % 60;
    return "التحديث بعد " + m + ":" + String(s).padStart(2, "0");
  }

  function syncChrome(st) {
    if (!st) return;
    if (document.activeElement !== $("autoChk"))
      $("autoChk").checked = !!st.auto_refresh;
    // Only tick once this session has actually searched. Before that the
    // backend sends no query and no armed refresh, and an empty string here
    // leaves the label blank instead of counting down a refresh that nobody
    // started.
    var cd = $("countdown");
    var text = st.last_query ? fmtCountdown(st.countdown_sec) : "";
    if (cd.textContent !== text) cd.textContent = text;
    $("searchBtn").disabled = !!st.searching;
  }

  function syncFromStatus(st) {
    if (!st) return;
    var first = !state.status;
    state.status = st;
    if (st.message && st.message !== state.lastMsg) {
      state.lastMsg = st.message;
      if (!first) toast(st.message);
    }
    syncChrome(st);
    if (st.searching !== state.wasSearching) {
      state.wasSearching = st.searching;
      apply();
    }
    if (state.pinned) return;   // a test fixture owns the rows
    var token = st.rows_token;
    if (token !== undefined && token !== state.rowsToken) {
      state.rowsToken = token;
      api("get_results").then(function (rows) {
        if (!rows) return;
        var firstLoad = !state.rows.length;
        state.rows = rows;
        state.selected = {};
        state.anchor = -1;
        apply();
      });
    }
  }

  function poll() {
    api("get_status").then(syncFromStatus);
  }

  /* Python pushes this for status-only updates (a site starting/finishing). */
  window.__pushStatus = function () { poll(); };
  window.__pushResults = function () { poll(); };

  /* Test harness: inject a fixture without pywebview. The fixture is pinned so
     the live poll cannot replace it with the app's real (empty) row set. */
  window.__injectTest = function (rows, status, sites) {
    state.pinned = true;
    state.rows = rows || [];
    state.status = status || state.status;
    if (sites) {
      state.sites = sites;
      state.siteOn = enabledMap();
    }
    if (status && status.rows_token !== undefined)
      state.rowsToken = status.rows_token;
    if (state.status && state.status.searching) renderSkeletons();
    syncChrome(state.status);
    render();
    return state.rows.length;
  };
  window.__unpinTest = function () { state.pinned = false; poll(); };
  window.__state = function () { return state; };
  window.__view = function () { return computeView(state.rows, state); };

  function enabledMap() {
    var m = {};
    state.sites.forEach(function (s) { if (s.enabled !== false) m[s.name] = true; });
    return m;
  }

  /* ------------------------------------------------------------------ *
   * Boot
   * ------------------------------------------------------------------ */
  /* The page can finish loading before pywebview injects its API. Waiting for
     it here is what makes the site chips, saved columns and saved filters
     appear at all; anything fetched before then would silently vanish. */
  function waitForBridge(cb, tries) {
    tries = tries || 200;
    if (bridge()) { cb(); return; }
    if (tries <= 0) {
      reportJsError("pywebview bridge never appeared");
      toast("تعذّر الاتصال بالبرنامج، يُرجى إغلاق النافذة وإعادة فتحها");
      return;
    }
    setTimeout(function () { waitForBridge(cb, tries - 1); }, 100);
  }

  var loaded = false;
  function loadAll() {
    if (loaded) return;
    loaded = true;
    api("get_sites").then(function (sites) {
      if (sites) {
        state.sites = sites;
        state.siteOn = enabledMap();
        apply();
      }
    });
    api("get_settings").then(function (s) {
      if (s) {
        // Theme first: the backend wins when it holds an explicit choice,
        // otherwise the local pre-paint value (or light) stays.
        if (s.theme === "dark" || s.theme === "light") applyTheme(s.theme);
        renderAppearGrid(applyAppearance(s.appearance || "default"));
        applyFontScale(s.font_scale || "medium");
        state.kind = s.kind || state.kind;
        state.minPrice = s.min_price || "";
        // saved_query is the last keyword the user searched. Pre-filling it
        // saves a retype, but it is only a hint in the field: the header and
        // the countdown stay neutral until a search actually runs this
        // session, so the window never claims work nobody asked for yet.
        if (s.keyword && document.activeElement !== $("q"))
          $("q").value = s.keyword;
        if (s.visible_columns && s.visible_columns.length)
          // Normalised for display only; the saved order in settings.json is
          // left untouched, so nothing the user configured is overwritten.
          state.cols = orderCols(s.visible_columns);
        if (s.f_min) $("fMin").value = state.fMin = s.f_min;
        if (s.f_max) $("fMax").value = state.fMax = s.f_max;
        if (s.f_disc) $("fDisc").value = state.fDisc = s.f_disc;
        if (document.activeElement !== $("excludeBox"))
          $("excludeBox").value = s.exclude_words || "";
        if (document.activeElement !== $("minBox"))
          $("minBox").value = s.min_price || "";
      }
      renderColMenu();
      apply();
    });
    poll();
  }

  function boot() {
    var local = readLocalTheme();
    if (local) applyTheme(local);
    try {
      var la = localStorage.getItem(APPEAR_KEY);
      if (la) applyAppearance(la);
      var lf = localStorage.getItem(FONT_KEY);
      if (lf) applyFontScale(lf);
    } catch (e) {}
    renderAppearGrid(
      document.documentElement.getAttribute("data-appearance") || "default");
    var themeBtn = $("themeBtn");
    if (themeBtn) themeBtn.addEventListener("click", function (e) {
      toggleThemeAnimated(e);
    });
    var fr = $("fontScale");
    if (fr) fr.addEventListener("input", function () {
      var s = FONT_SCALES[Number(fr.value)] || "medium";
      applyFontScale(s);
    });
    if (fr) fr.addEventListener("change", function () {
      var s = FONT_SCALES[Number(fr.value)] || "medium";
      api("set_font_scale", s);
    });
    renderColMenu();
    apply();
    setInterval(poll, 1000);
    window.addEventListener("pywebviewready", loadAll);
    waitForBridge(loadAll);
  }

  boot();
})();
