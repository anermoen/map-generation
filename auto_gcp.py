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
   distinctive magenta - color-threshold the screenshot to get a binary
   mask of all boundary lines visible (every nearby parcel, not just
   ours), via OpenCV.
2. Find the mask's connected contours (cv2.findContours) and pick the
   one with the largest bounding box - the property being viewed is
   presumably the one the screenshot was framed around, so its boundary
   is the most prominent line in frame. (A real assumption, not a
   certainty - see verify_registration() below for how this is checked
   after the fact, not just trusted.)
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
   *global* transform left nonsensical - degenerate/anisotropic scale,
   or the image effectively rotated ~25-160 degrees from reality - while
   still reporting a deceptively low RMSE. A low RMSE alone is
   therefore not sufficient evidence of a correct fit.
6. So every ICP result, not just the eventual winner, is filtered
   *before* RMSE is allowed to pick between them: _is_isotropic rejects
   transforms whose two axis scales differ by more than 15% (every
   genuine fit on real data lands within ~0.6%; the observed failure
   case was 2.36x), and _is_plausible_rotation rejects anything more
   than 20 degrees from north-up (real screenshots are all ~0 degrees;
   observed failures ranged 25-160 degrees) - norgeibilder.no's viewer
   has no rotation control, so every genuine screenshot is north-up.
   Only among the survivors does the lowest final RMSE win, and
   verify_registration() then double-checks that winner once more
   (isotropy and rotation again, plus: RMSE relative to implied pixel
   size, and the known boundary - projected into pixel space - mostly
   landing within the image bounds) before it's trusted.

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
    # -> for every raw input screenshot found (see plot_overlay.py's
    #    find_input_screenshots()) that isn't already in manifest.json,
    #    auto-extract GCPs, fit, and report per-year RMSE/quality.

    python3 auto_gcp.py --kommune Etnedal --gnr 123 --bnr 9 --years 1991 2006
    # -> just these years

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
from georeference_screenshot import simplified_vertices, fit_affine, write_boundary_geojson, \
    _merge_into_manifest
from plot_overlay import find_input_screenshots

MAGENTA_MIN_CHANNEL = 120       # R,B threshold for the boundary line's color
MAGENTA_GAP = 25                # how much higher R,B must be than G
MIN_CONTOUR_DIM = 80            # ignore tiny contours (noise, unrelated UI elements)

PIXEL_EPSILONS = (1.5, 2.5, 4.0)          # contour simplification levels tried
DP_WORLD_TOLERANCE = 5.0                  # world simplification used only for DP seed generation
DP_SKIP_COST = math.radians(25.0)         # penalty for the DP aligner skipping a world vertex
TOP_K_DP_SEEDS = 4                        # DP candidates carried through to ICP, per epsilon

ICP_INITIAL_GATE_M = 150.0                # generous first-pass match-acceptance distance
ICP_MIN_GATE_M = 6.0                      # floor the gate shrinks to
ICP_SHRINK = 0.65
ICP_MAX_ITERATIONS = 12
ICP_MIN_POINTS = 4


def extract_boundary_mask(img_rgb):
    r = img_rgb[:, :, 0].astype(int)
    g = img_rgb[:, :, 1].astype(int)
    b = img_rgb[:, :, 2].astype(int)
    mask = ((r > MAGENTA_MIN_CHANNEL) & (b > MAGENTA_MIN_CHANNEL) &
            (g < r - MAGENTA_GAP) & (g < b - MAGENTA_GAP))
    return (mask.astype(np.uint8)) * 255


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
                 min_gate_m=ICP_MIN_GATE_M, shrink=ICP_SHRINK, min_points=ICP_MIN_POINTS):
    """Iterative Closest Point: repeatedly project pixel_pts through the
    current transform, snap each to its nearest point on the property's
    actual (unsimplified) boundary ring, refit from the kept
    correspondences, and shrink the acceptance gate - the standard way
    to refine/correct a rough initial registration. Verified directly
    (see module docstring point 5) to reliably converge to the correct
    alignment even from quite poor/wrong seed transforms (RMSE >100m
    before refinement), as long as the seed is within the same rough
    ballpark (not, e.g., off by an order of magnitude in scale).
    Returns (transform, rmse, max_err, world_pts, pixel_pts, errors) or
    None if it never had enough points within the gate to fit."""
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
        mw = [tuple(ring_coords[i]) for i in idxs[keep]]
        mp = [tuple(pixel_arr[i]) for i in np.nonzero(keep)[0]]
        current, rmse, max_err, errors = fit_affine(mw, mp)
        result = (current, rmse, max_err, mw, mp, errors)
        gate = max(min_gate_m, gate * shrink)
    return result


def verify_registration(transform, rmse, img_shape, world_polygon):
    """Sanity checks after fitting - catches a wrong contour pick or a
    bad alignment (which would otherwise silently produce a
    plausible-looking but wrong GeoTIFF), rather than trusting the fit
    just because fit_affine() returned a number."""
    height, width = img_shape[:2]
    pixel_size = float(np.hypot(transform.a, transform.d) + np.hypot(transform.b, transform.e)) / 2
    reasons = []
    if pixel_size <= 0 or pixel_size > 50:
        reasons.append(f"implausible pixel size {pixel_size:.2f} m/px")
    if rmse > 3 * pixel_size and rmse > 15:
        reasons.append(f"RMSE {rmse:.1f}m is large relative to pixel size {pixel_size:.2f}m/px")
    if not _is_isotropic(transform):
        reasons.append("transform scale is not isotropic (likely a degenerate fit)")
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


