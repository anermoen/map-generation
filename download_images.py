#!/usr/bin/env python3
"""
Download historical aerial-photo crops for a property from Norge i
Bilder, as georeferenced GeoTIFFs - one per year with confirmed real
coverage (see imagery_search.py / README.md). Each image is cropped
tightly to the property's own extent (no added context margin), and
carries the spatial data needed for two planned follow-on uses:

  a) overlaying these images on hoydedata.no terrain data - the GeoTIFFs
     are written in EPSG:25833 (ETRS89 / UTM 33N), the same CRS
     Kartverket's own terrain products use, with an exact affine
     transform - directly overlayable in QGIS or similar without
     reprojecting.

  b) plotting a live phone GPS position onto an image - phone GPS reports
     WGS84 lat/lon, not UTM, so alongside the GeoTIFFs this script also
     writes boundary.geojson (the property outline in EPSG:4326) and
     manifest.json (per-image bounds in *both* EPSG:25833 and EPSG:4326,
     plus pixel dimensions) - enough for a mobile app to convert a GPS
     fix straight to a pixel coordinate without needing a full
     reprojection library on-device.

*** You need to run this yourself, not in the sandboxed dev environment ***
The image-serving WMS (services.norgeibilder.no) requires an
authenticated, IP-bound token. The metadata/search APIs used by
property.py and imagery_search.py are open and were verified working
from the dev sandbox; this script's actual GetMap calls were NOT
verified end-to-end there for exactly that reason. Run it from your own
machine, with a token generated moments before (see below) - not from a
different network than the one you'll actually download from, since the
token is bound to the requesting IP address.

Getting a token
------------------
1. Log in at https://norgeibilder.no/ with a GeoID account (create one
   there if you don't have one - it's free).
2. Go to https://services.norgeibilder.no/token, generate a token with
   Client = "Request IP" and whatever expiry you like (1 hour is enough
   for one run of this script).
3. Either pass it directly: --token <TOKEN>
   or set it in the environment: export NIB_TOKEN=<TOKEN>

Before downloading anything, verify the layer-naming assumption
--------------------------------------------------------------------
This script assumes a project's WMS layer name equals its `prosjektnavn`
(e.g. "Etnedal 2006") - the natural assumption for a "WMS-Prosjekt"
service, but unconfirmed, since GetCapabilities itself is behind the
same token and couldn't be checked from the dev sandbox. Run with
--list-layers first:

    python3 download_images.py --token <TOKEN> --list-layers

This fetches real GetCapabilities and prints the actual layer names/IDs
available, so you can confirm (or fix, via --layer-field id) the
assumption before batch-downloading.

Usage
-----
    python3 download_images.py --token <TOKEN>
    python3 download_images.py --token <TOKEN> --years 2006 2025
    python3 download_images.py --token <TOKEN> --kommune Etnedal --gnr 123 --bnr 9

Output, in ./123-9-Etnedal/ (a folder named after the property itself,
"<gnr>-<bnr>-<kommune>" - see property.py's property_code() - so the
folder/filenames alone establish which property they're for):
  - "123-9-Etnedal_1958.tif", ...  one GeoTIFF per year, cropped to the
    property's exact bounding box, tagged with photo date/source/etc.
  - "123-9-Etnedal_boundary.geojson"  property outline in EPSG:4326
  - "123-9-Etnedal_manifest.json"     per-image metadata for later use
"""

import argparse
import json
import os
import sys

import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.crs import CRS
from pyproj import Transformer
import requests
from shapely.geometry import mapping

from property import fetch_property
from imagery_search import find_covering_projects, best_covering_project

WMS_PROSJEKT_URL = "https://services.norgeibilder.no/wms/prosjekter"
WMS_CRS = "EPSG:25833"

# Years with confirmed real (not just bbox-overlapping) aerial coverage
# for 123/9 Etnedal, per imagery_search.py - see README.md's coverage
# table for how this was determined, and why 2017-2024 are absent.
DEFAULT_YEARS = [1958, 1970, 1991, 2006, 2011, 2016, 2025]

MAX_DIMENSION_PX = 3000   # cap on WIDTH/HEIGHT per GetMap request

_TO_WGS84 = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)


