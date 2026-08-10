/* Deliberately dumb SW: network-first, cache fallback, GET only.
   No precache list — online always gets the server's copy (no stale-JS trap).

   The cache fallback covers the SHELL only. /api/* is skipped outright (see
   the fetch handler), so an offline load now renders the shell with no data
   rather than a stale snapshot of your finances. That is the intended trade:
   cached account data outliving a sign-out is worse than an empty screen. */
const CACHE = "expenses-v2";

self.addEventListener("install", () => {
  // Without this, a new worker installs and then WAITS while any tab is still
  // controlled by the old one, so on an installed PWA the old worker and its
  // expenses-v1 cache (which may hold /api/* financial JSON from before this
  // app had a login) survive indefinitely — exactly what the version bump and
  // the activate sweep below exist to purge.
  //
  // The usual objection to skipWaiting — an open page ending up running a mix
  // of old and new assets — does not apply here specifically because this
  // worker is network-first with no precache list: nothing is served from cache
  // while online, so a claimed tab keeps getting the server's copy either way.
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  // Drops expenses-v1, which may still hold /api/* responses cached before the
  // dashboard had a login. clients.claim() then puts already-loaded tabs under
  // this worker, so the eviction is not deferred to their next navigation.
  e.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
  )).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  // Never cache API responses: private financial data, and the fallback below
  // would keep serving it after logout.
  if (new URL(e.request.url).pathname.indexOf("/api/") === 0) return;
  e.respondWith(
    fetch(e.request).then((res) => {
      // res.ok is TRUE for a FOLLOWED redirect, so anything the login gate
      // bounces lands here as a 200 full of login-page HTML — and caching that
      // would store it under the ORIGINAL url's key, replacing the app shell
      // with a stale login page on the next cache-fallback read. Verified
      // against this app's gate: GET /app.js while signed out is
      // 302 -> /login -> 200 text/html. The premise is pinned by
      // tests/test_auth_web.py::test_gated_asset_redirect_arrives_as_a_cacheable_200,
      // so re-verify there rather than by hand. (An earlier draft of this
      // comment cited /vendor/nunito-800.woff2; that route was allowlisted in
      // 89e0ede and no longer redirects, which would make this look untrue.)
      if (res.ok && !res.redirected) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      }
      return res;
    }).catch(() => caches.match(e.request))
  );
});
