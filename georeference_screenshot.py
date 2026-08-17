#!/usr/bin/env python3
"""
Georeference a browser screenshot of a historical aerial photo from
norgeibilder.no (viewed with the "eiendomsgrenser" / property-boundary
overlay turned on) - producing the same kind of tagged, spatially-located
GeoTIFF and manifest.json entry download_images.py's proper WMS download
would, but using the property boundary's own known real-world coordinates
as ground control points (GCPs) instead of an authenticated API call.

Why this exists
-----------------
download_images.py needs a Norge i Bilder WMS token, which needs a GeoID
account, which needs either Norge digitalt party membership or a paid
data-access agreement (see README.md's "Blocker" section) - neither
available today, and not worth pursuing yet at this project's current,
pre-client, idea-testing stage. But norgeibilder.no's own browser viewer
is free for anyone, no login: pick a historical "prosjekt" (year), turn
on the eiendomsgrenser layer, and the property's exact boundary is drawn
right on the photo. Since property.py already gives us that boundary's
true real-world coordinates exactly (the same source Kartverket's own
viewer draws from), a screenshot the boundary is visible in already
contains everything needed to work out pixel -> real-world coordinates:
pick a handful of points where the boundary line has a recognizable
kink/corner, match each to its known real-world (easting, northing), and
fit a similarity transform (uniform scale + one rotation angle +
translation - see fit_similarity()) relating pixel (col, row) to UTM
(easting, northing) - the same idea as the "world file" technique used
to georeference scanned paper maps for decades, applied here to a
screenshot instead, but deliberately not the further-general 6-parameter
affine that technique usually allows: see fit_similarity()'s own
docstring for why shear/independent-axis-scaling has no physical
meaning here and is excluded from the model entirely, not just
discouraged.

This is deliberately NOT as accurate as a real, properly-served GeoTIFF:
accuracy is limited by how precisely GCP pixels can be clicked (a few
pixels of human error), how zoomed-in the screenshot is (screen
resolution, not the source photo's native resolution, sets the
achievable ground sample distance), and how well-spread the chosen GCPs
are. It's a real, usable approximation for prototyping and client
communication - not for survey-grade work. The "fit" step reports its
own residual error in meters so you know which one you're getting.

The property's boundary here is a genuinely complex shape (221 original
vertices for 123/9 Etnedal - it follows a stream for much of its
length), too many to usefully label all of. "list-vertices" simplifies
it (Douglas-Peucker) down to a manageable set of real corners first.

Workflow (three subcommands)
------------------------------
1. list-vertices: prints and plots a simplified, numbered set of the
   property boundary's corners - a reference diagram to compare against
   the shape of the eiendomsgrenser line in your screenshot, so you can
   identify which vertex index is which visible corner. The four
   cardinal extremes (northmost/southmost/eastmost/westmost) are
   highlighted as usually the easiest to identify unambiguously even at
   low screenshot resolution.

       python3 georeference_screenshot.py list-vertices \\
           --kommune Etnedal --gnr 123 --bnr 9

2. pick: opens an interactive window on your screenshot - run this on
   your own machine, with a display (it needs a GUI backend). Click each
   recognizable boundary corner in turn; each click's pixel (x, y) is
   printed to the console, to copy into the next step alongside the
   vertex index you matched it to from step 1.

       python3 georeference_screenshot.py pick --screenshot photo_2016.png

   (No display available, or prefer not to click-and-copy? Any image
   viewer that shows a pixel/cursor position works too - this step is
   just a convenience.)

3. fit: pairs up vertex indices (from step 1) with pixel coordinates
   (from step 2 or elsewhere), fits the similarity transform (see
   fit_similarity()), reports the fit's residual error in meters, and
   writes a tagged GeoTIFF plus a manifest.json entry in the same
   format download_images.py produces.

       python3 georeference_screenshot.py fit \\
           --kommune Etnedal --gnr 123 --bnr 9 \\
           --screenshot photo_2016.png --year 2016 \\
           --gcp 4:512:300 --gcp 11:800:120 --gcp 27:200:900 --gcp 61:50:400

   (--gcp VERTEX_INDEX:PIXEL_X:PIXEL_Y, repeatable. The code requires at
   least 3, a practical safety margin above the true minimum - a
   similarity transform only has 4 unknowns, so 2 well-separated points
   are actually enough to solve it exactly. That's also why, unlike the
   general 6-parameter affine this project used to fit, even exactly 3
   GCPs now gives a real, meaningful residual error rather than an
   always-zero one - every point beyond the 2 truly needed adds genuine
   redundancy to check against. Still, use 4+, spread around the
   property and not collinear, for a better-constrained fit.)

Requires the same dependencies as download_images.py, plus matplotlib
(list-vertices, pick).
"""

