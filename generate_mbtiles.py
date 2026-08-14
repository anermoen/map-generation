#!/usr/bin/env python3
"""
Package each year's georeferenced aerial-photo GeoTIFF (from
auto_gcp.py / georeference_screenshot.py, EPSG:25833) into an MBTiles
file - a single SQLite database holding a Web Mercator (EPSG:3857)
raster tile pyramid, the standard offline format most mobile mapping
libraries (including osmdroid, the Android library this project's
"view yourself on historical aerial photos" phase is built on) can load
directly with no server involved.

Why write this instead of using GDAL's gdal2tiles.py / rio-mbtiles: this
sandbox has rasterio (which bundles its own GDAL internally) but no
standalone GDAL CLI, and rio-mbtiles pulls in a shapely version that
fails to build here (no system GEOS). Tile generation itself is simple
enough - reproject each destination tile's exact bounds out of the
source GeoTIFF and PNG-encode it - to do directly with rasterio.warp +
mercantile + Pillow + the stdlib's sqlite3, with no extra system
dependencies.

Per-tile transparency: the source GeoTIFF is the original rectangular
screenshot capture, north-up only to within a few degrees (see
auto_gcp.py's rotation tolerance) - so a destination tile's
axis-aligned square doesn't exactly match the source rectangle's own
(slightly rotated) footprint, especially at its edges. Each tile's alpha
channel is built by reprojecting a constant-255 array through the same
warp as the RGB bands: pixels that came from real source coverage end
up opaque, pixels outside the source footprint stay at the destination
array's zero-fill, i.e. transparent - so a year's tiles overlay cleanly
without a visible rectangular seam.

Zoom range: chosen from each GeoTIFF's own native pixel size
(pixel_size_m in manifest.json) via the standard Web Mercator
resolution formula, so a highly-zoomed-in screenshot (Nittedal,
~0.08 m/px) gets a deeper zoom range than a zoomed-out one (Etnedal,
~1.3 m/px) - there's no point generating tiles finer than the source
photo actually resolves.

Usage
-----
    python3 generate_mbtiles.py --kommune Etnedal --gnr 123 --bnr 9
    # -> one <property_code>_<year>.mbtiles per already-georeferenced
    #    year found in manifest.json, in the property's output folder

    python3 generate_mbtiles.py --kommune Etnedal --gnr 123 --bnr 9 --years 2025
    # -> just that year

or as a library:

    from generate_mbtiles import build_mbtiles
    build_mbtiles(tif_path, mbtiles_path)
"""

import argparse
import json
import math
import os
import sqlite3

import mercantile
import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling, transform_bounds
from rasterio.transform import from_bounds
from PIL import Image

from property import fetch_property, property_code

TILE_SIZE = 256
WEB_MERCATOR = "EPSG:3857"
LONLAT = "EPSG:4326"
EARTH_CIRCUMFERENCE_M = 156543.03392804097   # meters/pixel at zoom 0, equator (2*pi*R / 256)

ZOOM_LEVELS_BELOW_MAX = 6   # how many zoom-out steps to generate below each GeoTIFF's own native-resolution zoom
MIN_ZOOM_FLOOR = 10         # never generate below this - no benefit for a single small property
MAX_ZOOM_CEILING = 22       # sanity cap


def _native_max_zoom(pixel_size_m, latitude_deg):
    """Smallest zoom level whose tile resolution is at least as fine as
    the source GeoTIFF's own native pixel size, at this property's
    latitude (Web Mercator resolution varies with latitude - the same
    zoom level is coarser in meters/pixel further from the equator)."""
    lat_rad = math.radians(latitude_deg)
    ideal = math.log2((EARTH_CIRCUMFERENCE_M * math.cos(lat_rad)) / pixel_size_m)
    return min(MAX_ZOOM_CEILING, max(MIN_ZOOM_FLOOR, math.ceil(ideal)))


def _tile_alpha_and_rgb(src, dst_transform, dst_shape):
    """Reproject src's RGB bands and a synthetic constant-255 coverage
    band into one destination tile array, both through the identical
    warp - see module docstring's transparency note. Returns an
    (H, W, 4) uint8 RGBA array, or None if nothing from src actually
    landed in this tile (fully outside src's real footprint - the
    tile-listing step only guarantees bbox overlap, not true coverage
    for a rotated source rectangle)."""
    height, width = dst_shape
    dst_rgb = np.zeros((3, height, width), dtype=np.uint8)
    reproject(
        source=rasterio.band(src, [1, 2, 3]),
        destination=dst_rgb,
        src_transform=src.transform, src_crs=src.crs,
        dst_transform=dst_transform, dst_crs=WEB_MERCATOR,
        resampling=Resampling.bilinear,
    )

    src_alpha = np.full((src.height, src.width), 255, dtype=np.uint8)
    dst_alpha = np.zeros((height, width), dtype=np.uint8)
    reproject(
        source=src_alpha,
        destination=dst_alpha,
        src_transform=src.transform, src_crs=src.crs,
        dst_transform=dst_transform, dst_crs=WEB_MERCATOR,
        resampling=Resampling.bilinear,
    )

    if not dst_alpha.any():
        return None
    return np.dstack([dst_rgb[0], dst_rgb[1], dst_rgb[2], dst_alpha])