def list_layers(token):
    """Fetch and print real WMS GetCapabilities - use this to verify
    the layer-naming assumption before downloading anything."""
    params = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetCapabilities",
              "token": token}
    resp = requests.get(WMS_PROSJEKT_URL, params=params, timeout=60)
    resp.raise_for_status()
    print(resp.text)
    print(f"\n({len(resp.text)} characters of GetCapabilities XML above - "
          f"search it for a project name/id you expect, e.g. 'Etnedal 2006', "
          f"to see the exact <Name> the WMS layer uses.)")


def property_bbox(polygon, pad_fraction=0.0):
    """The property's own extent by default (pad_fraction=0.0) - images
    are cropped tightly to the property, not a padded context view. A
    nonzero pad_fraction is available (e.g. for visual inspection) but
    is not what the pipeline uses by default."""
    minx, miny, maxx, maxy = polygon.bounds
    if pad_fraction == 0.0:
        return (minx, miny, maxx, maxy)
    w, h = maxx - minx, maxy - miny
    pad_x, pad_y = w * pad_fraction, h * pad_fraction
    return (minx - pad_x, miny - pad_y, maxx + pad_x, maxy + pad_y)


def bounds_wgs84(bbox_25833):
    minx, miny, maxx, maxy = bbox_25833
    lon_min, lat_min = _TO_WGS84.transform(minx, miny)
    lon_max, lat_max = _TO_WGS84.transform(maxx, maxy)
    return (lon_min, lat_min, lon_max, lat_max)


def fetch_geotiff(project, bbox, out_path, prop, layer_field="name", token=None):
    """Fetch the WMS image and save as a tagged GeoTIFF. Returns
    (width_px, height_px) actually written."""
    minx, miny, maxx, maxy = bbox
    width_m, height_m = maxx - minx, maxy - miny

    px = project.pixel_size_m
    width_px = min(int(round(width_m / px)), MAX_DIMENSION_PX)
    height_px = min(int(round(height_m / px)), MAX_DIMENSION_PX)
    if width_px >= MAX_DIMENSION_PX or height_px >= MAX_DIMENSION_PX:
        print(f"  [note] requested resolution would exceed {MAX_DIMENSION_PX}px; "
              f"capped (effective pixel size is coarser than the source's {px}m)")

    layer_value = project.name if layer_field == "name" else project.project_id
    params = {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
        "LAYERS": layer_value, "STYLES": "",
        "CRS": WMS_CRS, "BBOX": f"{minx},{miny},{maxx},{maxy}",
        "WIDTH": str(width_px), "HEIGHT": str(height_px),
        "FORMAT": "image/png",
        "token": token,
    }

    resp = requests.get(WMS_PROSJEKT_URL, params=params, timeout=120)
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    if "xml" in content_type or "html" in content_type:
        raise RuntimeError(f"WMS returned an error instead of an image:\n{resp.text[:500]}")

    # Build the GeoTIFF ourselves from the request's own bbox/width/height
    # (which we know exactly - it's what we asked the server to render),
    # rather than trusting any georeferencing embedded in the response.
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    arr = np.array(img)   # (H, W, 3)
    transform = from_bounds(minx, miny, maxx, maxy, arr.shape[1], arr.shape[0])
    with rasterio.open(
        out_path, "w", driver="GTiff",
        height=arr.shape[0], width=arr.shape[1], count=3, dtype=arr.dtype,
        crs=CRS.from_epsg(25833), transform=transform,
        compress="lzw", photometric="RGB",
    ) as dst:
        for band in range(3):
            dst.write(arr[:, :, band], band + 1)
        dst.update_tags(
            MATRIKKELNUMMER=prop.matrikkelnummer,
            KOMMUNE=prop.kommunenavn,
            KOMMUNENUMMER=prop.kommunenummer,
            YEAR=str(project.year),
            PHOTO_DATE=project.photo_date,
            SOURCE_PROJECT=project.name,
            SOURCE_PROJECT_ID=str(project.project_id),
            PIXEL_SIZE_M=str(project.pixel_size_m),
            SOURCE="Norge i Bilder (Kartverket / Geovekst)",
        )
    return arr.shape[1], arr.shape[0]