import argparse
import json
import os

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.crs import CRS
from PIL import Image

from property import fetch_property
from download_images import bounds_wgs84, write_boundary_geojson

SIMPLIFY_TOLERANCE_M = 10.0   # default Douglas-Peucker tolerance for list-vertices


def simplified_vertices(polygon, tolerance):
    """Douglas-Peucker-simplified boundary corners, as a list of
    (easting, northing) - the exterior ring's closing duplicate point is
    dropped, so index i is a distinct corner."""
    simplified = polygon.simplify(tolerance, preserve_topology=True)
    return list(simplified.exterior.coords)[:-1]


def cardinal_extremes(coords):
    return {
        "northmost": max(range(len(coords)), key=lambda i: coords[i][1]),
        "southmost": min(range(len(coords)), key=lambda i: coords[i][1]),
        "eastmost": max(range(len(coords)), key=lambda i: coords[i][0]),
        "westmost": min(range(len(coords)), key=lambda i: coords[i][0]),
    }


def _plot_vertices(coords, extremes, prop, tolerance, n_original, out_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    xs = [c[0] for c in coords] + [coords[0][0]]
    ys = [c[1] for c in coords] + [coords[0][1]]
    fig, ax = plt.subplots(figsize=(9, 9))
    ax.plot(xs, ys, "-o", color="#3f5a8a", markersize=4, linewidth=1)
    extreme_idx = set(extremes.values())
    for i, (x, y) in enumerate(coords):
        color = "#b5502a" if i in extreme_idx else "#333333"
        weight = "bold" if i in extreme_idx else "normal"
        ax.annotate(str(i), (x, y), fontsize=7, color=color, fontweight=weight,
                    xytext=(3, 3), textcoords="offset points")
    ax.set_aspect("equal")
    ax.set_xlabel("Easting (m, EPSG:25833)")
    ax.set_ylabel("Northing (m, EPSG:25833)")
    ax.set_title(
        "{}, {}: {} candidate corners (simplified at {:g}m from {} original "
        "boundary points)\nred/bold = cardinal extremes (usually easiest to "
        "identify)".format(
            prop.matrikkelnummer, prop.kommunenavn, len(coords), tolerance, n_original),
        fontsize=10)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def cmd_list_vertices(args):
    prop = fetch_property(args.kommune, args.gnr, args.bnr)
    n_original = len(list(prop.polygon.exterior.coords)) - 1
    coords = simplified_vertices(prop.polygon, args.tolerance)
    extremes = cardinal_extremes(coords)

    print(f"{prop.matrikkelnummer} in {prop.kommunenavn}: {len(coords)} candidate "
          f"corner vertices (simplified at {args.tolerance:g}m tolerance from "
          f"{n_original} original boundary points)\n")
    for i, (x, y) in enumerate(coords):
        tag = ""
        for label, idx in extremes.items():
            if idx == i:
                tag = f"  <- {label}"
        print(f"  {i:3d}  easting={x:10.1f}  northing={y:10.1f}{tag}")

    basename = prop.code
    outdir = args.outdir or basename
    os.makedirs(outdir, exist_ok=True)
    out_path = os.path.join(outdir, f"{basename}_boundary_vertices.png")
    _plot_vertices(coords, extremes, prop, args.tolerance, n_original, out_path)
    print(f"\nSaved reference diagram: {out_path}")
    print("Compare its shape against the eiendomsgrenser line in your screenshot "
          "to identify which vertex index is which visible corner, then use those "
          "indices with the 'fit' subcommand (same --tolerance as used here).")


def cmd_pick(args):
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt

    img = Image.open(args.screenshot)
    fig, ax = plt.subplots(figsize=(12, 9))
    ax.imshow(img)
    ax.set_title(
        "Click each recognizable boundary corner, in any order.\n"
        "Pixel (x, y) of each click prints to the console below - note which "
        "vertex index (from list-vertices) each one is. Close the window when done.")

    def onclick(event):
        if event.xdata is None or event.ydata is None:
            return
        px, py = event.xdata, event.ydata
        print(f"clicked pixel: x={px:.1f}  y={py:.1f}")
        ax.plot(px, py, "+", color="red", markersize=14, markeredgewidth=2)
        fig.canvas.draw()

    fig.canvas.mpl_connect("button_press_event", onclick)
    plt.show()


def parse_gcp(s):
    """'vertex_idx:px:py' -> (vertex_idx, px, py)."""
    parts = s.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            f"--gcp must be VERTEX_INDEX:PIXEL_X:PIXEL_Y, got {s!r}")
    idx, px, py = parts
    try:
        return int(idx), float(px), float(py)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--gcp must be VERTEX_INDEX:PIXEL_X:PIXEL_Y (int:float:float), got {s!r}")


