// procmon service worker -- deliberately minimal.
//
// This exists mainly to satisfy Chrome/Android's PWA installability
// criteria (a registered service worker is one of the requirements for
// the "Add to Home Screen" install prompt to appear). It caches ONLY
// static assets (icons, manifest) -- never dashboard pages, /api/status,
// or /processes/*/logs. Caching those would risk showing a stale "still
// running" status for a process that actually crashed minutes ago,
// which is worse than no offline support at all for a monitoring tool
// whose entire point is showing what's true RIGHT NOW.

const CACHE_NAME = "procmon-static-v1";
const STATIC_ASSETS = [
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  const isStaticAsset = STATIC_ASSETS.some((path) => url.pathname === path);
  if (!isStaticAsset) {
    return; // deliberately do NOT call respondWith -- let the browser handle
             // everything else (pages, API calls, logs) as a normal, live
             // network request, never served from a cache
  }
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
