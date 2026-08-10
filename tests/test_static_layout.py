"""Structural assertions on the static dashboard.

These do not render anything. They lock the invariants the desktop tab
layout depends on, so an unrelated edit cannot silently break them.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"


def read_static(name):
    return (STATIC / name).read_text(encoding="utf-8")


def section(html, view_id):
    """Return the markup of one <section id="view-..."> including its tags."""
    start = html.index('id="%s"' % view_id)
    start = html.rindex("<section", 0, start)
    end = html.index("</section>", start) + len("</section>")
    return html[start:end]


def test_section_helper_slices_one_view():
    html = read_static("index.html")
    home = section(html, "view-home")
    assert home.startswith("<section")
    assert home.endswith("</section>")
    assert 'id="view-home"' in home
    assert 'id="view-txns"' not in home


def test_mobile_nav_keeps_seven_tabs():
    nav = read_static("index.html")
    nav = nav[nav.index('<nav id="nav"'):]
    nav = nav[:nav.index("</nav>")]
    assert re.findall(r'data-view="(\w+)"', nav) == [
        "home", "txns", "budgets", "insights", "portfolio", "pay", "cards"]


def test_desk_tabs_has_five_buttons():
    # Neither Transactions nor Budgets has a desktop tab: both dissolve into
    # Home's grid at >=1100px. #nav keeps all seven (see the test above) -- the
    # whole point is that mobile is untouched.
    html = read_static("index.html")
    strip = html[html.index('<nav id="desk-tabs"'):]
    strip = strip[:strip.index("</nav>")]
    assert re.findall(r'data-view="(\w+)"', strip) == [
        "home", "insights", "portfolio", "pay", "cards"]


def test_desktop_visibility_override_is_gone():
    css = read_static("styles.css")
    # This rule forced every section visible at once; tabs cannot work with it.
    # Verified 2026-07-28 to be the file's only use of the declaration.
    assert "display:flex !important" not in css
    assert "#view-insights{grid-column:1 / -1}" not in css
    assert "#view-portfolio{grid-column:1 / -1}" not in css


def test_portfolio_override_deleted_as_redundant():
    css = read_static("styles.css")
    # Only existed to out-specify .view{display:flex !important}. With that
    # gone, the global .hidden{display:none !important} suffices.
    assert "#view-portfolio.hidden" not in css
    assert ".hidden{display:none !important}" in css


def test_portfolio_button_hidden_in_both_strips():
    js = read_static("app.js")
    assert '#nav button[data-view="portfolio"]' in js
    assert '#desk-tabs button[data-view="portfolio"]' in js


def test_hash_routing_wired():
    js = read_static("app.js")
    assert "location.hash" in js
    assert "hashchange" in js
    assert "function resolveHash" in js


COL_COUNTS = {
    # Home dropped to 2 (rings + pending joined column 1; #home-recent is the
    # other, and it is display:none on desktop). Budgets collapsed to 1 so it
    # merges into Home as a SINGLE desktop column.
    "view-home": 2, "view-txns": 1, "view-budgets": 1,
    "view-insights": 2, "view-portfolio": 2,
}


def test_each_view_has_expected_columns():
    html = read_static("index.html")
    for view_id, expected in COL_COUNTS.items():
        body = section(html, view_id)
        assert body.count('class="col"') == expected, view_id


def test_txns_has_exactly_one_column():
    # Without a single wrapper, display:contents on #view-txns would spill
    # search, filters and list into three separate grid cells.
    html = read_static("index.html")
    assert section(html, "view-txns").count('class="col"') == 1


def test_col_is_a_mobile_noop():
    css = read_static("styles.css")
    assert ".col{display:contents}" in css


def test_per_tab_grid_templates_exist():
    css = read_static("styles.css")
    assert "#views.tab-home{grid-template-columns:1fr 1fr 1.4fr}" in css
    assert "#views.tab-insights" in css
    assert "#views.tab-portfolio" in css


def test_banner_spans_all_columns():
    css = read_static("styles.css")
    assert "#pf-banner{grid-column:1 / -1}" in css


def test_merged_sections_follow_home_in_document_order():
    # Both merges are GENERAL-sibling rules (~), so they require order, not
    # adjacency -- an earlier version of this test asserted no section could sit
    # between Home and Transactions, which over-constrained the markup and broke
    # the moment Budgets was placed there on purpose. Order still matters twice
    # over: `~` only matches later siblings, and grid columns follow DOM order,
    # which is what puts Budgets left of Transactions.
    html = read_static("index.html")
    home = html.index('id="view-home"')
    assert home < html.index('id="view-budgets"') < html.index('id="view-txns"')


def test_txns_merges_into_home_on_desktop():
    css = read_static("styles.css")
    assert "#view-home.active ~ #view-txns{display:contents}" in css


def test_home_recent_hidden_on_desktop():
    # The merged Transactions column supersedes it; showing both would list
    # the same spends twice side by side.
    css = read_static("styles.css")
    assert "#home-recent{display:none}" in css


def test_hash_corrections_use_replacestate():
    # show() assigns location.hash for real navigations, which pushes a history
    # entry. When it is CORRECTING a hash resolveHash() rejected (empty, garbage,
    # "#txns" on desktop) pushing would create an entry that Back lands on and
    # the hashchange listener immediately re-corrects -- trapping the user in
    # the app. Corrections must replace, not push.
    js = read_static("app.js")
    assert "history.replaceState" in js


def test_show_normalizes_the_merged_views_on_desktop():
    # drillDown(), the "/" and number shortcuts and a breakpoint change all
    # reach show() directly. Desktop has no txns and no budgets tab, and neither
    # #views.tab-txns nor tab-budgets has a grid template, so the normalization
    # lives in show() rather than in each caller.
    js = read_static("app.js")
    assert ('if (DESK.matches && (view === "txns" || view === "budgets")) '
            'view = "home"') in js


def test_breakpoint_change_reresolves_the_tab():
    # Crossing 1100px while on Transactions otherwise leaves #views in an
    # unstyled tab-txns state with no tab underlined.
    js = read_static("app.js")
    assert 'DESK.addEventListener("change"' in js


def test_unconfigured_portfolio_is_unroutable():
    # Without this, "#portfolio" on a Tiger-less install stays a valid hash:
    # show() renders the empty tab, loadPortfolio() redirects to Home, and Back
    # re-enters the same fetch.
    js = read_static("app.js")
    assert 'v === "portfolio" && pfUnavailable' in js


def test_resolve_hash_rejects_pay_when_unconfigured():
    # Same trap as #portfolio above: without this, "#pay" on an install with no
    # PAYSLIP_PDF_PASSWORD stays a valid hash, so show() renders an empty tab,
    # loadPayslips() redirects to Home, and Back re-enters the same fetch.
    js = read_static("app.js")
    assert 'v === "pay" && psUnavailable' in js


def test_pay_unavailable_is_set_before_redirecting():
    # psUnavailable must be assigned BEFORE show("home") runs, or resolveHash()
    # still accepts #pay and the correction PUSHES an entry Back bounces off.
    js = read_static("app.js")
    i = js.index("psUnavailable = true")
    j = js.index('if (currentView === "pay") show("home")')
    assert i < j


def test_pay_tab_has_a_grid_template():
    # Every tab keys the grid off #views.tab-<view>. A missing rule leaves the
    # section stranded in a grid with no template.
    css = read_static("styles.css")
    assert "#views.tab-pay" in css


def test_payslip_labels_are_not_interpolated_as_html():
    # Line-item labels come out of a parsed PDF -- employer wording, not user
    # input -- so an "&" or "<" must not be able to become markup.
    # Comment-blanked source: the explanation inside psRenderItems says the word
    # "innerHTML", so asserting against raw source matched the PROSE and would
    # stay green even if the code did interpolate.
    js, _ = js_ast("app.js")
    i = js.index("function psRenderItems")
    body = js[i:i + 1200]
    assert "innerHTML" not in body
    assert "textContent = it.label" in body


def test_pay_tab_offers_both_import_paths():
    pay = section(read_static("index.html"), "view-pay")
    assert 'id="ps-scan"' in pay      # mailbox scan, the /payslip equivalent
    assert 'id="ps-pick"' in pay      # file picker
    assert 'id="ps-drop"' in pay      # drag target
    assert 'type="file"' in pay and 'accept="application/pdf,.pdf"' in pay


def test_import_status_is_announced_to_screen_readers():
    # OCR takes seconds and the outcome is text-only -- with no live region a
    # screen-reader user gets no signal that anything happened at all.
    pay = section(read_static("index.html"), "view-pay")
    log = pay[pay.index('id="ps-import-log"'):]
    assert 'aria-live="polite"' in log[:200]


def test_file_input_is_cleared_so_the_same_file_can_be_retried():
    # Re-picking the SAME file fires no `change` event unless the input is
    # cleared, so a retry after a failed import would look dead.
    js, _ = js_ast("app.js")
    i = js.index('$("ps-file").addEventListener')
    body = js[i:i + 400]
    assert "value = " in body
    assert body.index("value = ") < body.index("psUpload(")


def test_upload_posts_the_raw_file_not_a_form():
    # Raw body is what lets the route skip python-multipart. A FormData here
    # would silently 400 against a server that never learned to parse one.
    js, _ = js_ast("app.js")
    i = js.index("function psUpload")
    body = js[i:i + 700]
    assert "FormData" not in body
    assert "body: file" in body


def test_stray_drops_do_not_navigate_away():
    # A PDF dropped just outside the target would otherwise be OPENED by the
    # browser, discarding the dashboard's state.
    js, _ = js_ast("app.js")
    assert 'document.addEventListener(ev' in js
    i = js.index('document.addEventListener(ev')
    assert "preventDefault" in js[i:i + 300]


def test_views_tab_class_is_swapped_not_clobbered():
    # Assigning .className wipes any other class on <main id="views">. Nothing
    # else sets one today, so this is about not leaving a trap for whoever adds
    # the first one.
    js = read_static("app.js")
    assert 'className = "tab-"' not in js
    assert 'classList.add("tab-" + view)' in js


def test_boot_does_not_double_fetch_portfolio():
    # show() already calls loadPortfolio() when it lands on the portfolio tab,
    # so the init() tail must skip it there or booting on #portfolio fires two
    # concurrent pairs of GETs.
    js = read_static("app.js")
    assert 'if (currentView !== "portfolio") loadPortfolio();' in js


def test_auth_pages_exist_and_post_to_the_right_routes():
    login = read_static("login.html")
    signup = read_static("signup.html")
    assert 'id="login-form"' in login
    assert 'id="signup-form"' in signup
    js = read_static("auth.js")
    assert "/api/auth/login" in js
    assert "/api/auth/signup" in js
    assert "/api/auth/status" in js


def test_auth_pages_do_not_load_the_app_bundle():
    # app.js is gated; pulling it into the login page would 302 and log a
    # confusing error on a page that is working fine.
    for name in ("login.html", "signup.html"):
        assert "/app.js" not in read_static(name), name


def test_all_static_js_parses_pre_es2019():
    # Enforces the constraint that used to be a manual step: no bare `catch {`,
    # no optional chaining, no nullish coalescing.
    import esprima
    for name in ("app.js", "auth.js", "sw.js"):
        esprima.parseScript(read_static(name))


def test_every_asset_a_public_page_needs_passes_the_gate():
    """The gate must let through everything an anonymous page actually fetches.

    Enumerating only the <link>/<script> URLs in the HTML is not enough, and
    that gap was a real prod bug: styles.css is public, its @font-face rules
    point at /vendor/nunito-*.woff2, and .auth-card h1 renders in that face --
    so loading /login fetched a font that the gate 302'd to the login page,
    producing an OTS sfntVersion error and a fallback typeface. It reproduced
    only in prod, because static/vendor/ is gitignored and built by the
    Dockerfile, so a dev checkout 404s instead of revealing the redirect.

    So: follow the stylesheet's url() references too.
    """
    from app.web import _is_public

    refs = set()
    for name in ("login.html", "signup.html"):
        refs |= set(re.findall(r'(?:href|src)="(/[^"]*)"', read_static(name)))
    refs |= set(re.findall(r"url\((/[^)]+)\)", read_static("styles.css")))

    assert "/vendor/nunito-800.woff2" in refs, "font ref vanished; test is moot"
    unreachable = sorted(r for r in refs if not _is_public(r))
    assert unreachable == [], unreachable


def test_auth_error_box_is_announced_and_unhidden_before_it_is_written():
    # .hidden is display:none !important, which keeps the node out of the
    # accessibility tree -- role="alert" only announces changes made while the
    # node is IN it, so the unhide must come first.
    for name in ("login.html", "signup.html"):
        assert 'id="auth-error"' in read_static(name)
        assert 'role="alert"' in read_static(name), name
    js = read_static("auth.js")
    unhide = js.index('box.classList.remove("hidden")')
    write = js.index("box.textContent = msg")
    assert unhide < write


def test_auth_error_does_not_use_white_on_bad():
    # var(--bad) is #EF8A80 in dark mode; white on it is 2.4:1, on the one
    # element the user must read to learn why sign-in failed.
    css = read_static("styles.css")
    block = css[css.index(".auth-error{"):]
    block = block[:block.index("}")]
    assert "var(--bad)" not in block
    assert "#fff" not in block


def test_auth_forms_guard_against_double_submit():
    # Each extra login POST is a REAL throttle failure against both the IP and
    # username keys, so a double-tap reaches LOGIN_MAX_FAILURES in three
    # perceived attempts and locks the owner out.
    js = read_static("auth.js")
    assert js.count("if (busy) return;") == 2
    assert "btn.disabled = on" in js


def test_auth_submit_clears_the_previous_error():
    # Otherwise a fixed mistake keeps showing its old message for the whole
    # round trip, which reads as "the button did nothing".
    js = read_static("auth.js")
    assert js.count("clearError();") == 2


def test_signed_in_visitor_to_login_is_sent_home_with_replace():
    # replace(), not href: an authed user landing on /login should not leave a
    # history entry that Back returns them to. Same rule as the hash router.
    js = read_static("auth.js")
    assert 'if (s.authed) { location.replace("/"); return; }' in js


# --- session / service-worker helpers ---------------------------------------
# Nothing in this suite executes JS, so a plain `assert "foo" in js` can only
# catch a rename, not a behaviour change. These helpers parse the file and hand
# back the source of ONE construct, so an assertion can say "skipWaiting is
# called by the install handler" rather than "the file mentions skipWaiting".


def js_ast(name):
    """(comment-blanked source, AST). Node ranges index into the returned
    source, so js_slice() gives back a construct's CODE.

    Comments are blanked (to spaces, offsets preserved) rather than left in
    place because these helpers exist to assert about behaviour, and this
    codebase comments heavily with the very identifiers under test. Concretely:
    logout()'s leading comment contains the string "/api/auth/logout", so
    asserting that substring against the raw function source matched the PROSE
    -- deleting the actual POST left the suite green.
    """
    import esprima
    src = read_static(name)
    tree = esprima.parseScript(src, options={"range": True, "comment": True}).toDict()
    chars = list(src)
    for c in tree.get("comments") or []:
        for i in range(*c["range"]):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars), tree


def js_nodes(node):
    """Every AST node in the tree (dicts), parents before children."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            for child in js_nodes(value):
                yield child
    elif isinstance(node, list):
        for value in node:
            for child in js_nodes(value):
                yield child


