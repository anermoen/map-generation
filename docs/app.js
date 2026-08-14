// Shows a bundled year of historical aerial photo (see export_web_tiles.py
// / generate_mbtiles.py) with the phone's live GPS position on top - the
// browser-based equivalent of android-app/, avoiding needing Android
// Studio installed at all. Which property to show comes from the
// ?property=<code> URL query param (property_code() naming convention,
// e.g. 14-987-Nittedal) if present and actually bundled, otherwise the
// first property in the registry - see loadPropertiesRegistry().

// Plain OSM streets as a fallback base layer beneath the historical
// overlay - visible wherever the bundled tiles don't cover (zoomed out
// past the property, or the surrounding neighborhood while approaching
// it) and wherever there's signal to fetch it, same trade-off as
// android-app's fallback: it's a live network layer, not bundled, so it
// simply won't load offline - the historical overlay + GPS dot (the
// core, offline-capable feature) don't depend on it either way.
// OpenStreetMap's tile usage policy requires attribution (below) and is
// fine for occasional personal use like this - not appropriate to
// swap in for anything with real traffic.
const OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

let map;
let tileLayer = null;
let manifest = null;
let propertyCode = null;
let locationMarker = null;
let accuracyCircle = null;

function showStatus(message) {
    const banner = document.getElementById("status-banner");
    banner.textContent = message;
    banner.hidden = false;
}

function clearStatus() {
    document.getElementById("status-banner").hidden = true;
}

async function init() {
    // Zoom control moved to bottom-left, not Leaflet's topleft default -
    // the toolbar spans the full width up there and would otherwise sit
    // on top of it (confirmed directly: the "+" button was rendering
    // completely hidden underneath the toolbar, only "-" was usable).
    map = L.map("map", { zoomControl: false, attributionControl: true });
    L.control.zoom({ position: "bottomleft" }).addTo(map);

    L.tileLayer(OSM_TILE_URL, { maxZoom: 19, attribution: OSM_ATTRIBUTION }).addTo(map);

    const registry = await loadPropertiesRegistry();
    if (!registry) return;
    setupPropertySelect(registry);

    startLocationTracking();
    document.getElementById("locate-button").addEventListener("click", recenterOnLocation);
}

async function loadPropertiesRegistry() {
    try {
        const response = await fetch("tiles/properties.json");
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return await response.json();
    } catch (err) {
        showStatus("Could not load the property list - are you offline and haven't visited this page before?");
        return null;
    }
}

function setupPropertySelect(registry) {
    const codes = Object.keys(registry).sort();
    const requested = new URLSearchParams(location.search).get("property");
    const initialCode = codes.includes(requested) ? requested : codes[0];

    const select = document.getElementById("property-select");
    for (const code of codes) {
        const option = document.createElement("option");
        option.value = code;
        option.textContent = registry[code];   // matrikkelnummer, e.g. "14/987"
        select.appendChild(option);
    }
    select.value = initialCode;
    select.addEventListener("change", () => loadProperty(select.value));

    loadProperty(initialCode);
}

async function loadProperty(code) {
    let response;
    try {
        response = await fetch(`tiles/${code}/manifest.json`);
    } catch (err) {
        showStatus(`Could not load imagery for "${code}" - are you offline and haven't visited this page before?`);
        return;
    }
    if (!response.ok) {
        showStatus(`No bundled imagery found for "${code}"`);
        return;
    }
    clearStatus();

    propertyCode = code;
    manifest = await response.json();
    history.replaceState(null, "", `?property=${code}`);

    const yearSelect = document.getElementById("year-select");
    yearSelect.innerHTML = "";
    const years = Object.keys(manifest.years).map(Number).sort((a, b) => a - b);
    for (const year of years) {
        const option = document.createElement("option");
        option.value = String(year);
        option.textContent = String(year);
        yearSelect.appendChild(option);
    }
    const latestYear = years[years.length - 1];
    yearSelect.value = String(latestYear);
    yearSelect.onchange = () => showYear(Number(yearSelect.value), false);

    showYear(latestYear, true);
}

function showYear(year, fitBounds) {
    const info = manifest.years[String(year)];
    if (!info) return;

    if (tileLayer) {
        map.removeLayer(tileLayer);
    }
    tileLayer = L.tileLayer(`tiles/${propertyCode}/${year}/{z}/{x}/{y}.png`, {
        minZoom: info.minzoom,
        maxZoom: info.maxzoom,
        maxNativeZoom: info.maxzoom,
        tileSize: 256,
        attribution: `${manifest.matrikkelnummer}, ${year} – Norge i Bilder`,
    });
    tileLayer.addTo(map);

    // Only fit the view when switching property (or on first load) -
    // switching years within the same property shouldn't yank the map
    // away from wherever the user has since panned/zoomed to while
    // walking around.
    if (fitBounds) {
        const [west, south, east, north] = info.bounds;
        map.fitBounds(L.latLngBounds([south, west], [north, east]), { padding: [20, 20] });
    }
}

function startLocationTracking() {
    if (!("geolocation" in navigator)) {
        showStatus("This browser doesn't support geolocation.");
        return;
    }
    navigator.geolocation.watchPosition(onPosition, onPositionError, {
        enableHighAccuracy: true,
        maximumAge: 2000,
        timeout: 15000,
    });
}

function onPosition(position) {
    const latlng = [position.coords.latitude, position.coords.longitude];
    if (!locationMarker) {
        locationMarker = L.circleMarker(latlng, {
            radius: 8, color: "#1a73e8", weight: 3, fillColor: "#4285f4", fillOpacity: 1,
        }).addTo(map);
        accuracyCircle = L.circle(latlng, {
            radius: position.coords.accuracy, color: "#4285f4", weight: 1, fillOpacity: 0.1,
        }).addTo(map);
    } else {
        locationMarker.setLatLng(latlng);
        accuracyCircle.setLatLng(latlng);
        accuracyCircle.setRadius(position.coords.accuracy);
    }
}

function onPositionError(error) {
    // PERMISSION_DENIED = 1, POSITION_UNAVAILABLE = 2, TIMEOUT = 3
    if (error.code === 1) {
        showStatus("Location access was denied - allow it in the browser's site settings to see your position.");
    } else {
        showStatus(`Location unavailable: ${error.message}`);
    }
}

function recenterOnLocation() {
    if (locationMarker) {
        map.setView(locationMarker.getLatLng(), Math.max(map.getZoom(), 19));
    } else {
        showStatus("Still waiting for a GPS fix…");
    }
}

if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
        navigator.serviceWorker.register("service-worker.js")
            .catch((err) => console.warn("Service worker registration failed:", err));
    });
}

init();
