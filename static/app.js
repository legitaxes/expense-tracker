/* Cashew-style SPA over the existing FastAPI endpoints. Vanilla JS.
   Views: Home / Transactions / Budgets (+ Add overlay, edit sheet). */
(function () {
  "use strict";

  // Greeting name is per-device, never shipped in code:
  // localStorage.setItem("expense.name", "Your Name") in the browser console.
  const NAME = localStorage.getItem("expense.name") || "";
  const ICONS = { food: "🍜", groceries: "🛒", transport: "🚌", shopping: "🛍️",
                  bills: "🧾", health: "💊", entertainment: "🎮", other: "📦" };
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

  const api = {
    get: (url) => fetch(url).then((r) => r.json()),
    send: (method, url, body) =>
      fetch(url, { method, headers: { "Content-Type": "application/json" },
                   body: JSON.stringify(body) }).then((r) => {
        if (!r.ok) throw new Error(method + " " + url + " → " + r.status);
        return r.json();
      }),
  };

  const state = { cats: [], month: currentYM(), search: "", txs: [], txShown: 10,
                  editing: null, addAmount: "", addCat: null, addType: "expense",
                  homeMonth: currentYM(), filterCat: null,
                  filterSrc: "all", minCents: 0,
                  merchants: [], merchShown: 5, heatDays: {},
                  year: String(new Date().getFullYear()),
                  yearData: null, yrCat: null, rhythmCat: null };
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

  function localDateKey(d) {
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") +
           "-" + String(d.getDate()).padStart(2, "0");
  }

  async function renderHome() {
    const ym = state.homeMonth;
    const [summary, trend, budgets, recent, pend] = await Promise.all([
      api.get("/api/summary?month=" + ym),
      api.get("/api/trend?granularity=day"),
      api.get("/api/budgets?month=" + ym),
      api.get("/api/transactions?limit=5"),
      api.get("/api/pending"),
    ]);
    $("spent-label").textContent = ym === currentYM() ? "Spent this month"
      : "Spent in " + new Date(ym + "-01").toLocaleDateString("en-SG",
          { month: "long", year: "numeric" });
    countUp($("month-total"), summary.total_cents);
    countUp($("desk-total"), summary.total_cents);
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
  }

  function renderRings(budgets) {
    $("home-budgets").innerHTML = budgets
      .sort((a, b) => b.spent_cents / b.limit_cents - a.spent_cents / a.limit_cents)
      .slice(0, 4)
      .map((b) => {
        const pct = b.limit_cents ? Math.max(0, b.spent_cents) / b.limit_cents * 100 : 0;
        const cls = pct >= 100 ? "over" : pct >= 80 ? "warn" : "";
        return `<div class="ring-item" data-cat="${esc(b.category)}" title="${esc(b.category)}: ${fmt(b.spent_cents)} of ${fmt(b.limit_cents)}">
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
  }

  // ---- transactions ---------------------------------------------------
  function txRow(t) {
    const chip = isAuto(t.source) ? ` <span class="auto">✉ auto</span>` : "";
    const inc = t.type === "income";
    const reimb = t.amount_cents < 0;
    const badge = t.reimbursed_cents > 0
      ? ` <span class="auto rb">↩️ ${fmt(t.reimbursed_cents)} back</span>` : "";
    const amt = reimb ? "−" + fmt(-t.amount_cents)
                      : (inc ? "+" : "-") + fmt(t.amount_cents);
    return `<div class="tx ${inc ? "income" : catClass(t.category)}${reimb ? " reimb" : ""}" data-id="${t.id}">
        <div class="ico">${inc ? "💰" : reimb ? "↩️" : icon(t.category)}</div>
        <div class="mid"><div class="t">${esc(t.note) || "(no note)"}</div>
          <div class="s">${esc(t.category)}${chip}${badge}</div></div>
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
      return `<div class="bcard" data-cat="${esc(b.category)}">
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

  function renderFilterChip() {
    const el = $("filter-chip");
    if (!state.filterCat) { el.classList.add("hidden"); el.innerHTML = ""; return; }
    el.classList.remove("hidden");
    el.innerHTML = `<button class="chip sel" id="filter-clear">${esc(state.filterCat)} ✕</button>`;
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
    $("yr-cats").innerHTML = y.by_category.map((c) => `<div class="pill" data-cat="${esc(c.category)}">
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
    const cats = Object.keys(catTotals).filter((c) => catTotals[c] > 0)
      .sort((a, b) => catTotals[b] - catTotals[a]);
    $("rhythm-cats").innerHTML = cats.map((c) => `<div class="pill" data-cat="${esc(c)}">
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
      return `<div class="pill" data-cat="${esc(cat)}">
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
    const txs = await api.get(
      `/api/transactions?date_from=${ym}-01T00:00:00&date_to=${ym}-${daysInMonth(ym)}`);
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
    let html = ["M", "T", "W", "T", "F", "S", "S"].map((w) =>
      `<div class="wd">${w}</div>`).join("");
    const lead = (new Date(y, m - 1, 1).getDay() + 6) % 7; // Mon-first
    for (let i = 0; i < lead; i++) html += `<div class="blank"></div>`;
    for (let d = 1; d <= daysInMonth(ym); d++) {
      const key = ym + "-" + String(d).padStart(2, "0");
      const v = byDay[key] ? Math.max(0, byDay[key].total) : 0;
      const lv = v === 0 ? 0 : v <= t1 ? 1 : v <= t2 ? 2 : v <= t3 ? 3 : 4;
      html += `<div class="lv${lv}" data-day="${key}">${d}</div>`;
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
  }

  // ---- goals ------------------------------------------------------------
  async function renderGoals() {
    const { goals } = await api.get("/api/goals");
    $("goal-list").innerHTML = goals.map((g) => {
      const pct = g.target_cents ? g.saved_cents / g.target_cents * 100 : 0;
      return `<div class="bcard" data-gid="${g.id}">
          <div class="top"><span class="name">🎯 ${esc(g.name)}</span>
            <span>${Math.round(pct)}%</span></div>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, pct)}%"></div></div>
          <div class="nums"><span class="money">${fmt(g.saved_cents)} of ${fmt(g.target_cents)}</span>
            <span><button class="linkish g-put">＋ Add</button>
              <button class="linkish g-del">🗑</button></span></div>
        </div>`;
    }).join("") || `<div class="empty">No goals yet — create one below.</div>`;
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
          return `<div class="link-row"><span>${esc(l.note)}</span>` +
                 `<span class="money plus">−${fmt(-l.amount_cents)}</span></div>`;
        }).join("") +
          `<div class="link-row net"><span>net</span>` +
          `<span class="money">${fmt(body.net_cents)}</span></div>`;
        linksEl.classList.remove("hidden");
      });
    }
    openOverlay("sheet-overlay");
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

  // ---- shell -------------------------------------------------------------
  const NAV_ORDER = ["home", "txns", "budgets", "insights"];
  let currentView = "home";
  function show(view) {
    const dir = NAV_ORDER.indexOf(view) >= NAV_ORDER.indexOf(currentView)
      ? "view-in-right" : "view-in-left";
    document.querySelectorAll("#views .view").forEach((v) => {
      const on = v.id === "view-" + view;
      v.classList.toggle("active", on);
      v.classList.remove("view-in-right", "view-in-left");
      if (on && view !== currentView && !REDUCED) {
        v.classList.add(dir);
        v.addEventListener("animationend", function h() {
          v.classList.remove(dir); v.removeEventListener("animationend", h);
        });
      }
    });
    document.querySelectorAll("#nav button").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === view));
    currentView = view;
  }

  // Overlays animate in via CSS; closing plays the exit animation, then hides.
  function openOverlay(id) {
    $(id).classList.remove("hidden", "closing");
  }
  function closeOverlay(id) {
    const el = $(id);
    if (el.classList.contains("hidden")) return;
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
    renderHome(); loadTxs(); renderBudgets(); loadMonthChips();
    renderHeatmap(); renderInsights(); renderGoals(); renderYear();
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
    state.cats = (await api.get("/api/categories")).categories;
    catGrid($("add-cats"), (c) => { state.addCat = c; });
    catGrid($("edit-cats"), () => {});
    $("budget-cat").innerHTML =
      state.cats.map((c) => `<option>${c}</option>`).join("");

    $("nav").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-view]");
      if (b) show(b.dataset.view);
    });
    // "See all": clear any drill-down filter, switch view, and scroll to it —
    // on desktop all views are visible in a grid, so without the scroll the
    // click looked like it did nothing.
    document.querySelectorAll("[data-goto]").forEach((el) =>
      el.addEventListener("click", () => {
        state.filterCat = null; setDonutPop(null);
        renderFilterChip(); renderTxList();
        show(el.dataset.goto);
        $("view-" + el.dataset.goto).scrollIntoView(
          { behavior: REDUCED ? "auto" : "smooth", block: "start" });
      }));

    $("fab").addEventListener("click", () => openOverlay("add-overlay"));
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
    $("tx-list").addEventListener("click", (e) => {
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
      const row = e.target.closest(".tx"); if (!row) return;
      api.get("/api/transactions?limit=5").then((rows) => {
        const tx = rows.find((t) => t.id === Number(row.dataset.id));
        if (tx) openEdit(tx);
      });
    });

    // month navigation on Home — drives the tx list, budgets and heatmap too
    function setMonth(ym) {
      state.homeMonth = ym;
      state.month = ym;
      renderHome(); loadMonthChips(); loadTxs(); renderBudgets(); renderHeatmap();
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
    $("filter-chip").addEventListener("click", () => {
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
        return;
      }
      if (e.key === "n" || e.key === "N") {
        openOverlay("add-overlay");
        e.preventDefault();
      } else if (e.key === "/") {
        show("txns");
        $("search").focus();
        e.preventDefault();
      } else if ("1234".includes(e.key)) {
        show(["home", "txns", "budgets", "insights"][Number(e.key) - 1]);
      }
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
      if (!name || !target) return toast("Name + target needed");
      await api.send("POST", "/api/goals", { name, target });
      $("goal-name").value = ""; $("goal-target").value = "";
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

    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
    show("home");
    refresh();
  }

  init();
})();