def fit_similarity(gcps_world, gcps_pixel):
    """gcps_world: list of (easting, northing); gcps_pixel: list of
    (pixel_x, pixel_y), same order/length (>=3). Returns (rasterio Affine
    mapping pixel->world, rmse_m, max_error_m, per-point error array).

    Fits a *similarity* transform (uniform scale + one rotation angle +
    translation - 4 real degrees of freedom), not a general 6-parameter
    affine. This is deliberate, not a simplification: the screenshots
    being georeferenced are captures of an already-orthorectified web
    map view (norgeibilder.no serves true orthophotos, and a 2D map
    viewer pans/zooms/[not-]rotates without ever shearing or unevenly
    stretching what it shows) - so the *true* pixel-to-world
    relationship can only ever be a rotation and a uniform scale, never
    shear or independent x/y scaling. An unconstrained 6-parameter
    affine fit can still "explain" GCP correspondence noise by
    inventing a small amount of shear/anisotropic scale that has no
    physical meaning, subtly distorting the photo's true proportions
    even on fits that otherwise look fine - constraining the fit
    to a similarity transform makes that impossible by construction,
    rather than trying to catch it after the fact (which is what
    auto_gcp.py's old isotropy sanity check was papering over - now
    removed as redundant, since every fit from this function is
    isotropic and shear-free by definition, not just usually so).

    The 4 unknowns (p, q, c, f - where p=s*cos(theta), q=s*sin(theta)
    for scale s and rotation theta) are solved by ordinary linear
    least squares, same as the previous 6-parameter version, just with
    the shear/anisotropic-scale degrees of freedom removed from the
    model entirely: easting = p*col + q*row + c,
    northing = q*col - p*row + f (the "-row"/sign pattern accounts for
    pixel row increasing downward while northing increases upward)."""
    n = len(gcps_world)
    if n < 3:
        raise ValueError(f"Need at least 3 GCPs to fit a 2D similarity transform, got {n}")

    A = np.zeros((2 * n, 4))
    b = np.zeros(2 * n)
    for i, ((px, py), (ex, nx)) in enumerate(zip(gcps_pixel, gcps_world)):
        A[2 * i] = [px, py, 1, 0]
        A[2 * i + 1] = [-py, px, 0, 1]
        b[2 * i] = ex
        b[2 * i + 1] = nx

    params, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
    p_, q_, c_, f_ = params
    a_, b_, d_, e_ = p_, q_, q_, -p_
    transform = Affine(a_, b_, c_, d_, e_, f_)

    predicted = np.array([[a_ * px + b_ * py + c_, d_ * px + e_ * py + f_]
                           for px, py in gcps_pixel])
    errors = np.linalg.norm(predicted - np.array(gcps_world), axis=1)
    return transform, float(np.sqrt(np.mean(errors ** 2))), float(errors.max()), errors