MAX_SCALE_ANISOTROPY = 1.15  # max allowed ratio between a transform's two axis scales -
                              # every genuine fit on this project's real screenshots landed
                              # within 0.6% of a perfectly square pixel (ratio ~1.000-1.006);
                              # a degenerate fit (points too clustered/collinear to properly
                              # constrain the affine fit) was directly observed at ratio 2.36 -
                              # this threshold has ample margin above the real cases while
                              # firmly excluding that failure mode
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
    angle = _rotation_angle_deg(transform)
    return abs(angle) <= max_deg


def _is_isotropic(transform, max_ratio=MAX_SCALE_ANISOTROPY):
    """A genuine screenshot-to-world transform has very close to the
    same scale in both pixel axes (screenshots aren't stretched
    differently in x vs y). A wildly anisotropic fit - verified as a
    real failure mode on this project's actual 1991 screenshot, where
    ICP locked onto a small, repetitive zigzag section of the boundary
    and produced a transform with one axis ~30x the other's scale,
    while still reporting a deceptively low RMSE on that self-selected
    handful of points - is a strong, cheap signal the fit is degenerate
    (under-constrained by near-collinear or clustered points), and
    should be disqualified before it's allowed to win on RMSE alone."""
    scale_x = np.hypot(transform.a, transform.d)
    scale_y = np.hypot(transform.b, transform.e)
    if scale_x <= 0 or scale_y <= 0:
        return False
    ratio = max(scale_x / scale_y, scale_y / scale_x)
    return ratio <= max_ratio


def _generate_seeds(pixel_pts, pixel_closed, world_polygon, top_k_dp_seeds):
    """Both independent seed strategies (see module docstring point 4) -
    a list of candidate transforms, none individually trusted."""
    seeds = []

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
                    seed_transform, _, _, _ = fit_affine(gw, gp)
                except np.linalg.LinAlgError:
                    continue
                seeds.append(seed_transform)

    return seeds


def auto_extract_gcps(screenshot_path, world_polygon,
                       pixel_epsilons=PIXEL_EPSILONS, top_k_dp_seeds=TOP_K_DP_SEEDS):
    """Returns (gcps_world, gcps_pixel, transform, rmse, max_err,
    diagnostics) - the best-scoring ICP-refined fit across every seed
    and contour simplification level tried - or raises ValueError with
    a clear reason if no confident match is found at all."""
    img = np.array(Image.open(screenshot_path).convert("RGB"))
    mask = extract_boundary_mask(img)
    contour = largest_contour(mask)
    if contour is None:
        raise ValueError("no boundary-colored contour found (magenta threshold too strict, "
                          "or the eiendomsgrenser layer wasn't on in this screenshot)")

    ring_coords = np.array(list(world_polygon.exterior.coords))
    ring_tree = cKDTree(ring_coords)

    best = None
    attempts = 0
    for epsilon in pixel_epsilons:
        pixel_pts, pixel_closed = simplify_contour(contour, img.shape, epsilon)
        if len(pixel_pts) < 6:
            continue

        for seed_transform in _generate_seeds(pixel_pts, pixel_closed, world_polygon, top_k_dp_seeds):
            attempts += 1
            icp_result = _icp_refine(seed_transform, pixel_pts, ring_tree, ring_coords)
            if icp_result is None:
                continue
            transform, rmse, max_err, gcps_world, gcps_pixel, errors = icp_result
            if len(gcps_world) < ICP_MIN_POINTS:
                continue
            if not _is_isotropic(transform) or not _is_plausible_rotation(transform):
                continue

            if best is None or rmse < best[0]:
                diagnostics = {
                    "epsilon": epsilon, "n_pixel_corners": len(pixel_pts),
                    "pixel_closed": pixel_closed, "n_gcp": len(gcps_world),
                    "contour_bbox": cv2.boundingRect(contour),
                }
                best = (rmse, max_err, gcps_world, gcps_pixel, transform, diagnostics)

    if best is None:
        raise ValueError(
            f"no confident GCP alignment found (tried {attempts} seed transforms across "
            f"the contour simplification grid) - contour bbox may be wrong, or too few real "
            f"corners visible")

    rmse, max_err, gcps_world, gcps_pixel, transform, diagnostics = best
    return gcps_world, gcps_pixel, transform, rmse, max_err, diagnostics


def fit_year(prop, outdir, basename, screenshot_path, year):
    gcps_world, gcps_pixel, transform, rmse, max_err, diag = auto_extract_gcps(
        screenshot_path, prop.polygon)

    img = np.array(Image.open(screenshot_path).convert("RGB"))
    ok, reasons, pixel_size = verify_registration(transform, rmse, img.shape, prop.polygon)

    print(f"  {year}: {diag['n_gcp']} ICP-matched GCPs (epsilon={diag['epsilon']:g}px), "
          f"RMSE={rmse:.2f}m max_err={max_err:.2f}m, pixel_size~{pixel_size:.3f}m/px")
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kommune", required=True)
    ap.add_argument("--gnr", type=int, required=True)
    ap.add_argument("--bnr", type=int, required=True)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--years", type=int, nargs="+", default=None,
                     help="only these years (default: every un-fitted raw screenshot found)")
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

    write_boundary_geojson(prop, outdir, basename)
    print(f"Auto-fitting {len(years)} year(s): {years}")
    ok, failed = [], []
    for year in years:
        if year not in input_screenshots:
            print(f"  {year}: no input screenshot found - skipping")
            failed.append(year)
            continue
        try:
            result = fit_year(prop, outdir, basename, input_screenshots[year], year)
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
