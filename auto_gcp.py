#!/usr/bin/env python3
"""
Automatically extract ground control points from a norgeibilder.no
screenshot and fit its georeferencing transform - replacing the manual
list-vertices/pick step in georeference_screenshot.py's workflow with a
fully automatic one, so a batch of screenshots (of any size/zoom/crop -
each manually captured screenshot can be different) can each be fit
without a human clicking corners.

How it works
------------
1. The eiendomsgrenser (property boundary) overlay is drawn in a
   distinctive, vivid color - but not always the *same* color: confirmed
   on this project's own real screenshots that norgeibilder.no draws it
   in magenta for one property (123/9 Etnedal) and green for another
   (14/987 Nittedal), consistently across every year for a given
   property, but differently between properties. detect_boundary_colors
   scans the screenshot's hues for vivid, line-shaped candidates (see
   its docstring) rather than assuming one fixed color, and - once per
   batch run, not once per year - the top candidate is confirmed
   interactively (confirm_boundary_color) before any fitting proceeds:
   a low-stakes, low-cost check that catches a wrong color guess before
   it can waste a whole batch's worth of fitting attempts on it. Pass
   --boundary-color to skip detection/confirmation entirely and force a
   specific color, or --yes to auto-accept the top detected candidate
   without prompting (for scripted/non-interactive use).
2. Color-threshold the screenshot with the confirmed color to get a
   binary mask of all boundary lines visible (every nearby parcel, not
   just ours), via OpenCV. Find the mask's connected contours
   (cv2.findContours) and pick the one with the largest bounding box -
   the property being viewed is presumably the one the screenshot was
   framed around, so its boundary is the most prominent line in frame.
   (A real assumption, not a certainty - see verify_registration() below
   for how this is checked after the fact, not just trusted.)
3. Simplify that contour to a set of corner points (cv2.approxPolyDP).

4. The hard part: which extracted pixel corner is which real-world
   vertex? Two independent strategies each generate a rough *seed*
   transform, rather than betting everything on one:

   a. A crude bounding-box mapping (_bbox_seed): scale+translate (no
      rotation) from the pixel contour's own bounding box to the
      property's bounding box. Assumes the screenshot is roughly
      north-up (the standard, un-rotated browser map view) and the
      whole property is visible in frame - a real assumption, but one
      that holds for every screenshot this project's manual capture
      process produces (the instructions are to zoom until the whole
      boundary fits on screen).
   b. Sequence alignment on turn angles (_dp_align/_candidate_alignments):
      the same idea as diffing text or aligning DNA (a global alignment
      allowing "insertions", via dynamic programming) - tried at every
      possible starting offset and both traversal directions, since
      turn-angle sequences alone don't encode which way is "forward" or
      where the sequence starts. Kept as a second, independent seed
      source for screenshots where the bbox assumption doesn't hold
      (e.g. a genuinely frame-clipped view).

   Testing against this project's real, independently-confirmed 2025 fit
   (RMSE 3.75m from 5 manually-matched GCPs) found that turn angles
   alone are not reliable enough to pick a single winning seed this way:
   they're rotation/scale-blind, so a *wrong* alignment can score a
   better angle-only cost than the truly correct one. Neither seed
   strategy above is trusted directly for that reason - both only ever
   feed into step 5.
5. Every seed (from both strategies, across a small grid of contour
   simplification levels) is refined by Iterative Closest Point (ICP,
   _icp_refine): project the pixel corners through the current
   transform, snap each to its nearest point on the property's actual,
   *unsimplified* boundary (not a simplified approximation - the exact
   ring polyline), refit from the kept correspondences, and repeat with
   a shrinking acceptance gate. This does most of the heavy lifting -
   verified directly that it can converge to the correct alignment
   (matching the manual benchmark's accuracy) even when seeded from a
   badly wrong starting transform (RMSE >100m before refinement) - but
   it is a *local* refinement method, and testing against this
   project's real screenshots found it can just as reliably converge to
   a self-consistent but wrong registration: a low RMSE on a small,
   locally-agreeing subset of points (e.g. a repetitive zigzag section
   of the boundary matching itself at the wrong offset), with the
   *global* transform left nonsensical - the image effectively rotated
   ~25-160 degrees from reality - while still reporting a deceptively
   low RMSE. A low RMSE alone is therefore not sufficient evidence of a
   correct fit. (Degenerate/anisotropic scale used to be a second real
   failure mode here too, but every transform this module fits - seeds
   and every ICP iteration alike, via fit_similarity() in
   georeference_screenshot.py - is now a constrained similarity
   transform, isotropic *by construction*, so that failure mode isn't
   reachable anymore, not just checked for after the fact.)
6. So every ICP result, not just the eventual winner, is filtered
   *before* RMSE is allowed to pick between them: _is_plausible_rotation
   rejects anything more than 20 degrees from north-up (real screenshots
   are all ~0 degrees; observed failures ranged 25-160 degrees) -
   norgeibilder.no's viewer has no rotation control, so every genuine
   screenshot is north-up. Only among the survivors does the lowest
   final RMSE win, and verify_registration() then double-checks that
   winner once more (rotation again, plus: RMSE relative to implied
   pixel size, and the known boundary - projected into pixel space -
   mostly landing within the image bounds) before it's trusted.

This is deliberately not attempted for photos where the boundary color
isn't a clean, isolable magenta (e.g. very low-contrast historical
scans) - it fails loudly (raises, or reports a poor-quality fit) rather
than guessing. It can also legitimately fail even on a usable
screenshot - confirmed on this project's own real 2006 screenshot
(tightly cropped, violating the bbox seed's "whole property visible"
assumption) - in which case georeference_screenshot.py's manual
list-vertices/pick/fit workflow is the fallback, not a bug to chase.

Usage
-----
    python3 auto_gcp.py --kommune Etnedal --gnr 123 --bnr 9
    # -> prompts once to confirm the auto-detected boundary-line color,
    #    then for every raw input screenshot found (see plot_overlay.py's
    #    find_input_screenshots()) that isn't already in manifest.json,
    #    auto-extract GCPs, fit, and report per-year RMSE/quality.

    python3 auto_gcp.py --kommune Etnedal --gnr 123 --bnr 9 --years 1991 2006
    # -> just these years

    python3 auto_gcp.py --kommune Nittedal --gnr 14 --bnr 987 --boundary-color 08bc46 --yes
    # -> skip the prompt entirely: force green, don't ask for confirmation

Output: same as georeference_screenshot.py fit - a tagged GeoTIFF and a
manifest.json entry per successfully-fit year, in the property's output
folder (property.py's property_code()).
"""

import argparse
import json
import math
import os

import cv2
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import Affine
from scipy.spatial import cKDTree
from PIL import Image

from property import fetch_property
from georeference_screenshot import simplified_vertices, fit_similarity, fit_translation_scale, \
    write_boundary_geojson, _merge_into_manifest
from plot_overlay import find_input_screenshots

MIN_CONTOUR_DIM = 80            # ignore tiny contours (noise, unrelated UI elements)