def fit_translation_scale(gcps_world, gcps_pixel):
    """Same signature/return shape as fit_similarity(), but constrained
    one step further: uniform scale + translation only, rotation fixed
    at exactly zero (3 real degrees of freedom, not 4). This is the
    *true* north-up model for these screenshots, not an approximation -
    norgeibilder.no's viewer has no rotation control at all, so a real
    capture is always exactly north-up; any nonzero rotation
    fit_similarity() finds is, by definition, either compensating for
    GCP correspondence noise or an outright wrong correspondence, never
    a genuine feature of the screenshot. auto_gcp.py tries this
    constrained fit across its whole seed/epsilon search first, and
    only allows fit_similarity()'s extra rotation degree of freedom as
    a fallback if nothing here verifies - confirmed directly that
    leaving rotation free as the *default* let it silently absorb
    correspondence errors into a plausible-looking but wrong small
    rotation on multiple real screenshots (124/9 Etnedal's 2006/2011/
    2016/1986, fitted at -15.8/-6.6/11.4/-18.6 degrees respectively,
    all visibly wrong against the live boundary despite passing every
    other check) - rotation is a last resort, not a first-class degree
    of freedom to spend on every fit by default.

    The 3 unknowns (s, c, f) are solved by ordinary linear least
    squares: easting = s*col + c, northing = -s*row + f (same
    row-increases-downward sign convention as fit_similarity())."""
    n = len(gcps_world)
    if n < 2:
        raise ValueError(f"Need at least 2 GCPs to fit a 2D translation+scale transform, got {n}")

    A = np.zeros((2 * n, 3))
    b = np.zeros(2 * n)
    for i, ((px, py), (ex, nx)) in enumerate(zip(gcps_pixel, gcps_world)):
        A[2 * i] = [px, 1, 0]
        A[2 * i + 1] = [-py, 0, 1]
        b[2 * i] = ex
        b[2 * i + 1] = nx

    params, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
    s_, c_, f_ = params
    a_, b_, d_, e_ = s_, 0.0, 0.0, -s_
    transform = Affine(a_, b_, c_, d_, e_, f_)

    predicted = np.array([[a_ * px + b_ * py + c_, d_ * px + e_ * py + f_]
                           for px, py in gcps_pixel])
    errors = np.linalg.norm(predicted - np.array(gcps_world), axis=1)
    return transform, float(np.sqrt(np.mean(errors ** 2))), float(errors.max()), errors


def _merge_into_manifest(manifest_path, prop, record, basename):
    if os.path.isfile(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
    else:
        bbox = prop.polygon.bounds
        manifest = {
            "matrikkelnummer": prop.matrikkelnummer,
            "kommune": prop.kommunenavn,
            "kommunenummer": prop.kommunenummer,
            "area_m2": prop.polygon.area,
            "crs": "EPSG:25833",
            "bounds_25833": list(bbox),
            "bounds_wgs84": list(bounds_wgs84(bbox)),
            "boundary_geojson": f"{basename}_boundary.geojson",
            "images": [],
        }
    manifest["images"] = [r for r in manifest["images"] if r.get("year") != record["year"]]
    manifest["images"].append(record)
    manifest["images"].sort(key=lambda r: r["year"])
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)


