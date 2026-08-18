#!/usr/bin/env python3
"""
Unpack each year's already-built MBTiles (from generate_mbtiles.py) into
a plain static tile directory - "<z>/<x>/<y>.png" files, the one format
GitHub Pages (or any other bare static file host - no server-side code
runs there) can actually serve. MBTiles itself is a single SQLite file,
which needs *something* running to read it and answer individual tile
requests (that's exactly what the Android app's osmdroid tile provider
does) - there's no server here to do that job, so this script does the
equivalent unpacking once, ahead of time, instead.

Deliberately does no reprojection/warping of its own - generate_mbtiles.py
already did that (GeoTIFF -> Web Mercator tile pyramid) and is the one
place that logic lives; this script only re-shapes its *output* (a
SQLite table of already-rendered tile PNGs) into a directory tree, plus
flips each tile's row back from MBTiles' TMS convention to the XYZ
convention plain web tile URLs (and Leaflet) expect - the same flip
generate_mbtiles.py applied going the other way, self-inverse.

Usage
-----
    python3 export_web_tiles.py --kommune Etnedal --gnr 123 --bnr 9
    # -> docs/tiles/<property_code>/<year>/<z>/<x>/<y>.png for every
    #    <property_code>_<year>.mbtiles found in the property's output
    #    folder, plus docs/tiles/<property_code>/manifest.json (years,
    #    zoom range, bounds - what the web app needs to build the year
    #    switcher and center the map, without guessing)
"""

import argparse
import glob
import json
import os
import sqlite3

from property import fetch_property

WEB_ROOT = "docs"


def _read_mbtiles(path):
    """Returns (metadata_dict, [(zoom, x, y_xyz, png_bytes), ...])."""
    conn = sqlite3.connect(path)
    metadata = dict(conn.execute("SELECT name, value FROM metadata"))
    tiles = []
    for zoom, x, tms_y, data in conn.execute(
            "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"):
        xyz_y = (1 << zoom) - 1 - tms_y   # MBTiles (TMS) -> plain web (XYZ) row order
        tiles.append((zoom, x, xyz_y, data))
    conn.close()
    return metadata, tiles


def export_year(mbtiles_path, dest_dir):
    """Unpacks one year's MBTiles into dest_dir/<z>/<x>/<y>.png. Returns
    (bounds_4326, minzoom, maxzoom, tile_paths) from its metadata -
    tile_paths is every "<z>/<x>/<y>.png" written, relative to dest_dir,
    so a service worker can precache the exact list for offline use
    without having to guess which x/y tiles exist at a given zoom."""
    metadata, tiles = _read_mbtiles(mbtiles_path)

    tile_paths = []
    for zoom, x, y, data in tiles:
        tile_dir = os.path.join(dest_dir, str(zoom), str(x))
        os.makedirs(tile_dir, exist_ok=True)
        with open(os.path.join(tile_dir, f"{y}.png"), "wb") as f:
            f.write(data)
        tile_paths.append(f"{zoom}/{x}/{y}.png")

    bounds = [float(v) for v in metadata["bounds"].split(",")]
    return bounds, int(metadata["minzoom"]), int(metadata["maxzoom"]), tile_paths


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kommune", required=True)
    ap.add_argument("--gnr", type=int, required=True)
    ap.add_argument("--bnr", type=int, required=True)
    ap.add_argument("--outdir", default=None,
                     help="where generate_mbtiles.py's output lives (default: the property's own folder)")
    ap.add_argument("--years", type=int, nargs="+", default=None,
                     help="only these years (default: every <property>_<year>.mbtiles found)")
    args = ap.parse_args()

    prop = fetch_property(args.kommune, args.gnr, args.bnr)
    basename = prop.code
    outdir = args.outdir or basename

    mbtiles_paths = sorted(glob.glob(os.path.join(outdir, f"{basename}_*.mbtiles")))
    if args.years:
        mbtiles_paths = [p for p in mbtiles_paths
                          if int(os.path.splitext(p)[0].rsplit("_", 1)[1]) in args.years]

    if not mbtiles_paths:
        raise SystemExit(f"no {basename}_<year>.mbtiles found in {outdir} - run generate_mbtiles.py first")

    web_dir = os.path.join(WEB_ROOT, "tiles", basename)
    manifest = {"matrikkelnummer": prop.matrikkelnummer, "years": {}}

    for path in mbtiles_paths:
        year = int(os.path.splitext(path)[0].rsplit("_", 1)[1])
        dest_dir = os.path.join(web_dir, str(year))
        bounds, minzoom, maxzoom, tile_paths = export_year(path, dest_dir)
        manifest["years"][str(year)] = {
            "bounds": bounds, "minzoom": minzoom, "maxzoom": maxzoom, "tiles": tile_paths,
        }
        print(f"  {year}: {len(tile_paths)} tiles -> {dest_dir}/")

    manifest_path = os.path.join(web_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Saved {manifest_path}")

    _update_properties_registry(basename, prop.matrikkelnummer, prop.kommunenavn)


def _update_properties_registry(basename, matrikkelnummer, kommunenavn):
    """docs/tiles/properties.json - every property export_web_tiles.py
    has ever exported, {code: {matrikkelnummer, kommunenavn}}. The one
    thing the web app can't discover on its own (a static site has no
    directory listing) - this is what lets it offer a property switcher
    instead of only ever showing whichever one is hardcoded as the
    default. kommunenavn is included alongside matrikkelnummer (not just
    the gnr/bnr) so the dropdown can show which kommune a gnr/bnr
    belongs to - gnr/bnr alone isn't unique across kommuner, so this
    matters as soon as there's more than one Etnedal-only bundle."""
    registry_path = os.path.join(WEB_ROOT, "tiles", "properties.json")
    registry = {}
    if os.path.isfile(registry_path):
        with open(registry_path) as f:
            registry = json.load(f)
    registry[basename] = {"matrikkelnummer": matrikkelnummer, "kommunenavn": kommunenavn}
    with open(registry_path, "w") as f:
        json.dump(registry, f, indent=2)
    print(f"Saved {registry_path}")


if __name__ == "__main__":
    main()
