#!/usr/bin/env python3
"""
Plot a georeferenced aerial-photo GeoTIFF (from georeference_screenshot.py
or, later, download_images.py) with the property's official cadastral
boundary (from property.py's live WFS fetch, not whatever eiendomsgrenser
line happens to be drawn in the source image) overlaid on top, in real
UTM coordinates - a visual check that a screenshot's georeferencing is
actually correct, and a self-contained image for client communication
(the point of this project's current stage - see README.md's "Status").

Rendering note: the image is drawn using its exact affine transform, not
just axis-aligned imshow(extent=...), so this is correct even if the
transform has rotation or shear (georeference_screenshot.py's GCP fit
can produce either, e.g. from a not-quite-north-up screenshot) - which a
naive extent-only plot would silently render wrong (stretched into the
bounding box instead of properly rotated).

Usage
-----
    python3 plot_overlay.py --kommune Etnedal --gnr 123 --bnr 9 \\
        --image 123-9-Etnedal/123-9-Etnedal_2016_screenshot.tif

or, to look the filename up from an existing manifest.json instead of
naming it directly:

    python3 plot_overlay.py --kommune Etnedal --gnr 123 --bnr 9 --year 2016

or, with neither --image nor --year: batch mode. Scans the property's
folder (default "<gnr>-<bnr>-<kommune>", e.g. 123-9-Etnedal - see
property.py's property_code()) for manually-captured screenshots named
"<gnr>-<bnr>-<kommune>-<year>.png", and:
  - for every year that's already been georeferenced (has an entry in
    manifest.json, from georeference_screenshot.py fit), (re)generates
    its overlay PNG - so dropping in new screenshots and re-fitting them
    is enough to keep every overlay current with one command;
  - for every year that has a raw screenshot but hasn't been fitted yet,
    reports it rather than silently skipping - fitting itself still
    needs ground control points identified for that specific image (see
    georeference_screenshot.py's list-vertices/pick/fit workflow), which
    isn't something this script can do on its own.

    python3 plot_overlay.py --kommune Etnedal --gnr 123 --bnr 9

Output: <image_basename>_overlay.png next to each input image by default.
"""

import argparse
import glob
import json
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.transforms import Affine2D
import rasterio
from rasterio.plot import reshape_as_image

from property import fetch_property, property_code

BOUNDARY_COLOR = "#e63946"


def resolve_image_path(args, outdir, basename):
    if args.image:
        return args.image
    manifest_path = os.path.join(outdir, f"{basename}_manifest.json")
    if not os.path.isfile(manifest_path):
        raise SystemExit(f"{manifest_path} not found - pass --image <path> directly, "
                          f"or run georeference_screenshot.py fit / download_images.py first")
    with open(manifest_path) as f:
        manifest = json.load(f)
    for record in manifest["images"]:
        if record["year"] == args.year:
            return os.path.join(outdir, record["filename"])
    available = [r["year"] for r in manifest["images"]]
    raise SystemExit(f"No image for year {args.year} in {manifest_path} "
                      f"(available years: {available})")


def find_input_screenshots(outdir, gardsnummer, bruksnummer, kommune_navn):
    """Manually-captured screenshots dropped into outdir, named
    "<gnr>-<bnr>-<kommune>-<year>.png" (whatever extension/case - the
    manual capture step doesn't force PNG). Returns {year: filepath},
    for the *raw inputs* - not to be confused with manifest.json's
    entries, which are the already-georeferenced outputs."""
    prefix = property_code(gardsnummer, bruksnummer, kommune_navn)
    pattern = re.compile(r"^" + re.escape(prefix) + r"-(\d+)\.(png|jpg|jpeg|tif|tiff)$",
                          re.IGNORECASE)
    found = {}
    if not os.path.isdir(outdir):
        return found
    for path in glob.glob(os.path.join(outdir, "*")):
        m = pattern.match(os.path.basename(path))
        if m:
            found[int(m.group(1))] = path
    return found


