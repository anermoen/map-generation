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
import subprocess
import sys

from property import fetch_property
from plot_overlay import find_input_screenshots

PYTHON = sys.executable


def run_step(description, script_args):
    print(f"\n=== {description} ===", flush=True)
    result = subprocess.run([PYTHON] + script_args)
    if result.returncode != 0:
        raise SystemExit(f"\n{description} failed (exit code {result.returncode}) - stopping.")


def push_to_github(commit_message):
    print("\n=== Pushing to GitHub ===", flush=True)
    subprocess.run(["git", "add", "docs", "run_all.py"], check=True)
    status = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if status.returncode == 0:
        print("Nothing new to commit (docs/ already up to date) - skipping push.")
        return
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