def _write_metadata(conn, name, bounds_4326, minzoom, maxzoom):
    west, south, east, north = bounds_4326
    rows = [
        ("name", name),
        ("format", "png"),
        ("type", "overlay"),
        ("version", "1.0"),
        ("description", f"{name} historical aerial photo overlay"),
        ("bounds", f"{west:.6f},{south:.6f},{east:.6f},{north:.6f}"),
        ("minzoom", str(minzoom)),
        ("maxzoom", str(maxzoom)),
    ]
    conn.executemany("INSERT INTO metadata (name, value) VALUES (?, ?)", rows)


def build_mbtiles(tif_path, mbtiles_path, name=None, zoom_levels_below_max=ZOOM_LEVELS_BELOW_MAX):
    """Reproject tif_path (any CRS) into a Web Mercator tile pyramid and
    write it as an MBTiles file at mbtiles_path (overwritten if it
    already exists). Returns (minzoom, maxzoom, tile_count)."""
    if os.path.exists(mbtiles_path):
        os.remove(mbtiles_path)

    with rasterio.open(tif_path) as src:
        bounds_4326 = transform_bounds(src.crs, LONLAT, *src.bounds)
        west, south, east, north = bounds_4326
        center_lat = (south + north) / 2

        pixel_size_m = float(np.hypot(src.transform.a, src.transform.d) +
                              np.hypot(src.transform.b, src.transform.e)) / 2
        max_zoom = _native_max_zoom(pixel_size_m, center_lat)
        min_zoom = max(MIN_ZOOM_FLOOR, max_zoom - zoom_levels_below_max)

        conn = sqlite3.connect(mbtiles_path)
        conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
                     "tile_row INTEGER, tile_data BLOB)")
        conn.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
        _write_metadata(conn, name or os.path.basename(mbtiles_path), bounds_4326, min_zoom, max_zoom)

        tile_count = 0
        for zoom in range(min_zoom, max_zoom + 1):
            for tile in mercantile.tiles(west, south, east, north, [zoom]):
                left, bottom, right, top = mercantile.xy_bounds(tile)
                dst_transform = from_bounds(left, bottom, right, top, TILE_SIZE, TILE_SIZE)
                rgba = _tile_alpha_and_rgb(src, dst_transform, (TILE_SIZE, TILE_SIZE))
                if rgba is None:
                    continue

                png_bytes = _encode_png(rgba)
                tms_row = (1 << zoom) - 1 - tile.y   # MBTiles uses TMS row order (flipped from XYZ)
                conn.execute(
                    "INSERT INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
                    "VALUES (?, ?, ?, ?)",
                    (zoom, tile.x, tms_row, png_bytes))
                tile_count += 1

        conn.commit()
        conn.close()

    return min_zoom, max_zoom, tile_count


def _encode_png(rgba):
    import io
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kommune", required=True)
    ap.add_argument("--gnr", type=int, required=True)
    ap.add_argument("--bnr", type=int, required=True)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--years", type=int, nargs="+", default=None,
                     help="only these years (default: every year in manifest.json)")
    args = ap.parse_args()

    prop = fetch_property(args.kommune, args.gnr, args.bnr)
    basename = prop.code
    outdir = args.outdir or basename

    manifest_path = os.path.join(outdir, f"{basename}_manifest.json")
    if not os.path.isfile(manifest_path):
        raise SystemExit(f"{manifest_path} not found - run auto_gcp.py first")
    with open(manifest_path) as f:
        manifest = json.load(f)

    records = manifest["images"]
    if args.years:
        records = [r for r in records if r["year"] in args.years]
        missing = set(args.years) - {r["year"] for r in records}
        if missing:
            raise SystemExit(f"year(s) {sorted(missing)} not in {manifest_path}")

    if not records:
        print("Nothing to do - no georeferenced years found.")
        return

    for r in sorted(records, key=lambda r: r["year"]):
        tif_path = os.path.join(outdir, r["filename"])
        mbtiles_path = os.path.join(outdir, f"{basename}_{r['year']}.mbtiles")
        min_zoom, max_zoom, tile_count = build_mbtiles(
            tif_path, mbtiles_path, name=f"{prop.matrikkelnummer} {r['year']}")
        print(f"  {r['year']}: {tile_count} tiles, zoom {min_zoom}-{max_zoom} -> {mbtiles_path}")


if __name__ == "__main__":
    main()
