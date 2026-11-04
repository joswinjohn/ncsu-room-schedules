// Offline support. Keeps a copy of the page and the schedule data so the site opens with no
// signal (basements, crowded lecture halls). Always tries the network first, so a new
// export shows up as soon as you're online; falls back to the saved copy when offline or
// when the network takes more than a few seconds.
const CACHE = "rooms-v2";
const CORE = ["./", "index.html", "data/schedule.json", "manifest.webmanifest",
  "icons/icon.svg", "icons/icon-192.png", "icons/apple-touch-icon.png",
  "vendor/leaflet/leaflet.js", "vendor/leaflet/leaflet.css"];
// Map tiles come from OpenStreetMap and aren't saved for offline use (only the browser's normal
// cache applies), so offline the map shows the building pins on a blank background.
const SLOW_MS = 4000;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(CORE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    const saved = () => caches.match(req, { ignoreSearch: true })
      .then((hit) => hit || (req.mode === "navigate" ? caches.match("index.html") : undefined));
    const network = fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
      return res;
    });
    event.waitUntil(network.then(() => {}, () => {})); // finish updating the saved copy
    const slow = new Promise((resolve) => setTimeout(resolve, SLOW_MS)).then(saved).then((hit) => hit || network);
    event.respondWith(Promise.race([network, slow]).catch(() => saved().then((hit) => hit || Response.error())));
    return;
  }

  // Google Fonts: a given URL never changes, so use the saved copy once there is one.
  if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
      return res;
    })));
  }
});
