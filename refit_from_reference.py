#!/usr/bin/env python3
"""
Re-fit years whose auto_gcp.py registration is wrong by matching each raw
screenshot directly against a *known-good* reference year's already-
georeferenced GeoTIFF, instead of re-running the boundary-color/ICP
pipeline that produced the bad fit in the first place.

Motivating case: 151-39-Etnedal's 2023 screenshot fit essentially
perfectly (RMSE 0.06m, visually confirmed against the live boundary),
while every other year fit with RMSE 2.3-4.5m against a pixel size of
~0.04-0.06m/px (i.e. 40-100 pixels of error) - passing auto_gcp.py's
verify_registration() only because its RMSE check has a permissive flat
5m floor (needed elsewhere for legitimately coarse screenshots), not
because the fit is actually good. Visual inspection of the overlays
confirmed it: the red WFS boundary sits rotated/shifted off the actual
house in 2016/2022/2025, but lands exactly on it in 2023. This is the
"self-consistent but wrong ICP convergence" failure mode documented in
auto_gcp.py's own module docstring - the boundary-color contour search
locked onto a plausible-looking wrong correspondence for the tightly-
cropped years, most likely aliasing against a similarly-shaped
neighboring parcel.

Why matching against a reference year sidesteps that: every screenshot
of a property shows the same physical ground (buildings, driveways,
roads) regardless of year - so if one year's registration is
independently trustworthy, SIFT keypoints matched between it and another
year's raw screenshot, converted from the reference's own pixel
coordinates into world coordinates via its known-good transform, become
ground control points for the target year. This is a completely
different source of correspondence than auto_gcp.py's boundary-color
contour, so it isn't subject to the same failure mode - and typically
yields many more, better-spread GCPs than the 4-6 boundary corners
auto_gcp.py has to work with.

Bootstrapping across multiple references: a decades-old, low-contrast, or
differently-graded screenshot can share too little visual texture with a
single distant reference (color 2023 vs. grayscale 1958, say) for SIFT to
find enough confident matches, even though the same year would match
just fine against a photometrically closer one - 2011 has far more in
common with 2016 (5 years apart, same vegetation/building state) than
with 2023 (12 years apart). So every already-fit year (the seed
--reference-year(s) plus every year this script itself successfully
re-fits) is added to a growing pool of usable references: each
still-unfit year is tried against every current reference, closest-year-
first, and the moment any one re-fit passes verify_registration it joins
the pool too - repeated in passes until a full pass makes no further
progress. This lets a single confirmed-good year cascade outward through
however many nearby years it can reach, rather than every year having to
independently match the one original reference.

How it works
------------
1. Load the reference year's GeoTIFF (already fit, already verified
   good) and one target year's raw screenshot.
2. SIFT-detect and match keypoints between them (ratio test to drop
   ambiguous matches).
3. Convert every matched reference-image keypoint to world coordinates
   via the reference GeoTIFF's own transform.
4. cv2.estimateAffinePartial2D (isotropic similarity, RANSAC) directly
   between target-image pixel coordinates and those world coordinates -
   both outlier-rejects the many wrong SIFT matches (mismatched trees,
   repeated roof lines, etc.) and gives a first-pass pixel->world
   transform in one step, with an inlier threshold in real meters.
5. Re-fit properly from just the RANSAC inliers using
   georeference_screenshot.py's own fit_translation_scale (zero
   rotation - the true model for this project, tried first) or
   fit_similarity (rotation allowed - fallback), then run auto_gcp.py's
   verify_registration() sanity checks before trusting it.

Usage
-----
    python3 refit_from_reference.py --kommune Etnedal --gnr 151 --bnr 39 \\
        --reference-year 2023
    # -> every other raw screenshot found, matched against the 2023
    #    GeoTIFF (growing to include every year this run itself re-fits -
    #    see "Bootstrapping" above); overwrites the .tif and
    #    manifest.json entry for any year whose new fit passes
    #    verify_registration

    python3 refit_from_reference.py --kommune Etnedal --gnr 151 --bnr 39 \\
        --reference-year 2023 2006 2016 2022 2025
    # -> seed the pool with several already-confirmed-good years at once
    #    (e.g. from a prior run) instead of growing it from a single one

    python3 refit_from_reference.py --kommune Etnedal --gnr 151 --bnr 39 \\
        --reference-year 2023 --years 2016 2022 --dry-run
    # -> just these years, print the result without writing anything

    python3 refit_from_reference.py --kommune Etnedal --gnr 151 --bnr 39 \\
        --reference-year 2023 --push
    # -> re-fit, then regenerate every downstream artifact that depends on
    #    the georeferencing (plot_overlay.py, generate_report.py,
    #    generate_mbtiles.py, export_web_tiles.py - the same steps
    #    run_all.py chains, run via the property's full year set since
    #    those tools operate on the whole property, not one year at a
    #    time) and push docs/ to GitHub, same opt-in-only behavior as
    #    run_all.py --push - skipped entirely if nothing was re-fit
    #    (dry runs, or every candidate year failed verify_registration).
"""