# Boundary-line color auto-detection (see detect_boundary_colors) - not a
# single fixed color: confirmed on real data that norgeibilder.no draws
# the eiendomsgrenser overlay in different colors for different
# properties/sessions (magenta for 123/9 Etnedal, green for 14/987
# Nittedal - both this project's real, manually-captured screenshots).
HSV_SAT_MIN = 100               # min saturation (0-255) to count as a "vivid" pixel
HSV_VAL_MIN = 100               # min value/brightness (0-255), same purpose
HUE_BIN_WIDTH = 16               # degrees, OpenCV hue range is 0-179
HUE_BIN_STEP = HUE_BIN_WIDTH // 2   # 50% overlap, so a true hue near a bin
                                     # edge is never split across two
                                     # under-threshold fragments
FILL_RATIO_MAX = 0.65           # reject a candidate if its largest contour
                                 # fills more than this fraction of its own
                                 # bounding box - a thin boundary line only
                                 # loosely fills its bbox (confirmed 0.0-0.5
                                 # on real data); a solid colored area (a
                                 # lake, a roof) fills most or all of it
MAX_COLOR_CANDIDATES = 4

PIXEL_EPSILONS = (1.5, 2.5, 4.0)          # contour simplification levels tried
DP_WORLD_TOLERANCE = 5.0                  # world simplification used only for DP seed generation
DP_SKIP_COST = math.radians(25.0)         # penalty for the DP aligner skipping a world vertex
TOP_K_DP_SEEDS = 4                        # DP candidates carried through to ICP, per epsilon

ICP_INITIAL_GATE_M = 150.0                # generous first-pass match-acceptance distance
ICP_MIN_GATE_M = 6.0                      # floor the gate shrinks to
ICP_SHRINK = 0.65
ICP_MAX_ITERATIONS = 12
ICP_MIN_POINTS = 4


def extract_boundary_mask(img_rgb, h_lo, h_hi,
                           s_min=HSV_SAT_MIN, v_min=HSV_VAL_MIN):
    """Binary mask of pixels within hue range [h_lo, h_hi] (OpenCV's
    0-179 hue scale, wrapping past 179/0 if h_hi < h_lo - needed for red,
    which straddles that boundary) and above the saturation/value
    thresholds - i.e. "vivid" pixels of roughly this hue, which is what
    isolates a boundary-line color from a photo's own much less
    saturated natural tones (soil, water, muted vegetation)."""
    hsv = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    hue_ok = (h >= h_lo) & (h <= h_hi) if h_lo <= h_hi else (h >= h_lo) | (h <= h_hi)
    mask = hue_ok & (s >= s_min) & (v >= v_min)
    return (mask.astype(np.uint8)) * 255


def _circular_hue_distance(a, b, mod=180):
    d = abs(a - b) % mod
    return min(d, mod - d)


def _hue_range_center(h_lo, h_hi, mod=180):
    if h_lo <= h_hi:
        return (h_lo + h_hi) / 2
    return ((h_lo + h_hi + mod) / 2) % mod


def detect_boundary_colors(img_rgb, max_candidates=MAX_COLOR_CANDIDATES):
    """Find candidate boundary-line hue ranges automatically, rather
    than assuming a single fixed color (see the constants above for
    why). Scans overlapping HUE_BIN_WIDTH-degree hue bins restricted to
    vivid (saturated/bright) pixels, and for each bin checks whether it
    contains a contour large enough to plausibly be a boundary line
    (largest_contour(), the same MIN_CONTOUR_DIM size filter used
    everywhere else in this module) that isn't just a solid-filled
    colored area (FILL_RATIO_MAX - see above). This also naturally
    excludes small colored UI elements, e.g. the parcel-number text
    labels' colored badges: confirmed on a real Nittedal screenshot
    that a label badge can have *more total pixels* than the actual
    boundary line at typical screenshot resolution, but a far smaller
    bounding box - which is exactly what this filters on, not raw pixel
    count. Returns a list of (h_lo, h_hi) hue ranges, largest first,
    capped at max_candidates and de-duplicated (adjacent overlapping
    bins that found the same real line only appear once) - empty if
    nothing plausible was found at all."""
    hsv_full = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV)
    found = []
    for h_lo in range(0, 180, HUE_BIN_STEP):
        h_hi = (h_lo + HUE_BIN_WIDTH - 1) % 180
        hue_ok = (hsv_full[:, :, 0] >= h_lo) & (hsv_full[:, :, 0] <= h_hi) if h_lo <= h_hi \
            else (hsv_full[:, :, 0] >= h_lo) | (hsv_full[:, :, 0] <= h_hi)
        mask = (hue_ok & (hsv_full[:, :, 1] >= HSV_SAT_MIN) &
                (hsv_full[:, :, 2] >= HSV_VAL_MIN)).astype(np.uint8) * 255
        contour = largest_contour(mask)
        if contour is None:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        fill_ratio = cv2.contourArea(contour) / (w * h) if w * h else 1.0
        if fill_ratio > FILL_RATIO_MAX:
            continue
        found.append((w * h, h_lo, h_hi))

    found.sort(key=lambda f: -f[0])
    kept = []
    for bbox_area, h_lo, h_hi in found:
        if any(_circular_hue_distance(h_lo, kept_h_lo) < HUE_BIN_WIDTH
               for _, kept_h_lo, _ in kept):
            continue
        kept.append((bbox_area, h_lo, h_hi))
        if len(kept) >= max_candidates:
            break
    return [(h_lo, h_hi) for _, h_lo, h_hi in kept]


# gnr/bnr label detection (see detect_label_seed_point) - norgeibilder.no
# renders every visible parcel's own number label in the same red-on-white
# style, regardless of property, so this range isn't specific to any one
# property the way the boundary-line hue is. Verified distinct from both
# this project's real boundary-line colors (magenta ~136-159, green
# ~56-71): red wraps the 0/179 hue seam, nowhere near either.
LABEL_HUE_LO, LABEL_HUE_HI = 170, 10    # wraps past 179/0, like extract_boundary_mask
LABEL_SAT_MIN = 120
LABEL_VAL_MIN = 150
LABEL_GLYPH_MIN_AREA = 4        # px - drop sub-pixel-noise specks
LABEL_GLYPH_MAX_AREA = 200      # px - drop anything glyph-sized text isn't (icons, thick lines)
LABEL_GROUP_GAP_PX = 12         # max gap between glyphs still counted as one label
ISOLATE_MASK_DILATE_PX = 2      # closes small antialiasing gaps before flood-filling
ISOLATE_MAX_FILL_FRAC = 0.6     # reject a flood-fill that escaped through a line gap


def _label_glyph_mask(img_rgb, s_min=LABEL_SAT_MIN, v_min=LABEL_VAL_MIN):
    return extract_boundary_mask(img_rgb, LABEL_HUE_LO, LABEL_HUE_HI, s_min=s_min, v_min=v_min)