def js_slice(src, node):
    return src[node["range"][0]:node["range"][1]]


def listener_source(name, event):
    """Source of the callback given to self.addEventListener(<event>, cb)."""
    src, tree = js_ast(name)
    for n in js_nodes(tree):
        callee = n.get("callee") or {}
        if n.get("type") != "CallExpression" or callee.get("type") != "MemberExpression":
            continue
        if ((callee.get("object") or {}).get("name") != "self"
                or (callee.get("property") or {}).get("name") != "addEventListener"):
            continue
        args = n.get("arguments") or []
        if len(args) > 1 and args[0].get("value") == event:
            return js_slice(src, args[1])
    raise AssertionError("no self.addEventListener(%r, ...) in %s" % (event, name))


def method_calls(name, prop):
    """Source of every `<expr>.<prop>(...)` call in the file."""
    src, tree = js_ast(name)
    out = []
    for n in js_nodes(tree):
        callee = n.get("callee") or {}
        if n.get("type") == "CallExpression" and callee.get("type") == "MemberExpression":
            if (callee.get("property") or {}).get("name") == prop:
                out.append(js_slice(src, n))
    return out


def function_source(name, fname):
    src, tree = js_ast(name)
    for n in js_nodes(tree):
        if n.get("type") == "FunctionDeclaration" and (n.get("id") or {}).get("name") == fname:
            return js_slice(src, n)
    raise AssertionError("no function %s() in %s" % (fname, name))


def assignment_source(name, target):
    """Source of the assignment whose left-hand side reads exactly `target`."""
    src, tree = js_ast(name)
    for n in js_nodes(tree):
        if n.get("type") == "AssignmentExpression" and js_slice(src, n["left"]) == target:
            return js_slice(src, n)
    raise AssertionError("nothing assigns to %s in %s" % (target, name))


def is_location_nav(src, n):
    """True for a node that navigates the browser via `location`.

    Structural on purpose. Asking "does the source text mention location and
    /login" also matches the file's outermost IIFE, which contains everything;
    and pinning one spelling misses the other three. Covers
    location.assign/replace(...) and assignment to location, location.href,
    window.location and window.location.href.
    """
    if n.get("type") == "CallExpression":
        callee = n.get("callee") or {}
        if callee.get("type") != "MemberExpression":
            return False
        if (callee.get("property") or {}).get("name") not in ("assign", "replace"):
            return False
        return js_slice(src, callee["object"]) in ("location", "window.location")
    if n.get("type") == "AssignmentExpression":
        return js_slice(src, n["left"]) in (
            "location", "location.href", "window.location", "window.location.href")
    return False