def write_boundary_geojson(prop, outdir, basename):
    """Property outline in EPSG:4326 - the CRS GPS fixes come in, so a
    mobile app can test a live position against the boundary directly."""
    to_wgs84 = _TO_WGS84
    polygon_wgs84 = _reproject_polygon(prop.polygon, to_wgs84)
    feature = {
        "type": "Feature",
        "properties": {
            "matrikkelnummer": prop.matrikkelnummer,
            "kommune": prop.kommunenavn,
            "kommunenummer": prop.kommunenummer,
            "area_m2": prop.polygon.area,
        },
        "geometry": mapping(polygon_wgs84),
    }
    geojson = {"type": "FeatureCollection", "crs": {"type": "name", "properties":
               {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}, "features": [feature]}
    path = os.path.join(outdir, f"{basename}_boundary.geojson")
    with open(path, "w") as f:
        json.dump(geojson, f, indent=2)
    return path


def _reproject_polygon(polygon, transformer):
    from shapely.ops import transform as shapely_transform
    return shapely_transform(lambda x, y: transformer.transform(x, y), polygon)


def write_manifest(prop, records, bbox_25833, outdir, basename):
    manifest = {
        "matrikkelnummer": prop.matrikkelnummer,
        "kommune": prop.kommunenavn,
        "kommunenummer": prop.kommunenummer,
        "area_m2": prop.polygon.area,
        "crs": "EPSG:25833",
        "bounds_25833": bbox_25833,
        "bounds_wgs84": bounds_wgs84(bbox_25833),
        "boundary_geojson": f"{basename}_boundary.geojson",
        "images": records,
    }
    path = os.path.join(outdir, f"{basename}_manifest.json")
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token", default=os.environ.get("NIB_TOKEN"),
                     help="Norge i Bilder auth token (or set NIB_TOKEN env var)")
    ap.add_argument("--kommune", default="Etnedal")
    ap.add_argument("--gnr", type=int, default=123)
    ap.add_argument("--bnr", type=int, default=9)
    ap.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS)
    ap.add_argument("--pad", type=float, default=0.0,
                     help="fractional bbox padding beyond the property's own extent "
                          "(default 0.0: crop tightly to the property)")
    ap.add_argument("--outdir", default=None,
                     help="default: a folder named after the property itself, "
                          "'<gnr>-<bnr>-<kommune>' (e.g. 123-9-Etnedal) - see property.py's "
                          "property_code()")
    ap.add_argument("--layer-field", choices=["name", "id"], default="name",
                     help="what to send as WMS LAYERS - project name or nib_project_id "
                          "(verify with --list-layers first if downloads fail)")
    ap.add_argument("--list-layers", action="store_true",
                     help="print real GetCapabilities and exit (no downloads)")
    args = ap.parse_args()

    if not args.token:
        sys.exit("No token provided. Pass --token <TOKEN> or set NIB_TOKEN. "
                  "See this script's docstring for how to generate one.")

    if args.list_layers:
        list_layers(args.token)
        return

    prop = fetch_property(args.kommune, args.gnr, args.bnr)
    print(f"Property: {prop.matrikkelnummer} in {prop.kommunenavn} "
          f"({prop.polygon.area:,.0f} m^2)")

    bbox = property_bbox(prop.polygon, args.pad)
    projects = find_covering_projects(prop.polygon)

    basename = prop.code
    outdir = args.outdir or basename
    os.makedirs(outdir, exist_ok=True)
    records = []
    for year in args.years:
        project = best_covering_project(projects, year)
        if project is None:
            print(f"{year}: no covering project found - skipping "
                  f"(see imagery_search.py output for near-misses)")
            continue
        filename = f"{basename}_{year}.tif"
        out_path = os.path.join(outdir, filename)
        print(f"{year}: fetching {project.name!r} ({project.pixel_size_m}m/px, "
              f"photo date {project.photo_date}) -> {out_path}")
        try:
            width_px, height_px = fetch_geotiff(
                project, bbox, out_path, prop, layer_field=args.layer_field, token=args.token)
            print(f"  saved {out_path} ({width_px}x{height_px}px)")
            records.append({
                "filename": filename,
                "year": year,
                "photo_date": project.photo_date,
                "source_project": project.name,
                "pixel_size_m": project.pixel_size_m,
                "width_px": width_px,
                "height_px": height_px,
            })
        except Exception as e:
            print(f"  FAILED: {e}")
            print(f"  (try --list-layers to check the exact LAYERS value the "
                  f"server expects for {project.name!r})")

    if records:
        boundary_path = write_boundary_geojson(prop, outdir, basename)
        manifest_path = write_manifest(prop, records, bbox, outdir, basename)
        print(f"\nWrote {boundary_path}")
        print(f"Wrote {manifest_path}")


if __name__ == "__main__":
    main()
