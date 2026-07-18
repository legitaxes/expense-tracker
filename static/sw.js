/* Deliberately dumb SW: network-first, cache fallback, GET only.
   No precache list — online always gets the server's copy (no stale-JS trap). */
const CACHE = "expenses-v1";
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  e.respondWith(
    fetch(e.request).then((res) => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      }
      return res;
    }).catch(() => caches.match(e.request))
  );
});
