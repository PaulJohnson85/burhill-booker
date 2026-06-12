// Minimal service worker: network-first, no caching of dynamic content.
// Exists mainly so the app qualifies as an installable PWA.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  // Pass everything through to the network — booking data must never be stale.
  e.respondWith(fetch(e.request).catch(() =>
    new Response("Offline — Burhill Booker needs a connection.", {
      status: 503,
      headers: { "Content-Type": "text/plain" },
    })));
});