def detect_label_seed_point(img_rgb, group_gap=LABEL_GROUP_GAP_PX):
    """Find the pixel position of the *target* property's own gnr/bnr
    label - not just any label, since every parcel visible in frame has
    one (its neighbors' included). norgeibilder.no draws every visible
    property's eiendomsgrenser boundary in the same overlay color,
    touching/sharing edges with neighboring parcels (see README) - the
    label position is what lets auto_extract_gcps isolate just the
    target's own loop from that fused mesh via a seeded flood-fill,
    rather than guessing from raw connected-component size.

    Disambiguating which label is the target's own doesn't need OCR:
    this project's whole manual-capture workflow always centers the
    viewer on the target property before taking the screenshot (that's
    the point of navigating to it first), so its label is reliably the
    one closest to the image's own geometric center - verified directly
    on 14/987 Nittedal's real screenshots, where the target's label
    centroid landed within a few pixels of true image-center while a
    neighboring parcel's own label sat far off at the frame edge.

    Returns (x, y) in pixel coordinates, or None if no plausible label
    was found (e.g. text too small/faint in a low-resolution historical
    photo) - callers should fall back to the un-isolated mask."""
    mask = _label_glyph_mask(img_rgb)
    n, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    glyph_idx = [i for i in range(1, n)
                 if LABEL_GLYPH_MIN_AREA <= stats[i, cv2.CC_STAT_AREA] <= LABEL_GLYPH_MAX_AREA]
    if not glyph_idx:
        return None

    parent = {i: i for i in glyph_idx}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for a in range(len(glyph_idx)):
        i = glyph_idx[a]
        xi, yi, wi, hi = stats[i, :4]
        for b in range(a + 1, len(glyph_idx)):
            j = glyph_idx[b]
            xj, yj, wj, hj = stats[j, :4]
            if (xi - group_gap < xj + wj and xj - group_gap < xi + wi and
                    yi - group_gap < yj + hj and yj - group_gap < yi + hi):
                union(i, j)

    groups = {}
    for i in glyph_idx:
        groups.setdefault(find(i), []).append(i)

    height, width = img_rgb.shape[:2]
    img_center = np.array([width / 2.0, height / 2.0])

    best_center, best_dist = None, None
    for members in groups.values():
        if len(members) < 2:
            continue  # a lone glyph-sized blob is more likely noise than a whole label
        pts = centroids[members]
        areas = stats[members, cv2.CC_STAT_AREA].astype(float)
        center = np.average(pts, axis=0, weights=areas)
        dist = float(np.hypot(*(center - img_center)))
        if best_dist is None or dist < best_dist:
            best_center, best_dist = center, dist

    if best_center is None:
        return None
    return float(best_center[0]), float(best_center[1])


def isolate_target_boundary_mask(boundary_mask, seed_xy, dilate_px=ISOLATE_MASK_DILATE_PX,
                                  max_fill_frac=ISOLATE_MAX_FILL_FRAC):
    """Restrict a boundary-color mask - which may include neighboring
    parcels' touching/shared-edge boundary lines, since norgeibilder.no
    draws every visible property's eiendomsgrenser in the same color,
    not just the target's - to just the loop enclosing seed_xy. Works by
    a seeded flood-fill of the *non*-boundary pixels: dilate the mask a
    little (closes small antialiasing gaps in the line), flood-fill the
    complement starting at the seed to find the one enclosed interior
    region, then re-dilate that region and intersect it with the
    original mask to recover exactly the boundary-colored pixels
    immediately surrounding it - excluding any other parcel's line not
    touching this specific interior, whether that's a separate closed
    loop sharing an edge or a dangling arm cut off by the frame.

    Returns the isolated mask (same shape as boundary_mask), or None if
    the fill looks unreliable - escaped through a real gap in the line
    and filled most of the frame, or the seed landed somewhere with no
    nearby free pixel at all - so callers can fall back to the
    un-isolated mask instead of trusting a bad isolation."""
    height, width = boundary_mask.shape[:2]
    kernel = np.ones((dilate_px * 2 + 1, dilate_px * 2 + 1), np.uint8)
    walls = cv2.dilate(boundary_mask, kernel)

    x = min(max(int(round(seed_xy[0])), 0), width - 1)
    y = min(max(int(round(seed_xy[1])), 0), height - 1)
    if walls[y, x]:
        # The label should normally sit inside the property, away from
        # its own line - but if it lands on/inside the dilated wall
        # anyway, search an expanding neighborhood for a free pixel
        # rather than give up outright.
        found = False
        for r in range(1, 15):
            y0, y1 = max(0, y - r), min(height, y + r + 1)
            x0, x1 = max(0, x - r), min(width, x + r + 1)
            free = ~walls[y0:y1, x0:x1].astype(bool)
            if free.any():
                iy, ix = np.argwhere(free)[0]
                y, x = y0 + iy, x0 + ix
                found = True
                break
        if not found:
            return None

    fillable = (~walls.astype(bool)).astype(np.uint8)
    _, comp_labels = cv2.connectedComponents(fillable, connectivity=4)
    target_label = comp_labels[y, x]
    if target_label == 0:
        return None
    interior = (comp_labels == target_label)

    interior_area = int(interior.sum())
    if interior_area == 0 or interior_area > max_fill_frac * width * height:
        return None

    interior_u8 = interior.astype(np.uint8) * 255
    dilated_interior = cv2.dilate(interior_u8, kernel)
    # Intersect with the *dilated* walls, not the original undilated
    # mask: interior's own edge sits at walls' outer boundary (already
    # dilate_px beyond the true line), so re-dilating interior by the
    # same amount brings it back only to about walls' edge, not all the
    # way back to the thin original mask's centerline pixels - verified
    # directly (14/987 Nittedal, 2015): ANDing with boundary_mask
    # produced zero overlap every time, while ANDing with walls (which
    # interior is adjacent to by construction, before any dilation)
    # reliably overlaps.
    isolated = cv2.bitwise_and(dilated_interior, walls)
    if not isolated.any():
        return None
    return isolated


def largest_contour(mask):
    # RETR_EXTERNAL (outer contours only) rather than RETR_LIST: for a
    # boundary that forms a fully closed loop entirely within frame, a
    # thin-stroke mask is topologically a ring, and RETR_LIST would
    # return both its outer and inner (hole) contours as separate,
    # near-duplicate candidates - RETR_EXTERNAL keeps just the outer one.
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if max(w, h) >= MIN_CONTOUR_DIM:
            candidates.append(c)
    if not candidates:
        return None
    return max(candidates, key=lambda c: cv2.boundingRect(c)[2] * cv2.boundingRect(c)[3])


def _touches_border(contour, img_shape, margin=2):
    height, width = img_shape[:2]
    x, y, w, h = cv2.boundingRect(contour)
    return x <= margin or y <= margin or x + w >= width - margin or y + h >= height - margin


def _split_there_and_back(points):
    """A boundary clipped by the screenshot frame isn't a closed loop -
    it's an open arc - but a thin-stroke mask's outline is still
    topologically one closed contour: out along one edge of the stroke,
    back along the other. Both edges trace the same real corners, so
    distance-from-start increases monotonically out to the true far end
    (where the stroke is cut off by the frame) then decreases back to
    (approximately) the start - split at that turnaround point and keep
    only the outbound half, which is now the real corner sequence with
    no doubling."""
    pts = np.asarray(points, dtype=float)
    d = np.hypot(pts[:, 0] - pts[0, 0], pts[:, 1] - pts[0, 1])
    turn = int(np.argmax(d))
    return [tuple(p) for p in points[:turn + 1]]


