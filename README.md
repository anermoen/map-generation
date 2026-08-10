# Map Generation: GIS + Norge i Bilder Historical Aerial Photos

Property boundaries from Kartverket's cadastre, used to extract
historical aerial photographs of that property from Norge i Bilder,
across as many decades as coverage allows.

## Status

Property boundary lookup and the coverage search are complete and
verified working. Downloading actual image pixels needs a token tied to
a personal GeoID account (see "Blocker" below) - `download_images.py` is
written and its non-network logic (property/project lookup, bbox/
resolution math, gap-year skipping) is verified, but the real WMS
GetMap call could only be confirmed to *reach* the server correctly
(fails with an auth error using a fake token, as expected); it needs a
real token, run from your own machine, to confirm end-to-end.

**Decisions made**: skip 2017-2024 (no dedicated aerial coverage - see
below) rather than substitute the far coarser satellite mosaic; you'll
run the download step yourself with your own GeoID token rather than
handing one to this sandboxed environment.

## `property.py`: cadastral boundary lookup

Fetches a property's exact polygon boundary from Kartverket's open WFS
service ("Matrikkelen - Eiendomskart Teig", no API key needed), given a
kommune name and gnr/bnr:

    python3 property.py Etnedal 123 9

Kommunenummer (the 4-digit code the WFS actually filters on) is resolved
at request time via Kartverket's Kommuneinfo API rather than hardcoded -
these numbers change (Etnedal's changed from 0541 to 3450 in a 2024
nationwide renumbering), so a hardcoded table would eventually go stale
silently.

**Verified for 123/9, Etnedal**: a single Teig, area 645,059 m^2 (~0.65
km^2), bounds (214493.684, 6764447.438, 215277.451, 6765663.722) in
EPSG:25833 (ETRS89 / UTM 33N).

## `imagery_search.py`: which years actually have coverage

Norge i Bilder's project-metadata API (`tjenester.norgeibilder.no`, also
open) lists ~4000 historical and current aerial-photo/satellite
"prosjekt" nationally. Having an overlapping *bounding box* is not the
same as a project's actual (irregular) flown coverage polygon containing
the property - verified directly: several projects whose bbox easily
contains 123/9 miss it by anywhere from a few hundred meters to several
kilometers once you check the real polygon. `imagery_search.py` does the
real check: pre-filters all ~4000 projects by bbox overlap (one API
call), then fetches full geometry only for that candidate set and tests
actual intersection.

**Verified coverage for 123/9, Etnedal, by requested year:**

| Year | Covering project | Photo date | Resolution | Type |
|---|---|---|---|---|
| 1958 | Nordre Etnedal - Aurdal 1958 | 1958-06-05 | 0.2 m | aerial |
| 1970 | Etnedal-Nordre Land 1970 | 1970-08-13 | 0.2 m | aerial |
| 1991 | Bruflat 1991 | 1991-07-05 | 1 m | aerial |
| 2006 | Etnedal 2006 | 2006-06-09 | 0.2 m | aerial |
| 2011 | Østlandet 2011 | 2011-09-16 | 0.4 m | aerial |
| 2016 | Østlandet 2016 (+ CIR variant) | 2016-10-03 | 0.25 m | aerial |
| 2017-2024 | *(none - see below)* | - | - | - |
| 2025 | Innlandet SV 2025 (+ CIR variant) | 2025-06-13 | 0.25 m | aerial |

**2017-2024 has no dedicated aerial-photo project covering this
property** - the nearest are "Valdres 2019/2023" (~3 km away) and
"Innlandet SV 2024" (1.9 km away), all real misses, not bbox artifacts.
The only thing that *does* cover every one of those years is the
"Sentinel-2 Skyfri mosaikk `<year>`" satellite product - 10 m/pixel,
about 40-100x coarser than the aerial photos above. At that resolution
the ~800m x 1200m property is roughly 80x120 pixels: a recognizable
shape, not the kind of detail an aerial photo gives.

This is a real property of Norway's aerial-photo program, not a search
gap: rural, low-population areas like this part of Etnedal are typically
reflown every ~10-15 years historically (and only recently, since the
2020s, on the ~4-year national cycle), not annually - so "one project
per calendar year" was never going to be fully achievable for a specific
year range that includes gap years, regardless of search thoroughness.

