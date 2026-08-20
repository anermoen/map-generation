// Caches the app shell plus every bundled property's tile PNGs, so the
// viewer keeps working with no signal at all after the first visit -
// the whole point of a field tool for walking a rural property. Which
// properties exist, and the exact tile list per property/year, both
// come from tiles/properties.json and each property's own
// tiles/<code>/manifest.json (see export_web_tiles.py) rather than
// being hardcoded here, so nothing goes stale as properties/years are
// added.

const CACHE_NAME = "aerial-viewer-v9";
// Bumped automatically by run_all.py's push_to_github()/bump_cache_version()
// on every push that actually has something new to deploy - do not bump
// by hand and expect it to stick, and do not rely on editing this file
// directly to invalidate caches. Necessary, not just tidy: this string
// changing is the *only* thing that makes an already-installed browser
// notice service-worker.js is byte-different, install the new version,
// and (via the activate handler below) throw away the old cache and
// re-fetch everything - see the tile-images paragraph just below for
// why that matters even for a URL that never changes.

// URLs that can change between visits - a new property, a new year, an
// app update - and so must never be served from cache while there's a
// network connection available, only as an offline fallback. Confirmed
// this was a real, live bug, not just a theoretical one: adding a
// third property (124/9 Etnedal) after the service worker had already
// cached tiles/properties.json on an earlier visit left it invisible
// to that browser indefinitely - the old cache-first-forever strategy
// had no way to notice the registry had changed, and nothing forces a
// visitor to revisit with a network connection *and* happen to get a
// byte-different service-worker.js at the same time. Tile images
// aren't in this list on purpose: a given year's tile at a given
// z/x/y is *usually* immutable once generated, so those stay
// cache-first for efficiency - no reason to re-fetch hundreds of
// unchanged tiles on every visit. "Usually" is doing real work in that
// sentence, though: confirmed directly that re-fitting an
// *already-published* year with corrected GCPs changes a tile's pixel
// content at that exact same URL (126/64 Etnedal's 2016 tile at
// z17/x69082/y37438 changed across 4 separate commits in one day) -
// isVolatile() has no way to catch that (the URL never changes), which
// is exactly why CACHE_NAME must be bumped on every push instead, not
// just ones that happen to add something new.
function isVolatile(url) {
    return url.endsWith("/properties.json") || url.endsWith("/manifest.json") ||
        url.endsWith("/app.js") || url.endsWith("/style.css");
}

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

// Network-first: try live network, cache the fresh response as it goes
// by (so the offline fallback stays reasonably current too), and only
// fall back to whatever's already cached if the network fetch fails.
// matchOptions is passed through to both the fallback match and the
// cache key, so a caller can e.g. ignore the search string.
//
// {cache: "no-store"} is doing real work here, not just being careful:
// GitHub Pages sends "Cache-Control: max-age=600" on every file it
// serves, including properties.json and service-worker.js itself
// (confirmed directly with curl -I against the live site) - a plain
// fetch() is still subject to the browser's own HTTP cache underneath
// the Service Worker Cache API, so without this, "network-first" could
// silently mean "whatever the browser's HTTP cache still has from the
// last 10 minutes first" instead, quietly reintroducing the same
// staleness this function exists to fix.
async function networkFirst(request, matchOptions) {
    try {
        const response = await fetch(request, { cache: "no-store" });
        const cache = await caches.open(CACHE_NAME);
        cache.put(matchOptions ? request.url.split("?")[0] : request, response.clone());
        return response;
    } catch (err) {
        const cached = await caches.match(request, matchOptions);
        if (cached) return cached;
        throw err;
    }
}

self.addEventListener("fetch", (event) => {
    if (event.request.mode === "navigate") {
        // The URL can carry a ?property=<code> query string (app.js
        // sets it via history.replaceState once a property loads) that
        // changes which property the already-loaded shell shows
        // client-side, not which file gets served - matched ignoring
        // it both ways here. Verified this was a real, live bug on its
        // own: the default property (alphabetically first) sets that
        // query string on the very first load, so an offline reload
        // immediately after missed an exact-match cache lookup and
        // failed instead of serving the cached shell.
        event.respondWith(networkFirst(event.request, { ignoreSearch: true }));
        return;
    }
    if (isVolatile(event.request.url)) {
        event.respondWith(networkFirst(event.request));
        return;
    }
    event.respondWith(
        caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
});