def plot_overlay(image_path, prop, out_path, title=None):
    with rasterio.open(image_path) as ds:
        img = reshape_as_image(ds.read())
        t = ds.transform
        width_px, height_px = ds.width, ds.height

    fig, ax = plt.subplots(figsize=(10, 10))
    im = ax.imshow(img, origin="upper", zorder=1)
    # rasterio's Affine(a,b,c,d,e,f) maps (col,row) -> (x,y) as
    # x = a*col + b*row + c, y = d*col + e*row + f - the same convention
    # matplotlib.transforms.Affine2D's 3x3 matrix uses, so this composes
    # directly onto the image artist (imshow's own pixel-space transform,
    # by default 0..width_px / 0..height_px with row 0 at top) rather than
    # approximating it with an axis-aligned extent=(left,right,bottom,top),
    # which would silently render any rotation/shear wrong.
    pixel_to_world = Affine2D(matrix=[[t.a, t.b, t.c],
                                       [t.d, t.e, t.f],
                                       [0.0, 0.0, 1.0]])
    im.set_transform(pixel_to_world + ax.transData)

    x, y = prop.polygon.exterior.xy
    ax.plot(x, y, color=BOUNDARY_COLOR, linewidth=2,
            label="Eiendomsgrense (matrikkel, live WFS)", zorder=5)
    for interior in prop.polygon.interiors:
        xi, yi = interior.xy
        ax.plot(xi, yi, color=BOUNDARY_COLOR, linewidth=2, zorder=5)

    # Axis limits: the image's own world-space corners, transformed through
    # the same affine - imshow no longer sets these itself once a custom
    # transform overrides its default extent.
    corners_px = [(0, 0), (width_px, 0), (width_px, height_px), (0, height_px)]
    corners_world = [(t.a * px + t.b * py + t.c, t.d * px + t.e * py + t.f)
                      for px, py in corners_px]
    xs = [c[0] for c in corners_world]
    ys = [c[1] for c in corners_world]
    pad = 0.04 * max(max(xs) - min(xs), max(ys) - min(ys))
    ax.set_xlim(min(xs) - pad, max(xs) + pad)
    ax.set_ylim(min(ys) - pad, max(ys) + pad)
    ax.set_aspect("equal")
    ax.set_xlabel("Easting (m, EPSG:25833)")
    ax.set_ylabel("Northing (m, EPSG:25833)")
    ax.legend(loc="upper right", fontsize=9)
    ax.set_title(title or f"{prop.matrikkelnummer}, {prop.kommunenavn}", fontsize=11)
    ax.grid(alpha=0.25, zorder=0)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kommune", default="Etnedal")
    ap.add_argument("--gnr", type=int, default=123)
    ap.add_argument("--bnr", type=int, default=9)
    ap.add_argument("--image", help="path to a georeferenced GeoTIFF")
    ap.add_argument("--year", type=int,
                     help="alternative to --image: look the filename up in manifest.json")
    ap.add_argument("--outdir", default=None,
                     help="default: a folder named after the property itself, "
                          "'<gnr>-<bnr>-<kommune>' (e.g. 123-9-Etnedal)")
    ap.add_argument("--out", help="output PNG path (single-image modes only; "
                                   "default: <image>_overlay.png)")
    args = ap.parse_args()

    basename = property_code(args.gnr, args.bnr, args.kommune)
    outdir = args.outdir or basename

    if args.image or args.year:
        image_path = resolve_image_path(args, outdir, basename)
        prop = fetch_property(args.kommune, args.gnr, args.bnr)
        out_path = args.out or os.path.splitext(image_path)[0] + "_overlay.png"
        plot_overlay(image_path, prop, out_path)
        print(f"Saved {out_path}")
        return

    # Batch/discovery mode: no --image or --year given.
    manifest_path = os.path.join(outdir, f"{basename}_manifest.json")
    fitted_years = {}
    if os.path.isfile(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
        fitted_years = {r["year"]: r["filename"] for r in manifest["images"]}

    input_screenshots = find_input_screenshots(outdir, args.gnr, args.bnr, args.kommune)

    if not fitted_years and not input_screenshots:
        raise SystemExit(
            f"No manifest and no input screenshots found in {outdir!r}. Drop manually-"
            f"captured screenshots in there named '{basename}-<year>.png', or pass "
            f"--image/--year directly.")

    prop = None
    if fitted_years:
        prop = fetch_property(args.kommune, args.gnr, args.bnr)

    print(f"{outdir}: {len(fitted_years)} georeferenced year(s), "
          f"{len(input_screenshots)} raw input screenshot(s) found.")

    for year in sorted(fitted_years):
        image_path = os.path.join(outdir, fitted_years[year])
        out_path = os.path.splitext(image_path)[0] + "_overlay.png"
        plot_overlay(image_path, prop, out_path)
        print(f"  {year}: saved {out_path}")

    not_yet_fitted = sorted(set(input_screenshots) - set(fitted_years))
    if not_yet_fitted:
        print("\nRaw screenshots found with no georeferenced fit yet - run "
              "georeference_screenshot.py's list-vertices/pick/fit workflow for these "
              "before an overlay can be made:")
        for year in not_yet_fitted:
            print(f"  {year}: {input_screenshots[year]}")


if __name__ == "__main__":
    main()
