#!/usr/bin/env python3
"""
Run the entire pipeline for one property, start to finish, in one
command - every step from README.md's "Full workflow, step by step"
section, chained together. Each step is invoked exactly as its own
documented CLI (a subprocess call, not reimplemented logic), so this
produces identical output to running them by hand in order, and stops
immediately if any step fails rather than plowing ahead on bad data.

The one step nothing here can do: capturing screenshots from
norgeibilder.no (see README.md's "Blocker" section - no WMS token, and
Playwright-based browser automation of the site was tried and abandoned
earlier in this project, over Shadow DOM issues with its zoom
controls). This script checks for existing raw screenshots up front
and stops with clear guidance if none are found, rather than run six
more steps against nothing.

Usage
-----
    python3 run_all.py --kommune Etnedal --gnr 124 --bnr 9
    # -> property.py, imagery_search.py, auto_gcp.py, plot_overlay.py,
    #    generate_report.py, generate_mbtiles.py, export_web_tiles.py,
    #    in order - requires raw screenshots already captured into
    #    124-9-Etnedal/124-9-Etnedal-<year>.png first

    python3 run_all.py --kommune Etnedal --gnr 124 --bnr 9 --push
    # -> also commits and pushes docs/ (and this script) to GitHub
    #    afterward, so GitHub Pages picks it up - opt-in, not automatic:
    #    a property's data going live on a public URL is a real,
    #    visible action worth a deliberate flag, not a script default.
"""

import argparse
import os
import re
import subprocess
import sys

from property import fetch_property
from plot_overlay import find_input_screenshots

PYTHON = sys.executable
SERVICE_WORKER_PATH = os.path.join("docs", "service-worker.js")


def run_step(description, script_args):
    print(f"\n=== {description} ===", flush=True)
    result = subprocess.run([PYTHON] + script_args)
    if result.returncode != 0:
        raise SystemExit(f"\n{description} failed (exit code {result.returncode}) - stopping.")


def bump_cache_version():
    """Bump service-worker.js's CACHE_NAME - the forcing function that
    makes a browser which already has this app installed actually
    notice new content exists. Confirmed necessary the hard way, not
    just in theory: service-worker.js's own isVolatile() list (this
    project's earlier caching fix) covers a *new* property/year showing
    up, but tile images are deliberately cache-first-forever on the
    assumption a given year's tile at a given z/x/y is immutable once
    generated - true when a year is only ever added, false the moment
    an *already-published* year gets re-fit with corrected GCPs (real
    case: 126/64 Etnedal's 2016 tile at z17/x69082/y37438 changed pixel
    content across 4 separate commits today while CACHE_NAME sat at v4
    the whole time - anyone who'd visited in between was stuck serving
    stale, wrong tiles indefinitely, with nothing to ever make them
    stop). Bumping CACHE_NAME on every real push - not just ones that
    happen to touch an already-published year - is deliberately blunt
    rather than trying to detect which case this is: a stale byte-for-
    byte-identical service-worker.js is the one thing that can never
    trigger a browser to even check for an update, so every push must
    change it, and re-downloading everything once per deploy is a cheap
    price for a field tool that isn't updated more than occasionally."""
    with open(SERVICE_WORKER_PATH) as f:
        content = f.read()
    match = re.search(r'CACHE_NAME = "aerial-viewer-v(\d+)"', content)
    if not match:
        raise SystemExit(f"Could not find CACHE_NAME in {SERVICE_WORKER_PATH} to bump - "
                          f"fix manually before pushing (see run_all.py's push_to_github).")
    new_version = int(match.group(1)) + 1
    new_content = content[:match.start(1)] + str(new_version) + content[match.end(1):]
    with open(SERVICE_WORKER_PATH, "w") as f:
        f.write(new_content)
    print(f"Bumped {SERVICE_WORKER_PATH} to CACHE_NAME v{new_version}")


def push_to_github(commit_message):
    print("\n=== Pushing to GitHub ===", flush=True)
    subprocess.run(["git", "add", "docs", "run_all.py"], check=True)
    status = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if status.returncode == 0:
        print("Nothing new to commit (docs/ already up to date) - skipping push.")
        return
    # Only bump once real content is actually about to be pushed - not
    # on every invocation regardless of whether anything changed, which
    # would turn a harmless no-op run into a pointless commit every
    # time. See bump_cache_version()'s own docstring for why this step
    # can't be skipped once there *is* something new to deploy.
    bump_cache_version()
    subprocess.run(["git", "add", "docs"], check=True)
    subprocess.run(["git", "commit", "-m", commit_message], check=True)
    subprocess.run(["git", "push", "origin", "main"], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kommune", required=True)
    ap.add_argument("--gnr", type=int, required=True)
    ap.add_argument("--bnr", type=int, required=True)
    ap.add_argument("--push", action="store_true",
                     help="commit and push docs/ to GitHub afterward (opt-in - see module docstring)")
    args = ap.parse_args()

    prop_args = ["--kommune", args.kommune, "--gnr", str(args.gnr), "--bnr", str(args.bnr)]

    run_step("1/7 property.py - cadastral boundary", ["property.py"] + prop_args)
    run_step("2/7 imagery_search.py - coverage search", ["imagery_search.py"] + prop_args)

    prop = fetch_property(args.kommune, args.gnr, args.bnr)
    outdir = prop.code
    raw_screenshots = find_input_screenshots(outdir, args.gnr, args.bnr, args.kommune)
    if not raw_screenshots:
        raise SystemExit(
            f"\nNo raw screenshots found in {outdir}/ - the one step this script can't do for you "
            f"(see module docstring: no WMS token, and browser automation of norgeibilder.no was "
            f"tried and abandoned earlier in this project). Capture some from norgeibilder.no first, "
            f"named '{outdir}-<year>.png', then re-run."
        )
    print(f"\nFound {len(raw_screenshots)} raw screenshot(s): {sorted(raw_screenshots)}")

    run_step("3/7 auto_gcp.py - automatic georeferencing", ["auto_gcp.py", "--yes"] + prop_args)

    manifest_path = os.path.join(outdir, f"{outdir}_manifest.json")
    if not os.path.isfile(manifest_path):
        raise SystemExit(
            f"\nNo years were fitted (no {manifest_path}) - auto_gcp.py couldn't confidently fit any "
            f"of the {len(raw_screenshots)} screenshot(s) found. The fallback, "
            f"georeference_screenshot.py's manual list-vertices/pick/fit workflow, needs a human at "
            f"the keyboard too - not something this script can do either. Stopping here."
        )

    run_step("4/7 plot_overlay.py - render overlays", ["plot_overlay.py"] + prop_args)
    run_step("5/7 generate_report.py - Word report", ["generate_report.py"] + prop_args)
    run_step("6/7 generate_mbtiles.py - offline map tiles", ["generate_mbtiles.py"] + prop_args)
    run_step("7/7 export_web_tiles.py - static web tiles", ["export_web_tiles.py"] + prop_args)

    print(f"\nDone: {outdir}/ is up to date, and docs/tiles/{outdir}/ is ready to serve.")

    if args.push:
        push_to_github(f"Add {prop.matrikkelnummer} {args.kommune} to the web viewer")
    else:
        print("Not pushed - pass --push to commit and push docs/ to GitHub.")


if __name__ == "__main__":
    main()