def test_service_worker_never_caches_api_responses():
    # Cached /api/* JSON is private financial data that the offline fallback
    # would keep serving after logout. Scoped to the fetch handler: the bail-out
    # is only a bail-out if it runs before respondWith.
    src, tree = js_ast("sw.js")
    handler = listener_source("sw.js", "fetch")
    assert 'indexOf("/api/") === 0' in handler
    assert handler.index('indexOf("/api/") === 0') < handler.index("respondWith")
    # Ordering alone only proves the CHECK runs first, not that it BAILS:
    # turning the body into a no-op leaves every API response cached again.
    guard = next(n for n in js_nodes(tree)
                 if n.get("type") == "IfStatement"
                 and 'indexOf("/api/") === 0' in js_slice(src, n["test"]))
    body = guard["consequent"]
    if body.get("type") == "BlockStatement":
        body = (body.get("body") or [None])[0] or {}
    assert body.get("type") == "ReturnStatement", js_slice(src, guard)


def test_service_worker_cache_name_bumped_and_old_ones_evicted():
    # Installed PWAs already hold API responses under expenses-v1; only a name
    # change plus an activate-time sweep removes them.
    assert 'CACHE = "expenses-v2"' in read_static("sw.js")
    activate = listener_source("sw.js", "activate")
    assert "caches.keys()" in activate
    assert "caches.delete" in activate
    assert "k !== CACHE" in activate


def test_new_service_worker_takes_over_immediately():
    # A new SW installs and then WAITS while any tab is still controlled by the
    # old one. On an installed PWA that leaves expenses-v1 -- the very cache of
    # /api/* financial JSON this task exists to purge -- alive indefinitely.
    # Safe here only because this SW is network-first with no precache list, so
    # a claimed tab is not running a half-old asset set.
    assert "self.skipWaiting()" in listener_source("sw.js", "install")
    chains = method_calls("sw.js", "waitUntil")
    assert len(chains) == 1, chains
    # Inside waitUntil, so activation (and therefore the eviction above) is not
    # deferred to each tab's next navigation.
    assert "clients.claim()" in chains[0]
    assert "caches.delete" in chains[0]


def test_service_worker_does_not_cache_followed_redirects():
    # res.ok is TRUE for a followed redirect, so a gated request the login gate
    # bounces reaches the cache write as a 200 full of login HTML -- stored
    # under the ORIGINAL url's key. See the companion premise test in
    # tests/test_auth_web.py, which proves the 302-to-200 shape against the
    # real gate.
    src, tree = js_ast("sw.js")
    puts = [n for n in js_nodes(tree)
            if n.get("type") == "CallExpression"
            and ((n.get("callee") or {}).get("property") or {}).get("name") == "put"]
    assert len(puts) == 1, "expected exactly one cache write"
    put = puts[0]
    guards = [n for n in js_nodes(tree)
              if n.get("type") == "IfStatement"
              and n["range"][0] <= put["range"][0]
              and put["range"][1] <= n["range"][1]]
    assert guards, "the cache write is unguarded"
    # Structural: the guard must ENCLOSE the write. Textual only in the exact
    # form of the negation, which nothing here can execute to check.
    conditions = " ".join(js_slice(src, g["test"]) for g in guards)
    assert "!res.redirected" in conditions, conditions
    # && not ||: flipping the operator satisfies a substring check while
    # restoring the exact bug (a redirected response is cached whenever it is
    # also ok, and every followed redirect is ok).
    tests = [g["test"] for g in guards if "!res.redirected" in js_slice(src, g["test"])]
    assert tests and all(t.get("operator") == "&&" for t in tests),         [js_slice(src, t) for t in tests]


def test_logout_posts_then_clears_caches_before_redirecting():
    body = function_source("app.js", "logout")
    assert "/api/auth/logout" in body
    assert "caches.keys()" in body
    # Order matters: navigation cancels in-flight work, so a redirect issued
    # first can leave the app shell and cached pages readable after sign-out.
    assert body.index("/api/auth/logout") < body.index("caches.keys()")
    assert body.index("caches.keys()") < body.index("goToLogin()")
    # The sweep must be AWAITED. Dropping the await reinstates exactly the race
    # the ordering above exists to prevent, while keeping every index assertion
    # green.
    assert "await Promise.all" in body


def test_sign_out_buttons_are_wired_before_the_first_await():
    # Nothing previously connected the buttons to logout(): deleting the
    # querySelectorAll(".logout-btn") block left all 429 tests green with the
    # buttons rendering and doing nothing. And the wiring has to happen before
    # init()'s first network call, or a boot that is slow -- or 401ing -- leaves
    # the user with no way out of a dashboard they cannot load.
    init = function_source("app.js", "init")
    assert ".logout-btn" in init
    assert init.index(".logout-btn") < init.index("await api.get")


def test_bfcache_restore_is_forced_through_the_gate():
    # location.replace() drops only the CURRENT history entry, but show()
    # pushes one per tab switch, so Back after signing out can still land on
    # "/#budgets" -- which the browser may restore from the back/forward cache
    # fully rendered, with no request and no session check. Reloading a
    # persisted restore re-runs the gate.
    js = read_static("app.js")
    assert 'window.addEventListener("pageshow"' in js
    assert "e.persisted" in js
    assert "location.reload()" in js


def test_fetch_wrapper_redirects_on_401():
    # One interception point rather than 11 call sites; scoped to the wrapper so
    # the assertion cannot be satisfied by a 401 check somewhere unrelated.
    wrapper = assignment_source("app.js", "window.fetch")
    assert "res.status === 401" in wrapper
    assert 'indexOf("/api/")' in wrapper
    assert "goToLogin()" in wrapper


def test_session_expiry_redirect_fires_once():
    # Several /api/* calls run in parallel on boot, so one expired session 401s
    # all of them; and logout() is already navigating while it clears caches.
    # The latch itself can only be pinned as a string match (nothing executes
    # here), but the count assertion is real: it fails if any new site assigns
    # the login redirect directly and bypasses the latch.
    guard = function_source("app.js", "goToLogin")
    assert "if (leaving) return;" in guard
    assert "leaving = true;" in guard
    # Counting one spelling was not enough: this previously asserted
    # count('location.href = "/login"') == 1, which a new
    # location.replace("/login") / location.assign("/login") /
    # window.location = "/login" anywhere else in the file would sail past
    # while still bypassing the latch. is_location_nav() walks the AST for
    # every location-rooted navigation instead, so the assertion is that the
    # only route to /login in this file is the latched one.
    src, tree = js_ast("app.js")
    navs = [js_slice(src, n) for n in js_nodes(tree)
            if is_location_nav(src, n) and "/login" in js_slice(src, n)]
    assert len(navs) == 1, navs
    assert navs[0] in guard, navs


def test_logout_button_present_in_both_headers():
    html = read_static("index.html")
    assert html.count('class="logout-btn"') == 2


def test_logout_button_is_positioned_inside_the_mobile_header():
    # .greet is position:relative and places its controls absolutely, so a
    # .logout-btn with only colour/size rules drops into normal flow and renders
    # as a stray glyph UNDER the greeting on mobile. Nothing here paints, so
    # this cannot catch a bad offset -- it catches the missing rule, which is
    # the mistake actually available to make.
    css = read_static("styles.css")
    assert ".greet .logout-btn{position:absolute" in css


def test_theme_and_logout_do_not_share_the_same_corner():
    # Both are absolutely positioned in .greet, so equal `right` values would
    # stack them on top of each other -- and the offsets must additionally keep
    # visual order matching DOM order (theme first, then logout), which is what
    # makes tab order correct ahead of the accessibility pass.
    css = read_static("styles.css")
    # `px` optional: a zero offset is idiomatically written unitless.
    theme = re.search(r"\.greet \.theme-btn\{[^}]*right:(\d+)(?:px)?", css)
    logout = re.search(r"\.greet \.logout-btn\{[^}]*right:(\d+)(?:px)?", css)
    assert theme and logout
    assert int(theme.group(1)) > int(logout.group(1)), "theme must sit inboard of sign-out"


def test_reimbursement_row_reads_as_money_coming_in():
    # A reimbursement is stored as a negative expense (so the month nets down),
    # but the row is what the user reads: S$30 came back in. It rendered
    # "-S$30.00" in green -- a minus sign and a positive colour on the same
    # row, contradicting each other. Sign and colour must now agree, so the
    # "+" branch and the .plus branch are keyed off the SAME condition.
    js = read_static("app.js")
    assert '(inc || reimb ? "+" : "-")' in js
    assert 'inc || reimb ? "plus" : ""' in js
    # The old form put a literal minus in front of a reimbursement.
    assert '"−" + fmt(-t.amount_cents)' not in js


def render_linking():
    """(comment-blanked file source, renderLinking's AST node)."""
    src, tree = js_ast("app.js")
    for n in js_nodes(tree):
        if (n.get("type") == "FunctionDeclaration"
                and (n.get("id") or {}).get("name") == "renderLinking"):
            return src, n
    raise AssertionError("no renderLinking() in app.js")


def test_the_two_similarly_named_link_containers_stay_distinct():
    # #edit-links (the paybacks a spend received) and #edit-link (the picker
    # for an incoming transfer) are one character apart and sit next to each
    # other in the sheet. Swapping them is a silent, plausible typo.
    html = read_static("index.html")
    assert 'id="edit-links" class="edit-links hidden"' in html
    assert 'id="edit-link" class="edit-link hidden"' in html
    src, fn = render_linking()
    assert js_slice(src, fn).count('$("edit-links")') == 0