import argparse
import json
import os

import cv2
import numpy as np
import rasterio
from rasterio.crs import CRS
from PIL import Image

from property import fetch_property
from plot_overlay import find_input_screenshots
from georeference_screenshot import fit_similarity, fit_translation_scale, _merge_into_manifest
from auto_gcp import verify_registration
from run_all import run_step, push_to_github

SIFT_N_FEATURES = 8000
RATIO_TEST = 0.85
RANSAC_THRESHOLD_M = 2.0     # inlier gate, in real-world meters (dst space)
RANSAC_CONFIDENCE = 0.999
MIN_INLIERS = 8


def _sift_keypoints(gray_img):
    sift = cv2.SIFT_create(nfeatures=SIFT_N_FEATURES)
    return sift.detectAndCompute(gray_img, None)


def load_reference(outdir, basename, year):
    """A reference is (transform, gray, kp, des) for an already-fit
    year's GeoTIFF - loaded once (whether it's a seed --reference-year
    or a year this script just re-fit itself) and reused for every
    still-unfit year that tries to match against it."""
    path = os.path.join(outdir, f"{basename}_{year}_screenshot.tif")
    if not os.path.isfile(path):
        raise SystemExit(f"Reference GeoTIFF not found: {path} - fit --reference-year first.")
    with rasterio.open(path) as ds:
        transform = ds.transform
        rgb = np.transpose(ds.read(), (1, 2, 0))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    kp, des = _sift_keypoints(gray)
    return transform, gray, kp, des


def _match_ratio_test(des_query, des_train):
    """BFMatcher + Lowe's ratio test between two descriptor sets. Returns
    a list of (query_idx, train_idx) index pairs - empty if either side
    has too few keypoints to match at all."""
    if des_query is None or des_train is None or len(des_query) < 2 or len(des_train) < 2:
        return []
    bf = cv2.BFMatcher(cv2.NORM_L2)
    raw_matches = bf.knnMatch(des_query, des_train, k=2)
    return [(m.queryIdx, m.trainIdx) for m, n in raw_matches if m.distance < RATIO_TEST * n.distance]


