/* Cashew-style SPA over the existing FastAPI endpoints. Vanilla JS.
   Views: Home / Transactions / Budgets (+ Add overlay, edit sheet). */
(function () {
  "use strict";

  // Sending the browser to /login is latched: several of the 11 fetch sites run
  // in parallel on boot, so one expired session 401s all of them at once, and
  // logout() is itself already navigating while it clears the caches. Each
  // unlatched assignment would restart the navigation.
  let leaving = false;
  function goToLogin() {
    if (leaving) return;
    leaving = true;
    // replace(), not href: href PUSHES, so Back would return to the rendered
    // dashboard. auth.js:81 already uses replace() for the mirror-image case.
    location.replace("/login");
  }

  // replace() alone is not enough. show() assigns location.hash on every real
  // tab switch, so "/" owns several history entries; replace() drops only the
  // current one and Back still lands on e.g. "/#budgets". That is a
  // cross-document navigation the browser may serve from the BACK/FORWARD
  // CACHE -- the whole dashboard restored fully rendered, every amount and note
  // readable, with no request made and no session cookie checked. "/" is served
  // with no Cache-Control and the app registers no unload handler, so nothing
  // else blocks bfcache. Reloading a restored page re-runs the gate.
  // Cache-Control: no-store is NOT a portable substitute -- Safari bfcaches
  // no-store pages anyway.
  window.addEventListener("pageshow", function (e) {
    if (e.persisted) location.reload();
  });

  // One interception point for session expiry. There are 11 fetch sites; a
  // per-call helper would be a large diff with a real chance of missing one,
  // and would not cover calls added later.
  (function () {
    const nativeFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      return nativeFetch(input, init).then(function (res) {
        // Covers a string, a Request (.url, absolute) and a URL object (no
        // .url, so String() is what makes it work). All 11 call sites pass
        // same-origin relative strings today; this is about the next one.
        const url = String((input && input.url) || input || "");
        // The gate answers an unauthenticated /api/* call with 401 JSON rather
        // than a 302, precisely so this branch can see it — a redirect would be
        // followed transparently and the login page parsed as JSON.
        if (res.status === 401 && url.indexOf("/api/") >= 0) goToLogin();
        return res;
      });
    };
  })();

  // Set your own greeting name from the browser console:
  //   localStorage.setItem("expense.name", "Alex")
  // Left blank the greeting just reads "Good morning".
  const NAME = localStorage.getItem("expense.name") || "";
  const ICONS = { food: "🍜", groceries: "🛒", transport: "🚌", shopping: "🛍️",
                  bills: "🧾", health: "💊", entertainment: "🎮", travel: "✈️",
                  other: "📦" };
  // Legacy rows (Cashew import) carry names like "Bills & Utilities" — key off
  // the first word so colors/icons still resolve; anything unknown falls to "other".
  const catKey = (cat) => {
    const m = String(cat || "").toLowerCase().match(/[a-z]+/);
    return m && ICONS[m[0]] ? m[0] : "other";
  };
  const icon = (cat) => ICONS[catKey(cat)];
  const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
  // Chart.js default tooltip bg is rgba(0,0,0,.8) — 20% see-through, so text
  // behind the canvas bleeds into the label. Solid dark reads on both themes.
  if (window.Chart) Chart.defaults.plugins.tooltip.backgroundColor = "#1d1b20";

  const $ = (id) => document.getElementById(id);
  // Sign goes before the currency ("−S$5.00", not "S$-5.00") — negatives show
  // up in day totals and heatmap tips now that reimbursements are negative rows.
  const fmt = (c) => (c < 0 ? "−" : "") + "S$" + (Math.abs(c) / 100).toLocaleString(
    "en-SG", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const catClass = (cat) => "cat-" + catKey(cat);
  // Escape user-controlled text before it touches innerHTML (notes are free text -> XSS guard).
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Chart.js paints to a <canvas>, which is opaque to a screen reader. Each
  // chart gets a sentence built from the same data it was drawn from, so the
  // description cannot drift from the picture.
  function describeChart(canvas, text) {
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", text);
  }

  // [["Food", 42000], ...] -> "Food S$420.00, Transport S$180.00, and 4 more"
  function topParts(pairs, n) {
    const head = pairs.slice(0, n).map((p) => p[0] + " " + fmt(p[1])).join(", ");
    const rest = pairs.length - n;
    return head + (rest > 0 ? ", and " + rest + " more" : "");
  }

  const api = {
    get: (url) => fetch(url).then((r) => r.json()),
    send: (method, url, body) =>
      fetch(url, { method, headers: { "Content-Type": "application/json" },
                   body: JSON.stringify(body) }).then((r) => {
        if (!r.ok) throw new Error(method + " " + url + " → " + r.status);
        return r.json();
      }),
  };

  // cardFilter: a card id, 0 for the Unassigned bucket, null for "no filter".
  // In-memory only, exactly like filterCat — NOT in the hash. resolveHash()
  // validates the WHOLE fragment against NAV_ORDER, so "#cards?card=3" would
  // resolve to home and show() would then PUSH the correction, which is the
  // Back-button trap documented at show().
  // cards: the active cards from /api/cards (feeds the picker, in API order).
  // cardNames: id -> name, so a row resolves its chip with no fetch of its own.
  // cardSources: the bindable email senders from the same payload, each with
  // the card it is attributed to today (null = nobody).
  const state = { cats: [], month: currentYM(), search: "", txs: [], txShown: 10,
                  editing: null, addAmount: "", addCat: null, addType: "expense",
                  homeMonth: currentYM(), filterCat: null,
                  filterSrc: "all", minCents: 0,
                  merchants: [], merchShown: 5, heatDays: {},
                  year: String(new Date().getFullYear()),
                  yearData: null, yrCat: null, rhythmCat: null,
                  cardFilter: null, cards: [], cardNames: {}, cardSources: [] };
  const THEMES = ["auto", "light", "dark"];
  const THEME_ICON = { auto: "◐", light: "☀", dark: "☾" };
  let theme = localStorage.theme || "auto";
  function applyTheme(t) {
    if (t === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
    document.querySelectorAll(".theme-btn").forEach((b) => (b.textContent = THEME_ICON[t]));
  }

  let sparkChart = null;
  let cashChart = null;
  let donutChart = null;
  const DONUT_POP = 18; // px a drilled-down slice pops out of the ring
  let rhythmChart = null;
  let rhythmAgg = null; // weekday×category sums backing the rhythm pill filter
  let yearChart = null;

  function currentYM() {
    const d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
  }
  function daysInMonth(ym) {
    const [y, m] = ym.split("-").map(Number);
    return new Date(y, m, 0).getDate();
  }
  function isAuto(src) { return src.endsWith("-email"); }

  // ---- home -----------------------------------------------------------
  function renderGreeting() {
    const h = new Date().getHours();
    const part = h < 12 ? "morning" : h < 18 ? "afternoon" : "evening";
    const dateLine = new Date().toLocaleDateString("en-SG",
      { weekday: "long", day: "numeric", month: "long" });
    const greet = NAME ? `Good ${part}, ${NAME}` : `Good ${part}`;
    $("greeting").textContent = greet;
    $("today-line").textContent = dateLine;
    $("desk-greet").textContent = greet;
    $("desk-date").textContent = dateLine;
  }

  // Animate a money element from its last shown value to `cents`.
  function countUp(el, cents) {
    const from = Number(el.dataset.cents || 0);
    el.dataset.cents = cents;
    if (from === cents || REDUCED) {
      el.textContent = fmt(cents);
      return;
    }
    const t0 = performance.now(), dur = 400;
    (function tick(t) {
      const p = Math.min(1, (t - t0) / dur);
      const ease = 1 - Math.pow(1 - p, 3);
      el.textContent = fmt(Math.round(from + (cents - from) * ease));
      if (p < 1) requestAnimationFrame(tick);
    })(t0);
  }

  // Stagger a container's freshly-rendered children (pairs with the CSS
  // rowin animation-delay on --i). Cap keeps long lists from settling slowly.
  function stagger(container, cap) {
    const kids = container.children;
    for (let i = 0; i < kids.length; i++) {
      kids[i].style.setProperty("--i", Math.min(i, cap || 10));
    }
  }

  // CSS custom-property lookup (category token colors for charts).
  const cssVar = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const catColor = (cat) =>
    cssVar("--c-" + catKey(cat) + "-fg") || cssVar("--c-other-fg");

  // Currency-aware money. The rest of the app is SGD-only and keeps using its
  // own inline "S$" formatting; the portfolio holds USD/HKD too.
  const CCY_SYMBOL = { SGD: "S$", USD: "US$", HKD: "HK$", CNH: "CN¥", EUR: "€" };
  function fmtMoney(cents, ccy) {
    const sym = CCY_SYMBOL[ccy] || (ccy ? ccy + " " : "$");
    const sign = cents < 0 ? "-" : "";
    return sign + sym + (Math.abs(cents) / 100).toLocaleString("en-SG",
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function localDateKey(d) {
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
           "-" + String(d.getDate()).padStart(2, "0");
  }

  async function renderHome() {
    const ym = state.homeMonth;
    const cp = cardParam();
    const [summary, trend, budgets, recent, pend] = await Promise.all([
      api.get("/api/summary?month=" + ym + cp),
      api.get("/api/trend?granularity=day" + cp),
      // Budgets are per-CATEGORY and have no card-scoped meaning: a ring 30%
      // filled by one card, against the full limit, reads "plenty left" on a
      // budget that is blown. Left whole on purpose; the .budget-scope note
      // says so while a filter is on.
      api.get("/api/budgets?month=" + ym),
      api.get("/api/transactions?limit=5"),
      api.get("/api/pending"),
    ]);
    $("spent-label").textContent = ym === currentYM() ? "Spent this month"
      : "Spent in " + new Date(ym + "-01").toLocaleDateString("en-SG",
          { month: "long", year: "numeric" });
    countUp($("month-total"), summary.total_cents);
    // #desk-head sits ABOVE the tab strip: it is on screen while the user is
    // on Insights, Portfolio, Pay or Cards, none of which respect the card
    // filter. A card-scoped number there would misreport all four, so it costs
    // one extra unfiltered aggregate — only while a filter is actually on.
    if (cp) {
      api.get("/api/summary?month=" + ym).then(function (all) {
        countUp($("desk-total"), all.total_cents);
      });
    } else {
      countUp($("desk-total"), summary.total_cents);
    }
    renderSpark(trend.points.filter((p) => p.period.startsWith(ym)), ym);
    renderDonut(summary);
    renderRings(budgets);
    renderPending(pend.pending);
    $("recent-list").innerHTML =
      recent.map(txRow).join("") || `<div class="empty">Nothing yet</div>`;
    stagger($("recent-list"));
  }

  function renderPending(rows) {
    $("pending-card").classList.toggle("hidden", !rows.length);
    $("pending-list").innerHTML = rows.map((p) => `<div class="tx" data-pid="${p.id}">
        <div class="ico">${p.type === "income" ? "💰" : "📧"}</div>
        <div class="mid"><div class="t">${esc(p.note) || "(no note)"}</div>
          <div class="s">${p.created_at.slice(0, 10)}</div></div>
        <div class="amt money">${fmt(p.amount_cents)}</div>
        <button class="p-acc" title="Add">✓</button>
        <button class="p-ign" title="Ignore">✕</button>
      </div>`).join("");
    stagger($("pending-list"));
  }

  function renderDonut(summary) {
    const total = fmt(summary.total_cents);
    countUp($("donut-total"), summary.total_cents);
    // 22px fits up to S$999.99 in the donut hole; step down for bigger months
    $("donut-total").classList.toggle("squeeze", total.length > 9);
    if (donutChart) donutChart.destroy();
    donutChart = new Chart($("donut"), {
      type: "doughnut",
      data: {
        labels: summary.by_category.map((c) => c.category),
        datasets: [{
          data: summary.by_category.map((c) => Math.max(0, c.total_cents) / 100),
          backgroundColor: summary.by_category.map((c) => catColor(c.category)),
          borderWidth: 0, hoverOffset: 10,
          // keep an active drill-down's slice popped across re-renders
          offset: summary.by_category.map(
            (c) => (c.category === state.filterCat ? DONUT_POP : 0)),
        }],
      },
      // Chart.js sizes the ring from the canvas minus the LARGEST slice
      // offset, so popping a slice shrank the whole donut (and hover
      // wobbled it). Pin the reserved headroom to the pop distance so the
      // radius is identical in idle, hovered, and popped states.
      plugins: [{ beforeDatasetsUpdate: (c) => {
        c.getDatasetMeta(0).controller.getMaxOffset = () => DONUT_POP;
      } }],
      options: { cutout: "70%", responsive: true, maintainAspectRatio: false,
        animation: REDUCED ? false : { duration: 600, easing: "easeOutQuart" },
        layout: { padding: 10 },
        onClick: (e, els) => {
          if (!els.length) return;
          const cat = donutChart.data.labels[els[0].index];
          // pop the slice out, then drill down once the motion reads
          setDonutPop(cat);
          setTimeout(() => drillDown(cat), REDUCED ? 0 : 300);
        },
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: (i) => " S$" + i.parsed.toFixed(2) } } } },
    });
    describeChart($("donut"), "Spending by category this month: " +
      topParts(summary.by_category.map((c) => [c.category, Math.max(0, c.total_cents)]), 3) +
      ". Total " + total + ".");
  }

  function renderRings(budgets) {
    $("home-budgets").innerHTML = budgets
      .sort((a, b) => b.spent_cents / b.limit_cents - a.spent_cents / a.limit_cents)
      .slice(0, 4)
      .map((b) => {
        const pct = b.limit_cents ? Math.max(0, b.spent_cents) / b.limit_cents * 100 : 0;
        const cls = pct >= 100 ? "over" : pct >= 80 ? "warn" : "";
        return `<div class="ring-item" data-cat="${esc(b.category)}" role="button" tabindex="0"
            aria-label="${esc(b.category)}: ${fmt(b.spent_cents)} of ${fmt(b.limit_cents)}, ${Math.round(pct)} percent"
            title="${esc(b.category)}: ${fmt(b.spent_cents)} of ${fmt(b.limit_cents)}">
            <div class="ring ${cls}" style="--p:${Math.min(100, pct)}">
              <span class="dot ${catClass(b.category)}">${icon(b.category)}</span></div>
            <div class="ring-pct sub">${Math.round(pct)}%</div>
          </div>`;
      }).join("");
    stagger($("home-budgets"));
    if (!REDUCED) {
      $("home-budgets").querySelectorAll(".ring").forEach(function (el) {
        const target = parseFloat(el.style.getPropertyValue("--p")) || 0;
        const t0 = performance.now();
        el.style.setProperty("--p", 0);
        (function tick(now) {
          const p = Math.min((now - t0) / 600, 1);
          el.style.setProperty("--p", (target * (1 - Math.pow(1 - p, 3))).toFixed(1));
          if (p < 1) requestAnimationFrame(tick);
        })(t0);
      });
    }
  }

  function renderSpark(points, ym) {
    const byDay = Object.fromEntries(points.map((p) => [p.period.slice(8), p.total_cents]));
    const today = ym === currentYM() ? new Date().getDate() : daysInMonth(ym);
    const labels = [], data = [];
    for (let d = 1; d <= today; d++) {
      labels.push(d);
      data.push((byDay[String(d).padStart(2, "0")] || 0) / 100);
    }
    if (sparkChart) sparkChart.destroy();
    sparkChart = new Chart($("spark"), {
      type: "line",
      data: { labels, datasets: [{ data, borderColor: "rgba(255,255,255,.95)",
        backgroundColor: "rgba(255,255,255,.18)", fill: true, tension: .35,
        pointRadius: 0, borderWidth: 2 }] },
      options: { plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: { x: { display: false }, y: { display: false } },
        responsive: true, maintainAspectRatio: false,
        animation: REDUCED ? false : { duration: 500, easing: "easeOutQuart" } },
    });
    describeChart($("spark"), "Daily spending for " + ym + " over " + labels.length +
      " days. Total " + fmt(Math.round(data.reduce((a, b) => a + b, 0) * 100)) +
      ", highest day " + fmt(Math.round(Math.max(0, ...data) * 100)) + ".");
  }

  // ---- cards -----------------------------------------------------------
  // One helper owns the wording, so the row chip, the pills and the filter
  // chip cannot describe the same bucket differently.
  function cardLabel(id) {
    if (id === null || id === undefined) return "Unassigned";
    // A row can point at an ARCHIVED card: /api/cards lists only active ones,
    // so the name is not in the map. Say so honestly — "Unassigned" would be a
    // lie about an assigned row, and blank reads as "no cards exist".
    return state.cardNames[id] || ("Card #" + id);
  }

  // A breakdown bucket carries its own name (the API joins the card row), so it
  // reads correctly even for an ARCHIVED card, which /api/cards — and therefore
  // the map above — leaves out. Same wording for the null bucket either way.
  function bucketLabel(b) {
    return b.card_id === null ? cardLabel(null) : (b.name || cardLabel(b.card_id));
  }

  // Fetched once per refresh() rather than lazily with the Cards tab: txRow()
  // resolves its chip from this map, and rows are on screen long before the
  // user ever opens that tab.
  async function loadCards() {
    const body = await api.get("/api/cards");
    state.cards = body.cards;
    // The bindable senders ride along with the cards rather than needing a
    // route of their own, and each one carries the card it currently goes to
    // — which is what lets the form warn before a rebind without a request
    // per keystroke.
    state.cardSources = body.sources || [];
    state.cardNames = {};
    state.cards.forEach(function (c) { state.cardNames[c.id] = c.name; });
  }

  // Cards carry no colour of their own, so the donut borrows the category
  // tokens in a fixed order. Every one of these is @property-registered with
  // syntax:'<color>' — an unregistered light-dark() token comes back from
  // getComputedStyle as literal text, which is truthy, so a `|| fallback`
  // guard would never fire and Chart.js would be handed a string.
  const CARD_COLOR_KEYS = ["transport", "shopping", "food", "bills",
                           "entertainment", "health", "groceries", "travel"];
  // Keyed off the card's IDENTITY, never its position in the breakdown array.
  // Buckets come and go as cards are used (and the Unassigned bucket appears
  // and disappears), so an index would repaint the same card a different
  // colour every month — the category donut is stable precisely because it
  // keys off the category name.
  function cardColor(bucket) {
    const key = bucket.card_id === null ? "other"
      : CARD_COLOR_KEYS[bucket.card_id % CARD_COLOR_KEYS.length];
    return cssVar("--c-" + key + "-fg") || cssVar("--c-other-fg");
  }

  let cardChart = null;
  let cardBuckets = null;  // last /api/cards/breakdown payload (null = never opened)

  function markCardPills() {
    const pills = $("card-pills").querySelectorAll(".chip[data-cid]");
    for (let i = 0; i < pills.length; i++) {
      const v = pills[i].dataset.cid;
      pills[i].classList.toggle("sel", v === "all"
        ? state.cardFilter === null : Number(v) === state.cardFilter);
    }
  }

  // The row-level half of the card filter, shared by the transaction list and
  // the heatmap so one rule cannot drift into two. 0 is the Unassigned bucket
  // and null is "no filter" — never test state.cardFilter for falsiness here.
  function cardMatch(t) {
    return state.cardFilter === null ||
      (state.cardFilter === 0 ? t.card_id === null : t.card_id === state.cardFilter);
  }

  // Query fragment for the active card filter, or "" so callers can append it
  // unconditionally. Explicit null check, not falsiness: 0 is the Unassigned
  // bucket and must produce &card_id=0.
  function cardParam() {
    return state.cardFilter === null ? "" : "&card_id=" + state.cardFilter;
  }

  // The Home dropdown and the Cards-tab donut write the SAME state.cardFilter,
  // so every visible consequence of a change lives here rather than at either
  // call site — otherwise drilling into a slice leaves the dropdown lying.
  function syncCardFilterUI() {
    const sel = $("card-filter");
    const want = state.cardFilter === null ? "" : String(state.cardFilter);
    sel.value = want;
    // An ARCHIVED card can be the live filter: month_card_spend still reports
    // its spend, so a Cards-tab pill drills into an id the option list — built
    // from /api/cards, which lists ACTIVE cards only — has no option for.
    // Assigning an unmatched value leaves selectedIndex at -1 and the control
    // renders BLANK while the 💳 chip and the list both say a filter is on:
    // exactly the "dropdown lying" state this function exists to prevent. Name
    // it with an ephemeral option instead — cardLabel() already takes the
    // honest-fallback stance ("Card #12") for precisely this case. The option
    // is not persisted: the next renderCardFilter() rebuild drops it.
    if (sel.value !== want) {
      const opt = document.createElement("option");
      opt.value = want;
      opt.textContent = cardLabel(state.cardFilter);
      sel.appendChild(opt);
      sel.value = want;
    }
    sel.classList.toggle("sel", state.cardFilter !== null);
    markCardPills();
    renderFilterChip();
    // Budgets deliberately ignore the card filter — a budget is per-category,
    // not per-card — so say so rather than let the mismatch read as a bug.
    const notes = document.querySelectorAll(".budget-scope");
    for (let i = 0; i < notes.length; i++) {
      notes[i].classList.toggle("hidden", state.cardFilter === null);
    }
  }

  // Options are rebuilt from state.cards on every refresh() so a renamed card
  // cannot linger under its old name. Rebuilding wipes the selection, which is
  // why one of the two sync paths at the end always restores it.
  function renderCardFilter() {
    const sel = $("card-filter");
    $("card-filter-row").classList.toggle("hidden", !state.cards.length);
    // A card can be deleted or archived while it is the active filter. Drop
    // the filter rather than leave the dropdown reading "All cards" over a
    // list that is still filtered. This is the ONE place falsiness is right:
    // it must skip 0 (Unassigned, never in cardNames) and null (no filter).
    const dropped = !!(state.cardFilter && !state.cardNames[state.cardFilter]);
    if (dropped) state.cardFilter = null;
    sel.innerHTML = `<option value="">💳 All cards</option>` +
      state.cards.map(function (c) {
        const sub = [c.kind, c.last4 ? "····" + c.last4 : ""].filter(Boolean).join(" ");
        return `<option value="${c.id}">${esc(c.name)}` +
          `${sub ? " · " + esc(sub) : ""}</option>`;
      }).join("") +
      `<option value="0">Unassigned</option>`;
    // Order matters: the rebuild above wipes the selection, so whichever sync
    // path runs has to run AFTER it.
    //
    // A drop is not a cosmetic change — it changes what every scoped widget
    // should show, and refresh() has already fired renderHeatmap() and
    // loadTxs(). Those two read cardMatch() AFTER their awaits, i.e. after
    // this drop lands, while renderHome() read cardParam() synchronously
    // BEFORE its first await. Ending at syncCardFilterUI() therefore left the
    // donut, month total and sparkline card-scoped while the heatmap and the
    // list went all-cards: two answers to "what did I spend" at once. Route
    // through applyCardFilter() so every widget agrees.
    //
    // Guarded on the drop rather than called unconditionally: every refresh()
    // reaches here, and the common case must not pay a second full repaint.
    if (dropped) applyCardFilter();
    else syncCardFilterUI();
  }

  // Everything the card filter scopes, in one place. Every writer of
  // state.cardFilter calls this, so no path can repaint half the page.
  // renderHome refetches the donut, the month total and the sparkline (5
  // requests, 6 while a filter is on — see the unfiltered #desk-total
  // aggregate) and renderHeatmap re-awaits a full month of /api/transactions;
  // only renderTxList is free, re-filtering state.txs in place. So this costs
  // 6-7 requests a call and is NOT cheap enough to fire speculatively — hence
  // the no-change guard below.
  //
  // The guard tracks the last APPLIED value, not the last requested one, and
  // starts at null to match state.cardFilter at boot. A closed <select> fires
  // `change` on every arrow keypress in Chrome and Firefox, so arrowing past
  // five cards used to mean ~35 requests and five overlapping renderHome()
  // fan-outs with no sequencing token — a slow early response could land last
  // and leave the donut on the wrong card. It also makes "See all" and the
  // chip dismiss free when no filter was set.
  //
  // syncCardFilterUI() stays ABOVE the guard on purpose: renderCardFilter()
  // calls in here right after rebuilding the <option> list, which wipes the
  // selection, so the sync can never be the thing that gets skipped.
  let appliedCardFilter = null;

  function applyCardFilter() {
    syncCardFilterUI();
    if (state.cardFilter === appliedCardFilter) return;
    appliedCardFilter = state.cardFilter;
    renderHome();
    renderHeatmap();
    renderTxList();
  }

  async function renderCards() {
    const ym = state.homeMonth;
    // Totals come from the API, never from summing the client's month of rows:
    // one source per question. (The FILTER is client-side — the rows are
    // already here — but what the donut says the month cost is not.)
    const body = await api.get("/api/cards/breakdown?month=" + ym);
    cardBuckets = body.buckets;
    $("cards-month").textContent = monthLabel(body.month);
    if (cardChart) { cardChart.destroy(); cardChart = null; }
    if (!cardBuckets.length) {
      $("card-pills").innerHTML = `<div class="empty">Nothing spent this month</div>`;
      return;
    }
    $("card-pills").innerHTML =
      `<button class="chip" data-cid="all">All</button>` +
      cardBuckets.map(function (b) {
        const id = b.card_id === null ? 0 : b.card_id;
        return `<button class="chip" data-cid="${id}">` +
               `${esc(bucketLabel(b))} · ${fmt(b.total_cents)}</button>`;
      }).join("");
    markCardPills();

    const labels = cardBuckets.map(bucketLabel);
    cardChart = new Chart($("by-card"), {
      type: "doughnut",
      data: {
        // Reimbursements are negative expense rows, so a bucket can net below
        // zero; a negative slice would draw as a positive wedge.
        labels: labels,
        datasets: [{ data: cardBuckets.map((b) => Math.max(0, b.total_cents) / 100),
          backgroundColor: cardBuckets.map(cardColor),
          borderWidth: 0, hoverOffset: 10 }],
      },
      // Same pin as the category donut: Chart.js sizes the ring from the canvas
      // MINUS the largest slice offset, so hovering would otherwise shrink and
      // wobble the whole thing.
      plugins: [{ beforeDatasetsUpdate: (c) => {
        c.getDatasetMeta(0).controller.getMaxOffset = () => DONUT_POP;
      } }],
      options: { cutout: "70%", responsive: true, maintainAspectRatio: false,
        animation: REDUCED ? false : { duration: 600, easing: "easeOutQuart" },
        layout: { padding: 10 },
        onClick: (e, els) => {
          if (!els.length) return;
          const b = cardBuckets[els[0].index];
          cardDrillDown(b.card_id === null ? 0 : b.card_id);
        },
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: (i) => " S$" + i.parsed.toFixed(2) } } } },
    });
    describeChart($("by-card"), "Spending by card in " + monthLabel(body.month) +
      ": " + topParts(cardBuckets.map(
        (b) => [bucketLabel(b), Math.max(0, b.total_cents)]), 3) + ".");
  }

  // Which row the picker is about. Held here rather than on the overlay so a
  // re-render of the list underneath cannot lose it mid-choice.
  let cardPickTid = null;

  function openCardPicker(tid) {
    cardPickTid = tid;
    // state.cards comes from /api/cards, which excludes archived cards — and
    // the API answers 400 "card is archived" for an assignment to one, so this
    // list must never be built from anything wider.
    const rows = state.cards.map(function (c) {
      const sub = [c.kind, c.last4 ? "····" + c.last4 : ""].filter(Boolean).join(" ");
      return `<button class="link-pick" data-cid="${c.id}">` +
             `<span class="lp-ico">💳</span>` +
             `<span class="lp-note">${esc(c.name)}</span>` +
             `<span class="lp-amt">${esc(sub)}</span></button>`;
    });
    // 0 is what the API maps back to NULL. Without this entry a wrongly
    // attributed row could be moved but never emptied.
    rows.push(`<button class="link-pick" data-cid="0">` +
      `<span class="lp-ico">∅</span><span class="lp-note">Unassigned</span>` +
      `<span class="lp-amt"></span></button>`);
    if (!state.cards.length) {
      rows.unshift(`<div class="empty">No cards yet — add one on the Cards tab.</div>`);
    }
    $("card-pick-list").innerHTML = rows.join("");
    openOverlay("card-overlay");
  }

  async function setCard(cid) {
    const tid = cardPickTid;
    closeOverlay("card-overlay");
    try {
      // A card-only PATCH sends nothing else; the API applies it separately so
      // it is not reported as "nothing to update". Correcting one here is what
      // teaches the importer which card that sender's instrument belongs to.
      await api.send("PATCH", "/api/transactions/" + tid, { card_id: cid });
      toast(cid ? "Card set" : "Card cleared");
    } catch (err) {
      toast("Could not set the card");
    }
    refresh();
  }

  // ---- the card list: add, edit, delete ---------------------------------
  // The id being edited, null while creating. ONE form serves both: the fields
  // are identical, so a second copy would drift the first time either grows one.
  let cardEditing = null;

  function cardRow(c) {
    // Everything here is already in the /api/cards payload, tx_count
    // included, so the list costs nothing beyond the fetch refresh() makes.
    const bound = state.cardSources.filter(function (s) { return s.card_id === c.id; })
      .map(function (s) { return s.label; });
    const sub = [c.kind, c.last4 ? "····" + c.last4 : "",
                 c.tx_count + (c.tx_count === 1 ? " transaction" : " transactions"),
                 bound.length ? "✉ " + bound.join(", ") : "",
                 c.match ? "matches " + c.match : ""].filter(Boolean).join(" · ");
    // The ROW opens the edit form, so it carries role+tabindex and the one
    // document-level Enter/Space handler activates it. The 🗑 is a real
    // <button>, which that handler bails out on before closest() can climb
    // here — otherwise Enter on the row would delete instead of edit.
    return `<div class="crow" data-cid="${c.id}" role="button" tabindex="0" title="Edit this card">
        <div class="ico">💳</div>
        <div class="mid"><div class="t">${esc(c.name)}</div>
          <div class="s">${esc(sub)}</div></div>
        <button class="linkish c-del" aria-label="Delete ${esc(c.name)}">🗑</button>
      </div>`;
  }

  function renderCardList() {
    const list = $("card-list");
    list.innerHTML = state.cards.map(cardRow).join("") ||
      `<div class="empty">No cards yet — add one below.</div>`;
    stagger(list);
  }

  // Options come from the payload, never retyped: a parser added to
  // trust_email.SOURCES shows up here with no edit. Each option names the card
  // that sender goes to today, so the confirm on submit is not a surprise.
  function fillBindPicker() {
    const keep = cardEditing ? "Leave sender attribution unchanged"
                             : "Don't attribute any sender";
    $("card-bind").innerHTML = `<option value="">${esc(keep)}</option>` +
      state.cardSources.map(function (s) {
        const owner = s.card_id ? " → " + cardLabel(s.card_id) : "";
        return `<option value="${esc(s.value)}">${esc(s.label + owner)}</option>`;
      }).join("");
  }

  function openCardForm(card) {
    cardEditing = card ? card.id : null;
    $("card-form-title").textContent = card ? "Edit card" : "New card";
    $("card-name").value = card ? card.name : "";
    $("card-kind").value = card ? card.kind : "debit";
    $("card-last4").value = card && card.last4 ? card.last4 : "";
    $("card-match").value = card && card.match ? card.match : "";
    fillBindPicker();
    $("card-form").classList.remove("hidden");
    $("card-add").classList.add("hidden");
    $("card-name").focus();
  }

  function closeCardForm() {
    cardEditing = null;
    $("card-form").classList.add("hidden");
    $("card-add").classList.remove("hidden");
    // Save and Cancel live INSIDE the form being hidden, and display:none
    // drops focus to <body>. The app's other forms are always visible, so
    // there is no precedent to inherit -- send it somewhere sensible.
    $("card-add").focus();
  }

  async function saveCard(e) {
    // The form has no action, so without this the dashboard reloads and loses
    // its state on every save.
    e.preventDefault();
    const editing = cardEditing;   // read before closeCardForm() clears it
    const name = $("card-name").value.trim();
    if (!name) return toast("Name the card");
    const bind = $("card-bind").value;
    // Rebinding moves every FUTURE import from that sender, so it is confirmed
    // rather than silent. The owner comes from the same /api/cards payload the
    // picker was built from — no request per keystroke — and only a DIFFERENT
    // card counts, or re-saving a card would ask about its own binding.
    // The wording is deliberately narrow. bind_source writes the SOURCE
    // DEFAULT, which is the LOWEST rung of the ladder in cards.effective_card:
    // a past correction (token memory) or another card's match rule both
    // outrank it, so a spend carrying a known instrument keeps going where it
    // went before. Promising to "move every future import" would be a lie the
    // resolver cannot keep -- Trust mail carries a stable instrument, so one
    // prior correction pins it.
    const owner = state.cardSources.find(function (s) { return s.value === bind; });
    if (owner && owner.card_id && owner.card_id !== editing &&
        !confirm(owner.label + " defaults to " + cardLabel(owner.card_id) +
                 " today. Make this card its default instead?\n\n" +
                 "Spends already corrected by hand, or matched by another " +
                 "card's rule, keep their current card.")) return;
    const body = { name: name, kind: $("card-kind").value,
                   last4: $("card-last4").value.trim(),
                   // "" clears the rule, never null: CardPatch reads null as
                   // "not supplied", so a `|| null` here would make a
                   // typed-and-regretted rule permanent. normalise_instrument("")
                   // is falsy, so an empty rule is inert server-side.
                   match: $("card-match").value.trim(),
                   bind_source: bind || null };
    try {
      await api.send(editing ? "PATCH" : "POST",
                     editing ? "/api/cards/" + editing : "/api/cards", body);
    } catch (err) {
      return toast("Could not save the card");
    }
    closeCardForm();
    toast(editing ? "Card updated" : "Card added");
    // Not just the list: a renamed card is on the donut, in the pills and in
    // every row chip, all of which read the map refresh() reloads.
    refresh();
  }

  async function deleteCard(c) {
    if (!confirm("Delete " + c.name + "?")) return;
    const r = await fetch("/api/cards/" + c.id, { method: "DELETE" });
    if (!r.ok) return toast("Could not delete the card");
    const body = await r.json();
    // archived:true means history still points here and the card was merely
    // hidden; false means the row is gone. One message for both would leave
    // the user not knowing which happened.
    toast(body.archived ? "Archived — past transactions keep this name"
                        : "Card deleted");
    if (cardEditing === c.id) closeCardForm();
    refresh();
  }

  // ---- transactions ---------------------------------------------------
  function txRow(t) {
    const chip = isAuto(t.source) ? ` <span class="auto">✉ auto</span>` : "";
    const inc = t.type === "income";
    const reimb = t.amount_cents < 0;
    const badge = t.reimbursed_cents > 0
      ? ` <span class="auto rb">↩️ ${fmt(t.reimbursed_cents)} back</span>` : "";
    // A reimbursement is stored as a NEGATIVE expense so the month nets down,
    // but that is ledger mechanics -- to the person reading the row, S$30 came
    // back INTO their account. It renders "+" (and green, via .plus) to match
    // income. Showing "-S$30.00" in green was contradicting itself.
    const amt = (inc || reimb ? "+" : "-") + fmt(Math.abs(t.amount_cents));
    // The chip is how a mis-attributed (or Unassigned) row gets fixed, and
    // fixing one is what teaches the importer — so it is on every row, not
    // only the ones the importer got wrong.
    const card = ` <span class="auto card-chip" data-tid="${t.id}" role="button"
        tabindex="0" title="Change card">💳 ${esc(cardLabel(t.card_id))}</span>`;
    return `<div class="tx ${inc ? "income" : catClass(t.category)}${reimb ? " reimb" : ""}" data-id="${t.id}" role="button" tabindex="0">
        <div class="ico">${inc ? "💰" : reimb ? "↩️" : icon(t.category)}</div>
        <div class="mid"><div class="t">${esc(t.note) || "(no note)"}</div>
          <div class="s">${esc(t.category)}${chip}${badge}${card}</div></div>
        <div class="amt money ${inc || reimb ? "plus" : ""}">${amt}</div>
      </div>`;
  }

  async function loadMonthChips() {
    const trend = await api.get("/api/trend?granularity=month");
    const months = trend.points.map((p) => p.period).reverse().slice(0, 6);
    if (!months.includes(currentYM())) months.unshift(currentYM());
    $("month-chips").innerHTML = months.map((m) => {
      const label = new Date(m + "-01").toLocaleDateString("en-SG",
        { month: "short", year: "2-digit" });
      return `<button class="chip ${m === state.month ? "sel" : ""}" data-m="${m}">${label}</button>`;
    }).join("") +
      `<button class="chip ${state.month === "all" ? "sel" : ""}" data-m="all">All</button>`;
  }

  async function loadTxs() {
    const q = state.month === "all"
      ? "?limit=500"
      : `?date_from=${state.month}-01T00:00:00&date_to=${state.month}-${daysInMonth(state.month)}`;
    state.txs = await api.get("/api/transactions" + q);
    state.txShown = 10;
    renderTxList();
  }

  function dayLabel(iso) {
    const d = new Date(iso + "T00:00:00");
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const diff = Math.round((today - d) / 86400000);
    if (diff === 0) return "Today";
    if (diff === 1) return "Yesterday";
    return d.toLocaleDateString("en-SG", { weekday: "short", day: "numeric", month: "short" });
  }

  function renderTxList() {
    const q = state.search.toLowerCase();
    const matches = state.txs.filter((t) =>
      (!state.filterCat || t.category === state.filterCat) &&
      // Joins the same AND chain rather than pre-filtering, so a card and a
      // category drill-down compose instead of clobbering each other.
      cardMatch(t) &&
      (state.filterSrc === "all" || (state.filterSrc === "auto") === isAuto(t.source)) &&
      (!state.minCents || t.amount_cents >= state.minCents) &&
      (!q || t.note.toLowerCase().includes(q) || t.category.toLowerCase().includes(q)));
    if (!matches.length) {
      $("tx-list").innerHTML = `<div class="empty">No transactions</div>`;
      return;
    }
    // Searching shows every match; the plain list paginates 10 at a time.
    const txs = q ? matches : matches.slice(0, state.txShown);
    const groups = new Map();
    for (const t of txs) {
      const day = t.created_at.slice(0, 10);
      if (!groups.has(day)) groups.set(day, []);
      groups.get(day).push(t);
    }
    let html = "";
    for (const [day, rows] of groups) {
      const total = rows.reduce((s, t) => s + (t.type === "income" ? -t.amount_cents : t.amount_cents), 0);
      html += `<div class="day"><span>${dayLabel(day)}</span><span class="money">${fmt(total)}</span></div>`;
      html += rows.map(txRow).join("");
    }
    if (!q && matches.length > state.txShown) {
      html += `<button id="tx-more" class="linkish more">Show more (${matches.length - state.txShown} left)</button>`;
    }
    $("tx-list").innerHTML = html;
    stagger($("tx-list"));
  }

  // ---- budgets ---------------------------------------------------------
  async function renderBudgets() {
    const ym = state.homeMonth;                       // follows the home ‹ › arrows
    const budgets = await api.get("/api/budgets?month=" + ym);
    const past = ym !== currentYM();
    const daysLeft = past ? 0 : daysInMonth(ym) - new Date().getDate() + 1;
    $("budget-list").innerHTML = budgets.map((b) => {
      const pct = b.limit_cents ? Math.max(0, b.spent_cents) / b.limit_cents * 100 : 0;
      const cls = pct >= 100 ? "over" : pct >= 80 ? "warn" : "";
      const left = Math.max(0, b.limit_cents - b.spent_cents);
      const pace = pct >= 100
        ? `over by ${fmt(b.spent_cents - b.limit_cents)}`
        : past ? "" : `${fmt(Math.floor(left / daysLeft))}/day left · ${daysLeft} days`;
      return `<div class="bcard" data-cat="${esc(b.category)}" role="button" tabindex="0">
          <div class="top">
            <span class="name"><span class="dot ${catClass(b.category)}">${icon(b.category)}</span>${esc(b.category)}</span>
            <span class="b-tools">${Math.round(pct)}%
              <button class="b-edit" title="Edit">✎</button>
              <button class="b-del" title="Delete">✕</button></span>
          </div>
          <div class="bar-track"><div class="bar-fill ${cls}" style="width:${Math.min(100, pct)}%"></div></div>
          <div class="nums"><span class="money">${fmt(b.spent_cents)} of ${fmt(b.limit_cents)}</span>
            <span>${pace}</span></div>
        </div>`;
    }).join("") || `<div class="empty">No budgets yet — set one below.</div>`;
    stagger($("budget-list"));
  }

  // ---- insights ---------------------------------------------------------
  function prevYM(ym) {
    const [y, m] = ym.split("-").map(Number);
    return m === 1 ? (y - 1) + "-12" : y + "-" + String(m - 1).padStart(2, "0");
  }
  function nextYM(ym) {
    const [y, m] = ym.split("-").map(Number);
    return m === 12 ? (y + 1) + "-01" : y + "-" + String(m + 1).padStart(2, "0");
  }

  // ---- category drill-down ----------------------------------------------
  // Pop the filtered category's slice out of the donut (null = all back home).
  // Chart.js animates offset changes, so clearing the filter glides the slice in.
  function setDonutPop(cat) {
    if (!donutChart) return;
    donutChart.data.datasets[0].offset =
      donutChart.data.labels.map((l) => (l === cat ? DONUT_POP : 0));
    donutChart.update();
  }

  function drillDown(cat) {
    state.filterCat = cat;
    setDonutPop(cat);
    renderFilterChip();
    renderTxList();
    show("txns");
  }

  // Two drill-downs can be live at once — a category from the donut and a card
  // from the Cards tab — so each chip carries what it clears and the container
  // handler branches on it. One chip that cleared everything would drop a
  // filter the user never touched.
  function renderFilterChip() {
    const el = $("filter-chip");
    const chips = [];
    if (state.filterCat) {
      chips.push(`<button class="chip sel" id="filter-clear" data-clear="cat">${esc(state.filterCat)} ✕</button>`);
    }
    if (state.cardFilter !== null) {   // 0 is Unassigned, not "no filter"
      chips.push(`<button class="chip sel" data-clear="card">💳 ` +
        `${esc(cardLabel(state.cardFilter === 0 ? null : state.cardFilter))} ✕</button>`);
    }
    el.classList.toggle("hidden", !chips.length);
    el.innerHTML = chips.join("");
  }

  // Mirrors drillDown(): #view-cards holds no transaction list, so a pill that
  // only set the filter would look like it did nothing at all.
  function cardDrillDown(cardId) {
    state.cardFilter = cardId;
    applyCardFilter();
    show("txns");   // show() folds txns into home on desktop; not special-cased
  }

  const MONTH_ABBR = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

  // Monthly bar values for the current pill selection (all categories when null).
  function yearBarData() {
    const y = state.yearData;
    return y.by_month.map((m) => (state.yrCat
      ? (m.by_category[state.yrCat] || 0) : m.total_cents) / 100);
  }

  // Toggle the per-category filter: bars + color swap in place so Chart.js
  // animates between the all-categories and single-category shapes.
  function setYearCat(cat) {
    state.yrCat = state.yrCat === cat ? null : cat;
    const pills = $("yr-cats").children;
    for (let i = 0; i < pills.length; i++) {
      pills[i].classList.toggle("sel", pills[i].dataset.cat === state.yrCat);
    }
    const ds = yearChart.data.datasets[0];
    ds.data = yearBarData();
    ds.backgroundColor = state.yrCat ? catColor(state.yrCat) : cssVar("--primary");
    yearChart.update();
  }

  async function renderYear() {
    const y = await api.get("/api/year/" + state.year);
    state.yearData = y;
    state.yrCat = null; // selection is per-year; reset when the year changes
    $("yr-label").textContent = state.year;
    countUp($("yr-total"), y.total_cents);
    $("yr-income").textContent = y.income_cents ? "Income " + fmt(y.income_cents) : "";
    if (yearChart) yearChart.destroy();
    yearChart = new Chart($("yearbars"), {
      type: "bar",
      // by_month stops at the current month for the current year — no future bars
      data: { labels: MONTH_ABBR.slice(0, y.by_month.length),
        datasets: [{ data: yearBarData(),
          backgroundColor: cssVar("--primary"), borderRadius: 4 }] },
      options: { responsive: true, maintainAspectRatio: false,
        animation: REDUCED ? false : { duration: 500, easing: "easeOutQuart" },
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: (i) => " S$" + i.parsed.y.toFixed(2) } } },
        scales: {
          x: { grid: { display: false },
               ticks: { maxRotation: 0, autoSkip: true, font: { size: 10 } } },
          y: { ticks: { callback: (v) => "S$" + v, maxTicksLimit: 4, font: { size: 10 } } } } },
    });
    const yb = yearBarData();
    // Math.max on an empty array is -Infinity, hence the guard.
    describeChart($("yearbars"), yb.length
      ? "Monthly spending in " + state.year + ": total " + fmt(y.total_cents) +
        " across " + yb.length + " months, highest " +
        MONTH_ABBR[yb.indexOf(Math.max(...yb))] + "."
      : "No spending recorded in " + state.year + ".");
    $("yr-cats").innerHTML = y.by_category.map((c) => `<div class="pill" data-cat="${esc(c.category)}" role="button" tabindex="0">
        <div class="name"><span class="dot ${catClass(c.category)}">${icon(c.category)}</span>${esc(c.category)}</div>
        <div class="nums money">${fmt(c.total_cents)}</div>
      </div>`).join("") || `<div class="empty">Nothing this year</div>`;
    stagger($("yr-cats"));
  }

  async function renderInsights() {
    const ym = currentYM();
    const [cur, last, merch, trend, cash, monthTxs] = await Promise.all([
      api.get("/api/summary?month=" + ym),
      api.get("/api/summary?month=" + prevYM(ym)),
      api.get("/api/merchants?month=" + ym),
      api.get("/api/trend?granularity=day"),
      api.get("/api/cashflow"),
      api.get(`/api/transactions?date_from=${ym}-01T00:00:00&date_to=${ym}-${daysInMonth(ym)}`),
    ]);
    renderCompare(cur, last);
    state.merchShown = 5;
    renderMerchants(merch.merchants);
    renderCashflow(cash.points);
    renderCallouts(monthTxs, merch.merchants,
                   trend.points.filter((p) => p.period.startsWith(ym)));
    renderRhythm(trend.points);
  }

  function renderCallouts(txs, merchants, dayPoints) {
    const spends = txs.filter((t) => t.type !== "income");
    const big = spends.reduce((a, b) => (b.amount_cents > (a ? a.amount_cents : 0) ? b : a), null);
    const freq = merchants.slice().sort((a, b) => b.count - a.count)[0];
    const worst = dayPoints.reduce((a, b) => (b.total_cents > (a ? a.total_cents : 0) ? b : a), null);
    const tile = (emoji, label, value, sub) => `<div class="card tile">
        <div class="t-emoji">${emoji}</div><div class="sub">${label}</div>
        <div class="t-val money">${value}</div><div class="sub t-sub">${sub}</div>
      </div>`;
    $("callouts").innerHTML =
      (big ? tile("💸", "Biggest purchase", fmt(big.amount_cents), esc(big.note) || "(no note)") : "") +
      (freq ? tile("🔁", "Most frequent", freq.count + "×", esc(freq.merchant)) : "") +
      (worst ? tile("📅", "Priciest day", fmt(worst.total_cents), dayLabel(worst.period)) : "");
    stagger($("callouts"));
  }

  // Weekday averages for the current pill selection (all categories when null).
  // Per-category cells clamp at 0 — a reimbursement-heavy window can net negative.
  function rhythmBarData() {
    return rhythmAgg.sums.map((s, i) => {
      const cents = state.rhythmCat
        ? Math.max(0, s[state.rhythmCat] || 0) : s._total;
      return cents / rhythmAgg.counts[i] / 100;
    });
  }

  // Same toggle pattern as the year view: pill click filters the bars in place.
  function setRhythmCat(cat) {
    state.rhythmCat = state.rhythmCat === cat ? null : cat;
    const pills = $("rhythm-cats").children;
    for (let i = 0; i < pills.length; i++) {
      pills[i].classList.toggle("sel", pills[i].dataset.cat === state.rhythmCat);
    }
    const ds = rhythmChart.data.datasets[0];
    ds.data = rhythmBarData();
    ds.backgroundColor = state.rhythmCat ? catColor(state.rhythmCat) : cssVar("--primary");
    rhythmChart.update();
  }

  function renderRhythm(points) {
    const byDay = Object.fromEntries(points.map((p) => [p.period, p]));
    const sums = [], counts = Array(7).fill(0), catTotals = {};
    for (let i = 0; i < 7; i++) sums.push({ _total: 0 }); // "_total" can't collide: fixed category names
    const start = new Date(); start.setDate(start.getDate() - 83); // 12 weeks
    const today = new Date();
    for (let d = new Date(start); d <= today; d.setDate(d.getDate() + 1)) {
      const wd = (d.getDay() + 6) % 7; // Mon=0
      const p = byDay[localDateKey(d)];
      if (p) {
        sums[wd]._total += p.total_cents;
        Object.keys(p.by_category).forEach((cat) => {
          sums[wd][cat] = (sums[wd][cat] || 0) + p.by_category[cat];
          catTotals[cat] = (catTotals[cat] || 0) + p.by_category[cat];
        });
      }
      counts[wd]++;
    }
    rhythmAgg = { sums: sums, counts: counts };
    state.rhythmCat = null;
    if (rhythmChart) rhythmChart.destroy();
    rhythmChart = new Chart($("rhythm"), {
      type: "bar",
      data: { labels: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        datasets: [{ data: rhythmBarData(),
          backgroundColor: cssVar("--primary"), borderRadius: 6 }] },
      options: { responsive: true, maintainAspectRatio: false,
        animation: REDUCED ? false : { duration: 500, easing: "easeOutQuart" },
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: (i) => " avg S$" + i.parsed.y.toFixed(2) } } },
        scales: { y: { ticks: { callback: (v) => "S$" + v } } } },
    });
    const rb = rhythmBarData();
    describeChart($("rhythm"), "Average spending by weekday: " +
      ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        .map((w, i) => w + " " + fmt(Math.round(rb[i] * 100))).join(", ") + ".");
    const cats = Object.keys(catTotals).filter((c) => catTotals[c] > 0)
      .sort((a, b) => catTotals[b] - catTotals[a]);
    $("rhythm-cats").innerHTML = cats.map((c) => `<div class="pill" data-cat="${esc(c)}" role="button" tabindex="0">
        <div class="name"><span class="dot ${catClass(c)}">${icon(c)}</span>${esc(c)}</div>
        <div class="nums money">${fmt(Math.round(catTotals[c] / 12))}/wk</div>
      </div>`).join("");
    stagger($("rhythm-cats"));
  }

  function deltaTag(d) {
    if (d > 0) return `<span class="up">▲ ${fmt(d)}</span>`;
    if (d < 0) return `<span class="down">▼ ${fmt(-d)}</span>`;
    return `<span class="sub">—</span>`;
  }

  function renderCompare(cur, last) {
    const lastBy = Object.fromEntries(last.by_category.map((c) => [c.category, c.total_cents]));
    const curBy = Object.fromEntries(cur.by_category.map((c) => [c.category, c.total_cents]));
    const cats = [...cur.by_category.map((c) => c.category),
                  ...last.by_category.map((c) => c.category).filter((c) => !(c in curBy))];
    const rows = cats.map((cat) => {
      const now = curBy[cat] || 0;
      return `<div class="pill" data-cat="${esc(cat)}" role="button" tabindex="0">
        <div class="name"><span class="dot ${catClass(cat)}">${icon(cat)}</span>${esc(cat)}</div>
        <div class="nums money">${fmt(now)} ${deltaTag(now - (lastBy[cat] || 0))}</div>
      </div>`;
    });
    const totalRow = `<div class="pill total"><div class="name">Total</div>
        <div class="nums money">${fmt(cur.total_cents)} ${deltaTag(cur.total_cents - last.total_cents)}</div></div>`;
    $("compare-list").innerHTML = totalRow + (rows.join("") || `<div class="empty">Nothing this month</div>`);
    stagger($("compare-list"));
  }

  function renderMerchants(rows) {
    state.merchants = rows;
    let html = rows.slice(0, state.merchShown).map((r) => `<div class="mcard">
        <div class="dot ${catClass(r.category)}">${icon(r.category)}</div>
        <div class="m-name">${esc(r.merchant)}</div>
        <div class="m-total money">${fmt(r.total_cents)}</div>
        <div class="sub">${r.count}× · ${esc(r.category)}</div>
      </div>`).join("") || `<div class="empty">Nothing this month</div>`;
    if (rows.length > state.merchShown)
      html += `<button id="merch-more" class="more">Show more (${rows.length - state.merchShown} left)</button>`;
    $("merchant-list").innerHTML = html;
    stagger($("merchant-list"));
  }

  // Month calendar heatmap — follows the home ‹ › arrows, hover shows the day's txns.
  async function renderHeatmap() {
    const ym = state.homeMonth;
    // No API change needed: these rows already carry card_id, and the heatmap
    // aggregates them here anyway.
    const txs = (await api.get(
      `/api/transactions?date_from=${ym}-01T00:00:00&date_to=${ym}-${daysInMonth(ym)}`))
      .filter((t) => cardMatch(t));
    const byDay = {};
    for (const t of txs) {
      if (t.type === "income") continue;
      const day = t.created_at.slice(0, 10);
      (byDay[day] = byDay[day] || { total: 0, txs: [] });
      byDay[day].total += t.amount_cents;
      byDay[day].txs.push(t);
    }
    state.heatDays = byDay;
    const totals = Object.values(byDay).map((d) => d.total).sort((a, b) => a - b);
    const q = (p) => totals.length ? totals[Math.floor(p * (totals.length - 1))] : 0;
    const t1 = q(0.25), t2 = q(0.5), t3 = q(0.75);
    const [y, m] = ym.split("-").map(Number);
    const monthName = new Date(y, m - 1, 1).toLocaleString(undefined, { month: "long" });
    let html = ["M", "T", "W", "T", "F", "S", "S"].map((w) =>
      `<div class="wd">${w}</div>`).join("");
    const lead = (new Date(y, m - 1, 1).getDay() + 6) % 7; // Mon-first
    for (let i = 0; i < lead; i++) html += `<div class="blank"></div>`;
    for (let d = 1; d <= daysInMonth(ym); d++) {
      const key = ym + "-" + String(d).padStart(2, "0");
      const v = byDay[key] ? Math.max(0, byDay[key].total) : 0;
      const lv = v === 0 ? 0 : v <= t1 ? 1 : v <= t2 ? 2 : v <= t3 ? 3 : 4;
      html += `<div class="lv${lv}" data-day="${key}" role="button" tabindex="0"
        aria-label="${d} ${monthName}: ${v ? fmt(v) + " spent" : "nothing spent"}">${d}</div>`;
    }
    $("heatmap").innerHTML = html;
    stagger($("heatmap"), 20);
  }

  function heatTip(cell) {
    const tip = $("hm-tip");
    if (!cell) { tip.classList.remove("show"); return; }
    const day = cell.dataset.day;
    const info = state.heatDays[day];
    const rows = info
      ? info.txs.slice(0, 5).map((t) =>
          `<div class="row"><span>${icon(t.category)} ${esc(t.note) || "(no note)"}</span>
             <span class="money">${fmt(t.amount_cents)}</span></div>`).join("") +
        (info.txs.length > 5 ? `<div class="sub">+${info.txs.length - 5} more</div>` : "")
      : `<div class="sub">No spending</div>`;
    tip.innerHTML = `<div class="d">${dayLabel(day)} · ` +
      `<span class="money">${fmt(info ? info.total : 0)}</span></div>` + rows;
    tip.classList.add("show");
    const r = cell.getBoundingClientRect();
    tip.style.left = Math.max(8, Math.min(innerWidth - tip.offsetWidth - 8,
      r.left + r.width / 2 - tip.offsetWidth / 2)) + "px";
    tip.style.top = Math.max(8, r.top - tip.offsetHeight - 8) + "px";
  }

  function renderCashflow(points) {
    const last6 = points.slice(-6);
    if (cashChart) cashChart.destroy();
    cashChart = new Chart($("cashflow"), {
      type: "bar",
      data: { labels: last6.map((p) => p.period),
        datasets: [
          { label: "In", data: last6.map((p) => p.income_cents / 100), backgroundColor: "#30a46c" },
          { label: "Out", data: last6.map((p) => p.expense_cents / 100), backgroundColor: "#e5484d" },
        ] },
      options: { responsive: true, maintainAspectRatio: false,
        animation: REDUCED ? false : { duration: 500, easing: "easeOutQuart" },
        plugins: { legend: { position: "bottom" } },
        scales: { y: { ticks: { callback: (v) => "S$" + v } } } },
    });
    describeChart($("cashflow"), "Income versus spending, last " + last6.length +
      " months: " + last6.map((p) => p.period + " in " + fmt(p.income_cents) +
        ", out " + fmt(p.expense_cents)).join("; ") + ".");
  }

  // ---- goals ------------------------------------------------------------
  const PRI = { 1: "⭐ High", 2: "", 3: "Low" };

  function paceLine(g) {
    const p = g.pace || {};
    // Committed monthly auto-save shown regardless of pace status — otherwise
    // arming auto-funding on a no-deadline goal has no visible confirmation.
    const monthly = g.monthly_cents ? `saving ${fmt(g.monthly_cents)}/mo` : null;
    if (g.bought_at) return `<div class="nums">✅ Bought</div>`;
    if (p.status === "done")
      return `<div class="nums">🎉 Fully saved${monthly ? " · " + monthly : ""}</div>`;
    if (p.status === "none")
      return monthly ? `<div class="nums">💵 ${monthly}</div>` : "";
    if (p.status === "overdue")
      return `<div class="nums warn">⚠️ Overdue (${g.target_date}) · ` +
        `${fmt(p.need_per_month)} short${monthly ? " · " + monthly : ""}</div>`;
    const by = g.target_date ? ` by ${g.target_date}` : "";
    let need = `needs ${fmt(p.need_per_month)}/mo${by}`;
    if (monthly) need += " · " + monthly;
    if (p.status === "behind")
      return `<div class="nums warn">⚠️ ${need} · behind ${fmt(p.shortfall_per_month)}/mo</div>`;
    return `<div class="nums">✅ ${need}</div>`;
  }

  function goalCard(g) {
    const pct = g.target_cents ? g.saved_cents / g.target_cents * 100 : 0;
    const bits = [PRI[g.priority], g.category ? "🏷 " + esc(g.category) : ""]
      .filter(Boolean).join(" · ");
    return `<div class="bcard${g.bought_at ? " done" : ""}" data-gid="${g.id}">
        <div class="top"><span class="name">🎯 ${esc(g.name)}</span>
          <span>${Math.round(pct)}%</span></div>
        <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, pct)}%"></div></div>
        <div class="nums"><span class="money">${fmt(g.saved_cents)} of ${fmt(g.target_cents)}</span>
          <span>${bits}</span></div>
        ${paceLine(g)}
        ${g.note ? `<div class="nums">📝 ${esc(g.note)}</div>` : ""}
        <div class="nums"><span></span><span>
          ${g.bought_at ? "" : `<button class="linkish g-put">＋ Add</button>
            <button class="linkish g-buy">✅ Bought</button>`}
          <button class="linkish g-del">🗑</button></span></div>
      </div>`;
  }

  async function renderGoals() {
    const { goals } = await api.get("/api/goals");
    const open = goals.filter((g) => !g.bought_at);
    const done = goals.filter((g) => g.bought_at);
    $("goal-list").innerHTML =
      (open.map(goalCard).join("") ||
        `<div class="empty">No goals yet — create one below.</div>`) +
      (done.length ? `<div class="section-title">Bought</div>` + done.map(goalCard).join("") : "");
    stagger($("goal-list"));
  }

  // ---- add flow ---------------------------------------------------------
  function catGrid(el, onPick) {
    el.innerHTML = state.cats.map((c) =>
      `<button class="cat-pick" data-cat="${c}">
         <span class="dot ${catClass(c)}">${icon(c)}</span>${c}</button>`).join("");
    el.addEventListener("click", (e) => {
      const btn = e.target.closest(".cat-pick");
      if (!btn) return;
      el.querySelectorAll(".cat-pick").forEach((b) => b.classList.remove("sel"));
      btn.classList.add("sel");
      onPick(btn.dataset.cat);
    });
  }

  function renderAddAmount() {
    const v = parseFloat(state.addAmount || "0") || 0;
    $("add-amount").textContent = fmt(Math.round(v * 100));
  }

  function resetAdd() {
    state.addAmount = ""; state.addCat = null;
    $("add-note").value = "";
    $("add-cats").querySelectorAll(".cat-pick").forEach((b) => b.classList.remove("sel"));
    state.addType = "expense";
    $("add-type").querySelectorAll("button").forEach((b) =>
      b.classList.toggle("sel", b.dataset.t === "expense"));
    $("add-cats").classList.remove("hidden");
    renderAddAmount();
  }

  async function submitAdd() {
    const amount = parseFloat(state.addAmount || "0");
    if (!amount) return toast("Enter an amount");
    if (state.addType === "expense" && !state.addCat) return toast("Pick a category");
    await api.send("POST", "/api/transactions",
      { amount, note: $("add-note").value.trim(), type: state.addType,
        category: state.addType === "income" ? "Income" : state.addCat });
    closeOverlay("add-overlay");
    toast("Recorded " + fmt(Math.round(amount * 100)));
    resetAdd();
    refresh();
  }

  // ---- edit sheet --------------------------------------------------------
  function openEdit(tx) {
    state.editing = tx;
    $("edit-amt").value = (Math.abs(tx.amount_cents) / 100).toFixed(2);
    $("edit-note").value = tx.note;
    $("edit-cats").querySelectorAll(".cat-pick").forEach((b) =>
      b.classList.toggle("sel", b.dataset.cat === tx.category));
    $("edit-cats").classList.toggle("hidden", tx.type === "income");
    const linksEl = $("edit-links");
    linksEl.classList.add("hidden");
    linksEl.innerHTML = "";
    if (tx.reimbursed_cents > 0) {
      api.get("/api/transactions/" + tx.id + "/links").then(function (body) {
        linksEl.innerHTML = body.links.map(function (l) {
          // "+" for the same reason txRow uses it: this is money that came
          // back in. A minus sign in the green .plus style contradicted itself.
          return `<div class="link-row"><span>${esc(l.note)}</span>` +
                 `<span class="money plus">+${fmt(-l.amount_cents)}</span></div>`;
        }).join("") +
          `<div class="link-row net"><span>net</span>` +
          `<span class="money">${fmt(body.net_cents)}</span></div>`;
        linksEl.classList.remove("hidden");
      });
    }
    renderLinking(tx);
    openOverlay("sheet-overlay");
  }

  // ---- reimbursement linking (the web half of the bot's ↩️ picker) --------
  // Two mutually exclusive states in one container: a payback shows what it is
  // paying back plus an Unlink; a plain incoming transfer shows the candidate
  // spends it could be paying back. Everything else renders nothing.
  async function renderLinking(tx) {
    const el = $("edit-link");
    el.innerHTML = "";
    el.classList.add("hidden");
    if (tx.link_tid) {
      const body = await api.get("/api/transactions/" + tx.link_tid + "/links");
      const o = body.original;
      el.innerHTML = `<div class="section-title">Paying back</div>` +
        `<div class="link-row"><span>${icon(o.category)} ${esc(o.note) || "(no note)"}</span>` +
        `<span class="money">${fmt(o.amount_cents)}</span></div>` +
        `<button id="edit-unlink" class="link-undo">Unlink</button>`;
      $("edit-unlink").addEventListener("click", () => doUnlink(tx));
      el.classList.remove("hidden");
      return;
    }
    if (tx.type !== "income") return;
    const body = await api.get(
      "/api/transactions/" + tx.id + "/reimburse-candidates");
    if (!body.candidates.length) return;  // nothing plausible; stay out of the way
    el.innerHTML = `<div class="section-title">Paying back a spend?</div>` +
      body.candidates.map(function (c) {
        return `<button class="link-pick" data-tid="${c.id}">` +
               `<span class="lp-ico">${icon(c.category)}</span>` +
               `<span class="lp-note">${esc(c.note) || "(no note)"}</span>` +
               `<span class="lp-amt money">${fmt(c.amount_cents)}</span></button>`;
      }).join("");
    el.querySelectorAll(".link-pick").forEach(function (b) {
      b.addEventListener("click", () => doLink(tx, b.dataset.tid));
    });
    el.classList.remove("hidden");
  }

  async function doLink(tx, targetTid) {
    await api.send("POST", "/api/transactions/" + tx.id + "/link",
                   { target_tid: parseInt(targetTid, 10) });
    closeOverlay("sheet-overlay");
    toast("Linked");
    refresh();
  }

  async function doUnlink(tx) {
    await api.send("POST", "/api/transactions/" + tx.id + "/unlink", {});
    closeOverlay("sheet-overlay");
    toast("Unlinked");
    refresh();
  }

  async function saveEdit() {
    const t = state.editing;
    const sel = $("edit-cats").querySelector(".cat-pick.sel");
    await api.send("PATCH", "/api/transactions/" + t.id, {
      amount: parseFloat($("edit-amt").value),
      note: $("edit-note").value.trim(),
      category: sel ? sel.dataset.cat : t.category,
    });
    closeOverlay("sheet-overlay");
    toast("Saved");
    refresh();
  }

  async function deleteEdit() {
    if (!confirm("Delete this transaction?")) return;
    await fetch("/api/transactions/" + state.editing.id, { method: "DELETE" });
    closeOverlay("sheet-overlay");
    toast("Deleted");
    refresh();
  }

  // ---- portfolio ---------------------------------------------------------
  let pfState = null;      // last /api/portfolio payload
  let pfHistory = null;    // last /api/portfolio/history payload
  let pfSgd = false;       // currency toggle
  let pfSpark = null, pfDonut = null;

  function pfRate(ccy) {
    if (ccy === "SGD") return 1;
    const t = (pfState.totals || []).find((x) => x.currency === ccy);
    return t && t.sgd_rate ? t.sgd_rate : null;
  }

  // True only if every held currency has a usable rate. A partial conversion
  // would silently under-report the total, which is worse than no toggle.
  function pfCanConvert() {
    // [].every() is vacuously true — zero totals (empty/cash-only account)
    // must NOT read as "can convert", or the toggle claims a capability the
    // page has no rates to back up.
    if (!pfState || !pfState.totals || !pfState.totals.length) return false;
    return pfState.totals.every((t) => t.currency === "SGD" || t.sgd_rate);
  }

  function pfConvert(cents, ccy) {
    const r = pfRate(ccy);
    return r ? Math.round(cents * r) : null;
  }

  function pfDisplay(cents, ccy) {
    if (!pfSgd) return fmtMoney(cents, ccy);
    const c = pfConvert(cents, ccy);
    return c === null ? fmtMoney(cents, ccy) : fmtMoney(c, "SGD");
  }

  function pfBanner() {
    const el = $("pf-banner");
    const err = pfState && pfState.last_error;
    const age = pfState ? pfState.stale_minutes : null;
    if (err && (!pfState.positions || !pfState.positions.length)) {
      el.className = "pf-banner error";
      el.textContent = "Could not reach Tiger — " + err;
    } else if (err) {
      el.className = "pf-banner";
      el.textContent = "Showing cached data" +
        (age === null ? "" : " from " + pfAge(age)) + " — sync failed: " + err;
    } else {
      el.className = "pf-banner hidden";
      el.textContent = "";
    }
  }

  function pfAge(minutes) {
    if (minutes === null || minutes === undefined) return "never";
    if (minutes < 1) return "just now";
    if (minutes < 60) return minutes + "m ago";
    const h = Math.floor(minutes / 60);
    if (h < 24) return h + "h ago";
    return Math.floor(h / 24) + "d ago";
  }

  function pfRenderHeader() {
    const totals = pfState.totals || [];
    let totalText = "—", plCents = 0, plBase = 0, plCcy = "", sub = "";

    if (pfSgd && pfCanConvert()) {
      let v = 0, p = 0, c = 0;
      totals.forEach((t) => {
        v += pfConvert(t.value_cents, t.currency);
        p += pfConvert(t.pl_cents, t.currency);
        c += pfConvert(t.cost_cents, t.currency);
      });
      totalText = fmtMoney(v, "SGD");
      plCents = p; plBase = c;
      sub = totals.map((t) => t.currency + " @ " +
        (t.currency === "SGD" ? "1.00" : Number(t.sgd_rate).toFixed(4))).join(" · ");
    } else {
      totalText = totals.length
        ? totals.map((t) => fmtMoney(t.value_cents, t.currency)).join("  ·  ")
        : "—";
      // Can't sum P/L across currencies without mixing units. Same fallback
      // pfRenderDonut uses when it can't convert: the dominant currency
      // (totals[0] — the API sorts totals by value_cents DESC).
      if (totals.length) {
        plCents = totals[0].pl_cents;
        plBase = totals[0].cost_cents;
        plCcy = totals[0].currency;
      }
      sub = totals.length > 1
        ? "Mixed currencies — totals not summed · P/L shown for " + plCcy + " only"
        : "";
    }

    $("pf-total").textContent = totalText;
    const pct = plBase ? (plCents / plBase) * 100 : 0;
    const pl = $("pf-pl");
    pl.className = "pf-pl " + (plCents < 0 ? "down" : "up");
    pl.textContent = (plCents < 0 ? "▾ " : "▴ ") +
      (pfSgd && pfCanConvert() ? fmtMoney(Math.abs(plCents), "SGD")
                               : fmtMoney(Math.abs(plCents), plCcy))
      + "  (" + pct.toFixed(2) + "%)";
    $("pf-sub").textContent = sub;
    $("pf-updated").textContent = "Updated " + pfAge(pfState.stale_minutes);
  }

  function pfRenderHoldings() {
    const list = $("pf-holdings");
    const rows = pfState.positions || [];
    list.innerHTML = "";
    rows.forEach(function (r) {
      const el = document.createElement("div");
      el.className = "pf-row card";
      const up = r.unrealized_pl_cents >= 0;
      el.innerHTML =
        '<div><div class="pf-sym"></div><div class="pf-name"></div>' +
        '<div class="pf-qty"></div></div>' +
        '<div class="pf-val"><div class="money"></div>' +
        '<div class="pf-chg ' + (up ? "up" : "down") + '"></div></div>';
      el.querySelector(".pf-sym").textContent = r.symbol;
      el.querySelector(".pf-name").textContent = r.name;
      el.querySelector(".pf-qty").textContent =
        r.quantity + " @ " + fmtMoney(r.avg_cost_cents, r.currency);
      el.querySelector(".money").textContent =
        pfDisplay(r.market_value_cents, r.currency);
      el.querySelector(".pf-chg").textContent =
        (up ? "+" : "") + pfDisplay(r.unrealized_pl_cents, r.currency);
      list.appendChild(el);
    });
    $("pf-empty").classList.toggle("hidden", rows.length > 0);
  }

  function pfRenderDonut() {
    const rows = (pfState.positions || []).slice(0, 12);
    if (pfDonut) { pfDonut.destroy(); pfDonut = null; }
    if (!rows.length) { $("pf-donut-total").textContent = ""; return; }

    // Slice values must share one unit or the proportions are meaningless.
    // Without full FX coverage, fall back to the largest single currency.
    let vals, labels;
    if (pfCanConvert()) {
      vals = rows.map((r) => pfConvert(r.market_value_cents, r.currency) / 100);
      labels = rows.map((r) => r.symbol);
    } else {
      const main = pfState.totals[0].currency;
      const only = rows.filter((r) => r.currency === main);
      vals = only.map((r) => r.market_value_cents / 100);
      labels = only.map((r) => r.symbol);
    }
    $("pf-donut-total").textContent = String(labels.length);

    const primary = cssVar("--primary");
    const colors = labels.map(function (_, i) {
      return "hsl(" + Math.round((i * 360) / labels.length) + " 55% 55%)";
    });
    pfDonut = new Chart($("pf-donut"), {
      type: "doughnut",
      data: { labels: labels, datasets: [{ data: vals, backgroundColor: colors,
              borderWidth: 0, hoverOffset: 0 }] },
      options: { responsive: true, maintainAspectRatio: false, cutout: "68%",
        plugins: { legend: { display: false },
          tooltip: { backgroundColor: cssVar("--surface") || primary,
            callbacks: { label: function (i) { return " " + i.label + ": " +
              i.parsed.toFixed(2); } } } } },
    });
    // vals is in dollars; scale back to cents for fmt().
    describeChart($("pf-donut"), "Portfolio allocation across " + labels.length +
      " holdings: " +
      topParts(labels.map((s, i) => [s, Math.round(vals[i] * 100)]), 4) + ".");
  }

  function pfRenderSpark() {
    if (pfSpark) { pfSpark.destroy(); pfSpark = null; }
    if (!pfHistory || !pfHistory.points || !pfHistory.points.length) return;

    // When converting, sum currencies per day using each row's OWN stored
    // rate, so the line reflects contemporaneous value rather than a
    // retroactive re-valuation. When NOT converting, summing raw cents
    // across currencies would mix unlike units — same fallback pfRenderDonut
    // uses: plot only the dominant currency (totals[0], sorted by
    // value_cents DESC by the API).
    const main = (pfState.totals && pfState.totals[0]) ? pfState.totals[0].currency : null;
    const byDate = {};
    pfHistory.points.forEach(function (p) {
      if (!pfSgd) {
        if (p.currency !== main) return;
        byDate[p.date] = (byDate[p.date] || 0) + p.value_cents;
        return;
      }
      const rate = p.currency === "SGD" ? 1 : p.sgd_rate;
      if (!rate) return;
      byDate[p.date] = (byDate[p.date] || 0) + p.value_cents * rate;
    });
    const dates = Object.keys(byDate).sort();
    if (!dates.length) return;

    pfSpark = new Chart($("pf-spark"), {
      type: "line",
      data: { labels: dates, datasets: [{ data: dates.map((d) => byDate[d] / 100),
              borderColor: cssVar("--primary"), borderWidth: 2,
              pointRadius: 0, tension: 0.3, fill: false }] },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false },
          tooltip: { backgroundColor: cssVar("--surface"),
            callbacks: { label: function (i) { return " " + i.parsed.y.toFixed(2); } } } },
        scales: { x: { display: false }, y: { display: false } } },
    });
    describeChart($("pf-spark"), "Portfolio value over " + dates.length +
      " days, from " + fmt(Math.round(byDate[dates[0]])) +
      " to " + fmt(Math.round(byDate[dates[dates.length - 1]])) + ".");
  }

  function pfRender() {
    // Guards the toggle-click entry point: a click can land here before the
    // first successful load (still loading, or the boot fetch rejected and
    // left pfState null with the banner already showing the failure).
    if (!pfState) return;
    pfBanner();

    // Toggle usability is independent of whether there's anything to render
    // — an empty/cash-only account has zero totals, so it must be disabled
    // on this path too, not just after a successful non-empty render below.
    const sgdBtn = $("pf-ccy-toggle").querySelector('[data-ccy="sgd"]');
    sgdBtn.disabled = !pfCanConvert();
    sgdBtn.title = pfCanConvert() ? "" : "FX rate unavailable";

    if (!pfState.positions || !pfState.positions.length) {
      $("pf-total").textContent = "—";
      $("pf-pl").textContent = "";
      $("pf-sub").textContent = "";
      $("pf-updated").textContent = "Updated " + pfAge(pfState.stale_minutes);
      // A poll failure already has its own message in the banner above;
      // showing "waiting for first sync" underneath a failure that already
      // happened would contradict it. Only show the empty-state card when
      // there's no error to explain the emptiness.
      $("pf-empty").classList.toggle("hidden", !!pfState.last_error);
      return;
    }
    pfRenderHeader();
    pfRenderHoldings();
    pfRenderDonut();
    pfRenderSpark();
  }

  async function loadPortfolio() {
    try {
      const [state, hist] = await Promise.all([
        fetch("/api/portfolio").then((r) => r.json()),
        fetch("/api/portfolio/history?days=90").then((r) => r.json()),
      ]);
      if (state.configured === false) {
        // Set before show() below, so resolveHash() already rejects #portfolio
        // and the redirect replaces the hash instead of pushing an entry that
        // Back would bounce into another round of these fetches.
        pfUnavailable = true;
        document.querySelectorAll('#nav button[data-view="portfolio"], ' +
          '#desk-tabs button[data-view="portfolio"]')
          .forEach(function (b) { b.classList.add("hidden"); });
        $("view-portfolio").classList.add("hidden");
        // The hash may have opened a tab that does not exist on this install.
        if (currentView === "portfolio") show("home");
        return;
      }
      pfState = state;
      pfHistory = hist;
      pfRender();
    } catch (err) {
      $("pf-banner").className = "pf-banner error";
      $("pf-banner").textContent = "Could not load portfolio: " + err;
    }
  }

  // ---- payslips ----------------------------------------------------------
  // Read-only: the bot's email poll does the decrypting and archiving. This
  // only renders what it stored, and never writes a transaction — salary
  // income comes from the bank credit alert, so a row here would double-count.
  let psList = null;        // [{period, net_cents, ...}] newest first
  let psIndex = 0;          // which one is on screen
  let psView = "items";     // items | pdf | text
  const psCache = {};       // period -> full breakdown

  async function loadPayslips() {
    try {
      const res = await api.get("/api/payslips");
      if (res.configured === false) {
        // Set BEFORE the show() below, exactly as pfUnavailable is: otherwise
        // resolveHash() still accepts #pay and the correction pushes a history
        // entry that Back bounces straight back into.
        psUnavailable = true;
        document.querySelectorAll('#nav button[data-view="pay"], ' +
          '#desk-tabs button[data-view="pay"]')
          .forEach(function (b) { b.classList.add("hidden"); });
        $("view-pay").classList.add("hidden");
        if (currentView === "pay") show("home");
        return;
      }
      psList = res.payslips || [];
      psIndex = 0;
      if (!psList.length) {
        $("ps-empty").classList.remove("hidden");
        $("ps-period-label").textContent = "Payslip";
        return;
      }
      $("ps-empty").classList.add("hidden");
      await psRender();
    } catch (err) {
      $("ps-period-label").textContent = "Could not load payslips: " + err;
    }
  }

  // ---- import (the web equivalents of the bot's /payslip) ----
  // Both paths POST and then reload the list, so a freshly archived month is
  // on screen without a manual refresh.

  function psBusy(on, msg) {
    ["ps-scan", "ps-pick"].forEach(function (id) { $(id).disabled = on; });
    if (msg) $("ps-import-log").textContent = msg;
  }

  function psImportSummary(body) {
    const rows = (body && body.imported) || [];
    if (!rows.length) {
      return body && body.scanned
        ? "Nothing to import — no payslip PDF in the last " + body.scanned +
          " payroll email(s). Try again with a wider scan, or upload the file."
        : "Nothing to import.";
    }
    return rows.map(function (r) {
      const ocr = r.text_source === "ocr" ? " · read by OCR" : "";
      const head = (r.parsed ? "✅ " : "⚠️ ") + r.period + " — " +
        r.items + " line items" + ocr;
      // Warnings come from the server so the bot and the dashboard can never
      // disagree about which figures are suspect.
      return [head].concat((r.warnings || []).map(function (w) {
        return "   ⚠️ " + w;
      })).join("\n");
    }).join("\n");
  }

  async function psAfterImport(resp) {
    let body = null;
    try { body = await resp.json(); } catch (e) { body = null; }
    if (!resp.ok) {
      // FastAPI puts the reason in `detail`; a proxy error has no JSON at all.
      psBusy(false, "⚠️ " + ((body && body.detail) || ("Failed (" + resp.status + ")")));
      return;
    }
    psBusy(false, psImportSummary(body));
    const landed = ((body && body.imported) || [])[0];
    // Drop the cache for what just changed: re-importing a month rewrites its
    // items, so a cached copy would show the previous read.
    ((body && body.imported) || []).forEach(function (r) {
      delete psCache[r.period];
    });
    psList = null;
    await loadPayslips();
    if (landed && psList) {
      const i = psList.findIndex(function (p) { return p.period === landed.period; });
      if (i >= 0) { psIndex = i; await psRender(); }
    }
  }

  async function psScanEmail() {
    psBusy(true, "⟳ Scanning payroll email… reading a payslip needs OCR, so " +
                 "this takes a moment.");
    try {
      await psAfterImport(await fetch("/api/payslips/import?limit=3",
                                      { method: "POST" }));
    } catch (err) {
      psBusy(false, "⚠️ " + err);
    }
  }

  async function psUpload(file) {
    if (!file) return;
    psBusy(true, "⟳ Reading " + file.name + "…");
    try {
      // Raw body, not multipart: one file, no other fields.
      await psAfterImport(await fetch("/api/payslips/upload", {
        method: "POST",
        headers: { "Content-Type": "application/pdf" },
        body: file,
      }));
    } catch (err) {
      psBusy(false, "⚠️ " + err);
    }
  }

  async function psRender() {
    const meta = psList[psIndex];
    if (!meta) return;
    if (!psCache[meta.period]) {
      psCache[meta.period] = await api.get("/api/payslips/" +
        encodeURIComponent(meta.period));
    }
    const slip = psCache[meta.period];
    $("ps-period-label").textContent = monthLabel(slip.period);
    $("ps-net").textContent = slip.net_cents === null
      ? "—" : fmtMoney(slip.net_cents, "SGD");
    $("ps-sub").textContent = slip.gross_cents === null ? "net pay"
      : "net pay · gross " + fmtMoney(slip.gross_cents, "SGD");
    $("ps-unparsed").classList.toggle("hidden", slip.parsed);
    const pdf = $("ps-pdf");
    pdf.href = "/api/payslips/" + encodeURIComponent(slip.period) + "/pdf";
    pdf.classList.remove("hidden");
    // Older payslip = further along the newest-first list.
    $("ps-prev").disabled = psIndex >= psList.length - 1;
    $("ps-next").disabled = psIndex <= 0;
    psRenderCheck(slip);
    psRenderDelta(slip);
    psRenderItems(slip);
    psApplyView();
  }

  // Whether the payslip's own totals agree with each other. This is the check
  // that catches a misread digit, so it is stated plainly rather than left for
  // the user to spot by eye — especially when the figures came from OCR.
  function psRenderCheck(slip) {
    const el = $("ps-check");
    const ocr = slip.text_source === "ocr" ? "Read by OCR · " : "";
    if (slip.reconciles === true) {
      el.textContent = ocr + "✓ gross − deductions = net";
      el.className = "sub ps-ok";
    } else if (slip.reconciles === false) {
      el.textContent = ocr + "⚠️ figures don't add up — check the original";
      el.className = "sub ps-bad";
    } else {
      el.textContent = ocr ? ocr.replace(/ · $/, "") : "";
      el.className = "sub";
    }
  }

  // Extracted figures / the original PDF / the raw text they came from. The
  // PDF is the authority: if extraction got something wrong, it is the only
  // way to tell, so it is a first-class view rather than an external link.
  function psApplyView() {
    const slip = psCache[(psList[psIndex] || {}).period];
    $("ps-items").classList.toggle("hidden", psView !== "items");
    $("ps-frame").classList.toggle("hidden", psView !== "pdf");
    $("ps-raw").classList.toggle("hidden", psView !== "text");
    $("ps-view-toggle").querySelectorAll("button").forEach(function (b) {
      b.classList.toggle("sel", b.dataset.psview === psView);
    });
    if (!slip) return;
    if (psView === "pdf") {
      // Set lazily: pointing the iframe at the PDF on every render would
      // re-download it each time the month changed.
      const src = "/api/payslips/" + encodeURIComponent(slip.period) + "/pdf";
      if ($("ps-frame").getAttribute("src") !== src) {
        $("ps-frame").setAttribute("src", src);
      }
    }
    if (psView === "text") $("ps-raw").textContent = slip.raw_text || "";
  }

  function monthLabel(period) {
    const [y, m] = period.split("-");
    return new Date(Number(y), Number(m) - 1, 1)
      .toLocaleDateString("en-SG", { month: "long", year: "numeric" });
  }

  // Month-over-month on net pay, against the NEXT entry in the list (the
  // previous payslip). Silent when there is nothing to compare against or
  // either month went unparsed.
  function psRenderDelta(slip) {
    const el = $("ps-delta");
    const prevMeta = psList[psIndex + 1];
    const prev = prevMeta && psCache[prevMeta.period];
    if (!prevMeta || slip.net_cents === null) { el.textContent = ""; return; }
    if (!prev) {
      // Fetch the neighbour once, then re-render; avoids blocking first paint.
      api.get("/api/payslips/" + encodeURIComponent(prevMeta.period))
        .then((p) => { psCache[prevMeta.period] = p; psRenderDelta(slip); })
        .catch(() => { el.textContent = ""; });
      return;
    }
    if (prev.net_cents === null) { el.textContent = ""; return; }
    const diff = slip.net_cents - prev.net_cents;
    el.className = "pf-pl " + (diff > 0 ? "up" : diff < 0 ? "down" : "");
    el.textContent = (diff > 0 ? "▲ " : diff < 0 ? "▼ " : "= ") +
      fmtMoney(Math.abs(diff), "SGD") + " vs " + monthLabel(prev.period);
  }

  function psRenderItems(slip) {
    const box = $("ps-items");
    box.textContent = "";
    let section = null;
    slip.items.forEach((it) => {
      if (it.section !== section) {
        section = it.section;
        if (section) {
          const h = document.createElement("div");
          h.className = "day";
          h.textContent = section;
          box.appendChild(h);
        }
      }
      const row = document.createElement("div");
      row.className = "tx";
      const mid = document.createElement("div");
      mid.className = "mid";
      const t = document.createElement("div");
      t.className = "t";
      // textContent, not innerHTML: these labels are parsed out of a PDF, so
      // an "&" or "<" in an employer's wording must not become markup.
      t.textContent = it.label;
      mid.appendChild(t);
      const amt = document.createElement("div");
      amt.className = "amt money";
      amt.textContent = fmtMoney(it.amount_cents, "SGD");
      row.appendChild(mid);
      row.appendChild(amt);
      box.appendChild(row);
    });
    if (!slip.items.length) {
      const p = document.createElement("div");
      p.className = "sub";
      p.textContent = "No line items were read from this payslip.";
      box.appendChild(p);
    }
  }

  async function pfRefresh() {
    const btn = $("pf-refresh");
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const resp = await fetch("/api/portfolio/refresh", { method: "POST" });
      // A proxy 502 or a crash answers with HTML, not JSON. Parsing before the
      // status check would throw into the catch below, losing the state reload
      // that puts the real reason in the banner.
      let body = null;
      try { body = await resp.json(); } catch (e) { body = null; }
      if (!resp.ok || !body) {
        toast("Sync failed");
        pfState = await fetch("/api/portfolio").then((r) => r.json());
      } else {
        pfState = body;
        pfHistory = await fetch("/api/portfolio/history?days=90")
          .then((r) => r.json());
        toast("Synced");
      }
      pfRender();
    } catch (err) {
      toast("Sync failed");
    }
    btn.disabled = false;
    btn.textContent = "↻";
  }

  // ---- shell -------------------------------------------------------------
  const NAV_ORDER = ["home", "txns", "budgets", "insights", "portfolio", "pay", "cards"];
  let currentView = "home";

  // Transactions is merged into Home's third column on desktop, so a #txns
  // link there resolves to Home — the content is present, just not its own tab.
  const DESK = window.matchMedia("(min-width:1100px)");
  // Set once /api/portfolio reports the install has no Tiger credentials.
  let pfUnavailable = false;
  // Same, for an install with no PAYSLIP_PDF_PASSWORD configured.
  let psUnavailable = false;
  function resolveHash() {
    let v = location.hash.slice(1);
    if (DESK.matches && (v === "txns" || v === "budgets")) v = "home";
    if (v === "portfolio" && pfUnavailable) v = "home";
    if (v === "pay" && psUnavailable) v = "home";
    return NAV_ORDER.indexOf(v) < 0 ? "home" : v;
  }

  function show(view) {
    // Every entry point lands here — nav buttons, the hash, drillDown(), the
    // "/" and "2" shortcuts, a breakpoint change — so desktop's missing txns
    // tab is normalized once, rather than each caller having to remember.
    if (DESK.matches && (view === "txns" || view === "budgets")) view = "home";
    const dir = NAV_ORDER.indexOf(view) >= NAV_ORDER.indexOf(currentView)
      ? "view-in-right" : "view-in-left";
    document.querySelectorAll("#views .view").forEach((v) => {
      const on = v.id === "view-" + view;
      v.classList.toggle("active", on);
      v.classList.remove("view-in-right", "view-in-left");
      // The view-in-* keyframes are mobile-only, so on desktop the animationend
      // handler below would never fire and never remove itself.
      if (on && view !== currentView && !REDUCED && !DESK.matches) {
        v.classList.add(dir);
        v.addEventListener("animationend", function h() {
          v.classList.remove(dir); v.removeEventListener("animationend", h);
        });
      }
    });
    document.querySelectorAll("#nav button, #desk-tabs button").forEach((b) => {
      const on = b.dataset.view === view;
      b.classList.toggle("active", on);
      // aria-current is the announced counterpart of .active. A class alone
      // tells a screen reader nothing about which tab is open.
      if (on) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    // Column counts differ per tab, so the grid template keys off this class.
    // Swap rather than assign .className, which would wipe any other class.
    const vw = $("views");
    Array.prototype.slice.call(vw.classList).forEach(function (c) {
      if (c.indexOf("tab-") === 0) vw.classList.remove(c);
    });
    vw.classList.add("tab-" + view);
    if (location.hash.slice(1) !== view) {
      // Assigning location.hash PUSHES a history entry. That is right for a
      // real tab switch, but wrong when we are correcting a hash resolveHash()
      // rejected (empty on first load, garbage, #txns on desktop): Back would
      // land on the rejected hash, hashchange would re-correct it, and the user
      // could never leave the app. replaceState also fires no hashchange, so
      // the correction does not re-enter show().
      if (resolveHash() === view) history.replaceState(null, "", "#" + view);
      else location.hash = view;
    }
    if (view === "portfolio" && !pfState) loadPortfolio();
    if (view === "pay" && !psList) loadPayslips();
    if (view === "cards" && !cardBuckets) renderCards();
    currentView = view;
  }

  async function logout() {
    // /api/auth/logout is NOT in the gate's allowlist, so a session that has
    // already lapsed answers 401 here. Deliberate: there is no session left to
    // delete. Note the wrapper above then fires goToLogin() DURING this await,
    // so on a lapsed session the sweep below races the navigation teardown and
    // may not finish. Accepted, not overlooked: cache v2 can only ever hold
    // public static assets, because every route serving private data lives
    // under /api/ and the service worker skips those outright. Losing the sweep
    // therefore costs nothing. (What removes the OLD expenses-v1 cache, which
    // did hold /api/* JSON, is the activate sweep in sw.js -- not this.)
    try { await fetch("/api/auth/logout", { method: "POST" }); } catch (e) {}
    // Clear the PWA cache before leaving, or the app shell and any cached
    // pages stay readable on this device after signing out.
    if (window.caches) {
      try {
        const keys = await caches.keys();
        await Promise.all(keys.map(function (k) { return caches.delete(k); }));
      } catch (e) {}
    }
    goToLogin();
  }

  // Overlays animate in via CSS; closing plays the exit animation, then hides.
  // Where focus was before a modal opened, so it can go back there on close.
  let overlayReturn = null;

  function openOverlay(id) {
    const el = $(id);
    // Only on a FRESH open. Opening a second overlay over the first (press ?
    // twice, or n with the sheet up) would otherwise record something inside
    // an overlay, and closing would "restore" focus to a hidden element.
    if (overlayReturn === null) overlayReturn = document.activeElement;
    // inert removes the background from the tab order AND from the
    // accessibility tree in one attribute -- it is what a hand-rolled focus
    // trap laboriously emulates. Iterating #app's children rather than naming
    // them means a future top-level element is sealed off without an edit.
    for (const c of $("app").children) if (c !== el) c.inert = true;
    el.classList.remove("hidden", "closing");
    const first = el.querySelector("input:not([type=hidden]),select,textarea,button");
    if (first) first.focus();
  }

  function closeOverlay(id) {
    const el = $(id);
    if (el.classList.contains("hidden")) return;
    // Un-inert BEFORE restoring focus: focus() on a still-inert element is
    // silently dropped to <body>. This cannot wait for the close animation.
    for (const c of $("app").children) c.inert = false;
    // The row that opened the sheet may have been re-rendered away by a
    // refresh, in which case there is nothing to go back to.
    if (overlayReturn && document.contains(overlayReturn)) overlayReturn.focus();
    overlayReturn = null;
    if (REDUCED) { el.classList.add("hidden"); return; }
    el.classList.add("closing");
    setTimeout(() => { el.classList.remove("closing"); el.classList.add("hidden"); }, 160);
  }

  let toastTimer = null;
  function toast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 1800);
  }

  function refresh() {
    // Card names are in hand BEFORE any row renders: txRow() resolves its chip
    // from state.cardNames with no fetch of its own, so a row drawn first would
    // label an assigned card "Card #3" and then never correct itself. The
    // catch keeps one failed card fetch from taking the whole dashboard down —
    // the chips degrade, nothing else does.
    loadCards().catch(function () {}).then(function () {
      renderHome(); loadTxs(); renderBudgets(); loadMonthChips();
      renderHeatmap(); renderInsights(); renderGoals(); renderYear();
      // Ungated, unlike the breakdown below: it renders from state.cards,
      // which was just loaded, so it costs no request — and a card renamed
      // from the list must not still read its old name in a row chip.
      renderCardList();
      // Options track renames and deletions, and it re-asserts the current
      // selection after rebuilding the list.
      renderCardFilter();
      // Only for a tab that has been opened once; show() does the first load.
      if (cardBuckets) renderCards();
    });
  }

  async function init() {
    renderGreeting();
    applyTheme(theme);
    document.querySelectorAll(".theme-btn").forEach((b) =>
      b.addEventListener("click", () => {
        theme = THEMES[(THEMES.indexOf(theme) + 1) % 3];
        localStorage.theme = theme;
        applyTheme(theme);
        refresh();
      }));
    // Wired here, alongside the theme buttons and before the first await, so a
    // slow (or 401-ing) boot still leaves a working way out.
    document.querySelectorAll(".logout-btn").forEach((b) =>
      b.addEventListener("click", logout));
    state.cats = (await api.get("/api/categories")).categories;
    catGrid($("add-cats"), (c) => { state.addCat = c; });
    catGrid($("edit-cats"), () => {});
    $("budget-cat").innerHTML =
      state.cats.map((c) => `<option>${c}</option>`).join("");
    state.cats.forEach((c) => {
      const o = document.createElement("option");
      o.value = c; o.textContent = c; $("goal-cat").appendChild(o);
    });

    $("nav").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-view]");
      if (b) show(b.dataset.view);
    });
    $("desk-tabs").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-view]");
      if (b) show(b.dataset.view);
    });
    // "See all": clear any drill-down filter, switch view, and scroll to it.
    // show() below now hides every other section on both mobile and desktop
    // (tabs, not an always-visible grid), so the scroll isn't for finding a
    // hidden section — it resets the viewport to the top of the newly-shown
    // one in case the previous tab had been scrolled down.
    // "See all" means all: it clears the card drill-down alongside the category
    // one. Leaving a card filter behind would land the user on a list that is
    // still filtered, right after asking to see everything — and the only clue
    // would be a chip they did not just click.
    document.querySelectorAll("[data-goto]").forEach((el) =>
      el.addEventListener("click", () => {
        state.filterCat = null; setDonutPop(null);
        // renderTxList() here is NOT redundant with applyCardFilter() below:
        // applyCardFilter() has a change-guard and no-ops (skipping its own
        // renderTxList) whenever no card filter was ever set, which is the
        // common case. This handler repaints for the state it owns
        // (filterCat) instead of depending on that function's internals.
        renderTxList();
        state.cardFilter = null; applyCardFilter();
        show(el.dataset.goto);
        $("view-" + el.dataset.goto).scrollIntoView(
          { behavior: REDUCED ? "auto" : "smooth", block: "start" });
      }));

    $("fab").addEventListener("click", () => openOverlay("add-overlay"));
    $("keys-close").addEventListener("click", () => closeOverlay("keys-overlay"));
    $("add-close").addEventListener("click", () => closeOverlay("add-overlay"));
    $("add-overlay").addEventListener("click", (e) => {
      if (e.target.id === "add-overlay") closeOverlay("add-overlay");
    });
    $("pad").addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      const k = b.dataset.k;
      if (k === "ok") return submitAdd();
      if (k === "back") state.addAmount = state.addAmount.slice(0, -1);
      else if (k === "clear") state.addAmount = "";
      else if (k === "dot") {
        if (!state.addAmount.includes(".")) state.addAmount = (state.addAmount || "0") + ".";
      } else if (state.addAmount.includes(".") &&
                 state.addAmount.split(".")[1].length >= 2) { /* max 2dp */ }
      else state.addAmount += k;
      renderAddAmount();
    });

    $("add-type").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-t]"); if (!b) return;
      state.addType = b.dataset.t;
      $("add-type").querySelectorAll("button").forEach((x) =>
        x.classList.toggle("sel", x === b));
      $("add-cats").classList.toggle("hidden", state.addType === "income");
    });

    $("search").addEventListener("input", (e) => {
      state.search = e.target.value; renderTxList();
    });
    $("month-chips").addEventListener("click", (e) => {
      const b = e.target.closest(".chip"); if (!b) return;
      state.month = b.dataset.m;
      loadMonthChips(); loadTxs();
    });
    $("src-filter").addEventListener("click", (e) => {
      if (e.target.closest("#csv-btn")) {
        location.href = "/api/export.csv?month=" + state.month;
        return;
      }
      const b = e.target.closest(".chip[data-s]"); if (!b) return;
      state.filterSrc = b.dataset.s;
      $("src-filter").querySelectorAll(".chip[data-s]").forEach((x) =>
        x.classList.toggle("sel", x === b));
      renderTxList();
    });
    $("min-amt").addEventListener("input", (e) => {
      state.minCents = Math.round((parseFloat(e.target.value) || 0) * 100);
      renderTxList();
    });
    // The card chip sits INSIDE .tx, whose handler opens the edit sheet — so
    // both lists that render txRow() have to intercept it first, or the picker
    // is unreachable by pointer.
    $("tx-list").addEventListener("click", (e) => {
      const pick = e.target.closest(".card-chip");
      if (pick) return openCardPicker(Number(pick.dataset.tid));
      if (e.target.closest("#tx-more")) {
        state.txShown += 10;
        renderTxList();
        return;
      }
      const row = e.target.closest(".tx"); if (!row) return;
      const tx = state.txs.find((t) => t.id === Number(row.dataset.id));
      if (tx) openEdit(tx);
    });
    $("recent-list").addEventListener("click", (e) => {
      const pick = e.target.closest(".card-chip");
      if (pick) return openCardPicker(Number(pick.dataset.tid));
      const row = e.target.closest(".tx"); if (!row) return;
      api.get("/api/transactions?limit=5").then((rows) => {
        const tx = rows.find((t) => t.id === Number(row.dataset.id));
        if (tx) openEdit(tx);
      });
    });
    $("card-pick-list").addEventListener("click", (e) => {
      const b = e.target.closest(".link-pick[data-cid]"); if (!b) return;
      setCard(Number(b.dataset.cid));
    });
    $("card-pick-cancel").addEventListener("click", () => closeOverlay("card-overlay"));
    $("card-overlay").addEventListener("click", (e) => {
      if (e.target.id === "card-overlay") closeOverlay("card-overlay");
    });
    $("card-add").addEventListener("click", function () { openCardForm(null); });
    $("card-cancel").addEventListener("click", closeCardForm);
    $("card-form").addEventListener("submit", saveCard);
    $("card-list").addEventListener("click", function (e) {
      const row = e.target.closest(".crow[data-cid]"); if (!row) return;
      const c = state.cards.find(function (x) {
        return x.id === Number(row.dataset.cid);
      });
      if (!c) return;
      // 🗑 first: it sits inside the row, whose own click opens the editor.
      if (e.target.closest(".c-del")) return deleteCard(c);
      openCardForm(c);
    });
    $("card-pills").addEventListener("click", (e) => {
      const b = e.target.closest(".chip[data-cid]"); if (!b) return;
      cardDrillDown(b.dataset.cid === "all" ? null : Number(b.dataset.cid));
    });
    $("card-filter").addEventListener("change", (e) => {
      const v = e.target.value;
      // "" is no filter, "0" is the Unassigned bucket. Number("") is 0, so
      // converting first would silently turn "All cards" into "Unassigned" —
      // branch on the STRING.
      state.cardFilter = v === "" ? null : Number(v);
      applyCardFilter();
    });

    // month navigation on Home — drives the tx list, budgets and heatmap too
    function setMonth(ym) {
      state.homeMonth = ym;
      state.month = ym;
      renderHome(); loadMonthChips(); loadTxs(); renderBudgets(); renderHeatmap();
      // The Cards tab reads state.homeMonth too, so it follows the same arrows
      // rather than sitting on a month the rest of the app has left behind.
      if (cardBuckets) renderCards();
    }
    $("hm-prev").addEventListener("click", () =>
      setMonth(prevYM(state.homeMonth)));
    $("hm-next").addEventListener("click", () => {
      if (state.homeMonth === currentYM()) return;
      setMonth(nextYM(state.homeMonth));
    });

    // heatmap day tooltip (hover on desktop, tap on phone)
    $("heatmap").addEventListener("mouseover", (e) =>
      heatTip(e.target.closest("[data-day]")));
    $("heatmap").addEventListener("mouseleave", () => heatTip(null));
    $("heatmap").addEventListener("click", (e) =>
      heatTip(e.target.closest("[data-day]")));
    // focusin/focusout, not focus/blur: only the former bubble, so delegation
    // from the container works the same way mouseover does.
    $("heatmap").addEventListener("focusin", (e) =>
      heatTip(e.target.closest("[data-day]")));
    $("heatmap").addEventListener("focusout", () => heatTip(null));
    addEventListener("scroll", () => heatTip(null), { passive: true });

    $("merchant-list").addEventListener("click", (e) => {
      if (!e.target.closest("#merch-more")) return;
      state.merchShown += 5;
      renderMerchants(state.merchants);
    });

    // category drill-down sources
    $("home-budgets").addEventListener("click", (e) => {
      const r = e.target.closest(".ring-item");
      if (r) drillDown(r.dataset.cat);
    });
    $("budget-list").addEventListener("click", async (e) => {
      const c = e.target.closest(".bcard[data-cat]"); if (!c) return;
      const cat = c.dataset.cat;
      if (e.target.closest(".b-edit")) {
        $("budget-cat").value = cat;
        $("budget-amt").focus();
        return;
      }
      if (e.target.closest(".b-del")) {
        if (!confirm(`Delete the ${cat} budget?`)) return;
        const r = await fetch("/api/budgets/" + encodeURIComponent(cat), { method: "DELETE" });
        toast(r.ok ? "Budget deleted" : "Delete failed");
        renderBudgets(); renderHome();
        return;
      }
      drillDown(cat);
    });
    $("pending-list").addEventListener("click", async (e) => {
      const row = e.target.closest(".tx"); if (!row) return;
      const pid = row.dataset.pid;
      if (e.target.closest(".p-acc")) {
        try { await api.send("POST", `/api/pending/${pid}/accept`, {}); toast("Imported"); }
        catch (err) { toast("Already handled"); }
        refresh();
      } else if (e.target.closest(".p-ign")) {
        try { await api.send("POST", `/api/pending/${pid}/ignore`, {}); toast("Ignored"); }
        catch (err) { toast("Already handled"); }
        refresh();
      }
    });
    $("compare-list").addEventListener("click", (e) => {
      const p = e.target.closest(".pill[data-cat]");
      if (p) drillDown(p.dataset.cat);
    });
    $("filter-chip").addEventListener("click", (e) => {
      // Branch on WHICH chip was hit: this used to clear filterCat on any click
      // in the container, which would drop the category drill-down whenever the
      // user dismissed the card one.
      const b = e.target.closest("[data-clear]"); if (!b) return;
      if (b.dataset.clear === "card") {
        state.cardFilter = null;
        applyCardFilter();
        return;
      }
      state.filterCat = null;
      setDonutPop(null);
      renderFilterChip();
      renderTxList();
    });

    // keyboard shortcuts (desktop) — inert while typing
    document.addEventListener("keydown", (e) => {
      if (e.target.matches("input,select,textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "Escape") {
        closeOverlay("add-overlay");
        closeOverlay("sheet-overlay");
        closeOverlay("keys-overlay");
        closeOverlay("card-overlay");
        return;
      }
      if (e.key === "n" || e.key === "N") {
        openOverlay("add-overlay");
        e.preventDefault();
      } else if (e.key === "/") {
        show("txns");
        $("search").focus();
        e.preventDefault();
      } else if (e.key === "?") {
        openOverlay("keys-overlay");
        e.preventDefault();
      } else if ("1234567".includes(e.key)) {
        // Read from whichever strip is rendered, never a literal list: show()
        // folds txns into home on desktop, so a hardcoded mobile order put 1
        // and 2 both on Home and left Portfolio unreachable. :not(.hidden)
        // drops Portfolio when this install has no Tiger credentials.
        const strip = DESK.matches ? "#desk-tabs" : "#nav";
        const views = [...document.querySelectorAll(strip + " button:not(.hidden)")]
          .map((b) => b.dataset.view);
        const v = views[Number(e.key) - 1];
        if (v) show(v);
      }
    });

    // Enter/Space on a role=button element runs the click delegation that
    // already exists for the pointer, so the two paths cannot diverge -- there
    // is only one handler, and it is the mouse's. Native controls bail out
    // first: they activate on their own, and .bcard/.tx wrap real buttons, so
    // closest() would otherwise climb past the button the user is on and fire
    // the whole card.
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      if (e.target.closest("button,a[href],input,select,textarea")) return;
      const el = e.target.closest('[role="button"]');
      if (!el) return;
      e.preventDefault();  // Space scrolls the page otherwise
      el.click();
    });

    // swipe row actions (phone): left = delete, right = edit
    let sw = null;
    $("tx-list").addEventListener("touchstart", (e) => {
      const row = e.target.closest(".tx"); if (!row) return;
      sw = { row, x: e.touches[0].clientX, y: e.touches[0].clientY, dx: 0 };
    }, { passive: true });
    $("tx-list").addEventListener("touchmove", (e) => {
      if (!sw) return;
      const dx = e.touches[0].clientX - sw.x;
      const dy = e.touches[0].clientY - sw.y;
      if (Math.abs(dy) > 30) { sw.row.style.transform = ""; sw = null; return; } // scrolling
      sw.dx = dx;
      sw.row.style.transform = `translateX(${dx}px)`;
    }, { passive: true });
    $("tx-list").addEventListener("touchend", async () => {
      if (!sw) return;
      const { row, dx } = sw;
      sw = null;
      row.style.transform = "";
      const tx = state.txs.find((t) => t.id === Number(row.dataset.id));
      if (!tx) return;
      if (dx <= -80) {
        if (!confirm("Delete this transaction?")) return;
        await fetch("/api/transactions/" + tx.id, { method: "DELETE" });
        toast("Deleted");
        refresh();
      } else if (dx >= 80) {
        openEdit(tx);
      }
    });

    $("edit-save").addEventListener("click", saveEdit);
    $("edit-delete").addEventListener("click", deleteEdit);
    $("edit-cancel").addEventListener("click", () => closeOverlay("sheet-overlay"));
    $("sheet-overlay").addEventListener("click", (e) => {
      if (e.target.id === "sheet-overlay") closeOverlay("sheet-overlay");
    });

    $("budget-save").addEventListener("click", async () => {
      const amt = parseFloat($("budget-amt").value);
      if (!amt) return toast("Enter a monthly limit");
      await api.send("PUT", "/api/budgets/" + encodeURIComponent($("budget-cat").value),
                     { amount: amt });
      $("budget-amt").value = "";
      toast("Budget saved");
      renderBudgets(); renderHome();
    });

    $("goal-save").addEventListener("click", async () => {
      const name = $("goal-name").value.trim();
      const target = parseFloat($("goal-target").value);
      if (!name || !(target > 0)) return toast("Name + target needed");
      const monthly = parseFloat($("goal-monthly").value);
      await api.send("POST", "/api/goals", {
        name, target,
        target_date: $("goal-date").value || null,
        monthly: monthly > 0 ? monthly : null,
        category: $("goal-cat").value || null,
        priority: parseInt($("goal-pri").value, 10),
        note: $("goal-note").value.trim(),
      });
      ["goal-name", "goal-target", "goal-date", "goal-monthly", "goal-note"]
        .forEach((id) => { $(id).value = ""; });
      $("goal-cat").value = "";
      $("goal-pri").value = "2";
      toast("Goal created");
      renderGoals();
    });
    $("goal-list").addEventListener("click", async (e) => {
      const card = e.target.closest(".bcard"); if (!card) return;
      const gid = Number(card.dataset.gid);
      if (e.target.closest(".g-del")) {
        if (!confirm("Delete this goal (and its history)?")) return;
        await fetch("/api/goals/" + gid, { method: "DELETE" });
        toast("Goal deleted");
        return renderGoals();
      }
      if (e.target.closest(".g-put")) {
        const amt = parseFloat(prompt("Add how much (S$)?") || "0");
        if (!amt) return;
        await api.send("POST", `/api/goals/${gid}/contribute`, { amount: amt });
        toast("Added " + fmt(Math.round(amt * 100)));
        return renderGoals();
      }
      if (e.target.closest(".g-buy")) {
        const raw = prompt("Price paid (blank = the target amount)?");
        if (raw === null) return;
        const price = raw.trim() === "" ? null : parseFloat(raw);
        if (price !== null && !(price >= 0)) return;
        await api.send("POST", `/api/goals/${gid}/bought`, { price });
        toast("Marked bought");
        renderGoals();
        return;
      }
    });

    $("yr-prev").addEventListener("click", () => {
      state.year = String(Number(state.year) - 1); renderYear();
    });
    $("yr-next").addEventListener("click", () => {
      if (state.year === String(new Date().getFullYear())) return;
      state.year = String(Number(state.year) + 1); renderYear();
    });
    $("yr-cats").addEventListener("click", (e) => {
      const pill = e.target.closest(".pill[data-cat]");
      if (pill) setYearCat(pill.dataset.cat);
    });
    $("rhythm-cats").addEventListener("click", (e) => {
      const pill = e.target.closest(".pill[data-cat]");
      if (pill) setRhythmCat(pill.dataset.cat);
    });

    $("ps-view-toggle").addEventListener("click", function (e) {
      const b = e.target.closest("button[data-psview]");
      if (!b) return;
      psView = b.dataset.psview;
      psApplyView();
    });

    // Newest-first list, so "older" steps forward through it.
    $("ps-prev").addEventListener("click", function () {
      if (psList && psIndex < psList.length - 1) { psIndex++; psRender(); }
    });
    $("ps-next").addEventListener("click", function () {
      if (psList && psIndex > 0) { psIndex--; psRender(); }
    });

    $("ps-scan").addEventListener("click", psScanEmail);
    $("ps-pick").addEventListener("click", function () { $("ps-file").click(); });
    $("ps-file").addEventListener("change", function (e) {
      const f = e.target.files[0];
      // Cleared before the upload: re-picking the SAME file fires no `change`
      // event otherwise, so a retry after a failure would look dead.
      e.target.value = "";
      psUpload(f);
    });

    const drop = $("ps-drop");
    ["dragenter", "dragover"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault();
        drop.classList.add("over");
      });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      drop.addEventListener(ev, function () { drop.classList.remove("over"); });
    });
    drop.addEventListener("drop", function (e) {
      e.preventDefault();
      psUpload(e.dataTransfer.files[0]);
    });
    // A file dropped slightly off-target would otherwise be OPENED by the
    // browser, navigating away from the dashboard and losing its state.
    ["dragover", "drop"].forEach(function (ev) {
      document.addEventListener(ev, function (e) {
        const t = e.target;
        // Drag targets are not always Elements (document, text nodes).
        if (!(t && t.closest && t.closest("#ps-drop"))) e.preventDefault();
      });
    });

    $("pf-refresh").addEventListener("click", pfRefresh);
    $("pf-ccy-toggle").addEventListener("click", function (e) {
      const b = e.target.closest("button[data-ccy]");
      if (!b || b.disabled) return;
      pfSgd = b.dataset.ccy === "sgd";
      $("pf-ccy-toggle").querySelectorAll("button").forEach(function (x) {
        x.classList.toggle("sel", x === b);
      });
      pfRender();
    });

    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
    show(resolveHash());
    window.addEventListener("hashchange", function () { show(resolveHash()); });
    // Crossing 1100px changes which tabs exist, so the current one is re-resolved
    // — otherwise resizing while on Transactions strands #views in tab-txns,
    // which has no grid template and no tab to underline.
    DESK.addEventListener("change", function () { show(resolveHash()); });
    refresh();
    // Hides the tab itself when unconfigured; must not block Home's paint.
    // show() already fetched if we booted straight onto the portfolio tab.
    if (currentView !== "portfolio") loadPortfolio();
  }

  init();
})();