def test_reopening_the_sheet_drops_the_previous_rows_picker():
    # The sheet is reused for every transaction, so whatever the last row put
    # in the container is still there. Clearing has to happen before the awaits
    # -- otherwise the stale picker is visible, and clickable, while the new
    # row's candidates are in flight.
    src, fn = render_linking()
    head = " ".join(js_slice(src, s) for s in fn["body"]["body"][:3])
    assert 'innerHTML = ""' in head
    assert 'classList.add("hidden")' in head


def test_a_payback_is_never_offered_something_else_to_pay_back():
    src, fn = render_linking()
    guard = None
    for n in js_nodes(fn):
        if n.get("type") == "IfStatement" and "tx.link_tid" in js_slice(src, n["test"]):
            guard = n
            break
    assert guard, "renderLinking no longer branches on tx.link_tid"
    assert guard["consequent"]["body"][-1]["type"] == "ReturnStatement", (
        "the already-linked branch must leave renderLinking; falling through "
        "offers a payback a second spend to pay back")
    assert "reimburse-candidates" not in js_slice(src, guard)


def test_only_incoming_money_is_offered_the_picker():
    # The candidates route 400s on anything that is not income, and api.get
    # does not check r.ok -- so dropping this guard turns every expense row
    # into a TypeError on an unawaited promise. Nothing visible, no picker.
    src, fn = render_linking()
    body = js_slice(src, fn)
    assert body.index('tx.type !== "income"') < body.index("reimburse-candidates")


def test_linking_and_unlinking_repaint_what_they_changed():
    # Both mutate the row's type and the month total. Without refresh() the
    # sheet closes onto a list still calling the transfer income.
    for fname in ("doLink", "doUnlink"):
        src = function_source("app.js", fname)
        assert 'closeOverlay("sheet-overlay")' in src, fname
        assert "refresh()" in src, fname


def test_link_and_unlink_call_routes_the_backend_actually_serves():
    # The two halves are in different languages; a rename on either side stays
    # silent until someone taps Unlink.
    js = read_static("app.js")
    api = (Path(__file__).resolve().parents[1] / "app" / "web.py").read_text(encoding="utf-8")
    for suffix in ("/link", "/unlink"):
        assert '"POST", "/api/transactions/" + tx.id + "%s"' % suffix in js
        assert '@app.post("/api/transactions/{tid}%s")' % suffix in api
    assert '"/api/transactions/" + tx.id + "/reimburse-candidates"' in js
    assert '@app.get("/api/transactions/{tid}/reimburse-candidates")' in api


def test_picker_rows_are_focusable_controls():
    # Keyboard reachability, ahead of the accessibility pass: a div with a
    # click handler is invisible to Tab and to screen readers.
    js = read_static("app.js")
    assert '<button class="link-pick"' in js
    assert '<button id="edit-unlink" class="link-undo"' in js


def test_keyboard_focus_is_visible_at_all():
    # Before this pass the ONLY focus rule in the file was on inputs, so every
    # button and every custom control took focus invisibly. :focus-visible
    # rather than :focus so a pointer click does not leave a ring behind.
    css = read_static("styles.css")
    assert ":focus-visible{outline:" in css


def test_header_glyph_buttons_are_a_real_tap_target():
    # 20px glyphs in a zero-padding button (the base `button` rule clears
    # padding) are a ~20x20 target. 44 is the touch floor, and it is what makes
    # the focus ring from the rule above legible. Asserted as ONE shared rule
    # because that is half the point -- these four were four different sizes.
    css = read_static("styles.css")
    m = re.search(r"\.theme-btn,\.logout-btn,\.mnav,\.mnav2\{([^}]*)\}", css)
    assert m, "the four glyph buttons must share one sizing rule"
    assert re.search(r"width:(\d+)px", m.group(1)).group(1) == "44"
    assert re.search(r"height:(\d+)px", m.group(1)).group(1) == "44"
    # The sizes these two used to carry must be gone from their OWN rules, or
    # whichever comes later in the file wins the cascade and the shared rule
    # above does nothing. Two traps here, both hit while writing this: a bare
    # `"width:26px" not in css` also matches .dot, which legitimately uses it;
    # and `"height:" not in rule` also matches the line-height these rules keep.
    # Hence: anchor the selector to column 0 so the shared rule (whose list ends
    # ",.mnav2{") is not what gets read, and anchor the property to a
    # declaration boundary.
    for sel in ("mnav", "mnav2"):
        own = re.search(r"(?m)^\." + sel + r"\{([^}]*)\}", css)
        assert own, sel
        for prop in ("width", "height"):
            assert not re.search(r"(?:^|;)\s*" + prop + r":", own.group(1)), (sel, prop)


def test_every_row_with_a_click_action_is_reachable_by_tab():
    # These are divs with delegated click handlers. Without tabindex they are
    # invisible to the keyboard, and this list is the app's primary content.
    js = read_static("app.js")
    for marker in ('<div class="tx ${inc ? "income"',
                   '<div class="ring-item"',
                   '<div class="bcard" data-cat=',
                   '<div class="pill" data-cat=',
                   '<div class="lv${lv}" data-day='):
        i = js.index(marker)
        tag = js[i:js.index(">", i)]
        assert 'role="button"' in tag, marker
        assert 'tabindex="0"' in tag, marker


def test_containers_whose_buttons_do_the_work_are_not_themselves_buttons():
    # The pending row and the goal card have NO row-level action -- their click
    # handlers act only when an inner button matched. Giving them role=button
    # would announce a control that does nothing and swallow a tab stop.
    js = read_static("app.js")
    for marker in ('<div class="tx" data-pid=',
                   '<div class="bcard${g.bought_at'):
        i = js.index(marker)
        assert 'role="button"' not in js[i:js.index(">", i)], marker


def test_enter_and_space_activate_a_role_button():
    src = read_static("app.js")
    i = src.index('e.key !== "Enter"')
    body = src[i:i + 600]
    assert '[role="button"]' in body
    assert "e.target.closest(" in body
    assert "e.preventDefault()" in body, "Space scrolls the page without this"
    assert ".click()" in body


def test_keyboard_activation_yields_to_native_controls():
    # .bcard and .tx wrap real <button>s. Without this bail-out, closest()
    # climbs PAST the button the user is actually on and fires the whole card.
    src = read_static("app.js")
    i = src.index('e.key !== "Enter"')
    body = src[i:i + 600]
    assert 'e.target.closest("button,a[href],input,select,textarea")' in body
    assert body.index("button,a[href]") < body.index('[role="button"]'),         "the native-control bail-out must run BEFORE the role=button lookup"


def test_heatmap_tooltip_follows_focus():
    # The tooltip is mouseover/mouseleave driven. focusin/focusout (which
    # bubble, unlike focus/blur) mirror it for the keyboard -- otherwise a
    # focused cell shows nothing, or shows a tip nothing ever dismisses.
    js = read_static("app.js")
    assert '$("heatmap").addEventListener("focusin"' in js
    assert '$("heatmap").addEventListener("focusout"' in js


def test_opening_an_overlay_seals_off_the_page_behind_it():
    # Without this the whole dashboard stays in the tab order behind a modal:
    # Tab walks out of the dialog and into content the user cannot see.
    # Iterating siblings rather than naming them means a future top-level
    # element is covered without an edit here.
    src = function_source("app.js", "openOverlay")
    assert 'for (const c of $("app").children) if (c !== el) c.inert = true;' in src
    assert "document.activeElement" in src
    assert ".focus()" in src


def test_closing_un_inerts_before_restoring_focus():
    # focus() on an element that is still inert is silently dropped to <body>.
    # The un-inert therefore cannot wait for the 160ms close animation.
    src = function_source("app.js", "closeOverlay")
    assert "c.inert = false" in src
    assert src.index("c.inert = false") < src.index("setTimeout"),         "un-inert must not be inside or after the close-animation timeout"
    assert src.index("c.inert = false") < src.index("overlayReturn.focus()")


def test_overlays_announce_themselves_as_modal_dialogs():
    html = read_static("index.html")
    for oid, label in (("add-overlay", "add-title"), ("sheet-overlay", "sheet-title")):
        i = html.index('id="%s"' % oid)
        tag = html[i:html.index(">", i)]
        assert 'role="dialog"' in tag, oid
        assert 'aria-modal="true"' in tag, oid
        assert 'aria-labelledby="%s"' % label in tag, oid
        assert 'id="%s"' % label in html, label


def test_tab_shortcuts_are_not_a_hardcoded_list():
    # The literal mapped 1-4 onto the MOBILE order, but show() folds txns into
    # home on desktop -- so 1 and 2 both landed on Home, 3/4 were off by one,
    # and Portfolio had no key. A literal cannot know about the merge.
    # Comment-blanked source: the explanatory comment inside this very branch
    # contains the literal ":not(.hidden)", so asserting against raw source
    # matched the PROSE -- deleting the selector left the test green.
    js, _ = js_ast("app.js")
    assert '["home", "txns", "budgets", "insights"]' not in js
    i = js.index('"1234567".includes(e.key)')
    body = js[i:i + 500]
    assert "DESK.matches" in body
    assert '"#desk-tabs" : "#nav"' in body
    assert ":not(.hidden)" in body,         "Portfolio and Pay are hidden with .hidden when unconfigured"