def _dedupe_consecutive(points, min_dist=4.0):
    """Safety net for the closed-loop (not frame-clipped) case: collapse
    any still-adjacent near-duplicate points (e.g. from stroke-width
    jitter), without assuming a there-and-back doubling."""
    out = []
    for p in points:
        if out and np.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) < min_dist:
            continue
        out.append(p)
    return out


def simplify_contour(contour, img_shape, epsilon):
    """Douglas-Peucker-simplify a contour at a given epsilon. Returns
    (points, is_closed) - is_closed says whether this is a genuine
    closed loop (full boundary visible) or an open arc (frame-clipped;
    see _split_there_and_back). Frame-clipped-ness depends only on the
    raw contour's bounding box, not on epsilon."""
    clipped = _touches_border(contour, img_shape)
    approx = cv2.approxPolyDP(contour, epsilon=epsilon, closed=True)
    raw = [tuple(p) for p in approx.reshape(-1, 2)]
    pts = _split_there_and_back(raw) if clipped else _dedupe_consecutive(raw)
    return pts, not clipped


def _bbox_seed(pixel_pts, world_polygon):
    """Crude scale+translate (no rotation) mapping the pixel contour's
    own bounding box to the property's bounding box - assumes the
    screenshot is roughly north-up and the whole property is visible in
    frame (true of every screenshot this project's manual capture
    process produces). Doesn't need to be accurate: it's only ever used
    as an ICP starting point (see _icp_refine), and verified directly to
    converge correctly even when quite rough."""
    px = np.array([p[0] for p in pixel_pts], dtype=float)
    py = np.array([p[1] for p in pixel_pts], dtype=float)
    if px.max() == px.min() or py.max() == py.min():
        return None
    minx, miny, maxx, maxy = world_polygon.bounds
    pixel_width, pixel_height = px.max() - px.min(), py.max() - py.min()
    scale_x = (maxx - minx) / pixel_width
    scale_y = (maxy - miny) / pixel_height
    # pixel row increases downward, northing increases upward -> y scale is negative
    c = minx - scale_x * px.min()
    f = maxy + scale_y * py.min()
    return Affine(scale_x, 0.0, c, 0.0, -scale_y, f)


def _turn_angles(coords, closed):
    """Interior-angle turn (radians, signed) at each vertex. For a
    closed loop, every point has one (cyclic neighbors). For an open
    chain, only interior points (index 1..n-2) do - the two endpoints
    aren't real corners (see simplify_contour/_split_there_and_back)."""
    coords = np.asarray(coords, dtype=float)
    n = len(coords)
    angles = np.zeros(n if closed else max(n - 2, 0))
    idx = range(n) if closed else range(1, n - 1)
    for out_i, i in enumerate(idx):
        p_prev = coords[i - 1]
        p_curr = coords[i]
        p_next = coords[(i + 1) % n]
        v1 = p_curr - p_prev
        v2 = p_next - p_curr
        a1 = np.arctan2(v1[1], v1[0])
        a2 = np.arctan2(v2[1], v2[0])
        da = (a2 - a1 + np.pi) % (2 * np.pi) - np.pi
        angles[out_i] = da
    return angles


def _dp_align(pixel_angles, world_window, skip_cost):
    """Global sequence alignment (Needleman-Wunsch style): every entry
    of pixel_angles must be matched, in order, to some entry of
    world_window, also in order - but world_window entries may be
    skipped ("inserted", at skip_cost each), so the two sequences don't
    need matching lengths. Returns (total_cost, pairs) where pairs is a
    list of (pixel_index, world_window_index), or None if no valid
    alignment exists (e.g. more pixel points than world_window points).

    Only ever used to generate ICP seeds (see module docstring point
    4b) - its angle-only cost is not, by itself, trusted to identify the
    correct alignment."""
    m, n = len(pixel_angles), len(world_window)
    if m == 0 or n < m:
        return None

    INF = np.inf
    dp = np.full((m + 1, n + 1), INF)
    dp[0, 0] = 0.0
    dp[0, 1:] = np.arange(1, n + 1) * skip_cost

    for i in range(1, m + 1):
        a = pixel_angles[i - 1]
        diff_row = np.abs((a - world_window + np.pi) % (2 * np.pi) - np.pi)
        match_candidates = dp[i - 1, 0:n] + diff_row   # match_candidates[j-1] -> option for dp[i,j]
        running = INF   # will hold dp[i, j-1] as j advances
        row = dp[i]
        for j in range(1, n + 1):
            running = min(running + skip_cost, match_candidates[j - 1])
            row[j] = running

    if not np.any(np.isfinite(dp[m, m:n + 1])):
        return None
    best_j = m + int(np.argmin(dp[m, m:n + 1]))
    total_cost = float(dp[m, best_j])

    # Backtrack from (m, best_j) - not (m, n) - since trailing world
    # points beyond the last real match are simply irrelevant, not
    # points that had to be "paid for" via skip_cost.
    i, j = m, best_j
    pairs = []
    while i > 0:
        if j > 0:
            diff = abs((pixel_angles[i - 1] - world_window[j - 1] + np.pi) % (2 * np.pi) - np.pi)
            if np.isclose(dp[i, j], dp[i - 1, j - 1] + diff, atol=1e-6):
                pairs.append((i - 1, j - 1))
                i -= 1
                j -= 1
                continue
        j -= 1
        if j < 0:
            return None   # inconsistent backtrack - shouldn't happen
    pairs.reverse()
    return total_cost, pairs


def _candidate_alignments(pixel_angles, world_angles, skip_cost, top_k):
    """Try every cyclic starting offset into world_angles and both
    traversal directions, returning the top_k lowest-(angle-)cost DP
    alignments as ICP seed candidates - see module docstring point 4b
    for why more than one, and why none of them are trusted outright."""
    n_world = len(world_angles)
    m_pixel = len(pixel_angles)
    if m_pixel == 0 or n_world < m_pixel:
        return []

    candidates = []
    for direction in (1, -1):
        seq = pixel_angles if direction == 1 else pixel_angles[::-1]
        for offset in range(n_world):
            if direction == 1:
                window = np.array([world_angles[(offset + k) % n_world] for k in range(n_world)])
            else:
                window = np.array([world_angles[(offset - k) % n_world] for k in range(n_world)])
            result = _dp_align(seq, window, skip_cost)
            if result is None:
                continue
            cost, raw_pairs = result
            pairs = []
            for seq_i, win_j in raw_pairs:
                pixel_i = seq_i if direction == 1 else (m_pixel - 1 - seq_i)
                world_i = (offset + win_j) % n_world if direction == 1 else (offset - win_j) % n_world
                pairs.append((pixel_i, world_i))
            candidates.append((cost, direction, offset, pairs))

    candidates.sort(key=lambda c: c[0])
    return candidates[:top_k]