## Blocker: downloading actual image pixels

The metadata APIs above (`tjenester.norgeibilder.no`,
`wfs.geonorge.no`, `ws.geonorge.no`) are open - no login needed. The
image-serving WMS (`services.norgeibilder.no`, ArcGIS Server-based) is
not: every endpoint tested (`/wms/prosjekter`, `/wms/ortofoto`, both the
OGC-WMS and native ArcGIS REST paths) returns `"Token Required"` -
confirmed via the ArcGIS REST diagnostic endpoint
(`/arcgis/rest/services/Prosjekter/MapServer?f=json`), not a network/
firewall issue on this end.

Per Kartverket's documentation, a token is generated at
`https://services.norgeibilder.no/token` using a **GeoID account**
(the same login as norgeibilder.no itself), is time-limited (1 hour/
day/week, chosen at generation time), and is **tied to the IP address**
the request comes from (`Client: Request IP`). The older legacy WMS
(`wms.geonorge.no`) uses a different, IP-*allowlist*-based auth scheme
and returned an explicit "TCP/IP address not found" rejection when
tested from here - also not self-servable.

See the bottom of this file for the two open decisions this raises.

## Coordinate systems

- **EPSG:25833** (ETRS89 / UTM zone 33N): the CRS both the cadastre WFS
  and Norge i Bilder's project geometries use natively. All property/
  project polygons in this project's code are in this CRS.
- **EPSG:4326** (WGS84 lon/lat): only used transiently, to compare
  against Norge i Bilder's project bounding boxes (`x_min`/`x_max`/
  `y_min`/`y_max`), which the API returns in lon/lat rather than UTM.

## Files

| File | Purpose |
|---|---|
| `property.py` | Fetch a cadastral property boundary by kommune + gnr/bnr |
| `imagery_search.py` | Find which Norge i Bilder projects actually cover a property, by year |
| `download_images.py` | **Run this yourself** (see below) - fetches one GeoTIFF per confirmed year |

## Running `download_images.py`

This step needs to run from your own machine, not this sandboxed
environment, because the token it needs is bound to the IP address that
requests it:

1. Log in at <https://norgeibilder.no/> with a GeoID account (free to
   create if you don't have one).
2. Generate a token at <https://services.norgeibilder.no/token> -
   Client = "Request IP", expiry 1 hour is plenty for one run.
3. Copy this project folder to your machine (or just these three .py
   files) and install dependencies: `pip install geopandas shapely
   pyproj rasterio Pillow requests`.
4. First, verify the layer-naming assumption (untested from the
   sandbox - see `download_images.py`'s docstring for why):
   `python3 download_images.py --token <TOKEN> --list-layers`
5. Then run the real download:
   `python3 download_images.py --token <TOKEN>`

Output, in `output/`:
- `123-9_Etnedal_<year>.tif` for each of 1958, 1970, 1991, 2006, 2011,
  2016, 2025 - GeoTIFFs (EPSG:25833) cropped tightly to the property's
  own bounding box (no extra context margin), each tagged with the
  photo date, source project, resolution, and property identifiers as
  embedded TIFF metadata (readable via `gdalinfo` or `rasterio`'s
  `.tags()`, so the info travels with the file even if renamed).
- `123-9_Etnedal_boundary.geojson` - the property outline reprojected to
  EPSG:4326 (WGS84 lon/lat), the CRS phone GPS reports in.
- `123-9_Etnedal_manifest.json` - one place with everything the two
  planned follow-on features need: each image's filename/year/photo
  date/resolution, and the shared bounding box in *both* EPSG:25833 and
  EPSG:4326 - so a mobile app can convert a GPS fix to a pixel
  coordinate on any of these images with just an affine transform
  (image bounds + pixel dimensions), no reprojection library required
  on-device.

Why EPSG:25833 for the images: it's the same CRS Kartverket's own
terrain data (hoydedata.no) uses, and the one the cadastre/imagery APIs
already return geometry in - so overlaying these GeoTIFFs on hoydedata.no
data later shouldn't need reprojection either.