def test_shortcut_cheatsheet_is_reachable_and_dismissable():
    js = read_static("app.js")
    assert 'e.key === "?"' in js
    assert 'openOverlay("keys-overlay")' in js
    # Escape closes every overlay, including the new one.
    i = js.index('e.key === "Escape"')
    assert 'closeOverlay("keys-overlay")' in js[i:i + 300]
    html = read_static("index.html")
    assert 'id="keys-overlay"' in html
    assert 'role="dialog"' in html[html.index('id="keys-overlay"'):][:200]


def test_a_second_overlay_does_not_clobber_the_focus_return():
    # Press ? twice, or n with the edit sheet already up, and the naive capture
    # records an element INSIDE an overlay -- closing then "restores" focus to
    # something hidden, which the browser drops to <body>.
    src = function_source("app.js", "openOverlay")
    assert "if (overlayReturn === null) overlayReturn = document.activeElement;" in src


def test_every_chart_gets_a_spoken_description():
    # A <canvas> is opaque to a screen reader -- it announces nothing at all.
    # Counting call sites means a chart added later without a label fails here.
    # 'describeChart($("' rather than 'describeChart(' so the helper's own
    # definition is not counted as a call.
    js = read_static("app.js")
    assert js.count("new Chart(") == js.count('describeChart($("')
    src = function_source("app.js", "describeChart")
    assert '"role", "img"' in src
    assert '"aria-label"' in src


def test_toast_is_announced():
    # "Deleted" / "Linked" is the only confirmation the app gives. Polite, not
    # assertive -- these are confirmations, not warnings.
    html = read_static("index.html")
    i = html.index('id="toast"')
    tag = html[i:html.index(">", i)]
    assert 'role="status"' in tag
    assert 'aria-live="polite"' in tag


def test_icon_only_buttons_have_an_accessible_name():
    # A button whose entire content is a glyph announces as "button" or as the
    # glyph's Unicode name. `title` is not a reliable accessible name.
    html = read_static("index.html")
    for bid in ("hm-prev", "hm-next", "yr-prev", "yr-next", "pf-refresh",
                "add-close", "fab", "add-save"):
        i = html.index('id="%s"' % bid)
        assert "aria-label=" in html[html.rindex("<button", 0, i):html.index(">", i)], bid
    themes = re.findall(r'<button class="theme-btn"[^>]*>', html)
    assert len(themes) == 2
    for t in themes:
        assert "aria-label=" in t
    for k in ("back", "clear"):
        i = html.index('data-k="%s"' % k)
        assert "aria-label=" in html[html.rindex("<button", 0, i):html.index(">", i)], k


def test_navs_are_distinguishable_and_the_open_tab_is_announced():
    html = read_static("index.html")
    assert '<nav id="desk-tabs" aria-label=' in html
    assert '<nav id="nav" aria-label=' in html
    js = read_static("app.js")
    i = js.index('#nav button, #desk-tabs button')
    body = js[i:i + 400]
    assert 'setAttribute("aria-current", "page")' in body
    assert 'removeAttribute("aria-current")' in body


def test_budgets_merges_into_home_on_desktop():
    css = read_static("styles.css")
    assert "#view-home.active ~ #view-budgets{display:contents}" in css


def test_dead_budgets_grid_template_is_gone():
    # Budgets can no longer be the active tab at desktop width, so a
    # tab-budgets template would be unreachable CSS.
    css = read_static("styles.css")
    assert "tab-budgets" not in css


def test_show_normalizes_budgets_on_desktop():
    # Same choke point as txns: nav clicks, the hash, the number-key shortcuts
    # and the breakpoint listener all route through show(), and resolveHash()
    # guards the initial load. Missing the breakpoint case strands #views in
    # tab-budgets, which no longer has a grid template.
    for fname in ("show", "resolveHash"):
        src = function_source("app.js", fname)
        assert "budgets" in src and "DESK.matches" in src, fname


def test_card_order_within_each_view():
    # .col is display:contents below 1100px, so this flattened sequence IS the
    # mobile rendering order -- pinning it makes any reordering deliberate
    # rather than a side effect of a desktop-column tweak, and index() raises
    # outright if a card is dropped. The heatmap, goals list and New goal form
    # deliberately moved from Budgets into Home (they cannot be in column 1 on
    # desktop and in the Budgets section at the same time -- a grid item lives
    # in exactly one column), which is why Budgets is now budgets-only.
    html = read_static("index.html")
    for view_id, markers in (
            ("view-home", ["spent-card", "By category", "home-budgets",
                           "Daily heatmap", ">Goals<", "goal-list", "New goal",
                           "pending-card", "home-recent"]),
            ("view-budgets", [">Budgets<", "budget-list", "Set a budget"])):
        body = section(html, view_id)
        pos = [body.index(m) for m in markers]
        assert pos == sorted(pos), view_id


def test_goal_widgets_left_budgets_together():
    # The goals list and its New goal form must not end up in different views:
    # a Budgets tab offering "New goal" with no list above it reads as broken.
    html = read_static("index.html")
    home, budgets = section(html, "view-home"), section(html, "view-budgets")
    for marker in ("goal-list", "New goal", 'id="heatmap"'):
        assert marker in home, marker
        assert marker not in budgets, marker


def test_desk_only_hides_on_mobile_rather_than_revealing_on_desktop():
    # .desk-only is applied to a .section-title, which is display:flex. A
    # "hide by default, display:block on desktop" pair would override that flex
    # on the very element it is meant to reveal. Hiding on mobile needs no
    # display mode on the desktop side, so nothing can be clobbered.
    css = read_static("styles.css")
    i = css.index("@media (max-width:1099px)")
    assert ".desk-only{display:none}" in css[i:i + 700]
    assert ".desk-only{display:block}" not in css


def test_cards_tab_is_in_both_strips():
    # Per strip, not a global count of 2 -- two buttons in the same strip would
    # satisfy a bare count while leaving the other strip without the tab.
    html = read_static("index.html")
    desktop = html.split('id="desk-tabs"')[1].split("</nav>")[0]
    mobile = html.split('id="nav"')[1].split("</nav>")[0]
    assert 'data-view="cards"' in desktop
    assert 'data-view="cards"' in mobile


def test_nav_order_includes_cards():
    # NAV_ORDER drives slide direction and hash validation; a view missing from
    # it resolves to home, so the tab would be unreachable by URL.
    src, _ = js_ast("app.js")
    assert '"cards"' in src.split("NAV_ORDER")[1].split("]")[0]


def test_digit_shortcut_whitelist_covers_every_mobile_tab():
    # The handler reads the live strip, but the digit whitelist is a literal.
    # Seven mobile tabs need seven digits or the last one is unreachable.
    html = read_static("index.html")
    tabs = html.split('id="nav"')[1].split("</nav>")[0].count("data-view=")
    src, _ = js_ast("app.js")
    whitelist = src.split(".includes(e.key)")[0].split('"')[-2]
    assert len(whitelist) >= tabs, f"{tabs} mobile tabs but whitelist is {whitelist!r}"


def test_by_card_chart_wrapper_has_a_height_rule():
    # Chart.js collapses a wrapper with no explicit height. Assert the HEIGHT,
    # not merely that the selector is mentioned -- the selector existing with
    # any other property would leave the canvas at zero.
    css = read_static("styles.css")
    assert re.search(r"#by-card-wrap\s*\{[^}]*height\s*:", css)


def test_card_filter_is_in_memory_state_not_the_hash():
    # The hash router validates the WHOLE fragment against NAV_ORDER, so a
    # "#cards?card=3" resolves to home and show() then PUSHES a correction --
    # the documented Back-button trap. Card filtering mirrors state.filterCat.
    src, _ = js_ast("app.js")
    assert "cardFilter" in src
    assert "card=" not in src


def test_breakdown_totals_come_from_the_api_not_the_client():
    # The month's transactions are client-side so filtering needs no fetch, but
    # the TOTALS must stay authoritative -- one source per question.
    src, _ = js_ast("app.js")
    assert "/api/cards/breakdown" in src


def test_show_opens_the_cards_tab_lazily():
    # Same shape as the portfolio and payslip tabs: nothing is fetched for a
    # tab the user never opens, and show() is the single choke point that knows.
    src = function_source("app.js", "show")
    assert 'view === "cards"' in src
    assert "renderCards()" in src


def test_the_card_donut_pins_its_radius_like_the_category_donut():
    # Chart.js sizes a doughnut from the canvas MINUS the largest slice offset,
    # so a hover offset shrinks the whole ring unless the headroom is pinned.
    src = function_source("app.js", "renderCards")
    assert "getMaxOffset" in src
    # Never CSS-pin the canvas: the wrapper carries the height (#by-card-wrap),
    # and a pinned canvas breaks tooltip hit-testing. Assert BOTH halves -- the
    # comment used to claim the CSS rule while only checking the JS option.
    assert "maintainAspectRatio: false" in src
    assert not re.search(r"#by-card\s*\{", read_static("styles.css"))