def fit_against_reference(prop, reference, target_gray, target_kp, target_des):
    """reference = (transform, gray, kp, des) from load_reference().
    target_kp/target_des/target_gray are precomputed once per target year
    (see main()) and reused across every reference it's tried against, so
    re-fitting against a growing reference pool doesn't mean re-running
    SIFT detection on the same target image over and over.

    Returns (transform, rmse, max_err, gcps_world, gcps_pixel, reasons) -
    reasons is empty iff the fit passed verify_registration."""
    ref_transform, ref_gray, ref_kp, ref_des = reference

    pairs = _match_ratio_test(target_des, ref_des)
    if len(pairs) < MIN_INLIERS:
        return None, None, None, None, None, ["too few ratio-tested SIFT matches against the reference"]

    target_pts = np.array([target_kp[i].pt for i, j in pairs], dtype=np.float64)
    ref_pts = np.array([ref_kp[j].pt for i, j in pairs], dtype=np.float64)
    world_pts = np.array([ref_transform * tuple(p) for p in ref_pts], dtype=np.float64)

    M, inlier_mask = cv2.estimateAffinePartial2D(
        target_pts, world_pts, method=cv2.RANSAC,
        ransacReprojThreshold=RANSAC_THRESHOLD_M, confidence=RANSAC_CONFIDENCE, maxIters=5000)
    if M is None or inlier_mask is None:
        return None, None, None, None, None, ["RANSAC could not find a consistent similarity transform"]

    inliers = inlier_mask.ravel().astype(bool)
    n_inliers = int(inliers.sum())
    if n_inliers < MIN_INLIERS:
        return None, None, None, None, None, [f"only {n_inliers} RANSAC inlier(s), need >= {MIN_INLIERS}"]

    gcps_world = [tuple(p) for p in world_pts[inliers]]
    gcps_pixel = [tuple(p) for p in target_pts[inliers]]

    for fit_fn in (fit_translation_scale, fit_similarity):
        transform, rmse, max_err, errors = fit_fn(gcps_world, gcps_pixel)
        ok, reasons, pixel_size = verify_registration(
            transform, rmse, target_gray.shape, prop.polygon, gcps_pixel=gcps_pixel)
        if ok:
            return transform, rmse, max_err, gcps_world, gcps_pixel, []
    return transform, rmse, max_err, gcps_world, gcps_pixel, reasons


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kommune", required=True)
    ap.add_argument("--gnr", type=int, required=True)
    ap.add_argument("--bnr", type=int, required=True)
    ap.add_argument("--reference-year", type=int, nargs="+", required=True, dest="reference_years",
                     help="one or more years already in manifest.json whose fit is independently "
                          "confirmed good - the seed pool. Every year this script itself successfully "
                          "re-fits joins the pool too, so a farther/harder year can chain off a nearer "
                          "one instead of only ever matching the original seed(s) (see module docstring's "
                          "\"Bootstrapping\" section)")
    ap.add_argument("--years", type=int, nargs="+", default=None,
                     help="only these years (default: every raw screenshot found except the reference)")
    ap.add_argument("--dry-run", action="store_true", help="print results, write nothing")
    ap.add_argument("--push", action="store_true",
                     help="after re-fitting, regenerate every downstream artifact (overlay/report/"
                          "mbtiles/web tiles) and commit+push docs/ to GitHub - same opt-in-only "
                          "behavior as run_all.py --push (see module docstring)")
    args = ap.parse_args()
    if args.push and args.dry_run:
        raise SystemExit("--push and --dry-run together don't make sense - --dry-run writes nothing "
                          "to push.")

    prop = fetch_property(args.kommune, args.gnr, args.bnr)
    basename = prop.code
    outdir = basename

    input_screenshots = find_input_screenshots(outdir, args.gnr, args.bnr, args.kommune)

    references = {}
    for ref_year in args.reference_years:
        references[ref_year] = load_reference(outdir, basename, ref_year)
        print(f"Reference {ref_year}: {len(references[ref_year][2])} SIFT keypoints")

    manifest_path = os.path.join(outdir, f"{basename}_manifest.json")
    old_records = {}
    if os.path.isfile(manifest_path):
        with open(manifest_path) as f:
            old_records = {r["year"]: r for r in json.load(f)["images"]}

    years = args.years or sorted(y for y in input_screenshots if y not in references)
    print(f"Re-fitting {len(years)} year(s), seeded from {sorted(references)}: {years}\n")

    succeeded, failed = [], []
    pending = []
    for year in years:
        if year in references:
            print(f"  {year}: already a reference year - skipping")
        elif year not in input_screenshots:
            print(f"  {year}: no raw screenshot found - skipping")
            failed.append(year)
        else:
            pending.append(year)

    target_cache = {}   # year -> (img, gray, kp, des), computed once even if tried against several refs

    def get_target(year):
        if year not in target_cache:
            img = np.array(Image.open(input_screenshots[year]).convert("RGB"))
            gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            kp, des = _sift_keypoints(gray)
            target_cache[year] = (img, gray, kp, des)
        return target_cache[year]

    # Greedy frontier expansion: each pass, try every still-pending year
    # against every current reference (closest year first - the whole
    # point of bootstrapping, see module docstring), and any success
    # immediately joins the reference pool for the *next* pass - so a
    # year that only 2016 (say) could reach, not the original 2023 seed,
    # still gets picked up once 2016 itself is fit. Repeat until a full
    # pass makes no further progress at all.
    progress = True
    while pending and progress:
        progress = False
        pending.sort(key=lambda y: min(abs(y - r) for r in references))
        still_pending = []
        for year in pending:
            img, gray, kp, des = get_target(year)
            ordered_refs = sorted(references, key=lambda r: abs(r - year))

            result = None
            used_ref = None
            attempts = []
            for ref_year in ordered_refs:
                transform, rmse, max_err, gcps_world, gcps_pixel, reasons = fit_against_reference(
                    prop, references[ref_year], gray, kp, des)
                attempts.append((ref_year, reasons))
                if not reasons:
                    result = (transform, rmse, max_err, gcps_world, gcps_pixel)
                    used_ref = ref_year
                    break

            old = old_records.get(year)
            old_desc = (f"was RMSE={old['rmse_m']:.2f}m/px={old['pixel_size_m'][0]:.3f}"
                        if old else "was unfitted")

            if result is None:
                tried = ", ".join(str(r) for r, _ in attempts)
                closest_ref, closest_reasons = attempts[0]
                print(f"  {year}: FAILED against [{tried}] ({old_desc}) - closest ({closest_ref}): "
                      f"{'; '.join(closest_reasons)}")
                still_pending.append(year)
                continue

            transform, rmse, max_err, gcps_world, gcps_pixel = result
            pixel_size = float(np.hypot(transform.a, transform.d))
            n_gcp = len(gcps_world)
            print(f"  {year}: matched against {used_ref}, {n_gcp} RANSAC-inlier GCPs, RMSE={rmse:.2f}m "
                  f"max_err={max_err:.2f}m pixel_size~{pixel_size:.3f}m/px ({old_desc})")
            succeeded.append(year)
            progress = True

            if not args.dry_run:
                filename = f"{basename}_{year}_screenshot.tif"
                out_path = os.path.join(outdir, filename)
                with rasterio.open(
                    out_path, "w", driver="GTiff",
                    height=img.shape[0], width=img.shape[1], count=3, dtype=img.dtype,
                    crs=CRS.from_epsg(25833), transform=transform,
                    compress="lzw", photometric="RGB",
                ) as dst:
                    for band in range(3):
                        dst.write(img[:, :, band], band + 1)
                    dst.update_tags(
                        MATRIKKELNUMMER=prop.matrikkelnummer, KOMMUNE=prop.kommunenavn,
                        KOMMUNENUMMER=prop.kommunenummer, YEAR=str(year),
                        SOURCE="norgeibilder.no browser viewer (manual screenshot)",
                        GEOREFERENCING_METHOD=f"reference-image feature matching against {used_ref}, "
                                               f"{n_gcp} RANSAC-inlier GCPs",
                        RMSE_M=f"{rmse:.2f}", MAX_ERROR_M=f"{max_err:.2f}",
                        PIXEL_SIZE_M=f"{pixel_size:.3f}",
                        WARNING="Approximate - georeferenced from a browser screenshot via reference-image "
                                "matching, not the source WMS. See refit_from_reference.py's docstring.",
                    )

                record = {
                    "filename": filename, "year": year,
                    "width_px": img.shape[1], "height_px": img.shape[0],
                    "pixel_size_m": [round(pixel_size, 3), round(pixel_size, 3)],
                    "georeferencing_method": "reference_image_matching",
                    "reference_year": used_ref,
                    "n_gcp": n_gcp, "rmse_m": round(rmse, 2), "max_error_m": round(max_err, 2),
                }
                _merge_into_manifest(manifest_path, prop, record, basename)

            # Join the reference pool - even on a dry run, so the printed
            # cascade preview reflects what a real run would actually
            # reach, not just what the seed(s) alone could fit directly.
            references[year] = (transform, gray, kp, des)

        pending = still_pending

    failed.extend(pending)
    print(f"\n{len(succeeded)} re-fit successfully: {succeeded}")
    if failed:
        print(f"{len(failed)} still need manual attention (georeference_screenshot.py's "
              f"list-vertices/pick/fit): {failed}")

    if not succeeded or args.dry_run:
        if succeeded and args.dry_run:
            print("\nDry run - nothing written, nothing regenerated.")
        return

    # These tools each operate on the property's whole year set (overlay/
    # report/tile export don't take --years), so - same as run_all.py -
    # they're re-run in full rather than trying to regenerate just the
    # re-fit years' own output in isolation.
    prop_args = ["--kommune", args.kommune, "--gnr", str(args.gnr), "--bnr", str(args.bnr)]
    print(f"\nRegenerating downstream output ({len(succeeded)} year(s) changed) ...")
    run_step("plot_overlay.py - render overlays", ["plot_overlay.py"] + prop_args)
    run_step("generate_report.py - Word report", ["generate_report.py"] + prop_args)
    run_step("generate_mbtiles.py - offline map tiles", ["generate_mbtiles.py"] + prop_args)
    run_step("export_web_tiles.py - static web tiles", ["export_web_tiles.py"] + prop_args)
    print(f"\nDone: {outdir}/ is up to date, and docs/tiles/{outdir}/ is ready to serve.")

    if args.push:
        seeds = ", ".join(str(y) for y in sorted(args.reference_years))
        push_to_github(f"Re-fit {prop.matrikkelnummer} {args.kommune} "
                        f"({', '.join(str(y) for y in succeeded)}) via reference-image matching "
                        f"seeded from {seeds}")
    else:
        print("Not pushed - pass --push to commit and push docs/ to GitHub.")


if __name__ == "__main__":
    main()
