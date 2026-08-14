// Caches the app shell plus every bundled property's tile PNGs, so the
// viewer keeps working with no signal at all after the first visit -
// the whole point of a field tool for walking a rural property. Which
// properties exist, and the exact tile list per property/year, both
// come from tiles/properties.json and each property's own
// tiles/<code>/manifest.json (see export_web_tiles.py) rather than
// being hardcoded here, so nothing goes stale as properties/years are
// added.

const CACHE_NAME = "aerial-viewer-v2";

const SHELL_URLS = [
    "./",
    "index.html",
    "app.js",
    "style.css",
    "manifest.webmanifest",
    "vendor/leaflet/leaflet.css",
    "vendor/leaflet/leaflet.js",
    "vendor/leaflet/images/marker-icon.png",
    "vendor/leaflet/images/marker-icon-2x.png",
    "vendor/leaflet/images/marker-shadow.png",
    "vendor/leaflet/images/layers.png",
    "vendor/leaflet/images/layers-2x.png",
    "icons/icon-192.png",
    "icons/icon-512.png",
];

async function precacheEverything() {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(SHELL_URLS);

    const registryUrl = "tiles/properties.json";
    const registry = await (await fetch(registryUrl)).json();
    await cache.add(registryUrl);

    for (const propertyCode of Object.keys(registry)) {
        const manifestUrl = `tiles/${propertyCode}/manifest.json`;
        const manifest = await (await fetch(manifestUrl)).json();
        await cache.add(manifestUrl);

        const tileUrls = Object.entries(manifest.years).flatMap(([year, info]) =>
            info.tiles.map((path) => `tiles/${propertyCode}/${year}/${path}`)
        );
        // Cache in small batches rather than one giant addAll() - keeps
        // a single failed tile fetch from aborting the whole precache.
        const batchSize = 25;
        for (let i = 0; i < tileUrls.length; i += batchSize) {
            await Promise.allSettled(
                tileUrls.slice(i, i + batchSize).map((url) => cache.add(url))
            );
        }
    }
}

self.addEventListener("install", (event) => {
    event.waitUntil(precacheEverything().then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
        ).then(() => self.clients.claim())
    );
});

self.addEventListener("fetch", (event) => {
    event.respondWith(
        caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
});