def test_every_card_donut_colour_token_is_property_registered():
    # An unregistered light-dark() custom property computes to LITERAL TEXT via
    # getComputedStyle -- truthy, so a `|| fallback` guard never fires and
    # Chart.js is handed a string it cannot paint.
    src, _ = js_ast("app.js")
    css = read_static("styles.css")
    body = re.search(r"CARD_COLOR_KEYS\s*=\s*\[([^\]]*)\]", src)
    assert body, "renderCards must draw its colours from a named key list"
    keys = re.findall(r'"([a-z]+)"', body.group(1))
    assert keys
    for k in keys + ["other"]:            # "other" backs the Unassigned bucket
        assert "@property --c-%s-fg" % k in css, k
    # Checking the key LIST is not enough -- nothing forced cardColor() to use
    # it, so pointing it at an unregistered token stayed green. Pin the token
    # shape the function actually builds.
    fn = function_source("app.js", "cardColor")
    for name in re.findall(r'cssVar\("(--[a-z-]+)"\)', fn):
        assert "@property %s" % name in css, name
    built = re.findall(r'cssVar\("(--[a-z-]*)"\s*\+', fn)
    assert built == ["--c-"] * len(built) and built, fn
    assert re.search(r'\+\s*"-fg"', fn), fn


def test_card_colours_key_off_identity_not_array_position():
    # Buckets appear and disappear month to month (a card goes unused, the
    # Unassigned bucket comes and goes), so an index repaints the same card a
    # different colour each month. The category donut is stable because it
    # keys off the category name.
    fn = function_source("app.js", "cardColor")
    assert "bucket.card_id % CARD_COLOR_KEYS.length" in fn, fn


def test_cards_are_loaded_before_any_row_paints():
    # txRow() resolves its chip label from state.cardNames, and rows are on
    # screen long before the user opens the Cards tab -- loading the map
    # lazily from show() left every chip reading "Card #<id>" until then.
    # The breakdown stays lazy; only the name map is eager.
    src = function_source("app.js", "refresh")
    assert "loadCards(" in src, src
    # Every row-painting render must sit AFTER the load, i.e. inside its .then.
    for painter in ("renderHome(", "loadTxs("):
        assert src.index("loadCards(") < src.index(painter), painter
    assert ".then(" in src, src


def test_transaction_rows_show_a_card_chip():
    # Asserted inside txRow(), not anywhere in the file: the picker's own
    # handler also mentions "card-chip", so a file-wide check would stay green
    # with nothing rendered to click.
    src = function_source("app.js", "txRow")
    assert "card-chip" in src


def test_unassigned_rows_are_labelled_not_blank():
    # A blank chip reads as "no card exists"; the bucket has a name and the row
    # must use it, matching the donut and the pills. One helper owns the label
    # so the row, the pills and the filter chip cannot word it differently.
    label = function_source("app.js", "cardLabel")
    assert '"Unassigned"' in label
    assert "cardLabel(" in function_source("app.js", "txRow")


def test_the_card_chip_is_keyboard_activatable():
    # Clickable non-buttons need role+tabindex; one document-level Enter/Space
    # handler then synthesises the click.
    #
    # Scoped to the chip's OWN tag. A window of surrounding characters also
    # catches the row's `<div class="tx" ... role="button" tabindex="0">`, so
    # both attributes appear twice and deleting them from the chip left this
    # green -- the chip is the only entry point to the learning path, and
    # losing tabindex makes it keyboard-unreachable.
    src = function_source("app.js", "txRow")
    i = src.index("card-chip")
    tag = src[src.rindex("<span", 0, i):src.index(">", i) + 1]
    assert 'role="button"' in tag, tag
    assert 'tabindex="0"' in tag, tag


def test_a_card_chip_click_opens_the_picker_not_the_edit_sheet():
    # The chip sits inside .tx, whose handler opens the edit sheet. Without an
    # early return ahead of that, the picker is unreachable by pointer -- and
    # both lists render rows with txRow(), so both handlers need the guard.
    src, _ = js_ast("app.js")
    for container in ('$("tx-list").addEventListener("click"',
                      '$("recent-list").addEventListener("click"'):
        body = src.split(container)[1][:700]
        assert "card-chip" in body, container
        assert body.index("card-chip") < body.index("openEdit"), container


def test_zero_is_the_unassigned_bucket_not_a_falsy_no_filter():
    # 0 selects rows with no card; null means "no card filter". Testing with
    # !state.cardFilter collapses the two and makes Unassigned unselectable.
    # renderTxList now delegates to the shared cardMatch() predicate (Task 4),
    # so the null-vs-falsy guard is asserted there instead of inline.
    src = function_source("app.js", "cardMatch")
    assert "state.cardFilter === null" in src
    assert "!state.cardFilter" not in src
    tx_src = function_source("app.js", "renderTxList")
    assert "cardMatch(" in tx_src
    assert "!state.cardFilter" not in tx_src
    # The pills handler CONSTRUCTS the value, so it needs the same guard:
    # `Number(b.dataset.cid) || null` turns the Unassigned pill into a second
    # "All" button and makes the bucket unfilterable from the UI.
    whole, _ = js_ast("app.js")
    pills = whole.split('$("card-pills").addEventListener')[1][:400]
    assert '=== "all"' in pills, pills
    assert "|| null" not in pills, pills


def test_the_card_filter_composes_with_the_other_row_filters():
    # One AND chain, not a pre-filter pass: category, source, minimum, search
    # and card all narrow the same list and none of them clobbers another.
    # The card leg is the shared cardMatch(t) predicate (Task 4), not an
    # inline state.cardFilter check.
    src = function_source("app.js", "renderTxList")
    for f in ("state.filterCat", "state.filterSrc", "state.minCents",
              "cardMatch("):
        assert f in src, f
    assert src.count("state.txs.filter") == 1


def test_both_filter_chips_are_distinguishable_and_clear_only_themselves():
    # The container's handler used to clear filterCat on ANY click inside
    # #filter-chip, so dismissing the card chip would silently drop the
    # category drill-down too.
    chip = function_source("app.js", "renderFilterChip")
    assert 'data-clear="cat"' in chip
    assert 'data-clear="card"' in chip
    src, _ = js_ast("app.js")
    body = src.split('$("filter-chip").addEventListener')[1][:600]
    assert "dataset.clear" in body
    assert "cardFilter" in body and "filterCat" in body
    # The branches must be EXCLUSIVE. Asserting both names appear in the
    # handler stays green even if the card branch also nulls filterCat, which
    # is the exact bug this test is named for.
    card_branch = body.split('dataset.clear === "card"')[1]
    card_branch = card_branch[:card_branch.index("}")]
    assert "filterCat" not in card_branch, card_branch


def test_the_picker_lists_active_cards_from_the_cached_map():
    # GET /api/cards excludes archived rows and the API answers 400 "card is
    # archived" for an assignment to one, so the picker must build from that
    # response -- and from the CACHE, never a fetch per row.
    src = function_source("app.js", "openCardPicker")
    assert "state.cards" in src
    assert "fetch(" not in src and "api.get(" not in src
    assert '"/api/cards"' in function_source("app.js", "loadCards")


def test_the_picker_can_clear_a_cards_assignment():
    # 0 is what the API maps back to NULL; without this entry a mis-attributed
    # row could be moved but never emptied.
    assert 'data-cid="0"' in function_source("app.js", "openCardPicker")
    send = function_source("app.js", "setCard")
    assert "card_id" in send
    assert '"PATCH", "/api/transactions/"' in send
    # ...and the delegated handler actually reaches it.
    src, _ = js_ast("app.js")
    body = src.split('$("card-pick-list").addEventListener')[1][:400]
    assert "setCard(" in body


def test_card_names_pass_through_esc_before_innerhtml():
    # Card names are user-typed: an unescaped "H&M" breaks the markup, the same
    # way an unescaped merchant stem already broke the bot's listing.
    src = function_source("app.js", "txRow")
    assert "${cardLabel(" not in src
    assert "esc(cardLabel(" in src
    src = function_source("app.js", "openCardPicker")
    assert "${c.name}" not in src
    assert "esc(c.name)" in src
    src = function_source("app.js", "renderCards")
    assert "${b.name}" not in src
    # Banning three literal spellings left the two most edit-prone holes open:
    # the pill label (${bucketLabel(b)}) and the picker's kind/last4 subtitle
    # (${sub}). Require EVERY interpolation in these templates to be wrapped,
    # so a new field cannot slip in raw. cardRow and fillBindPicker join the
    # list because both interpolate the same user-typed name.
    for fname in ("renderCards", "openCardPicker", "cardRow", "fillBindPicker"):
        for expr in re.findall(r"\$\{([^{}]*)\}", function_source("app.js", fname)):
            expr = expr.strip()
            wrapped = expr.startswith("esc(") or expr.startswith("fmt(")
            numeric = re.fullmatch(r"[\w.]+(_cents|\.length|id)?", expr) and (
                "name" not in expr and "Label" not in expr and expr != "sub")
            assert wrapped or numeric, "%s(): unescaped ${%s}" % (fname, expr)