def cmd_fit(args):
    prop = fetch_property(args.kommune, args.gnr, args.bnr)
    coords = simplified_vertices(prop.polygon, args.tolerance)

    gcps_world, gcps_pixel = [], []
    for idx, px, py in args.gcp:
        if not (0 <= idx < len(coords)):
            raise SystemExit(
                f"vertex index {idx} out of range (0-{len(coords) - 1} at "
                f"tolerance={args.tolerance:g}m) - use the same --tolerance as "
                f"list-vertices, or rerun list-vertices to check current indices")
        gcps_world.append(coords[idx])
        gcps_pixel.append((px, py))

    transform, rmse, max_err, errors = fit_similarity(gcps_world, gcps_pixel)

    print(f"Fitted affine transform from {len(args.gcp)} GCPs:")
    for (idx, px, py), err in zip(args.gcp, errors):
        print(f"  vertex {idx:3d}  pixel=({px:7.1f},{py:7.1f})  residual error = {err:6.2f} m")
    print(f"\nRMSE = {rmse:.2f} m   max error = {max_err:.2f} m")
    if len(args.gcp) == 3:
        print("(exactly 3 GCPs: the fit is mathematically exact through all three, so "
              "this RMSE is ~0 BY CONSTRUCTION - not a real accuracy estimate. Use 4+ "
              "GCPs, well spread around the property, for a meaningful error check.)")

    pixel_size_x = float(np.hypot(transform.a, transform.d))
    pixel_size_y = float(np.hypot(transform.b, transform.e))
    print(f"Implied effective pixel size: {pixel_size_x:.3f} x {pixel_size_y:.3f} m/pixel "
          f"(this screenshot's resolution, not the source photo's native resolution)")

    img = Image.open(args.screenshot).convert("RGB")
    arr = np.array(img)

    basename = prop.code
    outdir = args.outdir or basename
    os.makedirs(outdir, exist_ok=True)
    filename = f"{basename}_{args.year}_screenshot.tif"
    out_path = os.path.join(outdir, filename)

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
            YEAR=str(args.year),
            SOURCE="norgeibilder.no browser viewer (manual screenshot)",
            GEOREFERENCING_METHOD=f"manual GCP affine fit, {len(args.gcp)} points",
            RMSE_M=f"{rmse:.2f}",
            MAX_ERROR_M=f"{max_err:.2f}",
            PIXEL_SIZE_M=f"{pixel_size_x:.3f}x{pixel_size_y:.3f}",
            WARNING="Approximate - georeferenced from a browser screenshot, not the "
                    "source WMS. See georeference_screenshot.py's docstring.",
        )
    print(f"\nSaved {out_path} ({arr.shape[1]}x{arr.shape[0]}px)")

    boundary_path = write_boundary_geojson(prop, outdir, basename)
    manifest_path = os.path.join(outdir, f"{basename}_manifest.json")
    record = {
        "filename": filename,
        "year": args.year,
        "width_px": arr.shape[1],
        "height_px": arr.shape[0],
        "pixel_size_m": [round(pixel_size_x, 3), round(pixel_size_y, 3)],
        "georeferencing_method": "manual_gcp_affine_fit",
        "n_gcp": len(args.gcp),
        "rmse_m": round(rmse, 2),
        "max_error_m": round(max_err, 2),
    }
    _merge_into_manifest(manifest_path, prop, record, basename)
    print(f"Wrote {boundary_path}")
    print(f"Updated {manifest_path}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("list-vertices",
                         help="print/plot candidate boundary corner vertices")
    p1.add_argument("--kommune", required=True)
    p1.add_argument("--gnr", type=int, required=True)
    p1.add_argument("--bnr", type=int, required=True)
    p1.add_argument("--tolerance", type=float, default=SIMPLIFY_TOLERANCE_M,
                     help="Douglas-Peucker simplification tolerance in meters "
                          "(default %(default)g)")
    p1.add_argument("--outdir", default=None,
                     help="default: a folder named after the property itself, "
                          "'<gnr>-<bnr>-<kommune>' (e.g. 123-9-Etnedal)")
    p1.set_defaults(func=cmd_list_vertices)

    p2 = sub.add_parser("pick",
                         help="interactively read off pixel coordinates from a "
                              "screenshot (run on your own machine, needs a display)")
    p2.add_argument("--screenshot", required=True)
    p2.set_defaults(func=cmd_pick)

    p3 = sub.add_parser("fit",
                         help="fit the affine transform from GCPs and write a "
                              "georeferenced GeoTIFF + manifest entry")
    p3.add_argument("--kommune", required=True)
    p3.add_argument("--gnr", type=int, required=True)
    p3.add_argument("--bnr", type=int, required=True)
    p3.add_argument("--tolerance", type=float, default=SIMPLIFY_TOLERANCE_M,
                     help="must match the --tolerance used for list-vertices "
                          "(default %(default)g)")
    p3.add_argument("--screenshot", required=True)
    p3.add_argument("--year", type=int, required=True)
    p3.add_argument("--gcp", action="append", type=parse_gcp, required=True,
                     help="VERTEX_INDEX:PIXEL_X:PIXEL_Y - repeat for each GCP "
                          "(>=3, ideally 4+, spread around the property)")
    p3.add_argument("--outdir", default=None,
                     help="default: a folder named after the property itself, "
                          "'<gnr>-<bnr>-<kommune>' (e.g. 123-9-Etnedal)")
    p3.set_defaults(func=cmd_fit)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