def _icp_refine(seed_transform, pixel_pts, ring_tree, ring_coords,
                 max_iterations=ICP_MAX_ITERATIONS, initial_gate_m=ICP_INITIAL_GATE_M,
                 min_gate_m=ICP_MIN_GATE_M, shrink=ICP_SHRINK, min_points=ICP_MIN_POINTS,
                 dedupe_targets=False, allow_rotation=True):
    """Iterative Closest Point: repeatedly project pixel_pts through the
    current transform, snap each to its nearest point on the property's
    actual (unsimplified) boundary ring, refit from the kept
    correspondences, and shrink the acceptance gate - the standard way
    to refine/correct a rough initial registration. Verified directly
    (see module docstring point 5) to reliably converge to the correct
    alignment even from quite poor/wrong seed transforms (RMSE >100m
    before refinement), as long as the seed is within the same rough
    ballpark (not, e.g., off by an order of magnitude in scale).

    dedupe_targets: several distinct pixel points can snap to the very
    same nearest ring vertex - e.g. a cluster of near-duplicate corner
    candidates around one real corner, or a screenshot-frame crop
    artifact landing near a real vertex (verified directly on 14/987
    Nittedal: 4 separate pixel points all matched the same single world
    vertex, and the resulting over-weighted fit's single worst residual,
    3.77m, was the reported max error). Letting every one of them
    separately pull the least-squares fit toward that one target
    over-weights it and can visibly skew rotation/scale - if True, keep
    only the closest pixel point per distinct world-ring target each
    iteration. Off by default: on a well-conditioned fit (plenty of
    spread-out real corners, e.g. every 123/9 Etnedal screenshot) it
    makes no difference, but on a marginal one (few points to begin
    with, e.g. several 14/987 Nittedal screenshots) discarding points
    can tip a fit that would otherwise pass under the point-count floor
    - auto_extract_gcps tries both and lets its usual
    lowest-RMSE-that-passes-the-sanity-checks selection decide, the same
    candidate-generation pattern used for DP-alignment seeds.

    allow_rotation: which of georeference_screenshot.py's two fit
    functions refits each iteration - fit_similarity() (rotation
    allowed) if True, fit_translation_scale() (rotation fixed at zero -
    the true model; see its own docstring for why) if False. Every
    iteration uses the same one, so an allow_rotation=False run never
    drifts into a rotated result at all, not just discourages it."""
    fit_fn = fit_similarity if allow_rotation else fit_translation_scale
    current = seed_transform
    gate = initial_gate_m
    pixel_arr = np.array(pixel_pts, dtype=float)
    result = None
    for _ in range(max_iterations):
        world_pred = np.column_stack([
            current.a * pixel_arr[:, 0] + current.b * pixel_arr[:, 1] + current.c,
            current.d * pixel_arr[:, 0] + current.e * pixel_arr[:, 1] + current.f])
        dists, idxs = ring_tree.query(world_pred)
        keep = dists <= gate
        if int(keep.sum()) < min_points:
            break
        if dedupe_targets:
            best_for_target = {}
            for local_i in np.nonzero(keep)[0]:
                ring_i = int(idxs[local_i])
                d = dists[local_i]
                if ring_i not in best_for_target or d < best_for_target[ring_i][0]:
                    best_for_target[ring_i] = (d, local_i)
            if len(best_for_target) < min_points:
                break
            kept = [i for _, i in best_for_target.values()]
        else:
            kept = np.nonzero(keep)[0]
        mw = [tuple(ring_coords[idxs[i]]) for i in kept]
        mp = [tuple(pixel_arr[i]) for i in kept]
        current, rmse, max_err, errors = fit_fn(mw, mp)
        result = (current, rmse, max_err, mw, mp, errors)
        gate = max(min_gate_m, gate * shrink)
    return result


def verify_registration(transform, rmse, img_shape, world_polygon):
    """Sanity checks after fitting - catches a wrong contour pick or a
    bad alignment (which would otherwise silently produce a
    plausible-looking but wrong GeoTIFF), rather than trusting the fit
    just because fit_similarity() returned a number."""
    height, width = img_shape[:2]
    pixel_size = float(np.hypot(transform.a, transform.d) + np.hypot(transform.b, transform.e)) / 2
    reasons = []
    if pixel_size <= 0 or pixel_size > 50:
        reasons.append(f"implausible pixel size {pixel_size:.2f} m/px")
    # A pure ratio check (rmse > 3*pixel_size alone) over-rejects fine-
    # resolution screenshots: Nittedal's ~0.08 m/px years genuinely run
    # RMSE 1-2m (still an excellent absolute fit), which is 12-25x
    # pixel size - so some absolute floor is needed too, permissive
    # enough for that case. But requiring *both* conditions (the
    # original "and rmse > 15") let a real bad fit through uncaught:
    # 126/64 Etnedal's 2023 screenshot fit "succeeded" at RMSE=7.67m
    # (pixel_size ~0.148 m/px) - 17x the pixel size, but under the old
    # 15m floor - and was visibly, unambiguously wrong when checked
    # against the live boundary (see plot_overlay.py output), not just
    # a borderline case. Every genuinely good fit across this project's
    # real data, at any pixel size, has stayed under 2.55m RMSE - so
    # reject past whichever is more permissive of a flat 5m absolute
    # cap (over 2x that worst known-good case) or 3x pixel size (scales
    # up for a genuinely coarse screenshot, not just a fine one).
    if rmse > max(5.0, 3 * pixel_size):
        reasons.append(f"RMSE {rmse:.1f}m is large relative to pixel size {pixel_size:.2f}m/px")
    if not _is_plausible_rotation(transform):
        reasons.append(f"implied rotation {_rotation_angle_deg(transform):.1f} deg is not "
                        f"plausible for a north-up screenshot")

    # the known boundary, projected into pixel space, should mostly land
    # within a generous margin of the image bounds - if it's wildly
    # outside, the alignment is almost certainly wrong
    Minv = np.linalg.inv(np.array([[transform.a, transform.b], [transform.d, transform.e]]))
    coords = np.array(world_polygon.exterior.coords)
    px = (Minv @ (coords - np.array([transform.c, transform.f])).T).T
    margin = 0.5 * max(width, height)
    frac_outside = float(np.mean(
        (px[:, 0] < -margin) | (px[:, 0] > width + margin) |
        (px[:, 1] < -margin) | (px[:, 1] > height + margin)))
    if frac_outside > 0.9:
        reasons.append(f"{frac_outside:.0%} of the known boundary projects far outside the image")

    return (len(reasons) == 0), reasons, pixel_size


MAX_ROTATION_DEG = 20.0      # max allowed deviation from north-up (see _is_plausible_rotation)


def _rotation_angle_deg(transform):
    """Angle (degrees) between "pixel +x" and "world +easting" under
    this transform - 0 for a perfectly north-up, unrotated screenshot."""
    return float(np.degrees(np.arctan2(transform.d, transform.a)))