def test_the_card_picker_overlay_exists_and_escape_closes_it():
    # Every other overlay is dismissible with Escape; one that is not traps a
    # keyboard user behind an inert #app.
    html = read_static("index.html")
    assert 'id="card-overlay"' in html
    assert 'role="dialog"' in html[html.index('id="card-overlay"'):
                                   html.index('id="card-overlay"') + 200]
    src, _ = js_ast("app.js")
    body = src.split('if (e.key === "Escape")')[1][:400]
    assert '"card-overlay"' in body


def test_the_card_form_offers_exactly_the_kinds_the_api_accepts():
    # Scoped to the <select>'s own markup: the view also contains the word
    # "value" elsewhere, and a whole-file scan passes while the picker offers a
    # kind POST /api/cards answers 400 for.
    from app.web import CARD_KINDS
    view = section(read_static("index.html"), "view-cards")
    i = view.index('id="card-kind"')
    sel = view[i:view.index("</select>", i)]
    assert sorted(re.findall(r'value="(\w+)"', sel)) == sorted(CARD_KINDS)


def test_the_binding_picker_is_filled_from_the_api_not_retyped():
    # The senders are the six (soon seven) email parsers. Typing them into the
    # markup means Task 11's new parser silently never becomes bindable.
    view = section(read_static("index.html"), "view-cards")
    i = view.index('id="card-bind"')
    sel = view[i:view.index("</select>", i)]
    assert sel.count("<option") == 1, sel      # the placeholder, nothing else
    fn = function_source("app.js", "fillBindPicker")
    assert "state.cardSources" in fn, fn
    load = function_source("app.js", "loadCards")
    assert '"/api/cards"' in load and "sources" in load, load


def test_the_same_form_edits_a_card_instead_of_duplicating_it():
    # PATCH /api/cards/{id} shipped in Task 5 and was unreachable from any UI.
    # Assert the ONE send picks its verb -- checking that both verbs appear
    # somewhere in the function stays green if the edit path POSTs a copy.
    fn = function_source("app.js", "saveCard")
    assert fn.count("api.send(") == 1, fn
    call = fn.split("api.send(")[1]
    call = call[:call.index(");")]
    assert '"PATCH"' in call and '"POST"' in call, call
    assert "editing" in call, call
    assert re.search(r'"/api/cards/"\s*\+\s*editing', fn), fn


def test_the_card_form_never_navigates_on_submit():
    # A <form> with no action reloads the dashboard (losing its state) unless
    # the handler cancels the default.
    src, _ = js_ast("app.js")
    body = src.split('$("card-form").addEventListener')[1][:120]
    assert '"submit"' in body, body
    assert "preventDefault" in function_source("app.js", "saveCard")


def test_a_card_row_opens_the_form_prefilled():
    # Every field, from the card -- a form that opens blank in edit mode wipes
    # whatever the user does not retype.
    fn = function_source("app.js", "openCardForm")
    for field in ("card-name", "card-kind", "card-last4", "card-match"):
        assert re.search(r'\$\("%s"\)\.value\s*=[^;]*card' % field, fn), field
    src, _ = js_ast("app.js")
    body = src.split('$("card-list").addEventListener')[1][:600]
    assert "openCardForm(" in body, body
    # The 🗑 sits INSIDE the row, whose own click opens the editor, so the
    # delete branch must come first -- and it must be wired at all. Asserting
    # only that deleteCard() exists left the button unreachable while green:
    # unwiring it made the bin silently open the edit form instead.
    assert "deleteCard(" in body, body
    assert body.index('closest(".c-del")') < body.index("openCardForm("), body


def test_the_card_row_is_keyboard_activatable():
    # Clickable non-buttons need role+tabindex; the one document-level
    # Enter/Space handler then synthesises the click. Scoped to the row's OWN
    # opening tag -- the row wraps a real <button> (delete), so a window of
    # surrounding characters would catch that one's attributes instead.
    fn = function_source("app.js", "cardRow")
    i = fn.index("crow")
    tag = fn[fn.rindex("<div", 0, i):fn.index(">", i) + 1]
    assert 'role="button"' in tag, tag
    assert 'tabindex="0"' in tag, tag


def test_a_card_row_shows_its_transaction_count():
    # Already in the _card() payload, so this costs no extra call -- and it is
    # what tells the user whether deleting will archive or remove.
    assert "tx_count" in function_source("app.js", "cardRow")


def test_the_card_list_renders_from_the_cached_payload():
    fn = function_source("app.js", "renderCardList")
    assert "state.cards" in fn, fn
    assert "api.get(" not in fn and "fetch(" not in fn, fn


def test_deleting_a_card_asks_first():
    fn = function_source("app.js", "deleteCard")
    assert "confirm(" in fn, fn
    assert fn.index("confirm(") < fn.index("DELETE"), fn


def test_delete_reports_archiving_separately_from_removal():
    # The API answers archived:true when history kept the card and false when
    # it was removed outright. One message for both leaves the user not knowing
    # whether the card is gone or merely hidden.
    fn = function_source("app.js", "deleteCard")
    assert "body.archived" in fn, fn
    tern = fn[fn.index("body.archived"):]
    tern = tern[:tern.index(");")]
    assert "?" in tern and ":" in tern, tern
    assert len(set(re.findall(r'"([^"]+)"', tern))) >= 2, tern


def test_rebinding_a_sender_is_confirmed_before_it_is_sent():
    # Rebinding moves every FUTURE import from that sender, so it must not
    # happen silently -- and the confirm has to precede the request, not
    # apologise for it.
    fn = function_source("app.js", "saveCard")
    assert fn.index("confirm(") < fn.index("api.send("), fn
    guard = fn[:fn.index("confirm(")]
    # Owner comes from the cached /api/cards payload: no request per keystroke.
    assert "cardSources" in guard, guard
    # ...and only when ANOTHER card owns it. Confirming on every save (or when
    # the sender is already this card's) trains the user to click through it.
    assert "!== editing" in guard, guard
    assert "api.get(" not in fn and "fetch(" not in fn, fn


def test_clearing_the_match_rule_sends_an_empty_string_not_null():
    # CardPatch reads null as "not supplied", so `|| null` here would make a
    # typed-and-regretted rule permanent. "" is inert server-side because
    # normalise_instrument("") is falsy.
    fn = function_source("app.js", "saveCard")
    m = re.search(r"match:\s*([^,\n]+)", fn)
    assert m, fn
    assert "card-match" in m.group(1), m.group(1)
    assert "null" not in m.group(1), m.group(1)


def test_a_card_mutation_repaints_the_chart_and_the_row_chips():
    # A renamed card must not keep its old name on the donut, in the pills or
    # in a row chip. refresh() is the one path that reloads the name map and
    # repaints all of them.
    for fname in ("saveCard", "deleteCard"):
        assert "refresh()" in function_source("app.js", fname), fname
    ref = function_source("app.js", "refresh")
    assert "renderCardList()" in ref, ref
    assert "renderCards()" in ref, ref


def test_the_add_button_opens_the_form():
    src, _ = js_ast("app.js")
    body = src.split('$("card-add").addEventListener')[1][:200]
    assert "openCardForm(" in body, body


def test_card_filter_sits_above_the_category_donut():
    # The control is the entry point for a filter that scopes the donut, so
    # it has to be adjacent to it -- not parked in the header or below the
    # fold. Placement is the whole feature request.
    home = section(read_static("index.html"), "view-home")
    assert 'id="card-filter"' in home, home
    assert home.index('id="card-filter"') < home.index("By category"), home


def test_card_filter_all_cards_option_is_empty_string_not_zero():
    # "" is no filter and "0" is the Unassigned bucket. Giving "All cards"
    # value="0" would silently show only unassigned rows.
    home = section(read_static("index.html"), "view-home")
    sel = home[home.index('id="card-filter"'):]
    sel = sel[:sel.index("</select>")]
    assert 'value=""' in sel, sel


def test_card_filter_change_handler_distinguishes_empty_from_zero():
    # Number("") is 0, so `Number(e.target.value) || null` and friends turn
    # "All cards" into the Unassigned bucket. The handler must branch on the
    # STRING before converting.
    src, _ = js_ast("app.js")
    body = src.split('$("card-filter").addEventListener')[1][:400]
    assert '=== ""' in body or '== ""' in body, body
    assert "applyCardFilter()" in body, body


def test_every_card_filter_writer_goes_through_applyCardFilter():
    # Five places ASSIGN state.cardFilter: the dropdown, the Cards-tab donut,
    # "See all", the dismiss chip, and renderCardFilter dropping a vanished
    # card. Each must repaint the same things, or one path leaves the dropdown
    # reading a filter the list no longer has.
    # The negative lookahead matters: "state.cardFilter =" is a prefix of
    # "state.cardFilter ===", so a plain count would tally comparisons too.
    src, _ = js_ast("app.js")
    writes = re.findall(r"state\.cardFilter\s*=(?!=)", src)
    assert len(writes) == 5, "expected 5 writers, found %d" % len(writes)
    # 1 definition + 5 call sites, renderCardFilter's drop included: ending
    # that path at syncCardFilterUI() split Home into card-scoped and
    # all-cards widgets at the same time (see the test below).
    assert src.count("applyCardFilter()") >= 6, src.count("applyCardFilter()")
    assert "applyCardFilter()" in function_source("app.js", "cardDrillDown")


