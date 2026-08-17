# Map Generation: GIS + Norge i Bilder Historical Aerial Photos

Property boundaries from Kartverket's cadastre, used to extract
historical aerial photographs of that property from Norge i Bilder,
across as many decades as coverage allows.

## Status

Property boundary lookup and the coverage search are complete and
verified working. Downloading actual image pixels via the proper WMS
needs a GeoID-account token (see "Blocker" below) - `download_images.py`
is written and its non-network logic (property/project lookup, bbox/
resolution math, gap-year skipping) is verified, but the real WMS
GetMap call could only be confirmed to *reach* the server correctly
(fails with an auth error using a fake token, as expected); it needs a
real token, run from your own machine, to confirm end-to-end.

**Decisions made**: skip 2017-2024 (no dedicated aerial coverage - see
below) rather than substitute the far coarser satellite mosaic. All
three routes to a GeoID token (Norge digitalt membership, a paid
data-access agreement, or ordering specific historical photos directly
from Kartverket's archive) were assessed and set aside for now - none
make sense at this project's current pre-client, idea-testing stage (no
budget for direct costs, no client yet to justify formal membership).
Kartverket's own browser viewer (norgeibilder.no) is free to use for
anyone, though, and already shows the eiendomsgrenser (property
boundary) drawn on each historical photo - that boundary's already-known
real-world coordinates are enough to georeference a screenshot of it
directly, sidestepping the token requirement entirely for a
lower-accuracy but immediately usable result (see "Georeferencing a
norgeibilder.no screenshot" below). **Start with `auto_gcp.py`** - it
does this fully automatically, batching through every screenshot in a
property's folder with no human clicking corners; `georeference_screenshot.py`
is the manual fallback for whichever specific years it can't confidently
fit on its own. **Working end to end, for real**: all 43
manually-captured screenshots on hand across four real properties -
123/9 Etnedal (8), 14/987 Nittedal (14), 124/9 Etnedal (8), and 126/64
Etnedal (13) - are georeferenced fully automatically via `auto_gcp.py`,
zero exceptions, visually confirmed correct against the live boundary
for every one (see `auto_gcp.py`'s documented failure modes/fixes
below, especially fix 5, for how that went from "most of them" to "all
of them"). `plot_overlay.py`'s batch mode (see "Per-property output
folder" below) regenerates every overlay in one command.

## Full workflow, step by step

Every step below is its own script, run manually, one at a time -
nothing is auto-chained yet. That's deliberate for now: each step's
output is worth a look before moving on (a bad boundary fit, a coverage
search that came back empty, etc.), and a single orchestrating command
that hides that is a natural later step once the manual sequence has
been trusted through enough real properties, not before. All output for
one property lands in one shared folder - see "Per-property output
folder" below - so later steps find earlier steps' files with no extra
wiring.

**Every script below takes the property the same way**:
`--kommune`/`--gnr`/`--bnr`, all three required - every single one of
them, `property.py` and `imagery_search.py` included, fails loudly with
argparse's usage message if you omit any of them, rather than silently
defaulting to some other property. This wasn't always true - earlier
versions of `property.py`/`imagery_search.py` took the property as
positional arguments, and `plot_overlay.py`/`generate_report.py`
silently defaulted to Etnedal 123/9 if the flags were left off - both
fixed for exactly the reason the previous paragraph gives: an
inconsistent or silently-wrong CLI is worse for a "run it stepwise,
review each output" workflow than a script that just refuses to guess.
Replace `Etnedal 123 9` with your own kommune/gnr/bnr throughout.

1. **Look up the property boundary.**

       python3 property.py --kommune Etnedal --gnr 123 --bnr 9

   Fetches the cadastral polygon from Kartverket's live WFS and saves
   `property_polygon.csv`. Nothing to review here - every later step
   re-fetches the boundary live too, rather than trusting a possibly
   stale saved copy.

2. **Find which years actually have coverage.**

       python3 imagery_search.py --kommune Etnedal --gnr 123 --bnr 9

   Lists every Norge i Bilder project that covers the property, by
   year (and how far off the near-misses are), and saves
   `imagery_coverage.csv`. Worth a look before capturing any
   screenshots - no point capturing a year with no real coverage.

3. **Capture screenshots manually**, for whichever years step 2 showed
   real coverage for - no script for this yet (see "Blocker" above for
   why the proper WMS route isn't available). From norgeibilder.no,
   with the eiendomsgrenser (property boundary) layer switched on, save
   each into the property's output folder named
   `<gnr>-<bnr>-<kommune>-<year>.png` (e.g. `123-9-Etnedal-1970.png`) -
   the year must be *only* digits before the extension
   (`find_input_screenshots()`'s naming pattern requires it); a capture
   named e.g. `126-64-Etnedal-2011b.png` (trying a second angle for a
   year that didn't work the first time, say) won't match and is
   silently left out of every later step, not reported as an error.

4. **Georeference the screenshots.**

       python3 auto_gcp.py --kommune Etnedal --gnr 123 --bnr 9

   Automatic for every screenshot it can confidently fit (see
   "Automatic (`auto_gcp.py`) - start here" below for exactly how, and
   its real failure modes/fixes to date). Prints which years it
   couldn't - fall back to `georeference_screenshot.py`'s manual
   `list-vertices`/`pick`/`fit` workflow (below) for those specifically,
   not the whole batch.

5. **Render overlays for visual review.**

       python3 plot_overlay.py --kommune Etnedal --gnr 123 --bnr 9

   The photo, georeferenced, with the *live* cadastral boundary drawn
   on top - the actual check that a fit is correct, not just
   numerically plausible (a low RMSE alone isn't sufficient evidence -
   see `auto_gcp.py`'s documented failure modes below for real cases
   where it wasn't). Look at these before trusting step 4's output.

6. **Build the Word report.**

       python3 generate_report.py --kommune Etnedal --gnr 123 --bnr 9

   One A4 page per fitted year, each captioned with its real source
   project and accuracy - see "Generating the Word report" below.

7. **Package for on-phone viewing.**

       python3 generate_mbtiles.py --kommune Etnedal --gnr 123 --bnr 9

   One MBTiles file per fitted year - see "Viewing on Android" and
   "Viewing in a browser" below for the two ways to actually look at
   these on a phone, and why there are two. Both read this same output,
   nothing else from step 6 needed twice.

8. **For the browser viewer specifically, one more unpacking step**
   (not needed for the Android app, which reads MBTiles directly):

       python3 export_web_tiles.py --kommune Etnedal --gnr 123 --bnr 9

   Unpacks step 7's MBTiles into `docs/tiles/123-9-Etnedal/` as plain
   static files - see "Viewing in a browser" below for why that's a
   separate step and not just part of step 7.

**Once screenshots are captured (step 3), `run_all.py` chains steps 1,
2, and 4-8 into one command** - each one invoked exactly as its own
documented CLI above (a real subprocess call, not reimplemented logic),
stopping immediately if any step fails:

    python3 run_all.py --kommune Etnedal --gnr 124 --bnr 9
    python3 run_all.py --kommune Etnedal --gnr 124 --bnr 9 --push   # also commit + push docs/ to GitHub

Checks for raw screenshots up front and stops with clear guidance if
none are found, rather than run six more steps against nothing - the
one thing it still can't do for you is step 3 itself. `--push` is
opt-in, not automatic: a property's data going live on a public URL is
a real, visible action worth a deliberate flag, not a script's default
behavior. Verified for real on a third property, 124/9 Etnedal
("Langvassetera"): 6 of its 8 screenshots fit automatically (the other
2 correctly reported as needing `georeference_screenshot.py`'s manual
fallback, not silently skipped or treated as a pipeline failure),
report/tiles generated for those 6, and pushed straight to the live
site.

## `property.py`: cadastral boundary lookup

Fetches a property's exact polygon boundary from Kartverket's open WFS
service ("Matrikkelen - Eiendomskart Teig", no API key needed), given a
kommune name and gnr/bnr:

    python3 property.py --kommune Etnedal --gnr 123 --bnr 9

Kommunenummer (the 4-digit code the WFS actually filters on) is resolved
at request time via Kartverket's Kommuneinfo API rather than hardcoded -
these numbers change (Etnedal's changed from 0541 to 3450 in a 2024
nationwide renumbering), so a hardcoded table would eventually go stale
silently.

**Verified for 123/9, Etnedal**: a single Teig, area 645,059 m^2 (~0.65
km^2), bounds (214493.684, 6764447.438, 215277.451, 6765663.722) in
EPSG:25833 (ETRS89 / UTM 33N).

Besides printing a summary, this also saves the polygon to
`property_polygon.csv` in the property's output folder (see
"Per-property output folder" below) - a real CSV (not a `.dat` file,
which macOS has no default app for even though the content would be
plain text) so it opens directly as a spreadsheet in Numbers/Excel:
columns `part`/`ring`/`easting`/`northing`, one row per boundary point.
`read_polygon_csv()` parses it straight back into the same shapely
geometry, so it's a usable cache (e.g. during a Kartverket WFS outage),
not just a human-readable dump.

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

Run as `python3 imagery_search.py --kommune Etnedal --gnr 123 --bnr 9` -
`--kommune`/`--gnr`/`--bnr` are all required (same style as every other
script in this project; there's no default property, so running it with
any of them missing prints a usage error rather than silently searching
for some other property). Besides printing the
table above, this also saves every candidate project found (covering or
near-miss) to `imagery_coverage.csv` in the property's output folder -
a real CSV, opens directly as a spreadsheet in Numbers/Excel.

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

**A GeoID account itself is gated, not free-for-anyone** (corrected
after an earlier version of this doc said otherwise): per Geonorge's own
access documentation, "GeoID-tilgang gis bare til parter i Norge
digitalt-samarbeidet" (GeoID access is only given to parties in the
Norge digitalt collaboration) - Norge digitalt being Kartverket's
national geodata-sharing partnership of ~500 public/private
organizations, not a personal signup. There are two real paths to a
token:

1. **Your organization is (or becomes) a Norge digitalt party.**
   Membership is a party agreement, not a paid product - many Norwegian
   public bodies, municipalities, and research institutions already
   have one. Worth checking internally first (a GIS/geodata coordinator
   or IT department would know) before assuming it's not available -
   this is often the fastest path since it may already exist and just
   not be widely known within the organization.
2. **A direct data-access agreement with Kartverket**, for
   organizations that aren't (and don't want to become) full Norge
   digitalt parties - requested via kundesenter@kartverket.no. This is
   the "and those who enter into an agreement on data access" clause in
   Kartverket's own access text, and may be simpler/faster than a full
   partnership for a single project's worth of imagery.

Note this is a different thing from **data.norge.no** ("Data Norge"),
Norway's general open-data *catalog* (a discovery/metadata index across
public administration, not an access-control system) - if an
application through that route was rejected for lacking organizational
membership, it likely redirected into the Norge digitalt process above,
which is the actual gate.

## Georeferencing a norgeibilder.no screenshot (no token needed)

norgeibilder.no's browser viewer needs no login and lets you pick any
historical "prosjekt" (year) with the eiendomsgrenser (property
boundary) overlay turned on - so a screenshot already has the
property's boundary drawn on it, in exactly the shape and position
property.py already knows the real-world coordinates of. That's enough
to georeference the screenshot directly: match boundary corners in the
image to their known (easting, northing), and fit a similarity
transform (uniform scale + rotation + translation - the same idea as
the "world file" technique used to georeference scanned paper maps, but
constrained to what an orthophoto screenshot's geometry actually allows
- see `auto_gcp.py`'s fix 5 below for why) - sidestepping the WMS token
requirement entirely (see "Blocker" above), chosen because none of the
three routes to a real GeoID token fit this project's current stage.

Two scripts do this, and **they take their arguments differently** -
worth knowing before you type anything, since using one script's
command shape on the other fails with a confusing error rather than
just working:

- **`auto_gcp.py` - start here.** No subcommand - `--kommune`/`--gnr`/
  `--bnr` go straight on the command line:
  `python3 auto_gcp.py --kommune Etnedal --gnr 123 --bnr 9`. Batches
  through every un-fitted screenshot in the property's folder
  automatically.
- **`georeference_screenshot.py` - the fallback**, only for whichever
  specific years `auto_gcp.py` couldn't confidently fit on its own.
  This one *is* subcommand-based (like `git`): the first argument must
  be `list-vertices`, `pick`, or `fit`, with `--kommune`/`--gnr`/
  `--bnr` coming after that -
  `python3 georeference_screenshot.py list-vertices --kommune Etnedal
  --gnr 123 --bnr 9`. Typing the property directly as bare positional
  values with no subcommand and no `--kommune`/`--gnr`/`--bnr` flags -
  `python3 georeference_screenshot.py Etnedal 123 9` - fails with
  `error: argument command: invalid choice: 'Etnedal'`: argparse is
  trying to match "Etnedal" against `{list-vertices,pick,fit}` and
  failing, not rejecting the property.

### Automatic (`auto_gcp.py`) - start here

    python3 auto_gcp.py --kommune Etnedal --gnr 123 --bnr 9
    # -> for every raw input screenshot found that isn't already in
    #    manifest.json, auto-extract GCPs, fit, and report RMSE/quality

**Boundary color is auto-detected, not hardcoded.** norgeibilder.no's
property-boundary overlay isn't always the same color - verified
directly on this project's real screenshots: Etnedal's is magenta,
Nittedal's is green. `auto_gcp.py` scans the first available screenshot
in HSV space for a plausible boundary color (a thin, large-bounding-box,
mostly-unfilled contour - filtering out small colored UI elements like
parcel-number label badges, which can have *more* raw pixels than the
actual boundary line but a much smaller bounding box and a high fill
ratio) and proposes up to `MAX_COLOR_CANDIDATES` candidates, ranked most
likely first. Rather than silently trusting the top candidate, it asks
for interactive confirmation before running the batch:

    Detected boundary color candidate 1/3: green (#08bc46)
    Use this color? [y]es / [n]o, try next / [hex color, e.g. #ff00ff]:

Answer `y` to accept, `n` to see the next candidate, or type a hex color
directly if none of the auto-detected candidates look right. The
confirmed color is reused for every year in the batch (verified: it's
consistent across all years for a given property, since the *viewer's*
overlay color doesn't change with the underlying photo). For
non-interactive/scripted runs, skip the prompt with `--yes` (accepts the
top-ranked candidate automatically) or force a specific color up front
with `--boundary-color RRGGBB` (skips detection entirely):

    python3 auto_gcp.py --kommune Nittedal --gnr 14 --bnr 987 --boundary-color 08bc46 --yes

The corner-matching step (once the boundary mask is extracted) went
through two real, tested-and-discarded designs before landing on one
that actually works reliably - worth recording, since both failures are
the kind of thing that looks plausible until checked against real data:

1. **A rigid slide of the two corner sequences against each other**
   (matching pixel corners to world vertices index-by-index at a fixed
   offset) - breaks the moment the two independently-chosen
   simplification levels don't have exactly matching corner counts
   (the normal case, not an edge case): a single extra or missing
   corner anywhere desyncs every match after it.
2. **Proper sequence alignment** (dynamic programming, the same idea
   used to diff text or align DNA - allows the world side to "skip" a
   vertex the pixel simplification merged away) - fixes the desync
   problem, but turn angles alone turned out to be an insufficient
   signal on their own: verified directly on this project's real 2025
   screenshot that a geometrically *wrong* alignment can score a better
   (lower) angle-only cost than the actual correct one, since angles
   don't encode rotation or scale.

**What actually works**: treat sequence alignment (and a second,
independent crude bounding-box-based guess) as *rough seed generators*
only, then refine every seed with Iterative Closest Point (ICP) -
project pixel corners through the current transform, snap each to its
nearest point on the property's real (unsimplified) boundary, refit,
repeat with a shrinking match-distance gate. ICP does the real work:
verified directly that it reliably converges to the correct alignment
even from a badly wrong seed (RMSE >100 m before refinement) - so seed
quality only needs to be roughly plausible, not precise.

ICP is a *local* refinement method, though, and testing against real
screenshots found a second real failure mode worth recording: it can
converge to a self-consistent but globally wrong registration - a low
RMSE on a small, locally-agreeing subset of points (e.g. a repetitive
zigzag section of the boundary matching itself at the wrong offset),
while the transform itself is degenerate (compresses one axis far more
than the other) or the image ends up rotated 25-160 degrees from
reality - deceptively low RMSE, clearly wrong result if you looked at
the image. A low RMSE alone is not sufficient evidence of a correct
fit. Fixed by filtering *every* candidate fit, not just the
lowest-RMSE one, on two independent, cheap checks before RMSE is
allowed to pick a winner: the transform's two axis scales must be
within 15% of each other (every genuine fit on real data measured
within 0.6%; the observed degenerate case was 2.36x), and its implied
rotation must be within 20 degrees of north-up (real screenshots are
all ~0 degrees - norgeibilder.no's viewer has no rotation control a
user could trigger even by accident; observed failures ranged
25-160 degrees).

Two more real failure modes surfaced later, both from visually comparing
the *fitted* overlay against the live boundary rather than trusting a
passing RMSE/rotation/scale check alone - worth recording, since both
fits "succeeded" by every check above and were still visibly wrong:

1. **Duplicate ICP correspondences over-weighting one point.** Several
   distinct pixel corners can snap to the *same* nearest point on the
   world boundary ring in one ICP iteration - e.g. a cluster of
   near-duplicate corner candidates around one real corner, or (on
   14/987 Nittedal, whose screenshots crop the property on two frame
   edges at once, not the one edge typical of 123/9 Etnedal's) a
   crop-artifact point landing near a real vertex. Feeding several
   pixel points against one identical world target into the affine
   least-squares fit over-weights that target and skews rotation/scale
   - verified directly for Nittedal's 2025 screenshot: 4 separate pixel
   points all matched the same world vertex, and the resulting fit's
   single worst residual (3.77 m, the reported max error) came from
   exactly that point. Fixed by trying ICP both ways each iteration -
   once keeping every within-gate match, once keeping only the closest
   pixel point per distinct world target - as two more candidate fits
   in the same "generate candidates, let the lowest-RMSE-that-passes-
   the-checks win" pattern already used for DP-alignment seeds.
2. **A confirmed boundary color can still drift screenshot to
   screenshot.** The interactive color confirmation (see above) checks
   once per property and reuses that exact hue range for the whole
   batch - but the real rendered color isn't perfectly constant across
   a multi-decade screenshot set: verified directly that Etnedal's
   magenta measures hue ~143 in its earlier screenshots and ~151 in its
   later ones, and forcing one exact range made every one of the
   later ones fail outright. Fixed by falling back to that screenshot's
   own detected candidates (closest-hue-first) whenever the confirmed
   exact range finds nothing - but only as a fallback, tried strictly
   after the confirmed color, and only when the confirmed color found
   *no* valid fit at all: letting a fallback hue compete on raw RMSE
   once the confirmed color already succeeded let a wrong-hue candidate
   beat the correct line by 0.01 m RMSE on one screenshot while being
   visibly worse (18.9 degrees of implied rotation vs. 5.8).
3. **Neighboring parcels' boundaries are drawn in the same color and
   fused into the same contour.** norgeibilder.no's eiendomsgrenser
   layer draws *every* visible parcel's boundary in one overlay color,
   not just the target property's, and adjacent parcels share edges -
   so `largest_contour()`'s "biggest connected blob of the boundary
   color" is often not the target's own polygon but a mesh of several
   parcels fused together. Confirmed directly on 14/987 Nittedal: the
   single largest connected component of the boundary mask spanned
   (0, 0, 498, 590) in a 498x590 image - the *entire* frame - for a lot
   only ~40 m across, and the true source of the moderate-but-real
   rotation/scale-anisotropy left over from fixes 1-2 above (5-9 degrees
   on several years, still under the sanity thresholds but visibly
   wrong once you compare against the live boundary, not just a
   passing RMSE). Fixed with a real preprocessing stage: every visible
   parcel's own gnr/bnr number is rendered on it in the same
   distinctive style (red text, white halo) - not just the target's,
   its neighbors' too - but this project's manual-capture workflow
   always centers the viewer on the target property first, so its
   label is reliably the one closest to the screenshot's own geometric
   center (verified directly: within a few pixels of true image-center
   on every real screenshot tested, while a neighboring parcel's own
   label sat far off at the frame edge). `detect_label_seed_point()`
   finds it (no OCR needed - just the same HSV color-thresholding
   technique used for the boundary line itself, then picking whichever
   detected text blob is closest to center), and
   `isolate_target_boundary_mask()` uses it to seed a flood-fill of the
   *non*-boundary pixels, walled in by the boundary mask (dilated a
   couple of pixels first, to close small antialiasing gaps in the
   line) - the one enclosed interior region reached from that seed is,
   by construction, only the target's own loop, immune to whatever
   shape its neighbors' fused-on boundaries take. Tried as a preferred
   first tier ahead of the old whole-mask contour, same tiering
   rationale as fix 2: falls back to the old approach if no label is
   found (e.g. text too faint in a low-resolution historical photo) or
   the isolated region doesn't produce a valid fit, rather than
   compete the two on raw RMSE.
4. **The RMSE sanity check's absolute floor was far too loose.**
   `verify_registration()` rejected a fit only if RMSE was large *both*
   relative to pixel size (>3x) *and* in absolute terms (>15m) - the
   "and" was meant to stop the relative check alone from over-rejecting
   fine-resolution screenshots (Nittedal's ~0.08 m/px years genuinely
   run RMSE 1-2m, which is 12-25x pixel size yet still an excellent
   absolute fit) - but a 15m absolute floor is nowhere near what this
   project's real data ever produces: every genuinely good fit, across
   every property and pixel size tested, has stayed under 2.55m RMSE.
   Confirmed as a real, live bug, not just a theoretical gap: 126/64
   Etnedal's 2023 screenshot "succeeded" at RMSE=7.67m from just 4 GCPs
   - 17x its pixel size, but under the 15m floor - and was visibly,
   unambiguously wrong when checked against the live boundary, not a
   borderline case. Fixed by rejecting past whichever is more
   permissive of a flat 5m absolute cap (over 2x the worst known-good
   case) or 3x pixel size (still scales up for a genuinely coarse
   screenshot). Re-running the same screenshot after the fix didn't
   just correctly reject the bad candidate - with it excluded, a
   different, already-present candidate that the bad one had been
   out-competing on raw RMSE became the new best fit (RMSE=0.67m,
   visually pixel-perfect against the live boundary). Regression-tested
   against every already-fitted year across all four properties tested
   to date (43 screenshots) - no previously-good fit newly failed.
5. **The fitted transform could shear or scale the photo's two axes
   unevenly - geometrically meaningless for an orthophoto.** Every fit
   up to this point solved for a general 6-parameter affine transform
   (independent x/y scale, plus shear) rather than being constrained to
   what a screenshot of an orthophoto actually *can* be related to
   world coordinates by: a rotation and a single uniform scale, nothing
   more (norgeibilder.no serves true orthophotos, and a 2D map viewer
   pans/zooms/[not-]rotates without ever shearing or unevenly
   stretching what it shows). The isotropy/rotation sanity checks
   further up existed only because of this - post-hoc filters for a
   failure mode the fitting model itself shouldn't have been able to
   produce in the first place. `fit_similarity()`
   (`georeference_screenshot.py`, used by both `auto_gcp.py` and the
   manual fallback below) now solves for the correct, smaller 4-unknown
   model directly (uniform scale + rotation + translation) - shear and
   anisotropic scale aren't rejected after the fact anymore, they're
   simply not representable, so `_is_isotropic()` became unconditionally
   true and was removed as dead code rather than kept as a vestigial
   check. Verified this wasn't just a purity improvement: re-running
   every already-fitted screenshot across all four properties (43
   total) under the new constraint, *every single one* - including
   every year that previously needed `georeference_screenshot.py`'s
   manual fallback - now fits automatically, several with visibly
   better accuracy than before. The likely reason: removing shear/
   anisotropic-scale as an escape hatch means a wrong point
   correspondence can no longer be locally "explained away" by
   inventing unphysical distortion, so ICP is forced to either
   genuinely converge correctly or fail cleanly, rather than settling
   into a self-consistent-looking but wrong local optimum. Fixing this
   also surfaced a second, related bug: `auto_gcp.py`'s confirmed-color
   tiering (fix 2 above) decided whether to stop searching fallback
   hues using a looser internal filter than `verify_registration()`'s
   real bar, so a forced color that technically "passed" the loose
   filter but was actually just the wrong (drifted) color for that
   year - real case: 126/64 Etnedal's 2019 and 2023 screenshots, RMSE
   9.66m and 5.26m under the confirmed-but-wrong hue - could stop the
   search before ever trying the hue that actually fit well. Fixed by
   having the tiering decision call `verify_registration()` itself,
   the same check that will decide pass/fail anyway, instead of a
   separate threshold that could silently drift out of sync with it.
6. **Rotation itself was still a free parameter by default - one step
   short of the true model.** Fix 5 removed shear/anisotropic-scale as
   an unphysical escape hatch, but `fit_similarity()`'s remaining
   rotation degree of freedom is *also* unphysical here:
   norgeibilder.no's viewer has no rotation control at all (already
   established above, as the basis for `_is_plausible_rotation()`'s
   20-degree gate), so a real capture is always exactly north-up - any
   nonzero rotation a fit finds is, by definition, absorbing GCP
   correspondence noise into a plausible-looking but wrong small
   rotation, not a genuine feature of the screenshot. Confirmed
   directly and visibly, not just numerically: 124/9 Etnedal's 2006,
   2011, 2016, and 1986 screenshots fit at -15.8, -6.6, 11.4, and -18.6
   degrees respectively - each individually under the 20-degree gate,
   so none were rejected, but each visibly wrong against the live
   boundary once actually looked at (the whole reason a passing RMSE
   alone was never treated as sufficient evidence elsewhere in this
   file). `fit_translation_scale()` (`georeference_screenshot.py`) adds
   the true, further-constrained 3-unknown model (uniform scale +
   translation, rotation fixed at exactly zero); `auto_gcp.py` now
   tries every seed/epsilon/dedupe/color combination at zero rotation
   *first*, and only allows `fit_similarity()`'s rotation as a fallback
   if nothing at zero rotation verifies - rotation is a last resort
   tried once across the whole search, not a default degree of freedom
   spent on every fit.

   Getting the *ordering* of that fallback right took two more real
   bugs found while verifying this fix, not just the constraint itself:
   (a) rotation was originally decided *within* one color candidate's
   search before moving to the next color - so on 124/9 Etnedal's 2011
   screenshot, the confirmed (but that year, drifted-wrong) color's own
   zero-rotation search correctly found nothing, but letting *that one
   wrong color* fall back to rotation found a self-consistent,
   verify_registration-passing-but-wrong fit (RMSE 2.3m, -6.6 degrees)
   before the search ever reached the drift-fallback color that fits
   this year well at zero rotation - fixed by making rotation the
   outermost tier, tried across *every* color candidate at zero
   rotation before any color is allowed to use rotation at all. (b)
   Within one color/rotation combination, the epsilon/seed/dedupe grid
   was picking whichever candidate had the lowest raw RMSE, not
   whichever actually passed `verify_registration()` - a few points can
   fit deceptively tightly by chance (near-collinear or otherwise
   unrepresentative) and score a lower RMSE than a well-supported,
   genuinely-correct candidate sitting right next to it in the same
   grid, while failing verification for an unrelated reason (e.g.
   projecting mostly outside the image) - silently hiding the good
   candidate from ever being returned. Fixed by preferring a verified
   candidate over a merely lower-RMSE one at that finest grain too, the
   same "prefer ok, then lowest RMSE" pattern now applied consistently
   at every tier of the search (epsilon/seed/dedupe, color, and
   rotation alike). Regression-tested after both fixes: all four
   properties' 43 screenshots still fit fully automatically, and every
   single one now measures *exactly* zero rotation, with no case
   needing the fallback at all once the search was correct.

**Verified on all four properties' full real screenshot sets**: every
single screenshot across 123/9 Etnedal (8), 14/987 Nittedal (14), 124/9
Etnedal (8), and 126/64 Etnedal (13) - 43 screenshots total, zero
exceptions - now fits fully automatically, visually confirmed correct
via `plot_overlay.py`, not just numerically plausible. That weren't
always all: `georeference_screenshot.py`'s manual fallback (below) put
in genuine work fitting 124/9 Etnedal's 1986/1991 and 126/64 Etnedal's
1986/1991/2001/2011 by hand at the time (see the manual-fallback
section's own worked example) - fix 5 above is what later made even
those fit automatically on a from-scratch re-run, not a claim that
they always had.

`auto_gcp.py` still fails loudly (an error, or reporting no confident
match) rather than silently accepting a wrong registration on whichever
year, someday, defeats all of the above - `georeference_screenshot.py`'s
manual workflow (below) remains the intended fallback for that case,
and `manifest.json`'s `georeferencing_method` field always records
which path (`automatic_gcp_extraction` vs. `manual_gcp_affine_fit`)
produced each year's fit.

### Manual fallback (`georeference_screenshot.py`)

Only needed for a year `auto_gcp.py` couldn't confidently fit on its
own (above) - currently none of this project's four real properties has
any (all 43 real screenshots tested to date fit automatically - see
`auto_gcp.py`'s fix 5 above), though a low-quality or unusually-cropped
screenshot could still land here for a property not yet tested. Also
worth knowing even though it's not live on any current property: 124/9
Etnedal's 1986/1991 and 126/64 Etnedal's 1986/1991/2001/2011 were real,
genuine uses of this fallback at the time (before fix 5 made even those
fit automatically on a later re-run) - real sessions, not hypotheticals,
though the worked example just below (123/9, a generic illustration
predating that work) isn't itself from either of them.
Deliberately approximate, not a substitute for a real WMS download -
accuracy is limited by screenshot resolution (whatever zoom level the
browser was at, not the source photo's native resolution) and how
precisely boundary corners can be clicked. The `fit` step reports its
own residual error in meters, so the accuracy achieved is known, not
assumed. 123/9's boundary is a genuinely complex shape (221 original
vertices - it follows a stream for much of its length), so
`list-vertices` simplifies it down to a manageable, labeled set of real
corners first, highlighting the four cardinal extremes as usually the
easiest to identify unambiguously even in a low-resolution screenshot.
For closely-spaced candidate corners, identify them by prediction and
verification rather than guessing blind: fit a preliminary transform
from a few unambiguous points (e.g. the single northmost vertex - unique
on the whole polygon), use it to predict where other vertices should
fall in the image, and check each prediction against the actual
screenshot before accepting it as a GCP.

Workflow (subcommand first - see the CLI-shape callout above):

    python3 georeference_screenshot.py list-vertices --kommune Etnedal --gnr 123 --bnr 9
    # -> prints/plots numbered candidate corners; compare its shape against
    #    your screenshot's eiendomsgrenser line to identify matching corners

    python3 georeference_screenshot.py pick --screenshot photo_2016.png
    # -> click each recognizable corner; pixel (x,y) prints to console
    #    (run on your own machine - needs a display)

    python3 georeference_screenshot.py fit --kommune Etnedal --gnr 123 --bnr 9 \
        --screenshot photo_2016.png --year 2016 \
        --gcp 0:209:815 --gcp 6:1718:1083 --gcp 13:1104:2545 --gcp 19:474:2116
    # -> fits the transform, reports RMSE, writes a tagged GeoTIFF +
    #    manifest.json entry in the same format download_images.py uses

Needs at least 3 GCPs - a practical safety margin, not the true minimum:
`fit_similarity()`'s constrained model (see `auto_gcp.py`'s fix 5 above)
has only 4 unknowns, so 2 well-separated points are actually enough to
solve it exactly. That also means, unlike the general 6-parameter affine
this project used to fit, even exactly 3 GCPs now gives a real,
meaningful RMSE rather than an always-~0 one. Still, use 4+, well spread
around the property and not collinear, for a better-constrained fit.

**Verified two ways**: first end-to-end with a synthetic test screenshot
(the property boundary rendered at a known 0.5 m/pixel, 2-degree-rotated
transform, fed back through `list-vertices` + `fit`) - recovered the
exact known transform to numerical precision (RMSE ~1e-5 m), confirming
the affine-fit math and the GeoTIFF/manifest writing both work
correctly. Then for real: a real 2025 screenshot fit with 5 GCPs
identified by the predict-and-verify method above gave RMSE = 3.75 m
(max error 6.65 m) on a ~1.3 m/pixel image - about 3 pixels of error -
and `plot_overlay.py`'s output (boundary fetched fresh from the live
WFS, overlaid on the real photo) lines up almost exactly with the
boundary already drawn into the photo by norgeibilder.no itself.

## Per-property output folder

Every script that writes files takes `--kommune`/`--gnr`/`--bnr` and
defaults its output folder to `property.py`'s `property_code()`:
`<gnr>-<bnr>-<kommune>`, e.g. `123-9-Etnedal` - so the folder name alone
establishes which property everything inside it belongs to, without
needing to open a manifest, and multiple properties stay cleanly
separated as sibling folders (override with `--outdir` if needed).

To add a new year: capture a screenshot as described above and drop it
into that folder, named `<gnr>-<bnr>-<kommune>-<year>.png` (e.g.
`123-9-Etnedal-2011.png`) - matching what `georeference_screenshot.py
fit` and `download_images.py` themselves write for the *processed*
outputs, just without the `_<year>_screenshot.tif`/similar suffix, so
the two are never confused for one another.

Then run `plot_overlay.py` with no `--image`/`--year` for **batch
mode**: it scans the folder for both already-georeferenced years (in
`manifest.json`) and raw screenshots named as above, regenerates the
overlay PNG for every already-georeferenced year in one go, and reports
which raw screenshots don't have a fit yet (fitting itself still needs
`auto_gcp.py` run first - and, for whichever years that can't
confidently fit, `georeference_screenshot.py`'s manual `list-vertices`/
`pick`/`fit` workflow - not something this step does on its own):

    python3 plot_overlay.py --kommune Etnedal --gnr 123 --bnr 9
    # 123-9-Etnedal: 1 georeferenced year(s), 8 raw input screenshot(s) found.
    #   2025: saved 123-9-Etnedal/123-9-Etnedal_2025_screenshot_overlay.png
    #
    # Raw screenshots found with no georeferenced fit yet - run
    # auto_gcp.py (or, if it can't confidently fit a given year,
    # georeference_screenshot.py's list-vertices/pick/fit workflow) for
    # these before an overlay can be made:
    #   1958: 123-9-Etnedal/123-9-Etnedal-1958.png
    #   1970: 123-9-Etnedal/123-9-Etnedal-1970.png
    #   ...

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
| `property.py` | Fetch a cadastral property boundary by kommune + gnr/bnr; `property_code()` names the per-property output folder |
| `imagery_search.py` | Find which Norge i Bilder projects actually cover a property, by year |
| `download_images.py` | Needs a GeoID token (see "Blocker") - fetches one GeoTIFF per confirmed year via the proper WMS |
| `auto_gcp.py` | **Start here** - georeferences a batch of screenshots automatically (DP alignment + bbox seeds, refined by ICP); no subcommand |
| `georeference_screenshot.py` | Manual fallback for years `auto_gcp.py` can't fit - georeferences one screenshot from human-identified GCPs; **subcommand-based** (`list-vertices` / `pick` / `fit`) |
| `plot_overlay.py` | Plots a georeferenced photo with the live cadastral boundary overlaid; batch mode processes every already-fitted year in one go |
| `generate_report.py` | Builds a Word report (A4, one overlay figure per fitted year) - see "Generating the Word report" below |
| `generate_mbtiles.py` | Packages each fitted year into an offline map-tile file for the Android app - see "Viewing on Android" below |
| `export_web_tiles.py` | Unpacks that same MBTiles output into plain static tiles for the browser viewer - see "Viewing in a browser" below |
| `run_all.py` | Chains steps 1, 2, 4-8 of the full workflow below into one command, once screenshots are captured |

## Generating the Word report (`generate_report.py`)

    python3 generate_report.py --kommune Etnedal --gnr 123 --bnr 9
    # -> <property>_Report.docx in the property's output folder

Pages are set to real A4 (21.0 x 29.7 cm, 1.5 cm side / 2.0 cm top-bottom
margins) rather than python-docx's US Letter default, and each figure is
sized to the full usable page width/height (not a fixed inch guess) so
every overlay image fills as much of the sheet as it can.

Each figure's caption re-queries `imagery_search.py` live (not a cached
value) for that year's real covering Norge i Bilder project, and reports
its name, photo date, and native (source-photo) resolution alongside the
fit's own RMSE/max-error/GCP-count - e.g.:

> Figure 2. 123/9, 1970 - source: 'Etnedal-Nordre Land 1970' (aerial,
> photo date 1970-08-13, 0.2 m/px native resolution). Georeferenced via
> Automatic (auto_gcp.py) (17 GCPs, RMSE = 2.22 m, max error = 4.37 m).

If a screenshot's year label doesn't exactly match any covering
project's own year field, the caption falls back to the nearest
covering project within 2 years and says so explicitly rather than
reporting nothing - a real safety net for a real mistake: 123/9
Etnedal's 1958 screenshot was originally mislabelled 1956 (a manual
capture typo, since fixed by renaming the file/manifest entry), and
before that fix the caption correctly caught the mismatch instead of
silently reporting nothing:

> Figure 1. 123/9, 1956 - source: 'Nordre Etnedal - Aurdal 1958' (aerial,
> photo date 1958-06-05, 0.2 m/px native resolution) [nearest covering
> project, dated 1958 - screenshot labelled 1956]. ...

## Viewing on Android (`generate_mbtiles.py` + `android-app/`)

    python3 generate_mbtiles.py --kommune Nittedal --gnr 14 --bnr 987
    # -> one <property>_<year>.mbtiles per fitted year, in the property's output folder

Packages each already-georeferenced GeoTIFF into an
[MBTiles](https://github.com/mapbox/mbtiles-spec) file - a single
SQLite database holding a Web Mercator (EPSG:3857) raster tile pyramid,
the standard offline format most Android/iOS mapping libraries can load
directly, no tile server involved. Written directly against rasterio +
[mercantile](https://github.com/mapbox/mercantile) + Pillow + the
stdlib's `sqlite3` rather than GDAL's own `gdal2tiles.py` - this
sandbox has no standalone GDAL CLI, and `rio-mbtiles` pulls in a
shapely version that fails to build here (no system GEOS); reprojecting
each destination tile straight out of the source GeoTIFF turned out to
be simple enough to not need either. Zoom range is chosen per year from
its own native resolution (`pixel_size_m` in `manifest.json`), so a
tightly-zoomed screenshot (Nittedal, ~0.08 m/px) gets a deeper zoom
range than a zoomed-out one (Etnedal, ~1.3 m/px) - no benefit to tiling
finer than the source photo actually resolves. Each tile's alpha
channel is built by reprojecting a constant-255 array through the exact
same warp as the RGB bands, so a tile's real (slightly rotated) photo
footprint blends transparently into whatever's behind it rather than
showing a visible rectangular seam - verified by decoding a real
generated tile and inspecting it directly (14/987 Nittedal, 2015).

`android-app/` is a from-scratch Kotlin/Android Studio project (see its
own README) that bundles a property's `.mbtiles` files as assets and
shows them with the phone's live GPS position on top via
[osmdroid](https://github.com/osmdroid/osmdroid) - chosen for its
built-in MBTiles archive support and no API-key requirement. A year
spinner switches which year's imagery overlay is shown; the GPS dot
stays on across every year. Currently bundled with all 14 of 14/987
Nittedal's fitted years. Written outside Android Studio (this sandbox
has no Android SDK/emulator to build or run it against) - see the app
README's "Status" section before treating it as verified. In practice,
this also turned out to be the harder of the two viewing options to
actually get running: a company-managed Mac's software restrictions
blocked installing Android Studio at all (not on the pre-approved
install list, and the account had no admin rights to authorize it any
other way, including via Homebrew) - which is what "Viewing in a
browser" below exists to sidestep entirely.

## Viewing in a browser (`export_web_tiles.py` + `docs/`)

    python3 export_web_tiles.py --kommune Nittedal --gnr 14 --bnr 987
    # -> docs/tiles/14-987-Nittedal/<year>/<z>/<x>/<y>.png for every year,
    #    plus docs/tiles/14-987-Nittedal/manifest.json

No Android Studio, no app install, no IT approval needed - just a URL
in the phone's own browser. This exists specifically because
`android-app/` turned out to hit a real, unresolvable-that-day
blocker (see above); a browser sidesteps it entirely, since nothing
needs installing beyond a browser the phone already has.

MBTiles (what `generate_mbtiles.py` already produces) is a single
SQLite file - fine for the Android app, which has a real program
(osmdroid) to read it and answer individual tile requests, but GitHub
Pages (and static file hosts generally) run no server-side code at
all; they can only serve files that already exist as files.
`export_web_tiles.py` does the equivalent unpacking once, ahead of
time, straight from `generate_mbtiles.py`'s own output - no
reprojection logic duplicated, it just re-shapes already-rendered
tiles into a directory tree (and flips each tile's row from MBTiles'
TMS convention back to the XYZ convention plain web tiles use - the
same flip `generate_mbtiles.py` applied going the other way,
self-inverse).

`docs/` is the whole browser app - plain HTML/CSS/JS, no build step,
using [Leaflet](https://leafletjs.com/) (vendored locally into
`docs/vendor/`, not loaded from a CDN, so it's covered by the offline
caching below too) for the map and the browser's standard Geolocation
API for the live position dot - the direct web equivalent of
`android-app/`'s osmdroid + GPS pairing. A year `<select>` swaps which
year's tile layer is shown; which property to display comes from a
`?property=<code>` URL query parameter (defaulting to 14/987 Nittedal
if omitted - no in-app property picker across multiple bundled
properties yet, same gap `android-app/` has). Verified for real with a
headless-browser test (Playwright): loaded the page, simulated a GPS
fix at the property, switched years, and confirmed both the location
dot and the correct year's photo rendered correctly - not just that
the files exist.

**No live boundary check, and no context beyond the bundled tiles by
default.** The property boundary line you see isn't a separate,
computed layer the app checks your position against - it's whatever
color line norgeibilder.no drew *into that year's own photo pixels*.
Standing just outside the property, your dot lands on the correct
real-world spot in the photo, visibly on the wrong side of the drawn
line - but nothing flags or warns you about it. And since the bundled
tiles only cover the property's own tight crop, wandering far enough
outside it used to mean a blank grey screen with no context at all -
fixed by adding plain [OpenStreetMap](https://www.openstreetmap.org/)
streets as a fallback base layer beneath the historical overlay
(visible wherever the historical tiles don't cover, and wherever
there's signal - it's a live network layer, not bundled, so it simply
won't load offline; the historical overlay + GPS dot, the actual
offline-capable core, don't depend on it either way). Verified this
fallback works and pulls in real street/building context around the
property, not just a blank tile.

Also found and fixed while testing this: Leaflet's default zoom
+/- control renders in the top-left corner of the map by default,
which collided with the toolbar (full-width, also top) - confirmed
directly in a screenshot that the "+" button was rendering completely
hidden underneath the toolbar, leaving only "-" usable. Moved to
bottom-left, clear of both the toolbar and the locate button/
attribution at bottom-right. Pinch-to-zoom and double-tap-to-zoom were
never affected either way - both are gesture-based and Leaflet enables
them by default, independent of the on-screen button control.

**Works offline after the first visit** - important for a rural
property with weak signal, and the reason it's not just a plain static
site. `docs/service-worker.js` precaches the app shell plus every
bundled tile PNG (reading the exact list from each property's
`manifest.json`, rather than guessing which tiles exist) the first
time the page loads with a connection, so every later visit - GPS and
all - works with no network at all. `docs/manifest.webmanifest` lets
the phone's browser "Add to Home Screen" too, so it opens and behaves
like a regular app icon despite being a web page underneath.

**A property that's live on the server can still be invisible to an
already-visited browser** - a real bug, not a hypothetical one:
`tiles/properties.json` and each property's `manifest.json` were
originally cached the same cache-first-forever way as the (genuinely
immutable) tile images. Fine at first visit, but nothing ever
revalidated them afterward - a browser that had visited before 124/9
Etnedal was added kept serving that stale, 2-property registry
indefinitely, even though the live site had 3, and would have hit the
same problem again on every future property/year with no warning.
Fixed by giving `properties.json`, each property's `manifest.json`,
`app.js`, and `style.css` a network-first strategy instead (live data
when there's a connection, the cached copy only as an offline
fallback) - tile images stay cache-first, since those genuinely never
change once generated. Verified by reproducing the exact failure
directly: seeded a stale registry into a real browser cache, confirmed
the old code kept serving it, confirmed the fix self-heals to the live
registry while online, and confirmed the offline fallback - the actual
point of the service worker - still works correctly afterward.

**Deploying it**: push this repo to GitHub (already done - see below),
then a one-time setting on GitHub's own site (not a script - no `gh`
CLI available to automate it, and it's a repo settings change, not a
file): **Settings -> Pages -> Source: Deploy from a branch -> Branch:
`main`, folder: `/docs`**. GitHub then serves `docs/` at
`https://<username>.github.io/<repo>/` - open that URL (or bookmark/
"Add to Home Screen" it) on the phone and it's live, no further steps.
Re-running `export_web_tiles.py` and pushing is the entire update
process for adding a property or a new year - no rebuild, no
reinstall, unlike the Android app.

## Running `download_images.py`

This step needs to run from your own machine, not this sandboxed
environment, because the token it needs is bound to the IP address that
requests it:

1. Get a GeoID account - see "Blocker" above: this requires your
   organization to be a Norge digitalt party, or a direct data-access
   agreement via kundesenter@kartverket.no. Then log in at
   <https://norgeibilder.no/>.
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

Output, in `123-9-Etnedal/` (see "Per-property output folder" above):
- `123-9-Etnedal_<year>.tif` for each of 1958, 1970, 1991, 2006, 2011,
  2016, 2025 - GeoTIFFs (EPSG:25833) cropped tightly to the property's
  own bounding box (no extra context margin), each tagged with the
  photo date, source project, resolution, and property identifiers as
  embedded TIFF metadata (readable via `gdalinfo` or `rasterio`'s
  `.tags()`, so the info travels with the file even if renamed).
- `123-9-Etnedal_boundary.geojson` - the property outline reprojected to
  EPSG:4326 (WGS84 lon/lat), the CRS phone GPS reports in.
- `123-9-Etnedal_manifest.json` - one place with everything the two
  planned follow-on features need: each image's filename/year/photo
  date/resolution, and the shared bounding box in *both* EPSG:25833 and
  EPSG:4326 - so a mobile app can convert a GPS fix to a pixel
  coordinate on any of these images with just an affine transform
  (image bounds + pixel dimensions), no reprojection library required
  on-device. The same manifest.json (and folder) is shared with
  `georeference_screenshot.py fit` - `fit` merges into it rather than
  overwriting, so images from both methods coexist in one manifest.

Why EPSG:25833 for the images: it's the same CRS Kartverket's own
terrain data (hoydedata.no) uses, and the one the cadastre/imagery APIs
already return geometry in - so overlaying these GeoTIFFs on hoydedata.no
data later shouldn't need reprojection either.