def _is_plausible_rotation(transform, max_deg=MAX_ROTATION_DEG):
    """norgeibilder.no's viewer has no rotation control a user could
    accidentally trigger - every screenshot this project's manual
    capture process produces is north-up. This turned out to be an
    essential, not optional, check: verified directly on this project's
    real screenshots that ICP can converge to a self-consistent but
    wrong registration - RMSE as low as ~1-2m on its own self-selected
    points, low enough to look like a good fit and pass verify_
    registration's other checks - where the image ends up rotated by
    anywhere from ~25 deg to ~155-160 deg (effectively upside down) from
    reality. A low RMSE alone is not sufficient evidence of correctness;
    this rotation-plausibility check is what actually catches those
    cases, applied to every candidate before RMSE comparison, not just
    the eventual winner."""
    # arctan2 already returns a value in (-180, 180] - no extra folding
    # needed. Deliberately not treated as "close to 0 mod 180": a true
    # 180-degree-flipped (upside-down) registration is exactly as wrong
    # as any other large rotation and must be rejected the same way.
    #
    # This project used to also separately check the transform's two
    # axis scales weren't anisotropic (a real failure mode once seen on
    # a real screenshot: ICP locked onto a repetitive zigzag section of
    # the boundary and produced one axis ~30x the other's scale, while
    # still reporting a deceptively low RMSE) - now removed as
    # unreachable dead code, not just unneeded: fit_similarity()
    # (georeference_screenshot.py) fits a constrained similarity
    # transform, which is isotropic *by construction*, so every
    # transform this module ever produces already satisfies that
    # check unconditionally.
    angle = _rotation_angle_deg(transform)
    return abs(angle) <= max_deg


def _generate_seeds(pixel_pts, pixel_closed, world_polygon, top_k_dp_seeds, allow_rotation=True):
    """Both independent seed strategies (see module docstring point 4) -
    a list of candidate transforms, none individually trusted.

    allow_rotation: passed through to which fit function builds the
    DP-alignment seeds (see _icp_refine's own allow_rotation for why) -
    _bbox_seed is unaffected either way, since it's already rotation-
    free and ICP immediately refits it with the right one regardless."""
    seeds = []
    fit_fn = fit_similarity if allow_rotation else fit_translation_scale

    bbox_seed = _bbox_seed(pixel_pts, world_polygon)
    if bbox_seed is not None:
        seeds.append(bbox_seed)

    pixel_angles = _turn_angles(pixel_pts, closed=pixel_closed)
    if len(pixel_angles) >= 4:
        world_coords = simplified_vertices(world_polygon, DP_WORLD_TOLERANCE)
        if len(world_coords) >= len(pixel_angles):
            world_angles = _turn_angles(world_coords, closed=True)
            start_shift = 0 if pixel_closed else 1
            for cost, direction, offset, pairs in _candidate_alignments(
                    pixel_angles, world_angles, DP_SKIP_COST, top_k_dp_seeds):
                gw, gp = [], []
                for pixel_i, world_i in pairs:
                    gw.append(world_coords[world_i])
                    gp.append(pixel_pts[pixel_i + start_shift])
                if len(gw) < 3:
                    continue
                try:
                    seed_transform, _, _, _ = fit_fn(gw, gp)
                except np.linalg.LinAlgError:
                    continue
                seeds.append(seed_transform)

    return seeds