def test_card_filter_drops_a_vanished_card_but_keeps_unassigned():
    # A deleted or archived card leaves state.cardFilter pointing at nothing;
    # the option disappears and the select falls back to "All cards" while the
    # list stays filtered. This is the ONE place falsiness is correct -- 0
    # (Unassigned) and null must both survive the check.
    fn = function_source("app.js", "renderCardFilter")
    assert "state.cardFilter = null" in fn, fn
    assert "cardNames" in fn, fn


def test_dropping_a_vanished_card_repaints_every_widget():
    # renderHome() reads cardParam() SYNCHRONOUSLY, before its first await;
    # renderHeatmap() and loadTxs() apply cardMatch() AFTER theirs -- i.e.
    # after refresh() has reached renderCardFilter(). So a drop that only
    # re-synced the control left the donut/total/spark card-scoped while the
    # heatmap and list went all-cards. The drop must route through
    # applyCardFilter(), and only on an actual drop: every refresh() reaches
    # here and must not pay a second full repaint.
    fn = function_source("app.js", "renderCardFilter")
    assert re.search(r"if \(dropped\) applyCardFilter\(\);", fn), fn
    assert re.search(r"else syncCardFilterUI\(\);", fn), fn
    # ...and after the <option> rebuild, which wipes the current selection.
    assert fn.index("sel.innerHTML") < fn.index("applyCardFilter()"), fn


def test_sync_names_a_filter_with_no_matching_option():
    # An ARCHIVED card keeps its spend in the Cards donut, so a pill can drill
    # into an id /api/cards no longer lists. Assigning an unmatched value to a
    # <select> leaves selectedIndex at -1 and the control renders BLANK over a
    # list that is still filtered. Reuse cardLabel()'s honest fallback rather
    # than writing a second label path.
    fn = function_source("app.js", "syncCardFilterUI")
    assert 'const want =' in fn, fn
    assert re.search(r"if \(sel\.value !== want\)", fn), fn
    assert "cardLabel(state.cardFilter)" in fn, fn
    assert "appendChild(opt)" in fn, fn


def test_home_summary_and_trend_carry_the_card_param():
    # The donut, the month total and the sparkline all come from these two
    # calls. Without the param they keep showing all cards under a filter.
    #
    # Assert the exact call EXPRESSIONS, not a proximity window. The previous
    # form did fn.index(call) then looked for "cp" within 90 characters, which
    # for /api/summary bled forward into the ADJACENT /api/trend line: deleting
    # "+ cp" from the summary call left this test green (reproduced 2026-08-09,
    # the two most central widgets of the feature silently unfiltered).
    fn = function_source("app.js", "renderHome")
    assert "cardParam()" in fn, fn
    for call in ('"/api/summary?month=" + ym + cp',
                 '"/api/trend?granularity=day" + cp'):
        assert call in fn, (call, fn)


def test_desk_total_stays_unfiltered():
    # #desk-head sits ABOVE the tab strip and is on screen while the user is
    # on Insights, Portfolio, Pay or Cards -- none of which respect the card
    # filter. A card-scoped number there misreports all four.
    fn = function_source("app.js", "renderHome")
    i = fn.index('$("desk-total")')
    window = fn[max(0, i - 400):i]
    assert "/api/summary?month=" in window, window
    assert "cp" in window, window
    # Presence of both substrings is not enough: adding "+ cp" to the header's
    # own aggregate -- the exact bug this test is named for -- keeps both in
    # the window. Assert the call between the `if (cp)` branch and the
    # countUp() carries no card param at all.
    unfiltered = fn[fn.index("if (cp)"):i]
    assert "+ cp" not in unfiltered, unfiltered


def test_apply_card_filter_is_a_no_op_on_no_change():
    # Each call costs 6-7 requests (5 in renderHome, +1 unfiltered summary
    # while a filter is on, +1 in renderHeatmap) with no sequencing token, and
    # a closed <select> fires `change` on every arrow keypress in Chrome and
    # Firefox -- so arrowing through the list raced five renderHome() fan-outs
    # against each other. Guard on the last APPLIED value.
    src, _ = js_ast("app.js")
    assert re.search(r"let appliedCardFilter = null;", src), "no tracker, or not null-initialised"
    fn = function_source("app.js", "applyCardFilter")
    guard = re.search(r"if \(state\.cardFilter === appliedCardFilter\) return;", fn)
    assert guard, fn
    assert "appliedCardFilter = state.cardFilter;" in fn, fn
    # The sync must sit ABOVE the guard and the repaints below it:
    # renderCardFilter() calls in here straight after rebuilding the <option>
    # list, which wipes the selection, so the sync can never be skipped.
    assert fn.index("syncCardFilterUI()") < guard.start() < fn.index("renderHome()"), fn


def css_desktop_block():
    """Body of the >=1100px media query, brace-matched so nested rules are safe."""
    css = read_static("styles.css")
    i = css.index("{", css.index("@media (min-width:1100px)"))
    depth, j = 0, i
    while True:
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[i + 1:j]
        j += 1


def test_one_budget_scope_note_renders_at_each_width():
    # Both views carry the note: on mobile they are separate tabs and Home has
    # its own rings. But #view-home.active ~ #view-budgets makes them children
    # of ONE grid at >=1100px, so with a filter on the user read "Budgets cover
    # all cards" twice, in adjacent columns.
    html = read_static("index.html")
    assert 'class="budget-scope' in section(html, "view-home")
    assert 'class="budget-scope' in section(html, "view-budgets")
    assert html.count('class="budget-scope') == 2, html.count('class="budget-scope')
    # Hidden on desktop, never revealed -- same direction (and same block) as
    # the #home-recent rule it sits beside.
    assert "#view-home .budget-scope{display:none}" in css_desktop_block()


def test_card_param_keeps_zero_and_drops_null():
    # "" is appended unconditionally by callers, so no-filter must yield the
    # empty string -- and 0 (Unassigned) must still produce &card_id=0.
    fn = function_source("app.js", "cardParam")
    assert "=== null" in fn, fn
    assert "card_id=" in fn, fn


def test_heatmap_and_tx_list_share_one_row_predicate():
    # Two copies of the 0-vs-null rule is two chances to get it wrong.
    for fname in ("renderHeatmap", "renderTxList"):
        assert "cardMatch(" in function_source("app.js", fname), fname


def test_apply_card_filter_repaints_every_scoped_widget():
    fn = function_source("app.js", "applyCardFilter")
    for call in ("syncCardFilterUI()", "renderHome()", "renderHeatmap()",
                 "renderTxList()"):
        assert call in fn, fn


def goto_click_handler_source():
    """Source of the callback passed to el.addEventListener("click", ...)
    inside document.querySelectorAll("[data-goto]").forEach(...).

    Not listener_source()/function_source(): this callback is an arrow
    function nested two calls deep (querySelectorAll(...).forEach((el) =>
    el.addEventListener("click", cb))), not a top-level self.addEventListener
    or a named function. "el" is reused as a callback param elsewhere in the
    file (catGrid's el.addEventListener("click", ...)), so the walk has to
    anchor on the querySelectorAll("[data-goto]") call and search only
    inside its forEach callback, not the whole file.
    """
    src, tree = js_ast("app.js")
    for n in js_nodes(tree):
        callee = n.get("callee") or {}
        if n.get("type") != "CallExpression" or callee.get("type") != "MemberExpression":
            continue
        if (callee.get("property") or {}).get("name") != "forEach":
            continue
        qsa = callee.get("object") or {}
        qsa_callee = qsa.get("callee") or {}
        if (qsa_callee.get("property") or {}).get("name") != "querySelectorAll":
            continue
        qsa_args = qsa.get("arguments") or []
        if not qsa_args or qsa_args[0].get("value") != "[data-goto]":
            continue
        for_each_cb = (n.get("arguments") or [None])[0]
        for m in js_nodes(for_each_cb):
            m_callee = m.get("callee") or {}
            if m.get("type") != "CallExpression" or m_callee.get("type") != "MemberExpression":
                continue
            if (m_callee.get("property") or {}).get("name") != "addEventListener":
                continue
            m_args = m.get("arguments") or []
            if len(m_args) > 1 and m_args[0].get("value") == "click":
                return js_slice(src, m_args[1])
    raise AssertionError("no [data-goto] forEach el.addEventListener('click', ...) in app.js")


def test_see_all_handler_repaints_its_own_filter():
    # applyCardFilter() no-ops (skips its own renderTxList()) whenever no
    # card filter was ever set -- the common case: tap a donut slice, land
    # on Transactions, go back to Home, tap "See all". The handler clears
    # filterCat itself, so it must repaint the list itself rather than
    # depending on applyCardFilter()'s internals for a paint it doesn't
    # unconditionally do. Regression: the chip/donut cleared but the list
    # stayed filtered with no visible reason why.
    cb = goto_click_handler_source()
    assert "renderTxList()" in cb, cb
    assert "applyCardFilter()" in cb, cb
