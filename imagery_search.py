#!/usr/bin/env python3
"""
Find which Norge i Bilder aerial-photo (and satellite-mosaic) "prosjekt"
(flight/imagery projects) actually cover a given property polygon, and
in which years - as opposed to merely having a bounding box that
overlaps it. This distinction matters: for a small rural property,
national flight-block boundaries routinely miss it by anywhere from a
few hundred meters to several kilometers despite an overlapping bbox
(verified directly for 123/9 Etnedal - see README.md's coverage table).

Data source
-----------
Norge i Bilder's project-metadata REST API (open, no authentication) -
shared with Kartverket's Hoydedata (LIDAR) API, same URL shape:

    https://tjenester.norgeibilder.no/rest/projectMetadata.ashx

Two request modes used here:
  1. No 'Projects' filter, ReturnGeometry:false - returns lightweight
     metadata (including a lon/lat bounding box) for *all* ~4000
     national projects in one call. Used as a cheap pre-filter.
  2. 'Projects':'<exact name>', ReturnGeometry:true - returns the full
     coverage polygon (EPSG:25833) plus all metadata for one project.
     Used only for the bbox-pre-filtered candidates, since fetching
     full geometry for all ~4000 projects individually would mean
     ~4000 HTTP round trips.

Note: this metadata API is open, but the actual image-serving WMS
(services.norgeibilder.no) requires an authenticated, IP-bound token
from a GeoID account - see README.md for what that means for the next
step (actually downloading pixels).

Usage
-----
    python3 imagery_search.py                    # uses 123/9 Etnedal
    python3 imagery_search.py <kommune> <gnr> <bnr>

or as a library:

    from imagery_search import find_covering_projects
    from property import fetch_property
    prop = fetch_property("Etnedal", 123, 9)
    projects = find_covering_projects(prop.polygon)
"""

import sys
import urllib.parse
import concurrent.futures
from dataclasses import dataclass

import requests
from pyproj import Transformer
from shapely.geometry import shape

from property import fetch_property

METADATA_URL = "https://tjenester.norgeibilder.no/rest/projectMetadata.ashx"
SATELLITE_ORTOFOTOTYPE = "6"   # Sentinel-2 mosaics etc - ~10m/pixel, not aerial photos


@dataclass
class ImageryProject:
    name: str
    project_id: str
    year: int
    photo_date: str
    pixel_size_m: float
    ortofototype: str
    is_satellite: bool
    is_cir: bool          # colour infrared, rather than true colour
    geometry: object       # shapely geometry, EPSG:25833
    distance_m: float      # 0.0 if it actually covers the property


def _request(params_dict):
    request_str = "{" + ",".join(f"{k}:{v}" for k, v in params_dict.items()) + "}"
    url = METADATA_URL + "?request=" + urllib.parse.quote(request_str)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _bbox_candidates(property_polygon):
    """Cheap first pass: fetch bbox-only metadata for every national
    project and keep those whose lon/lat bbox overlaps the property."""
    data = _request({"ReturnMetadata": "true", "ReturnGeometry": "false"})
    transformer = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)
    minx, miny, maxx, maxy = property_polygon.bounds
    lon_min, lat_min = transformer.transform(minx, miny)
    lon_max, lat_max = transformer.transform(maxx, maxy)

    candidates = []
    for feat in data["ProjectMetadata"]:
        p = feat.get("properties", feat)
        try:
            x_min, x_max = float(p["x_min"]), float(p["x_max"])
            y_min, y_max = float(p["y_min"]), float(p["y_max"])
        except (KeyError, TypeError, ValueError):
            continue
        if x_min <= lon_max and x_max >= lon_min and y_min <= lat_max and y_max >= lat_min:
            candidates.append(p["prosjektnavn"])
    return candidates


def _fetch_geometry(name):
    data = _request({"Projects": f"'{name}'", "ReturnMetadata": "true", "ReturnGeometry": "true"})
    if not data.get("Success") or not data.get("ProjectMetadata"):
        return name, None
    return name, data["ProjectMetadata"][0]


def find_covering_projects(property_polygon, max_workers=12):
    """Returns a list of ImageryProject for every national project whose
    bbox overlaps the property, sorted by year then distance - so
    callers can see both exact matches (distance_m == 0) and how close
    the nearest miss was."""
    candidate_names = _bbox_candidates(property_polygon)

    projects = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for name, feat in ex.map(_fetch_geometry, candidate_names):
            if feat is None:
                continue
            geom = shape(feat["geometry"])
            p = feat["properties"]
            projects.append(ImageryProject(
                name=name,
                project_id=p.get("nib_project_id"),
                year=int(p["aar"]),
                photo_date=p.get("fotodato_date"),
                pixel_size_m=float(p["pixelstorrelse"]),
                ortofototype=p.get("ortofototype"),
                is_satellite=(p.get("ortofototype") == SATELLITE_ORTOFOTOTYPE),
                is_cir="CIR" in name,
                geometry=geom,
                distance_m=geom.distance(property_polygon),
            ))

    projects.sort(key=lambda pr: (pr.year, pr.distance_m))
    return projects


def best_covering_project(projects, year, allow_satellite=False, max_distance_m=0.0):
    """Pick the best project for a given year: covers the property
    (distance <= max_distance_m), preferring non-CIR, highest resolution
    (smallest pixel size). Returns None if nothing qualifies.

    allow_satellite=False (the default) means a year with only a
    Sentinel-2-style satellite mosaic (~10m/pixel) covering the property,
    and no real aerial photo, returns None rather than silently
    substituting the much coarser satellite product - callers that do
    want the satellite fallback for gap years should pass True."""
    candidates = [p for p in projects if p.year == year and p.distance_m <= max_distance_m]
    if not allow_satellite:
        candidates = [p for p in candidates if not p.is_satellite]
    if not candidates:
        return None
    non_cir = [p for p in candidates if not p.is_cir]
    if non_cir:
        candidates = non_cir
    return min(candidates, key=lambda p: p.pixel_size_m)


def main():
    if len(sys.argv) == 4:
        kommune, gnr, bnr = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    elif len(sys.argv) == 1:
        kommune, gnr, bnr = "Etnedal", 123, 9
    else:
        print("Usage: python3 imagery_search.py [<kommune> <gnr> <bnr>]")
        sys.exit(1)

    prop = fetch_property(kommune, gnr, bnr)
    print(f"Property: {prop.matrikkelnummer} in {prop.kommunenavn}, area {prop.polygon.area:,.0f} m^2\n")

    projects = find_covering_projects(prop.polygon)
    for p in projects:
        cov = "*** COVERS ***" if p.distance_m == 0 else f"{p.distance_m:.0f}m away"
        kind = "SATELLITE" if p.is_satellite else ("CIR" if p.is_cir else "aerial")
        print(f"{p.year}  {p.name!r:42s} {p.photo_date}  {p.pixel_size_m:>5g}m/px  "
              f"{kind:9s}  {cov}")


if __name__ == "__main__":
    main()