def auto_extract_gcps(screenshot_path, world_polygon,
                       pixel_epsilons=PIXEL_EPSILONS, top_k_dp_seeds=TOP_K_DP_SEEDS,
                       boundary_color=None):
    """Returns (gcps_world, gcps_pixel, transform, rmse, max_err,
    diagnostics) - the best-scoring ICP-refined fit across every color
    candidate, seed, and contour simplification level tried - or raises
    ValueError with a clear reason if no confident match is found at
    all.

    boundary_color: optional (h_lo, h_hi) hue range (OpenCV 0-179 scale)
    to force a specific color instead of auto-detecting (see
    detect_boundary_colors and main()'s --boundary-color, which accepts
    a hex RGB string and converts it) - for the rare screenshot where
    detection picks the wrong candidate, e.g. because two similarly
    prominent vivid regions are both plausible line shapes."""
    img = np.array(Image.open(screenshot_path).convert("RGB"))

    if boundary_color is not None:
        # The confirmed color can legitimately drift a bit from
        # screenshot to screenshot - verified directly on 123/9
        # Etnedal, whose real eiendomsgrenser magenta shifts from hue
        # ~143 (its first four screenshots) to ~151 (its later four),
        # straddling the edge of a single fixed hue bin's tolerance and
        # making forcing one exact range fail outright on half the
        # batch. Try the confirmed color first (the fast, normal path),
        # then fall back to this screenshot's own detected candidates,
        # closest-hue-first, so a real per-screenshot shift still finds
        # the right line instead of failing loudly for no good reason.
        own_candidates = [c for c in detect_boundary_colors(img) if c != boundary_color]
        forced_center = _hue_range_center(*boundary_color)
        own_candidates.sort(
            key=lambda c: _circular_hue_distance(_hue_range_center(*c), forced_center))
        color_candidates = [boundary_color] + own_candidates
    else:
        color_candidates = detect_boundary_colors(img)
    if not color_candidates:
        raise ValueError(
            "no boundary-colored contour found in any hue - the eiendomsgrenser layer may not "
            "have been on in this screenshot, or its line color is too faint/desaturated to "
            "isolate automatically (try --boundary-color to force a specific color)")

    ring_coords = np.array(list(world_polygon.exterior.coords))
    ring_tree = cKDTree(ring_coords)

    label_seed = detect_label_seed_point(img)

    def best_fit_for_contour(contour, h_lo, h_hi, allow_rotation):
        """Search every epsilon/seed/dedupe combination for one already-
        extracted contour at one rotation-freedom level, returning
        (best_local_or_None, attempts). Prefers a candidate that
        actually passes verify_registration over one with merely lower
        raw RMSE - a few points can fit deceptively tightly by chance
        (near-collinear or otherwise unrepresentative), scoring a lower
        RMSE than a well-supported, genuinely-correct candidate right
        next to it in the same search grid while failing verification
        for an unrelated reason (e.g. projecting mostly outside the
        image) - picking by raw RMSE alone at this fine a grain can
        silently prefer the degenerate one and never surface the good
        one to the caller at all."""
        local_best = None
        local_best_ok = False
        local_attempts = 0

        for epsilon in pixel_epsilons:
            pixel_pts, pixel_closed = simplify_contour(contour, img.shape, epsilon)
            if len(pixel_pts) < 6:
                continue

            for seed_transform in _generate_seeds(pixel_pts, pixel_closed, world_polygon,
                                                    top_k_dp_seeds, allow_rotation=allow_rotation):
                for dedupe_targets in (False, True):
                    local_attempts += 1
                    icp_result = _icp_refine(seed_transform, pixel_pts, ring_tree, ring_coords,
                                              dedupe_targets=dedupe_targets,
                                              allow_rotation=allow_rotation)
                    if icp_result is None:
                        continue
                    transform, rmse, max_err, gcps_world, gcps_pixel, errors = icp_result
                    if len(gcps_world) < ICP_MIN_POINTS:
                        continue
                    if not _is_plausible_rotation(transform):
                        continue

                    ok, _, _ = verify_registration(transform, rmse, img.shape, world_polygon)
                    if (local_best is None or (ok and not local_best_ok)
                            or (ok == local_best_ok and rmse < local_best[0])):
                        diagnostics = {
                            "epsilon": epsilon, "n_pixel_corners": len(pixel_pts),
                            "pixel_closed": pixel_closed, "n_gcp": len(gcps_world),
                            "contour_bbox": cv2.boundingRect(contour),
                            "hue_range": (h_lo, h_hi), "dedupe_targets": dedupe_targets,
                            "allow_rotation": allow_rotation,
                        }
                        local_best = (rmse, max_err, gcps_world, gcps_pixel, transform, diagnostics)
                        local_best_ok = ok
        return local_best, local_attempts

    def best_fit_for_color(h_lo, h_hi, allow_rotation):
        """Try the target property's own isolated boundary loop first
        (see isolate_target_boundary_mask - excludes neighboring
        parcels' touching/shared-edge lines, which the raw whole-mask
        contour doesn't distinguish from the target's own), falling
        back to the raw whole-mask contour if isolation isn't available
        or doesn't work out for this screenshot. Returns
        (best_local_or_None, attempts, contour_found)."""
        mask = extract_boundary_mask(img, h_lo, h_hi)
        attempts = 0

        if label_seed is not None:
            isolated = isolate_target_boundary_mask(mask, label_seed)
            if isolated is not None:
                contour = largest_contour(isolated)
                if contour is not None:
                    local_best, local_attempts = best_fit_for_contour(contour, h_lo, h_hi, allow_rotation)
                    attempts += local_attempts
                    if local_best is not None:
                        local_best[5]["label_seeded"] = True
                        return local_best, attempts, True

        contour = largest_contour(mask)
        if contour is None:
            return None, attempts, attempts > 0
        local_best, local_attempts = best_fit_for_contour(contour, h_lo, h_hi, allow_rotation)
        attempts += local_attempts
        if local_best is not None:
            local_best[5]["label_seeded"] = False
        return local_best, attempts, True

    def search_colors(allow_rotation):
        """Search every color candidate at one rotation-freedom level,
        returning (best_or_None, ok, attempts, tried_any_contour) - same
        per-color tiering as before (confirmed color first, drift-
        fallback colors only tried if the confirmed one doesn't verify),
        just parameterized by which fit family every candidate uses -
        see the outer allow_rotation loop below for why this whole
        search is itself repeated once per rotation-freedom level,
        rather than rotation being decided within one color alone."""
        best = None
        best_ok = False
        attempts = 0
        tried_any_contour = False
        for h_lo, h_hi in color_candidates:
            local_best, local_attempts, contour_found = best_fit_for_color(h_lo, h_hi, allow_rotation)
            attempts += local_attempts
            tried_any_contour = tried_any_contour or contour_found
            if local_best is None:
                continue

            if boundary_color is None:
                # free auto-detection: every candidate is equally
                # untrusted, so search all of them and let the lowest
                # RMSE that passes the sanity checks win, same as always.
                if best is None or local_best[0] < best[0]:
                    best = local_best
                continue

            # boundary_color given: color_candidates[0] is the confirmed,
            # trusted color and the rest are only a drift-tolerance fallback
            # for when it doesn't actually produce a trustworthy fit - not
            # just "produces something", but something that would itself
            # pass verify_registration's own bar (checked here directly,
            # not a separate, looser threshold that could drift out of sync
            # with it - the internal per-candidate filter above only checks
            # rotation plausibility, not RMSE-vs-pixel-size). Verified this
            # distinction matters for real: 126/64 Etnedal's confirmed color
            # still found *some* candidate passing that looser filter for
            # 2019/2023 (RMSE 9.66m/5.26m - clearly the wrong color for
            # those specific years, a real drift case, see
            # detect_boundary_colors' docstring), which used to stop the
            # search right there, before ever trying the color that
            # actually fit those years well. Once any color's result
            # genuinely passes, stop - competing a fallback hue against an
            # already-good confirmed-color fit purely on RMSE risks a
            # razor-thin, wrong win instead (verified separately: 14/987
            # Nittedal's 2017 screenshot had a stray hue candidate beat the
            # correct green line by 0.01m RMSE while being visibly worse -
            # 18.9 deg of implied rotation vs. 5.8).
            ok, _, _ = verify_registration(local_best[4], local_best[0], img.shape, world_polygon)
            if best is None or (ok and not best_ok) or (ok == best_ok and local_best[0] < best[0]):
                best = local_best
                best_ok = ok
            if ok:
                break

        if boundary_color is None and best is not None:
            best_ok, _, _ = verify_registration(best[4], best[0], img.shape, world_polygon)
        return best, best_ok, attempts, tried_any_contour

    # Rotation is a last resort, tried only across *every* color
    # candidate at zero rotation first failing to verify - not decided
    # within one color alone. Confirmed this distinction matters for
    # real, not just in principle: on 124/9 Etnedal's 2011 screenshot,
    # the confirmed (but for that year, hue-drifted-wrong) color's own
    # zero-rotation search correctly found nothing, but letting *that
    # one wrong color* fall back to rotation found a self-consistent,
    # verify_registration-passing-but-wrong fit (RMSE 2.3m, rotation
    # -6.6 degrees) before ever trying the drift-fallback color that
    # fits this year well at zero rotation - rotation "explaining away"
    # a wrong color, not just wrong-correspondence noise within a
    # right one. Trying every color at zero rotation before letting any
    # color use rotation closes that gap.
    best = None
    best_ok = False
    attempts = 0
    tried_any_contour = False
    for allow_rotation in (False, True):
        round_best, round_ok, round_attempts, round_contour = search_colors(allow_rotation)
        attempts += round_attempts
        tried_any_contour = tried_any_contour or round_contour
        if round_best is not None and (best is None or (round_ok and not best_ok)
                                        or (round_ok == best_ok and round_best[0] < best[0])):
            best = round_best
            best_ok = round_ok
        if round_ok:
            break

    if not tried_any_contour:
        raise ValueError(
            f"no boundary-colored contour found (tried hue ranges {color_candidates} - the "
            f"eiendomsgrenser layer may not have been on in this screenshot)")
    if best is None:
        raise ValueError(
            f"no confident GCP alignment found (tried {attempts} seed transforms across "
            f"{len(color_candidates)} color candidate(s) and the contour simplification grid) "
            f"- contour bbox may be wrong, or too few real corners visible")

    rmse, max_err, gcps_world, gcps_pixel, transform, diagnostics = best
    return gcps_world, gcps_pixel, transform, rmse, max_err, diagnostics


def fit_year(prop, outdir, basename, screenshot_path, year, boundary_color=None):
    gcps_world, gcps_pixel, transform, rmse, max_err, diag = auto_extract_gcps(
        screenshot_path, prop.polygon, boundary_color=boundary_color)

    img = np.array(Image.open(screenshot_path).convert("RGB"))
    ok, reasons, pixel_size = verify_registration(transform, rmse, img.shape, prop.polygon)

    seeded = "label-isolated" if diag.get("label_seeded") else "whole-mask"
    print(f"  {year}: {diag['n_gcp']} ICP-matched GCPs (epsilon={diag['epsilon']:g}px, "
          f"hue={diag['hue_range']}, {seeded}), RMSE={rmse:.2f}m max_err={max_err:.2f}m, "
          f"pixel_size~{pixel_size:.3f}m/px")
    if not ok:
        print(f"    REJECTED - {'; '.join(reasons)}")
        return None

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
            GEOREFERENCING_METHOD=f"automatic GCP extraction + ICP registration, "
                                   f"{diag['n_gcp']} points",
            RMSE_M=f"{rmse:.2f}", MAX_ERROR_M=f"{max_err:.2f}",
            PIXEL_SIZE_M=f"{pixel_size:.3f}",
            WARNING="Approximate - georeferenced from a browser screenshot via automatic "
                    "boundary detection, not the source WMS. See auto_gcp.py's docstring.",
        )

    manifest_path = os.path.join(outdir, f"{basename}_manifest.json")
    record = {
        "filename": filename, "year": year,
        "width_px": img.shape[1], "height_px": img.shape[0],
        "pixel_size_m": [round(pixel_size, 3), round(pixel_size, 3)],
        "georeferencing_method": "automatic_gcp_extraction",
        "n_gcp": diag["n_gcp"], "rmse_m": round(rmse, 2), "max_error_m": round(max_err, 2),
    }
    _merge_into_manifest(manifest_path, prop, record, basename)
    print(f"    Saved {out_path}")
    return out_path


def _parse_hex_color(s):
    """'ff00ff' or '#ff00ff' -> (h_lo, h_hi) hue range, centered on that
    color's exact OpenCV hue with +-HUE_BIN_WIDTH/2 tolerance - for
    --boundary-color, forcing a specific color instead of auto-detecting
    (see detect_boundary_colors)."""
    s = s.lstrip("#")
    if len(s) != 6:
        raise argparse.ArgumentTypeError(f"--boundary-color must be 6 hex digits (RRGGBB), got {s!r}")
    try:
        r, g, b = (int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise argparse.ArgumentTypeError(f"--boundary-color must be 6 hex digits (RRGGBB), got {s!r}")
    hue = int(cv2.cvtColor(np.uint8([[[r, g, b]]]), cv2.COLOR_RGB2HSV)[0, 0, 0])
    half = HUE_BIN_WIDTH // 2
    return ((hue - half) % 180, (hue + half) % 180)


_HUE_NAMES = [
    (5, "red"), (18, "orange"), (33, "yellow"), (75, "green"),
    (95, "cyan"), (130, "blue"), (155, "magenta/pink"), (172, "red/pink"),
]


def _describe_hue_range(h_lo, h_hi):
    """(h_lo, h_hi) -> a human-readable "name (hex)" string, for showing
    a detected color to the user in a way that's actually recognizable
    (a raw OpenCV hue number like 70 means nothing at a glance)."""
    center = ((h_lo + h_hi) / 2) if h_hi >= h_lo else (((h_lo + h_hi + 180) / 2) % 180)
    name = next((n for cutoff, n in _HUE_NAMES if center <= cutoff), "red")
    rgb = cv2.cvtColor(np.uint8([[[int(center), 255, 255]]]), cv2.COLOR_HSV2RGB)[0, 0]
    return f"{name} (#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x})"


def confirm_boundary_color(candidates, auto_yes=False):
    """Interactively confirm the auto-detected boundary-line color
    before proceeding, rather than silently trusting detection -
    prompts with the top candidate, and on "no" offers the next one
    (detect_boundary_colors already ranks them by how convincingly
    line-shaped their largest contour is), or lets the user type a hex
    color directly. Returns the confirmed (h_lo, h_hi), or None if the
    user aborts (empty candidate list, or explicitly declines all of
    them). auto_yes=True (--yes) skips the prompt and accepts the top
    candidate outright, for non-interactive/scripted use."""
    if not candidates:
        return None
    if auto_yes:
        print(f"Boundary color (auto-accepted, --yes): {_describe_hue_range(*candidates[0])}")
        return candidates[0]

    remaining = list(candidates)
    while remaining:
        h_lo, h_hi = remaining.pop(0)
        answer = input(
            f"Detected boundary-line color: {_describe_hue_range(h_lo, h_hi)} - "
            f"use this for the whole batch? [Y/n, or type a hex color like ff00ff]: "
        ).strip()
        if answer == "" or answer.lower() in ("y", "yes"):
            return (h_lo, h_hi)
        if answer.lower() in ("n", "no"):
            continue
        try:
            return _parse_hex_color(answer)
        except argparse.ArgumentTypeError as e:
            print(f"  {e} - try again.")
            remaining.insert(0, (h_lo, h_hi))

    print("No candidate color confirmed.")
    hex_answer = input("Type a hex color directly (e.g. ff00ff), or leave blank to abort: ").strip()
    if not hex_answer:
        return None
    return _parse_hex_color(hex_answer)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kommune", required=True)
    ap.add_argument("--gnr", type=int, required=True)
    ap.add_argument("--bnr", type=int, required=True)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--years", type=int, nargs="+", default=None,
                     help="only these years (default: every un-fitted raw screenshot found)")
    ap.add_argument("--boundary-color", type=_parse_hex_color, default=None,
                     help="force a specific eiendomsgrenser line color instead of "
                          "auto-detecting/confirming interactively - a 6-digit hex RGB, "
                          "e.g. ff00ff for magenta or 08bc46 for green")
    ap.add_argument("--yes", action="store_true",
                     help="skip the interactive color-confirmation prompt, auto-accepting "
                          "the top auto-detected candidate (for non-interactive/scripted use; "
                          "ignored if --boundary-color is also given)")
    args = ap.parse_args()

    prop = fetch_property(args.kommune, args.gnr, args.bnr)
    basename = prop.code
    outdir = args.outdir or basename

    input_screenshots = find_input_screenshots(outdir, args.gnr, args.bnr, args.kommune)
    manifest_path = os.path.join(outdir, f"{basename}_manifest.json")
    fitted_years = set()
    if os.path.isfile(manifest_path):
        with open(manifest_path) as f:
            fitted_years = {r["year"] for r in json.load(f)["images"]}

    years = args.years or sorted(set(input_screenshots) - fitted_years)
    if not years:
        print("Nothing to do - no un-fitted input screenshots found.")
        return

    # Color is confirmed once (from the first available screenshot) and
    # reused for the whole batch, not re-detected/re-asked per year -
    # confirmed on real data that norgeibilder.no's eiendomsgrenser color
    # is consistent across every screenshot of the same property (both
    # Etnedal's magenta and Nittedal's green held across all years
    # tested), even though it differs *between* properties.
    boundary_color = args.boundary_color
    if boundary_color is None:
        first_year = next((y for y in years if y in input_screenshots), None)
        if first_year is not None:
            img = np.array(Image.open(input_screenshots[first_year]).convert("RGB"))
            candidates = detect_boundary_colors(img)
            boundary_color = confirm_boundary_color(candidates, auto_yes=args.yes)
            if boundary_color is None:
                print("No boundary color confirmed - aborting "
                      "(pass --boundary-color to skip this prompt).")
                return

    write_boundary_geojson(prop, outdir, basename)
    print(f"Auto-fitting {len(years)} year(s): {years}")
    ok, failed = [], []
    for year in years:
        if year not in input_screenshots:
            print(f"  {year}: no input screenshot found - skipping")
            failed.append(year)
            continue
        try:
            result = fit_year(prop, outdir, basename, input_screenshots[year], year,
                               boundary_color=boundary_color)
            (ok if result else failed).append(year)
        except ValueError as e:
            print(f"  {year}: FAILED - {e}")
            failed.append(year)

    print(f"\n{len(ok)} succeeded: {ok}")
    if failed:
        print(f"{len(failed)} need manual attention (georeference_screenshot.py's "
              f"list-vertices/pick/fit): {failed}")


if __name__ == "__main__":
    main()
