#!/usr/bin/env python3
"""Generate a printable interlocking jigsaw of exactly N pieces from a map 3MF.

The map surface is the thing being protected.  New York is dense enough that a
jigsaw outline dragged across it lands on a building in almost every segment,
which is why ``plan_map_chunks`` treats footprints as hard keep-outs.  So the
puzzle is cut on two levels instead of one:

* **Above the floor plane** every piece is a plain rectangle of a regular
  ``rows x cols`` grid.  Seen from above the map is crossed by straight,
  hairline-thin lines and nothing else -- a building is clipped by a straight
  edge or not at all, and no seam wanders.
* **Below the floor plane** the same joint becomes a classic jigsaw curve with
  a knob on every edge.  None of it is visible once the puzzle is assembled and
  none of it is visible while it is being assembled either; it is what makes
  the pieces hold on to each other.

That split is free because the generator already guarantees the lower level is
a solid slab.  ``crossings.minimum_crossing_floor_mm`` keeps every tunnel floor
and colour skin at least one layer *above* ``base_mm``, and only the foundation
material reaches below it, so ``z < base_mm`` is a flat prism of one filament
across the whole footprint.  The knobs are carved out of that prism, and the
map above it is never consulted.

The two levels are cut by different means for the same reason they look
different.  The rectangular level is separated with plane splits, which are
cheap on a mesh of several million triangles; the jigsaw level is a Boolean
against a prism, which is cheap only because the slab it cuts is a box.

There is a second clearance a flat jigsaw does not need.  A knob reaches *under*
the neighbour it locks into, and that neighbour's surface tier starts at the
floor plane, so a knob extruded to full height would present its top face flat
against the underside of the neighbour's surface and be printed onto.  Every
knob is therefore recessed, and the neighbour bridges the gap.

This script reads and writes 3MF.  It needs no source caches, no Bambu Studio
profiles and no job directory: the input project already carries the print
profile it was resolved against, and every clearance defended here is derived
from that rather than assumed.  What it writes it then reads back and audits the
way ``validate_3mf`` audits a map, piece by piece, plus the two rules only a
puzzle has: neighbouring pieces must not touch, and none may come closer than
the joint is built to.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import re
import shlex
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import shapely
import trimesh
from lxml import etree
from PIL import Image, ImageDraw
from shapely.geometry import LineString, MultiLineString, Polygon, box
from shapely.ops import polygonize, unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cache_common import Progress  # noqa: E402
from mesh_precision import (classify_cavity_shells,  # noqa: E402
                            classify_positive_shells,
                            minimum_printable_shell_volume_mm3, prepare_export_mesh)

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
MAT = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"
IDENTITY = "1 0 0 0 1 0 0 0 1 0 0 0"

# Object ids for the assembled pieces live above the per-material mesh ids so
# the two numbering spaces in one 3MF cannot collide.
PIECE_ID_BASE = 1000
MAX_PIECES = PIECE_ID_BASE // 4 - 1

# Shape of one jigsaw knob, as fractions of the edge it sits on.  These are the
# proportions of a cardboard puzzle: a neck about a quarter of the edge, a head
# reaching a fifth of the edge away from it.  They are deliberately not tunable
# per edge -- what varies from edge to edge is jitter, below.
DEFAULT_TAB_SIZE = 0.20
DEFAULT_TAB_NECK = 0.24
# The knob's root is wider than its neck, which is what makes the neck the
# throat of the joint rather than one pinch among several, and is capped so a
# knob cannot swallow the edge it sits on.
TAB_ROOT_RATIO = 2.0
TAB_MAX_ROOT = 0.62
# Where the neck sits between the straight edge and the head's peak.
TAB_NECK_HEIGHT = 0.42
# How much wider than its own neck a printed knob's head should be, and may be.
# A cardboard puzzle sits near the first figure; past it the knob is chunky but
# sound, and the run says so. Past the second the head really is a lump on a
# stalk and the run refuses.
TAB_TARGET_HEAD_RATIO = 1.25
TAB_MAX_HEAD_RATIO = 1.6
# Jitter is what stops a regular grid from producing interchangeable pieces.
TAB_SIZE_JITTER = 0.12
TAB_POSITION_JITTER = 0.06
# Sampling floor and ceiling for the knob's cubics.  How many are actually used
# is derived from the nozzle, below; these only keep a very coarse or very fine
# profile from becoming unusable or enormous.
# The export cleanup's own degenerate-face threshold, restated so the audit of
# the written file judges a triangle exactly as the writer did.
MINIMUM_FACE_AREA_MM2 = 1e-12
# Serialization rounds coordinates; nothing is allowed to move further than this
# from where it was built, and nothing may sit outside the map by more than it.
PLACEMENT_TOLERANCE_MM = 0.002
# How much the floor slab's cross section may vary with depth before it stops
# being the prism the knobs are carved from.
FOOTPRINT_AREA_TOLERANCE = 0.002
# Coverage below which a cell is treated as untouched by the model rather than
# as catching a sliver of it; a numerical threshold, not a design one.
SLIVER_COVERAGE = 1e-6
# The shortest shared edge that counts as two covered regions being joined
# rather than meeting at a corner.
JOINED_EDGE_MM = 1e-6
MINIMUM_BEZIER_SAMPLES = 8
MAXIMUM_BEZIER_SAMPLES = 96


class PuzzleError(ValueError):
    """The requested puzzle cannot be cut from the supplied model."""


# --------------------------------------------------------------------------
# Grid
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Grid:
    """A rows x cols division of a ``width`` x ``height`` rectangle."""

    rows: int
    cols: int
    width: float
    height: float

    @property
    def cell_width(self) -> float:
        return self.width / self.cols

    @property
    def cell_height(self) -> float:
        return self.height / self.rows

    @property
    def aspect(self) -> float:
        """Piece aspect ratio, always >= 1."""
        long_side = max(self.cell_width, self.cell_height)
        short_side = min(self.cell_width, self.cell_height)
        return long_side / short_side


@dataclass(frozen=True)
class Layout:
    """Which cells of a grid make up each piece.

    A generated map is whatever polygon it was cropped to -- a rectangle, or the
    street-following outline a multi-plate plan cut -- so a grid laid over its
    bounding box has cells the model never reaches, and cells it clips to a
    crumb.  The first are not pieces.  The second are absorbed into a
    neighbouring piece rather than printed, which is what gives an irregular
    jigsaw its odd border pieces, so a piece is a *group* of cells and the
    count the user asked for is a count of the groups.
    """

    grid: "Grid"
    groups: tuple                 # one tuple of (row, col) cells per piece

    @property
    def pieces(self) -> int:
        return len(self.groups)

    @property
    def seeds(self) -> tuple:
        """The cell each piece is named and labelled for."""
        return tuple(group[0] for group in self.groups)

    def owner(self) -> dict:
        """Cell to piece index, for every cell any piece holds."""
        return {cell: index for index, group in enumerate(self.groups) for cell in group}


def cell_coverage(grid: "Grid", footprint):
    """What the model covers of each cell, row-major: the geometry and the fraction."""
    boxes = np.array([
        box(col * grid.cell_width, row * grid.cell_height,
            (col + 1) * grid.cell_width, (row + 1) * grid.cell_height)
        for row in range(grid.rows) for col in range(grid.cols)], dtype=object)
    covered = shapely.intersection(boxes, footprint)
    return covered, shapely.area(covered) / (grid.cell_width * grid.cell_height)


def layout_for(grid: "Grid", footprint, min_fill: float, min_join: float = 0.0):
    """Group a grid's cells into pieces, absorbing what the outline clips to a crumb.

    A cell holding at least ``min_fill`` of itself seeds a piece.  A cell the
    outline clips smaller than that cannot be a piece -- there is no room on it
    for a knob, and nothing to pick up -- but it cannot be discarded either,
    because that would cut a notch out of the map.  It is attached instead to
    whichever neighbouring piece already holds the most, which is exactly the
    odd-shaped border piece an irregular jigsaw has.

    Rejecting those grids instead, which is the obvious first thing to try, does
    not survive contact with a real plate: over the tracked Manhattan plan, a
    plate whose outline carries a thousand vertices has essentially no grid that
    escapes clipping something, and the count the user asked for becomes
    unreachable at every size.

    Returns the layout and how many crumbs could not be attached to anything.
    """
    shapes, coverage = cell_coverage(grid, footprint)

    def at(cell):
        return shapes[cell[0] * grid.cols + cell[1]]

    def joined(a, b):
        """Whether two cells' covered parts share an edge worth keeping.

        Long enough to survive the clearance band that will be cut around it and
        still leave something printable: a merge across a hair severs in the
        mesh even though the polygon looks connected.
        """
        return shapely.intersection(at(a), at(b)).length > max(min_join, JOINED_EDGE_MM)

    owner, stranded = {}, 0
    for index in np.flatnonzero(coverage >= min_fill):
        cell = (int(index) // grid.cols, int(index) % grid.cols)
        # A cell the outline enters twice is two lobes, not a piece.
        if at(cell).geom_type != "Polygon":
            stranded += 1
            continue
        owner[cell] = cell
    held = {cell: coverage[cell[0] * grid.cols + cell[1]] for cell in owner}
    loose = [(int(i) // grid.cols, int(i) % grid.cols) for i in np.flatnonzero(
        coverage > SLIVER_COVERAGE)]
    loose = [cell for cell in loose if cell not in owner]
    while loose:
        progressed = False
        for cell in list(loose):
            row, col = cell
            # Cell adjacency is not region adjacency: on an irregular outline two
            # neighbouring cells can each hold a corner of the map that never
            # touches the other, and joining them makes one piece in two halves.
            hosts = [owner[n] for n in ((row - 1, col), (row + 1, col), (row, col - 1),
                                        (row, col + 1))
                     if n in owner and joined(cell, n)]
            if not hosts:
                continue
            best = max(hosts, key=lambda seed: (held[seed], seed))
            owner[cell] = best
            held[best] += coverage[row * grid.cols + col]
            loose.remove(cell)
            progressed = True
        if not progressed:
            break
    groups = {}
    for cell, seed in owner.items():
        groups.setdefault(seed, []).append(cell)
    for seed, cells in groups.items():
        if len(cells) > 1 and unary_union([at(cell) for cell in cells]).geom_type != "Polygon":
            stranded += 1
    ordered = tuple(tuple([seed] + sorted(cell for cell in groups[seed] if cell != seed))
                    for seed in sorted(groups))
    return Layout(grid, ordered), len(loose) + stranded


def candidate_grids(width: float, height: float, max_aspect: float, max_cells: int):
    """Every grid worth testing on a footprint of this shape, up to ``max_cells``.

    The aspect limit is what makes this cheap: it pins the column count to a
    narrow band around the row count, so the search is linear in the number of
    rows rather than quadratic in the pair.
    """
    for rows in range(1, max_cells + 1):
        low = max(1, int(math.floor(width * rows / (height * max_aspect))))
        high = min(int(math.ceil(width * rows * max_aspect / height)), max_cells // rows)
        for cols in range(low, high + 1):
            grid = Grid(rows, cols, width, height)
            if grid.aspect <= max_aspect + 1e-9:
                yield grid


def reachable_layouts(footprint, width: float, height: float, pieces: int,
                      max_aspect: float, min_fill: float, min_join: float = 0.0) -> dict:
    """The best clean grid for every piece count this footprint can be cut into.

    Enumerated once and bucketed by the count it produces, rather than searched
    per count.  Answering "can it make sixty?" and "what can it make instead?"
    then costs the same single pass -- the version that re-searched for each
    suggestion spent minutes on a plate whose outline has six hundred vertices.
    """
    fill = footprint.area / (width * height)
    max_cells = max(4, int(math.ceil(pieces / max(fill, 0.05))) * 2)
    best = {}
    for grid in candidate_grids(width, height, max_aspect, max_cells):
        layout, slivers = layout_for(grid, footprint, min_fill, min_join)
        if slivers or not layout.pieces:
            continue
        key = (grid.aspect, grid.rows * grid.cols, grid.rows)
        if layout.pieces not in best or key < best[layout.pieces][0]:
            best[layout.pieces] = (key, layout)
    return {count: entry[1] for count, entry in best.items()}


def factor_pairs(pieces: int):
    """Every ``(rows, cols)`` whose product is exactly ``pieces``."""
    if pieces < 1:
        raise PuzzleError("A puzzle needs at least one piece")
    return [(rows, pieces // rows) for rows in range(1, pieces + 1) if pieces % rows == 0]


def choose_layout(footprint, width: float, height: float, pieces: int,
                  max_aspect: float, min_fill: float, min_join: float = 0.0) -> Layout:
    """Pick the grid whose cells over this footprint are exactly ``pieces``, squarest.

    The piece count is exact by contract.  Over a rectangle that means a
    factorisation of the count, and a prime count has only the 1 x N one --
    rather than quietly cutting twenty-three ribbons the run fails and names
    counts nearby that work.  Over an irregular outline the grid is no longer
    tied to the count at all: a 12 x 11 grid on a chunk the model half fills may
    be the one that yields exactly sixty pieces.

    A grid is rejected outright if any cell catches a sliver of the model --
    coverage above nothing but below ``min_fill``.  Those become pieces too
    small to print a knob on or to pick up, and there is always another grid.
    """
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise PuzzleError("Model footprint must be finite and positive")
    if max_aspect < 1:
        raise PuzzleError("Maximum piece aspect ratio must be at least 1")
    fill = footprint.area / (width * height)
    reachable = reachable_layouts(footprint, width, height, pieces, max_aspect, min_fill,
                                  min_join)
    if pieces not in reachable:
        raise PuzzleError(
            f"No grid cuts exactly {pieces} pieces from this {width:g} x {height:g} mm outline "
            f"({fill:.0%} of its bounding box) at {max_aspect:g}:1 pieces and a {min_fill:.0%} "
            f"minimum fill. Try {', '.join(str(n) for n in nearest_counts(reachable, pieces))} "
            "pieces, or relax --max-piece-aspect or --min-piece-fill.")
    return reachable[pieces]


def best_layout(footprint, width: float, height: float, pieces: int,
                max_aspect: float, min_fill: float):
    """The squarest grid giving exactly ``pieces`` clean cells, or None."""
    return reachable_layouts(footprint, width, height, pieces, max_aspect, min_fill).get(pieces)


def nearest_counts(reachable, pieces: int, count: int = 3):
    """The counts closest to the one asked for that this footprint can make."""
    ordered = sorted(reachable, key=lambda n: (abs(n - pieces), n))[:count]
    return sorted(ordered) or ["some other count"]


# --------------------------------------------------------------------------
# Knob profile
# --------------------------------------------------------------------------

def _cubic(p0, c0, c1, p1, samples: int, include_start: bool):
    """Sample one cubic Bezier as ``samples`` points along it."""
    t = np.linspace(0.0, 1.0, samples + 1)[(0 if include_start else 1):]
    t = t.reshape(-1, 1)
    u = 1.0 - t
    points = (u ** 3) * np.asarray(p0) + 3 * (u ** 2) * t * np.asarray(c0) \
        + 3 * u * (t ** 2) * np.asarray(c1) + (t ** 3) * np.asarray(p1)
    return points


def knob_profile(length: float, neck: float, height: float, undercut: float,
                 centre: float | None = None, samples: int = MINIMUM_BEZIER_SAMPLES) -> np.ndarray:
    """Return one edge's cut profile as ``(along, across)`` points in millimetres.

    ``along`` runs from 0 to ``length``; ``across`` is the perpendicular
    excursion, positive on one side of the straight edge.  ``height`` is the
    peak of the head, ``neck`` its throat and ``undercut`` how far the head
    reaches back past that throat.

    The shape is built so the neck really is the throat.  Going up from the
    straight edge the knob narrows monotonically from a root twice the neck's
    width down to the neck, and only then balloons out again: every control
    point of the shoulder is held inside the root-to-neck span, so the shoulder
    cannot contribute a second, wider interference behind the designer's back.
    That leaves exactly one undercut in the joint, which is the one solved for
    here -- and it is solved rather than tabulated because a fit allowance has
    to mean the same number of millimetres on a 20 mm edge as on a 60 mm one,
    while every other proportion scales with the edge.
    """
    if not all(math.isfinite(value) for value in (length, neck, height, undercut)):
        raise PuzzleError("Knob geometry must be finite")
    if length <= 0 or neck <= 0 or height <= 0:
        raise PuzzleError("Knob length, neck and height must be positive")
    if undercut < 0:
        raise PuzzleError("Knob undercut cannot be negative")
    centre = length / 2 if centre is None else float(centre)
    root = min(TAB_ROOT_RATIO * neck, TAB_MAX_ROOT * length)
    if root >= neck + 2 * undercut + length or root <= neck:
        raise PuzzleError(f"Knob root {root:g} mm is not wider than its {neck:g} mm neck")
    if centre - root / 2 <= 0 or centre + root / 2 >= length:
        raise PuzzleError(
            f"A knob {root:g} mm wide does not fit centred at {centre:g} mm on a {length:g} mm edge")

    root_left, root_right = centre - root / 2, centre + root / 2
    neck_left, neck_right = centre - neck / 2, centre + neck / 2
    neck_height = TAB_NECK_HEIGHT * height
    # A cubic's midpoint is (2*ends + 6*controls)/8; solve for the control
    # height that puts the head's peak exactly at `height`.
    head_height = (8 * height - 2 * neck_height) / 6
    spread = _solve_head_spread(neck_left, neck_right, neck_height, head_height,
                                undercut, neck, samples)

    parts = [
        np.array([[0.0, 0.0], [root_left, 0.0]]),
        _cubic((root_left, 0.0), (root_left + 0.55 * (neck_left - root_left), 0.0),
               (neck_left, 0.35 * neck_height), (neck_left, neck_height),
               samples, include_start=False),
        _cubic((neck_left, neck_height), (neck_left - spread, head_height),
               (neck_right + spread, head_height), (neck_right, neck_height),
               samples, include_start=False),
        _cubic((neck_right, neck_height), (neck_right, 0.35 * neck_height),
               (root_right + 0.55 * (neck_right - root_right), 0.0), (root_right, 0.0),
               samples, include_start=False),
        np.array([[length, 0.0]]),
    ]
    profile = np.vstack(parts)
    if not LineString(profile).is_simple:
        raise PuzzleError(
            f"Knob profile self-intersects on a {length:g} mm edge with neck {neck:g} mm, "
            f"height {height:g} mm and undercut {undercut:g} mm; reduce --tab-undercut-mm "
            "or --tab-size")
    return profile


def derive_undercut(neck_mm: float, clearance_mm: float, interference_mm: float) -> float:
    """How far a knob's head must out-reach its neck to lock at this gap.

    Both halves of a joint are eroded by half the clearance, so the head has to
    cover the whole gap before any of the overhang is left to hold with.  The
    lock the hand feels is what survives: ``undercut - clearance``.

    The lock wins over the knob's looks, and that is a deliberate reversal of
    what this function used to do.  Capping the undercut to keep the printed
    head within ``target_ratio`` of its own neck is the prettier rule, but on a
    small piece at a gap wide enough to slice it drives the lock to nothing, and
    a puzzle that will not hold together is a worse object than one with chunky
    knobs.  So the proportion is what gives; the caller measures it with
    `printed_head_ratio`, notes it past ``TAB_TARGET_HEAD_RATIO`` and refuses
    only past ``TAB_MAX_HEAD_RATIO``.
    """
    neck = neck_mm / 2 - clearance_mm / 2
    if neck <= 0:
        raise PuzzleError(
            f"A {clearance_mm:g} mm clearance leaves nothing of a {neck_mm:g} mm knob neck")
    return clearance_mm + interference_mm


def printed_head_ratio(neck_mm: float, clearance_mm: float, undercut_mm: float) -> float:
    """How much wider a printed knob's head is than its own neck.

    Not the ratio of the curve that was drawn: both halves of a joint are eroded
    by half the clearance, which slims the neck and the head by the same amount
    and so makes the *ratio* worse.  This is the shape that comes off the
    printer, and it is the one that has to look like a jigsaw knob and not snap.
    """
    neck = neck_mm / 2 - clearance_mm / 2
    if neck <= 0:
        raise PuzzleError(
            f"A {clearance_mm:g} mm clearance leaves nothing of a {neck_mm:g} mm knob neck")
    return (neck + undercut_mm) / neck


def knob_interference(profile: np.ndarray) -> float:
    """Measure a profile's real interference per side, in millimetres.

    The number the joint is actually built to is not a control point but a
    property of the finished curve: how far the widest part of the knob exceeds
    the narrowest part it has to pass through on the way in.  Measuring it back
    off the sampled polyline is what keeps ``--tab-undercut-mm`` honest if the
    control points above are ever retuned.
    """
    profile = np.asarray(profile, dtype=float)
    peak = int(np.argmax(profile[:, 1]))
    left, right = profile[:peak + 1], profile[peak:][::-1]
    if profile[peak, 1] <= 0:
        return 0.0
    levels = np.linspace(0.0, profile[peak, 1], 256)
    left_x = np.interp(levels, left[:, 1], left[:, 0])
    right_x = np.interp(levels, right[:, 1], right[:, 0])
    width = right_x - left_x
    throat = np.minimum.accumulate(width)
    return float(np.max(width - throat) / 2)


def _solve_head_spread(neck_left, neck_right, neck_height, head_height,
                       undercut, neck, samples):
    """Find the head control offset that produces exactly ``undercut`` per side."""
    if undercut <= 0:
        return 0.0
    limit = 4.0 * neck

    def overhang(spread):
        curve = _cubic((neck_left, neck_height), (neck_left - spread, head_height),
                       (neck_right + spread, head_height), (neck_right, neck_height),
                       samples, include_start=True)
        return neck_left - float(curve[:, 0].min())

    if overhang(limit) < undercut:
        raise PuzzleError(
            f"An undercut of {undercut:g} mm does not fit on a knob with a {neck:g} mm neck; "
            "reduce --tab-undercut-mm or raise --tab-neck")
    low, high = 0.0, limit
    for _ in range(60):
        middle = (low + high) / 2
        if overhang(middle) < undercut:
            low = middle
        else:
            high = middle
    return high


# --------------------------------------------------------------------------
# Cut curves
# --------------------------------------------------------------------------

def edge_curve(start, end, rng, *, size, neck, undercut,
               samples=MINIMUM_BEZIER_SAMPLES) -> LineString:
    """One interior grid edge, carrying one knob, placed in the model plane.

    The two endpoints are written back exactly as supplied.  They are lattice
    nodes shared with the three edges that meet there, and a node reproduced by
    arithmetic instead of copied lands a bit away from its neighbours often
    enough to matter: ``polygonize`` then fails to close that corner and the two
    pieces either side of the edge come out merged into one.
    """
    start = np.asarray(start, dtype=float)
    end = np.asarray(end, dtype=float)
    delta = end - start
    length = float(np.hypot(*delta))
    if length <= 0:
        raise PuzzleError("A grid edge has zero length")
    direction = delta / length
    normal = np.array([-direction[1], direction[0]])

    flip = 1.0 if rng.random() < 0.5 else -1.0
    scale = 1.0 + rng.uniform(-TAB_SIZE_JITTER, TAB_SIZE_JITTER)
    shift = rng.uniform(-TAB_POSITION_JITTER, TAB_POSITION_JITTER) * length
    profile = knob_profile(length, neck * length, size * length * scale, undercut,
                           centre=length / 2 + shift, samples=samples)
    points = start + profile[:, 0:1] * direction + (profile[:, 1:2] * flip) * normal
    points[0], points[-1] = start, end
    return LineString(points)


def grid_nodes(grid: Grid):
    """The lattice every cut endpoint is taken from, computed exactly once."""
    xs = [col * grid.cell_width for col in range(grid.cols + 1)]
    ys = [row * grid.cell_height for row in range(grid.rows + 1)]
    xs[-1], ys[-1] = grid.width, grid.height
    return xs, ys


def cut_curves(grid: Grid, rng, *, size, neck, undercut,
               samples=MINIMUM_BEZIER_SAMPLES) -> dict:
    """Every interior edge of the grid, keyed by the two cells it separates.

    One curve per shared edge, used by both pieces that meet on it, is what
    makes the two halves of a joint the same shape by construction rather than
    by a tolerance.  Keying them by the cell pair is what lets an edge *inside*
    a piece -- one that absorbed a crumb of the map's border -- simply not be
    cut.
    """
    xs, ys = grid_nodes(grid)
    curves = {}
    for col in range(1, grid.cols):
        for row in range(grid.rows):
            curves[((row, col - 1), (row, col))] = edge_curve(
                (xs[col], ys[row]), (xs[col], ys[row + 1]), rng,
                size=size, neck=neck, undercut=undercut, samples=samples)
    for row in range(1, grid.rows):
        for col in range(grid.cols):
            curves[((row - 1, col), (row, col))] = edge_curve(
                (xs[col], ys[row]), (xs[col + 1], ys[row]), rng,
                size=size, neck=neck, undercut=undercut, samples=samples)
    return curves


def cut_edges(layout: Layout, curves: dict) -> list:
    """Only the edges that actually separate two different pieces."""
    owner = layout.owner()
    return [curve for cells, curve in curves.items()
            if owner.get(cells[0]) != owner.get(cells[1])]


def cell_box(grid: Grid, cell, origin=(0.0, 0.0)):
    row, col = cell
    return box(origin[0] + col * grid.cell_width, origin[1] + row * grid.cell_height,
               origin[0] + (col + 1) * grid.cell_width, origin[1] + (row + 1) * grid.cell_height)


def floor_polygons(layout: Layout, curves, clearance: float, footprint,
                   minimum_width: float = 0.0) -> list[Polygon]:
    """Piece footprints for the floor slab, in the layout's order, gap included.

    The gap is subtracted as one band centred on the interior cuts rather than
    by shrinking each piece, so the outer edge of the map keeps its exact
    dimensions and every joint gets the same clearance from both sides.  The
    footprint is then what clips each piece, so a piece on an irregular outline
    keeps the map's own edge on one side and a jigsaw edge on the others.
    """
    grid = layout.grid
    cut = cut_edges(layout, curves)
    outline = box(0.0, 0.0, grid.width, grid.height)
    network = unary_union([outline.boundary, MultiLineString(cut)])
    faces = [face for face in polygonize(network) if face.area > 1e-9]
    gap = unary_union(cut).buffer(clearance / 2, cap_style="square", join_style="round",
                                  quad_segs=8) if clearance > 0 else None
    # The face count is not the test -- cells the model never reaches are not
    # cut apart from each other, so the network legitimately encloses fewer
    # regions than the grid has cells. What has to hold is that every piece owns
    # a region of its own: two knobs that overlapped would merge two pieces into
    # one face, and that shows up here as a face claimed twice.
    claimed = {}
    ordered = []
    for index, (row, col) in enumerate(layout.seeds):
        centre = shapely.Point((col + 0.5) * grid.cell_width, (row + 0.5) * grid.cell_height)
        matches = [number for number, face in enumerate(faces) if face.contains(centre)]
        if len(matches) != 1:
            raise PuzzleError(
                f"Piece {piece_label(row, col)} matched {len(matches)} cut regions")
        if matches[0] in claimed:
            raise PuzzleError(
                f"Pieces {claimed[matches[0]]} and {piece_label(row, col)} share one cut region, "
                "so their knobs overlapped -- reduce --tab-size or --tab-undercut-mm")
        claimed[matches[0]] = piece_label(row, col)
        matches = [faces[matches[0]]]
        piece = matches[0] if gap is None else matches[0].difference(gap)
        piece = piece.intersection(footprint)
        if piece.geom_type != "Polygon" or piece.is_empty:
            raise PuzzleError(
                f"Piece {piece_label(row, col)} came out as {piece.geom_type} once the "
                f"{clearance:g} mm clearance and the map's own outline were taken off it; "
                "reduce --clearance-mm or raise --min-piece-fill")
        # Connected is not enough: a piece joined through a neck thinner than an
        # extrusion looks like one polygon and comes out of the Boolean as two
        # solids. Eroding by half a nozzle is the cheap way to see the neck.
        if minimum_width > 0:
            core = piece.buffer(-minimum_width / 2)
            if core.is_empty or core.geom_type != "Polygon":
                raise PuzzleError(
                    f"Piece {piece_label(row, col)} is pinched thinner than {minimum_width:g} mm "
                    "somewhere, so it would print as two pieces. Try a different --seed, "
                    "--pieces or --grid, or raise --min-piece-fill.")
        ordered.append(piece)
    return ordered


# --------------------------------------------------------------------------
# 3MF input
# --------------------------------------------------------------------------

@dataclass
class SourceProject:
    """Everything read out of the input 3MF."""

    path: Path
    meshes: list                      # (member object id, vertices, triangles)
    object_member: str
    colors: list
    part_names: list
    extruders: list
    title: str
    description: str
    designer: str
    copyright: str
    plate_name: str
    transform: str
    profile: "PrintProfile"
    project: bytes
    thumbnail: bytes | None
    passthrough: dict                 # archive members copied unchanged


def read_project(path: Path) -> SourceProject:
    """Load the four material meshes and the project settings of a map 3MF."""
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        wrapper = etree.fromstring(archive.read("3D/3dmodel.model"))
        core = f"{{{CORE}}}"
        components = wrapper.findall(f".//{core}component")
        if not components:
            raise PuzzleError(f"{path} has no component objects; it is not a map project")
        member = components[0].get(f"{{{PROD}}}path", "").lstrip("/")
        if not member or member not in names:
            raise PuzzleError(f"{path} references a mesh member {member!r} it does not contain")
        if len({component.get(f"{{{PROD}}}path") for component in components}) != 1:
            raise PuzzleError(f"{path} spreads its materials over several mesh members")
        item = wrapper.find(f".//{core}item")
        transform = item.get("transform", IDENTITY) if item is not None else IDENTITY

        def metadata(name, default=""):
            found = wrapper.find(f"{core}metadata[@name='{name}']")
            return found.text if found is not None and found.text else default

        settings = etree.fromstring(archive.read("Metadata/model_settings.config"))
        parts = settings.findall(".//object/part")
        part_names, extruders = [], []
        for part in parts:
            name = part.find("metadata[@key='name']")
            extruder = part.find("metadata[@key='extruder']")
            part_names.append(name.get("value") if name is not None else "")
            extruders.append(extruder.get("value") if extruder is not None else "1")
        plate = settings.find(".//plate/metadata[@key='plater_name']")

        with archive.open(member) as handle:
            meshes, colors = read_meshes(handle)
        if len(meshes) != len(part_names):
            raise PuzzleError(
                f"{path} declares {len(part_names)} parts but carries {len(meshes)} meshes")

        passthrough = {}
        for name in ("Metadata/generation_command.json", "Metadata/slice_info.config"):
            if name in names:
                passthrough[name] = archive.read(name)
        settings_member = "Metadata/project_settings.config"
        project = archive.read(settings_member) if settings_member in names else b""
        profile = print_profile(project)
        thumbnail = archive.read("Metadata/plate_1.png") if "Metadata/plate_1.png" in names else None

    return SourceProject(
        path=path, meshes=meshes, object_member=member, colors=colors,
        part_names=part_names, extruders=extruders,
        title=metadata("Title", path.stem), description=metadata("Description"),
        designer=metadata("Designer"), copyright=metadata("Copyright"),
        plate_name=plate.get("value") if plate is not None else metadata("Title", path.stem),
        transform=transform, profile=profile, project=project, thumbnail=thumbnail,
        passthrough=passthrough,
    )


@dataclass(frozen=True)
class PrintProfile:
    """The machine measurements the cut has to obey, read off the input project.

    None of these are the puzzle's to choose.  The project was resolved against
    an installed Bambu profile for a particular nozzle, layer height, wall count
    and plate, and every bound this script defends follows from those rather
    than from a constant written here:

    ``clearance``      two outer-wall line widths, and the reason is the map,
                       not the joint. A city at 1:5670 carries thousands of
                       details finer than one bead -- parapets, roof fixtures,
                       narrow setbacks. The slicer still lays a full bead down
                       the middle of each, and Arachne widens a lone bead up to
                       about twice the nominal width, so a bead can spill a
                       whole line width past the feature it is drawing. Two such
                       features facing each other across a seam therefore need
                       two line widths between them or their paths overlap.

                       Measured on a hundred-piece cut of the tracked example:
                       0.40 mm and 0.50 mm are both refused by Bambu's
                       multi-object path check, 0.84 mm slices clean. The
                       uncut map has the same features -- 133 sub-bead islands
                       at the layer that first failed, against 6 across the two
                       pieces blamed -- and slices only because a single object
                       has nothing to be compared against.

                       Widening is not free: both halves of a joint are eroded
                       by half the gap, so a wider gap costs knob slenderness.
                       `printed_head_ratio` reports what that comes to.
    ``vertical``       two layers. A knob lies under its neighbour's surface
                       tier, so the joint needs clearance in Z as well: one
                       layer of air, and one for the sag of the layer the
                       neighbour bridges across the gap.
    ``crumb``          one nozzle-width square at one layer height -- the
                       smallest thing the printer can put down, and so the line
                       between Boolean debris and a real loose fragment. Shared
                       with ``validate_3mf`` through ``mesh_precision``.
    ``narrowest knob`` the walls alone must fit across a knob's throat, twice
                       ``wall_loops`` of wall line width.
    ``interference``   half a nozzle: a few times the machine's own dimensional
                       error, so the lock is something the pieces feel rather
                       than something lost in tolerance.
                       The undercut that carries it is derived per knob by
                       `derive_undercut`, because the two halves of a joint are
                       each eroded by half the gap: a head has to out-reach its
                       neck by the whole gap before any overhang is left to lock
                       with, and on a small neck that would make a lump.
    ``printable width`` two minimum bead widths.  Below it Arachne lays no
                       extrusion at all and the layer slices away to nothing,
                       which is fatal on a puzzle: the empty layer belongs to
                       one piece rather than to the whole map, and Bambu
                       refuses an object that has one.  Measured against the
                       slicer on the tracked example, every layer it dropped
                       was 0.644 mm across or narrower and every layer it kept
                       was 0.677 mm or wider -- ``2 x min_bead_width`` is
                       0.68 mm.
    ``plate``          the printable area, less what the configured brim needs.
    ``brim``           kept only where a brim loop cannot fit between two
                       pieces; a brim that reaches across the seam welds the
                       first layer of the whole puzzle together.
    ``knob facets``    chords no coarser than half a nozzle, so the only curved
                       surface in the model is not the thing that shows.
    """

    nozzle_mm: float
    layer_height_mm: float
    initial_layer_height_mm: float
    min_bead_width_mm: float
    wall_loops: int
    wall_line_width_mm: float
    outer_wall_line_width_mm: float
    plate_mm: tuple
    brim_width_mm: float
    brim_object_gap_mm: float
    print_sequence: str

    @property
    def clearance_mm(self) -> float:
        return 2 * self.outer_wall_line_width_mm

    @property
    def interference_mm(self) -> float:
        return self.nozzle_mm / 2

    @property
    def minimum_printable_width_mm(self) -> float:
        """Narrowest cross section the slicer will lay any extrusion into."""
        return 2 * self.min_bead_width_mm

    def layer_indices(self, low_mm: float, high_mm: float):
        """The printed layers whose tops fall in ``(low_mm, high_mm]``.

        The first layer is its own height and every layer after it is
        ``layer_height``, which is how the slicer numbers them and therefore
        the only numbering worth measuring a section against.
        """
        first, step = self.initial_layer_height_mm, self.layer_height_mm
        if step <= 0:
            return range(0, 0)
        return range(max(1, int(math.ceil((low_mm - first) / step + 1e-9))),
                     int(math.floor((high_mm - first) / step + 1e-9)) + 1)

    def layer_top_mm(self, index: int) -> float:
        return self.initial_layer_height_mm + self.layer_height_mm * index

    @property
    def vertical_clearance_mm(self) -> float:
        return 2 * self.layer_height_mm

    @property
    def brim_margin_mm(self) -> float:
        return self.brim_width_mm + self.brim_object_gap_mm if self.brim_width_mm else 0.0

    def brim_bridges_gap(self, clearance_mm: float) -> bool:
        """Whether a brim loop fits in the gap between two neighbouring pieces.

        A brim is laid outward from each object's first layer and clipped back
        from every other object by ``brim_object_gap``.  Where what is left
        between two pieces is still an extrusion wide, the slicer fills it, and
        the first layer of the print welds the puzzle into a tile.
        """
        if not self.brim_width_mm:
            return False
        return (clearance_mm - 2 * self.brim_object_gap_mm) >= self.outer_wall_line_width_mm

    @property
    def crumb_mm3(self) -> float:
        return minimum_printable_shell_volume_mm3(self.nozzle_mm, self.layer_height_mm)

    @property
    def narrowest_knob_neck_mm(self) -> float:
        return 2 * self.wall_loops * self.wall_line_width_mm

    def knob_samples(self, span_mm: float) -> int:
        chord = self.nozzle_mm / 2
        return int(min(MAXIMUM_BEZIER_SAMPLES,
                       max(MINIMUM_BEZIER_SAMPLES, math.ceil(span_mm / chord))))

    def complete(self) -> bool:
        return all(value > 0 for value in (self.nozzle_mm, self.layer_height_mm,
                                           self.initial_layer_height_mm,
                                           self.min_bead_width_mm,
                                           self.wall_line_width_mm,
                                           self.outer_wall_line_width_mm, *self.plate_mm)) \
            and self.wall_loops > 0


def print_profile(settings: bytes | None) -> PrintProfile:
    """Read the resolved print profile out of a project's settings."""
    empty = PrintProfile(0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, (0.0, 0.0), 0.0, 0.0, "")
    if not settings:
        return empty
    try:
        resolved = json.loads(settings)
    except ValueError:
        return empty

    def measure(key, limit=100.0, default=0.0):
        value = resolved.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) and 0 <= number < limit else default

    nozzle = measure("nozzle_diameter", 2.0)

    def fraction_of_nozzle(key, default):
        """A width the profile may state in millimetres or as a % of nozzle."""
        value = resolved.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        text = str(value).strip() if value is not None else ""
        if text.endswith("%"):
            try:
                return nozzle * float(text[:-1]) / 100
            except ValueError:
                return default
        return measure(key, 4.0, default)

    # Arachne will not lay a bead narrower than this, so a section that cannot
    # hold two of them side by side slices to nothing.
    min_bead = fraction_of_nozzle("min_bead_width", 0.85 * nozzle)
    # The walls across a knob's throat are inner walls once there is more than
    # one loop; fall back through the profile's other widths to the nozzle.
    width = (measure("inner_wall_line_width", 4.0) or measure("line_width", 4.0)
             or measure("outer_wall_line_width", 4.0) or nozzle)
    # The wall that faces the gap between two pieces is the outer one.
    outer = (measure("outer_wall_line_width", 4.0) or measure("line_width", 4.0)
             or width)
    corners = []
    for corner in resolved.get("printable_area") or []:
        try:
            x, y = str(corner).lower().split("x")
            corners.append((float(x), float(y)))
        except ValueError:
            corners = []
            break
    plate = ((max(x for x, _ in corners) - min(x for x, _ in corners),
              max(y for _, y in corners) - min(y for _, y in corners))
             if len(corners) >= 3 else (0.0, 0.0))
    brimming = str(resolved.get("brim_type", "no_brim")) != "no_brim"
    return PrintProfile(nozzle, measure("layer_height", 2.0),
                        measure("initial_layer_print_height", 2.0)
                        or measure("layer_height", 2.0),
                        min_bead,
                        int(measure("wall_loops", 100.0, 0.0)), width, outer, plate,
                        measure("brim_width") if brimming else 0.0,
                        measure("brim_object_gap"),
                        str(resolved.get("print_sequence", "")))


def read_meshes(handle):
    """Stream one 3MF mesh member into numpy arrays, one entry per object."""
    core = f"{{{CORE}}}"
    material = f"{{{MAT}}}"
    meshes, colors = [], []
    vertices, triangles, current = [], [], None
    events = ("start", "end")
    tags = (f"{core}object", f"{core}vertex", f"{core}triangle", f"{material}color")
    for event, element in etree.iterparse(handle, events=events, tag=tags):
        tag = element.tag.rsplit("}", 1)[1]
        if event == "start":
            if tag == "object":
                current = element.get("id")
                vertices, triangles = [], []
            continue
        if tag == "vertex":
            vertices.append((element.get("x"), element.get("y"), element.get("z")))
        elif tag == "triangle":
            triangles.append((element.get("v1"), element.get("v2"), element.get("v3")))
        elif tag == "color":
            colors.append((element.get("color") or "")[:7])
        elif tag == "object":
            meshes.append((current, np.array(vertices, dtype=np.float64),
                           np.array(triangles, dtype=np.int64)))
            vertices, triangles = [], []
        element.clear()
        while element.getprevious() is not None:
            del element.getparent()[0]
    return meshes, colors


# --------------------------------------------------------------------------
# Cutting
# --------------------------------------------------------------------------

def to_manifold(vertices, triangles):
    """Load one material mesh at full precision.

    ``Mesh64`` rather than ``Mesh``: the generator's Boolean seam repairs are
    deliberately sub-micron, and truncating those vertices to float32 merges
    distinct ones back together and recreates the zero-area triangles the
    export cleanup removed.  ``build_map_meshes.solid`` takes the same care for
    the same reason.
    """
    import manifold3d as md
    mesh = md.Mesh64(np.ascontiguousarray(vertices, dtype=np.float64),
                     np.ascontiguousarray(triangles, dtype=np.uint64))
    solid = md.Manifold(mesh)
    if solid.status() != md.Error.NoError:
        raise PuzzleError(f"A material mesh did not load as a solid: {solid.status()}")
    return solid


def derive_foundation(solids) -> int:
    """The filament carrying the substrate is the one that reaches the plate.

    Which index that is depends on ``--foundation-color`` at generation time, so
    reading it off the geometry is more robust than assuming the default, and it
    is unambiguous: every other material is a surface standing on this one.
    """
    reaching = [index for index, solid in enumerate(solids)
                if solid.bounding_box()[2] <= 1e-6]
    if len(reaching) != 1:
        raise PuzzleError(
            f"{len(reaching)} of this project's {len(solids)} filaments reach the plate, so the "
            "substrate cannot be identified; pass --foundation-material")
    return reaching[0]


def derive_floor(solids, foundation: int, layer_height_mm: float) -> float:
    """The highest plane the jigsaw joint can hide below, in millimetres.

    A finished project records no ``base_mm``, and it does not need to. What the
    cut actually requires is a plane with only substrate below it, and the mesh
    states that directly: the lowest point of any other filament is where the
    map's colour begins. One layer below that is the same shape of bound the
    generator sets for itself in ``crossings.minimum_crossing_floor_mm``, and it
    keeps the cut plane off a colour skin's underside rather than grazing it --
    which is what sheds Boolean debris.

    The result is a derived floor, not the generator's own: it lands wherever
    the map's lowest colour happens to be, and is usually close to ``base_mm``.
    ``--floor-mm`` overrides it for a map whose base is known.
    """
    others = [solid.bounding_box()[2] for index, solid in enumerate(solids)
              if index != foundation and not solid.is_empty()]
    if not others:
        raise PuzzleError(
            "This project has only one filament, so there is no colour skin to place the "
            "floor plane below; pass --floor-mm")
    floor = min(others) - float(layer_height_mm)
    if floor <= float(layer_height_mm):
        raise PuzzleError(
            f"The lowest colour skin sits at z={min(others):g} mm, leaving no room for a joint "
            f"below it at {layer_height_mm:g} mm layers")
    return floor


def section_polygon(section, allow_empty: bool = False) -> Polygon:
    """One Manifold cross section as a shapely geometry, holes included.

    Manifold states a hole by winding it the other way round, so the rings are
    sorted by signed area rather than assumed to be a simple outline: a chunk
    cut around a park or a basin is an ordinary polygon for this purpose and
    must not come back as a solid blob.

    An empty section is an error for the callers that measure the footprint --
    a map with no cross section is a broken input -- but an ordinary answer for
    the layer scan, which asks about heights the piece may not reach at all.
    """
    shells, holes = [], []
    for ring in section.to_polygons():
        ring = np.asarray(ring, dtype=float)
        if len(ring) < 3:
            continue
        x, y = ring[:, 0], ring[:, 1]
        area = 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))
        (shells if area > 0 else holes).append(Polygon(ring))
    if not shells:
        if allow_empty:
            return Polygon()
        raise PuzzleError("A cross section of the model enclosed no area")
    outline = unary_union(shells)
    return outline.difference(unary_union(holes)) if holes else outline


def measure_footprint(solid, floor_mm: float, samples: int = 5):
    """The model's own outline below the floor, and proof it is a prism there.

    The outline is whatever the generator produced -- a rectangular crop, or the
    street-following polygon a multi-plate plan cut -- so it is measured rather
    than assumed.  What the puzzle actually needs is not that the outline be
    any particular shape but that it not *change* with depth: the knobs are
    carved out of this slab, and a slab that narrows or grows partway down would
    put them in geometry that is carrying map detail.  Slicing at several depths
    and requiring one constant area is that check, and it makes no assumption
    about the shape at all.
    """
    heights = np.linspace(0.15 * floor_mm, 0.85 * floor_mm, samples)
    sections = [solid.slice(float(height)) for height in heights]
    areas = [float(section.area()) for section in sections]
    if min(areas) <= 0:
        raise PuzzleError(f"The model encloses no area below its {floor_mm:g} mm floor")
    if max(areas) - min(areas) > FOOTPRINT_AREA_TOLERANCE * max(areas):
        raise PuzzleError(
            f"The floor slab measures between {min(areas):g} and {max(areas):g} mm2 over the "
            f"depths sampled below {floor_mm:g} mm, so it is not the constant prism this cut "
            "assumes. Lower --floor-mm.")
    return section_polygon(sections[len(sections) // 2])


def validate_floor(solids, floor_mm: float, foundation: int):
    """Confirm ``z < floor_mm`` really is a solid slab of one filament.

    The whole two-level cut rests on this.  If a later generator change lets a
    tunnel floor or a colour skin dip below the base, the knobs would be carved
    out of geometry that is also carrying map detail, and the run must stop
    rather than quietly cut a hole in a road.
    """
    for index, solid in enumerate(solids):
        low = solid.bounding_box()[2]
        if index == foundation:
            if low > 1e-6:
                raise PuzzleError(
                    f"The foundation material starts at z={low:g} mm, so there is no floor slab "
                    "to cut a puzzle out of")
            continue
        if low < floor_mm - 1e-6:
            raise PuzzleError(
                f"Material {index} reaches down to z={low:g} mm, below the {floor_mm:g} mm floor; "
                f"pass --floor-mm {low:g} or smaller")
    return measure_footprint(solids[foundation], floor_mm)


def split_cells(solid, grid: Grid, origin):
    """Split one solid on the bare grid lines, into one part per cell.

    Plane splits rather than a Boolean per cell: the surface meshes here carry
    millions of triangles, and a half-space split costs a fraction of a general
    intersection against a prism of the same extent.  Splitting hierarchically
    also means each later cut runs against a solid that is already a fraction
    of the original.

    No clearance is taken out here.  A piece may hold more than one cell, and
    a gap cut between two cells of the same piece would saw it in half; the
    clearance is applied once, later, as part of each piece's own seat.
    """
    columns, rest = [], solid
    for col in range(1, grid.cols):
        left, rest = rest.split_by_plane(
            [-1.0, 0.0, 0.0], -(origin[0] + col * grid.cell_width))
        columns.append(left)
    columns.append(rest)

    cells = [[None] * grid.cols for _ in range(grid.rows)]
    for col, column in enumerate(columns):
        rest = column
        for row in range(1, grid.rows):
            lower, rest = rest.split_by_plane(
                [0.0, -1.0, 0.0], -(origin[1] + row * grid.cell_height))
            cells[row - 1][col] = lower
        cells[grid.rows - 1][col] = rest
    return cells


def drop_negligible_shells(solid, crumb_mm3: float):
    """Remove the CSG debris a plane split leaves behind.

    Cutting a mesh with a plane that grazes its existing geometry sheds
    disconnected shells of six triangles and no volume.  They are judged by the
    bound `validate_3mf` uses on the finished assembly, so the puzzle and the
    map agree about what counts as a loose piece of print rather than each
    keeping its own tolerance.
    """
    import manifold3d as md
    parts = solid.decompose()
    if len(parts) <= 1:
        return solid, 0, len(parts)
    kept = [part for part in parts if part.volume() > crumb_mm3]
    if len(kept) == len(parts):
        return solid, 0, len(parts)
    if not kept:
        return md.Manifold(), len(parts), 0
    return md.Manifold.compose(kept), len(parts) - len(kept), len(kept)


def unprintable_ceiling(solids, floor_mm: float, profile: PrintProfile):
    """The height above which a piece would print nothing at all, or None.

    A puzzle asks a question of the map that the map was never asked before.
    New York at 1:5670 is full of masts, finials and antennas that taper to a
    point, and a slicer lays no extrusion into a section narrower than two
    minimum beads: those layers come out empty.  On the uncut map that is
    invisible, because the layer belongs to one object covering the whole city
    and the rest of the city fills it.  Cut into pieces, the spire is alone in
    its object, the layer really is empty, and Bambu refuses the plate:

        Object can't be printed for empty layer between 11.24 and 12.2.

    So each piece is measured the way the slicer will measure it -- layer by
    layer, on the union of its filaments, against
    ``profile.minimum_printable_width_mm`` -- and the answer is the top of the
    layer below the *lowest* one that slices away.  Lowest, not highest: a
    layer that prints nothing carries nothing above it either, so everything
    over it is hanging in mid-air whatever its own section looks like.

    Nothing printable is lost.  The material removed is the material the
    slicer already declines to lay down, on the uncut map as much as here; the
    puzzle merely has to say so, because an object may not contain it.
    """
    if profile.minimum_printable_width_mm <= 0 or profile.layer_height_mm <= 0:
        return None
    top = max(float(solid.bounding_box()[5]) for solid in solids)
    reach = profile.minimum_printable_width_mm / 2
    ceiling = None
    for index in reversed(profile.layer_indices(floor_mm, top)):
        plane = profile.layer_top_mm(index) - profile.layer_height_mm / 2
        section = unary_union([section_polygon(solid.slice(plane), allow_empty=True)
                               for solid in solids])
        if section.is_empty or section.buffer(-reach).is_empty:
            ceiling = profile.layer_top_mm(index) - profile.layer_height_mm
    return ceiling


def seat_polygons(layout: Layout, low, clearance: float) -> list[Polygon]:
    """Each piece's own cells, inset by half the clearance where a neighbour meets it.

    This is the footprint of the piece above the floor plane, and the part of
    its floor that has its own surface tier standing on it.  Everything of a
    piece outside its seat is knob, and lies under a *neighbour's* surface.

    Like the floor's gap, the inset is subtracted as a band over the grid lines
    that are actually cut, so the map's outer edge keeps its dimensions and a
    line inside a merged piece is not cut at all.
    """
    grid = layout.grid
    owner = layout.owner()
    segments = []
    for col in range(1, grid.cols):
        x = low[0] + col * grid.cell_width
        for row in range(grid.rows):
            if owner.get((row, col - 1)) != owner.get((row, col)):
                segments.append(LineString([(x, low[1] + row * grid.cell_height),
                                            (x, low[1] + (row + 1) * grid.cell_height)]))
    for row in range(1, grid.rows):
        y = low[1] + row * grid.cell_height
        for col in range(grid.cols):
            if owner.get((row - 1, col)) != owner.get((row, col)):
                segments.append(LineString([(low[0] + col * grid.cell_width, y),
                                            (low[0] + (col + 1) * grid.cell_width, y)]))
    band = (unary_union(segments).buffer(clearance / 2, cap_style="square", join_style="mitre")
            if segments and clearance > 0 else None)
    seats = []
    for group in layout.groups:
        cells = unary_union([cell_box(grid, cell, low) for cell in group])
        seats.append(cells if band is None else cells.difference(band))
    return seats


def floor_prisms(slab, polygons, seats, floor_mm: float, recess_mm: float,
                 overlap_mm: float = 0.0):
    """Carve the floor slab into knobbed pieces, recessing every knob.

    A knob reaches under the neighbour it locks into, and that neighbour's
    surface tier starts at the floor plane.  Extruded to full floor height the
    knob's top face would therefore lie flat against the underside of the
    neighbour's surface, and the printer would lay that surface straight onto
    it: two pieces welded across a joint designed to come apart.  So the knob
    stops ``recess_mm`` short of the plane and the neighbour bridges the gap,
    which is a short bridge anchored on three sides and the only overhang the
    cut creates.

    Under its own seat the floor is carried ``overlap_mm`` *past* the plane, into
    material the surface tier also holds.  Meeting the surface exactly on the
    plane instead leaves two solids touching face to face, which a union does
    not always merge -- the piece then leaves the cut as two printable
    components and fails its own connectivity check.  One layer of overlap is
    inside geometry the piece owns either way, so it adds nothing and costs
    nothing.

    Cheap despite being a general Boolean, because ``slab`` is nearly a box: the
    expensive surface geometry was left on the other side of the floor plane.
    """
    import manifold3d as md
    prisms = []
    for index, (polygon, seat) in enumerate(zip(polygons, seats)):
        # Manifold reads winding, shapely does not guarantee it, and a
        # clockwise ring under FillRule.Positive yields an empty cross section
        # -- which then silently empties the union it was meant to complete
        # rather than failing anywhere near the cause.
        seated = polygon.intersection(seat)
        knobs = polygon.difference(seat)
        parts = []
        for shape, height in ((seated, floor_mm + overlap_mm),
                              (knobs, floor_mm - recess_mm)):
            if shape.is_empty or height <= 0:
                continue
            parts.append(cross_section(shape).extrude(height + 1.0).translate([0.0, 0.0, -1.0]))
        if not parts:
            raise PuzzleError(f"Floor piece {index} has no footprint to extrude")
        prism = slab ^ md.Manifold.batch_boolean(parts, md.OpType.Add)
        if prism.is_empty() or prism.volume() <= 0:
            raise PuzzleError(
                f"Floor piece {index} carved out empty against a {slab.volume():g} mm3 slab; "
                f"its footprint measures {polygon.area:g} mm2")
        prisms.append(prism)
    return prisms


def cross_section(shape):
    """One shapely polygon or multipolygon as a Manifold cross section.

    Manifold reads winding and shapely does not guarantee it; a clockwise ring
    under ``FillRule.Positive`` yields an empty section, which then silently
    empties whatever it was meant to build rather than failing near the cause.
    """
    import manifold3d as md
    rings = []
    for polygon in shapely.get_parts(shape):
        if polygon.geom_type != "Polygon" or polygon.is_empty:
            continue
        oriented = shapely.geometry.polygon.orient(polygon, 1.0)
        rings.append(np.asarray(oriented.exterior.coords)[:-1, :2].tolist())
        rings += [np.asarray(ring.coords)[:-1, :2].tolist() for ring in oriented.interiors]
    if not rings:
        raise PuzzleError("A piece footprint holds no polygon to extrude")
    return md.CrossSection(rings, md.FillRule.Positive)


# --------------------------------------------------------------------------
# 3MF output
# --------------------------------------------------------------------------

def piece_label(row: int, col: int) -> str:
    """Spreadsheet-style label, so a piece can be found in the preview."""
    letters = ""
    index = col
    while True:
        letters = chr(ord("A") + index % 26) + letters
        index = index // 26 - 1
        if index < 0:
            break
    return f"{letters}{row + 1}"


def write_project(output: Path, source: SourceProject, pieces, layout: Layout, plan: dict,
                  project: bytes, thumbnail: bytes | None = None):
    """Write one 3MF holding every piece as its own printable object."""
    grid = layout.grid
    identity = f"{source.title}|{grid.rows}x{grid.cols}|{len(pieces)}|{plan['seed']}"
    uid = lambda name: str(uuid.uuid5(uuid.NAMESPACE_URL, "3d-nyc/puzzle/" + identity + "/" + name))
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", output.stem).strip("._") or "nyc_puzzle"
    member = f"3D/Objects/{slug}.model"

    mesh_ids = {}
    next_id = 1
    for piece in pieces:
        for material, _ in piece["meshes"]:
            mesh_ids[(piece["index"], material)] = next_id
            next_id += 1

    resources, build = [], []
    for piece in pieces:
        object_id = PIECE_ID_BASE + piece["index"]
        components = "".join(
            f'<component objectid="{mesh_ids[(piece["index"], material)]}" p:path="/{member}" '
            f'transform="{IDENTITY}" p:UUID="{uid(f"component/{piece['index']}/{material}")}"/>\n'
            for material, _ in piece["meshes"])
        resources.append(
            f'<object id="{object_id}" type="model" name="{escape(piece["name"])}" '
            f'p:UUID="{uid(f"piece/{piece['index']}")}"><components>\n{components}</components></object>')
        build.append(
            f'<item objectid="{object_id}" transform="{source.transform}" printable="1" '
            f'p:UUID="{uid(f"instance/{piece['index']}")}"/>')

    wrapper = f'''<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="{CORE}" xmlns:p="{PROD}" unit="millimeter" requiredextensions="p" xml:lang="en-US">
<metadata name="Application">BambuStudio-02.08.02.61</metadata><metadata name="BambuStudio:3mfVersion">1</metadata>
<metadata name="Title">{escape(source.title)} - {len(pieces)} piece puzzle</metadata>
<metadata name="Description">{escape(source.description)} Cut into {grid.rows} x {grid.cols} interlocking pieces; the jigsaw joint is below the {plan['floor_mm']:g} mm floor and the map above it is cut on straight lines.</metadata>
<metadata name="Designer">{escape(source.designer)}</metadata>
<metadata name="Copyright">{escape(source.copyright)}</metadata>
<resources>
''' + "\n".join(resources) + f'''
</resources>
<build p:UUID="{uid('build')}">
''' + "\n".join(build) + '''
</build></model>'''

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                         allowZip64=True) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/><Default Extension="png" ContentType="image/png"/><Default Extension="json" ContentType="application/json"/></Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        archive.writestr("3D/3dmodel.model", wrapper)
        archive.writestr("3D/_rels/3dmodel.model.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/{member}" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        with archive.open(member, "w", force_zip64=True) as raw:
            stream = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
            stream.write(f'<?xml version="1.0" encoding="UTF-8"?><model xmlns="{CORE}" xmlns:p="{PROD}" xmlns:m="{MAT}" unit="millimeter" requiredextensions="p m"><metadata name="BambuStudio:3mfVersion">1</metadata><resources><m:colorgroup id="1008">')
            for color in source.colors:
                stream.write(f'<m:color color="{color}FF"/>')
            stream.write("</m:colorgroup>")
            for piece in pieces:
                for material, mesh in piece["meshes"]:
                    write_mesh_object(stream, mesh_ids[(piece["index"], material)], material,
                                      f'{piece["name"]} - {source.part_names[material]}',
                                      mesh, uid)
            stream.write("</resources><build/></model>")
            stream.flush()
            stream.detach()
        archive.writestr("Metadata/model_settings.config",
                         model_settings(source, pieces, mesh_ids, output.name))
        for name, payload in source.passthrough.items():
            archive.writestr(name, payload)
        archive.writestr("Metadata/project_settings.config", project)
        if thumbnail:
            archive.writestr("Metadata/plate_1.png", thumbnail)
        archive.writestr("Metadata/puzzle_plan.json", json.dumps(plan, indent=2))
    return mesh_ids


def write_mesh_object(stream, object_id, material, name, mesh, uid):
    stream.write(f'<object id="{object_id}" name="{escape(name)}" type="model" pid="1008" '
                 f'pindex="{material}" p:UUID="{uid(f"mesh/{object_id}")}"><mesh><vertices>\n')
    for start in range(0, len(mesh.vertices), 10000):
        stream.writelines(f'<vertex x="{x:.17g}" y="{y:.17g}" z="{z:.17g}"/>\n'
                          for x, y, z in mesh.vertices[start:start + 10000])
    stream.write("</vertices><triangles>\n")
    for start in range(0, len(mesh.faces), 10000):
        stream.writelines(f'<triangle v1="{int(a)}" v2="{int(b)}" v3="{int(c)}"/>\n'
                          for a, b, c in mesh.faces[start:start + 10000])
    stream.write("</triangles></mesh></object>\n")


def model_settings(source: SourceProject, pieces, mesh_ids, source_file: str) -> bytes:
    """One Bambu object per piece, each still carrying its own four filaments."""
    root = etree.Element("config")
    for piece in pieces:
        object_id = PIECE_ID_BASE + piece["index"]
        node = etree.SubElement(root, "object", id=str(object_id))
        etree.SubElement(node, "metadata", key="name", value=piece["name"])
        etree.SubElement(node, "metadata", key="extruder", value="1")
        etree.SubElement(node, "metadata",
                         face_count=str(sum(len(mesh.faces) for _, mesh in piece["meshes"])))
        for material, mesh in piece["meshes"]:
            part = etree.SubElement(node, "part", id=str(mesh_ids[(piece["index"], material)]),
                                    subtype="normal_part")
            for key, value in (("name", source.part_names[material]),
                               ("matrix", "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"),
                               ("source_file", source_file),
                               ("source_object_id", str(piece["index"])),
                               ("source_volume_id", str(material)),
                               ("extruder", source.extruders[material])):
                etree.SubElement(part, "metadata", key=key, value=value)
            etree.SubElement(part, "mesh_stat", face_count=str(len(mesh.faces)),
                             edges_fixed="0", degenerate_facets="0", facets_removed="0",
                             facets_reversed="0", backwards_edges="0")
    plate = etree.SubElement(root, "plate")
    for key, value in (("plater_id", "1"), ("plater_name", f"{source.plate_name} puzzle"),
                       ("locked", "false"), ("filament_map_mode", "Auto For Flush"),
                       ("filament_maps", "1 1 1 1"), ("filament_volume_maps", "0 0 0 0")):
        etree.SubElement(plate, "metadata", key=key, value=value)
    for piece in pieces:
        instance = etree.SubElement(plate, "model_instance")
        for key, value in (("object_id", str(PIECE_ID_BASE + piece["index"])),
                           ("instance_id", "0"), ("identify_id", str(piece["index"] + 1))):
            etree.SubElement(instance, "metadata", key=key, value=value)
    assemble = etree.SubElement(root, "assemble")
    for piece in pieces:
        etree.SubElement(assemble, "assemble_item", object_id=str(PIECE_ID_BASE + piece["index"]),
                         instance_id="0", transform=source.transform, offset="0 0 0")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def write_manifest(path: Path, plan: dict):
    """The cut's own record, beside the 3MF it describes."""
    path.write_text(json.dumps(plan, indent=2) + "\n")


def render_basemap(solids, colors, low, high, pixels_per_mm: float):
    """A top-down render of the map itself, framed exactly on its footprint.

    The cut is only worth checking against the thing it cuts: whether a seam
    lands on a tower, whether a knob reaches into the park.  This is the same
    rasteriser `render_map` uses for a job's preview, driven from the solids in
    hand and from the model's measured bounds rather than from a job config, so
    the image covers the footprint one-to-one and the cut can be drawn straight
    over it in model millimetres.
    """
    import render_map

    width, height = float(high[0] - low[0]), float(high[1] - low[1])
    pixels_x = max(1, int(round(width * pixels_per_mm)))
    pixels_y = max(1, int(round(height * pixels_per_mm)))
    soup, tints = [], []
    for solid, color in zip(solids, colors):
        mesh = solid.to_mesh64()
        vertices = np.asarray(mesh.vert_properties[:, :3], np.float32)
        faces = np.asarray(mesh.tri_verts, np.int64)
        if not len(faces):
            continue
        soup.append(vertices[faces])
        rgb = np.array([int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)], np.float32)
        tints.append(np.broadcast_to(rgb, (len(faces), 3)))
    if not soup:
        raise PuzzleError("The model has no triangles to render a preview from")
    # Straight down, less the hair of tilt the camera basis needs to stay
    # non-degenerate; over the model's own height that is microns of parallax.
    camera = render_map.camera(-90.0, 89.99,
                               ((low[0] + high[0]) / 2, (low[1] + high[1]) / 2, 0.0),
                               width, pixels_x, pixels_y)
    image = render_map.render(np.ascontiguousarray(np.concatenate(soup)),
                              np.ascontiguousarray(np.concatenate(tints)), camera)
    return image


def plate_thumbnail(basemap, grid: Grid, polygons, low, longest_edge: int = 2400) -> bytes:
    """The slicer's plate image, showing the cut rather than the uncut map.

    Bambu Studio shows this next to the plate, and on a puzzle the one thing
    worth seeing there is where the pieces fall -- so the outlines are drawn
    into the render rather than the source model's own thumbnail being carried
    through, which would show an undivided map the project no longer contains.
    """
    image = Image.fromarray(basemap).convert("RGB")
    scale = image.width / grid.width
    draw = ImageDraw.Draw(image)
    for polygon in polygons:
        points = [(round((x - low[0]) * scale, 2), round(image.height - (y - low[1]) * scale, 2))
                  for x, y in polygon.exterior.coords]
        draw.line(points, fill=(20, 24, 26), width=max(1, round(scale * 0.5)), joint="curve")
    if max(image.size) > longest_edge:
        factor = longest_edge / max(image.size)
        image = image.resize((round(image.width * factor), round(image.height * factor)),
                             Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def write_preview(path: Path, grid: Grid, polygons, low, basemap=None):
    """An SVG of the cut over the map it was cut from, at model scale.

    Nothing is drawn inside a piece.  The preview exists to be checked against
    the map before a long print -- whether the knobs read as a normal jigsaw,
    whether a seam crosses something it should not -- and a label in every cell
    is noise over the only thing worth looking at.
    """
    pad = 6.0
    width, height = grid.width + 2 * pad, grid.height + 2 * pad
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.2f}mm" '
             f'height="{height:.2f}mm" viewBox="0 0 {width:.2f} {height:.2f}">',
             f'<rect width="{width:.2f}" height="{height:.2f}" fill="#f4f2ec"/>',
             f'<g transform="translate({pad:.2f},{height - pad:.2f}) scale(1,-1)">']
    if basemap is not None:
        buffer = io.BytesIO()
        Image.fromarray(basemap).save(buffer, "PNG", optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        # Placed flipped back upright inside the y-up group, covering exactly
        # the footprint the render was framed on.
        parts.append(f'<g transform="translate(0,{grid.height:.4f}) scale(1,-1)">'
                     f'<image x="0" y="0" width="{grid.width:.4f}" height="{grid.height:.4f}" '
                     f'preserveAspectRatio="none" href="data:image/png;base64,{encoded}"/></g>')
    parts.append('<g fill="none" stroke="#14181a" stroke-width="0.6" stroke-linejoin="round" '
                 'stroke-opacity="0.85">')
    for polygon in polygons:
        coords = " ".join(f"{x - low[0]:.3f},{y - low[1]:.3f}" for x, y in polygon.exterior.coords)
        parts.append(f'<polygon points="{coords}"/>')
    parts.append("</g></g></svg>")
    path.write_text("\n".join(parts) + "\n")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def inherited_cavities(solids, nozzle_mm: float, layer_height_mm: float):
    """The sealed chambers the uncut model already has.

    A puzzle piece is a subset of the map, so a chamber found inside one is
    either something the cut sealed off -- a void that used to vent through
    ground now in another piece -- or something the map was already carrying.
    Only the first is this script's to answer for.

    The distinction is not academic.  ``generate_3mf`` runs this Boolean only
    under ``--full-validation``, so a model generated without it has never had
    the check applied: on the tracked Manhattan plan, plate A5 turns out to hold
    three sealed chambers of its own, and one of them is exactly the chamber the
    puzzle cut from it reported.  Failing the puzzle for that would be blaming
    the cut for the map.
    """
    import manifold3d as md
    assembly = md.Manifold.batch_boolean(list(solids), md.OpType.Add)
    printable, _, _ = classify_cavity_shells(assembly.decompose(), nozzle_mm, layer_height_mm)
    return printable


def matches_cavity(cavity, known, tolerance: float = 0.02) -> bool:
    """Whether a piece's chamber is one the whole model already had."""
    volume, thickness = cavity
    return any(abs(volume - other) <= tolerance * max(volume, other, 1e-9)
               and abs(thickness - depth) <= tolerance * max(thickness, depth, 1e-9)
               for other, depth in known)


def validate_puzzle(output: Path, source: SourceProject, expected, footprint, *,
                    clearance_mm: float, vertical_clearance_mm: float,
                    layout: Layout, labels, solids=None):
    """Re-read the written 3MF and audit it the way `validate_3mf` audits a map.

    Independent of everything above it: the archive is reopened, the meshes are
    parsed back out of the serialized XML rather than reused from memory, and
    the geometry is judged on what a slicer will actually receive.  That is the
    point of the exercise -- an in-memory solid that never survived
    serialization intact has not been checked at all.

    The per-piece rules are `validate_3mf`'s own, applied one piece at a time:
    every material mesh closed, wound consistently, positive in volume and free
    of degenerate triangles; the piece's four filaments unioning to exactly one
    printable component with no sealed printable chamber; nothing outside the
    map's own footprint or below the plate.  The rule it adds is the one that
    only a puzzle has -- that neighbouring pieces are genuinely separate, held
    apart by the full clearance, so the plate comes off in pieces.
    """
    import manifold3d as md

    grid = layout.grid
    count = layout.pieces
    separation_mm = min(clearance_mm, vertical_clearance_mm)
    report = {"model": str(output), "pieces": {}, "clearance_mm": clearance_mm,
              "knob_recess_mm": vertical_clearance_mm, "required_separation_mm": separation_mm}
    crumb = expected["crumb_mm3"]
    with zipfile.ZipFile(output) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise PuzzleError(f"3MF ZIP integrity check failed for member {corrupt!r}")
        names = set(archive.namelist())
        for member, payload in source.passthrough.items():
            if archive.read(member) != payload:
                raise PuzzleError(
                    f"{member} was not carried through from {source.path} unchanged")
        wrapper = etree.fromstring(archive.read("3D/3dmodel.model"))
        if wrapper.get("unit") != "millimeter":
            raise PuzzleError(
                f"3MF wrapper must use millimeter units; found {wrapper.get('unit')!r}")
        core = f"{{{CORE}}}"
        production = f"{{{PROD}}}path"
        objects = wrapper.findall(f".//{core}object")
        items = wrapper.findall(f".//{core}item")
        if len(objects) != count or len(items) != count:
            raise PuzzleError(
                f"3MF declares {len(objects)} objects and {len(items)} build items for a "
                f"{count} piece puzzle")
        if {item.get("objectid") for item in items} != {obj.get("id") for obj in objects}:
            raise PuzzleError("3MF build items do not name the objects the wrapper declares")
        components = wrapper.findall(f".//{core}component")
        paths = {component.get(production) for component in components}
        if len(paths) != 1 or None in paths:
            raise PuzzleError(
                f"3MF components must reference one shared production object path; found {paths}")
        member = next(iter(paths)).lstrip("/")
        if member not in names:
            raise PuzzleError(f"3MF references a missing object member {member!r}")
        declared = sorted(int(component.get("objectid")) for component in components)
        if declared != sorted(expected["mesh_ids"]):
            raise PuzzleError(
                f"3MF component ids {declared[:8]}... do not match the {len(expected['mesh_ids'])} "
                "meshes that were written")

        settings = etree.fromstring(archive.read("Metadata/model_settings.config"))
        if len(settings.findall(".//object")) != count:
            raise PuzzleError("Bambu object settings do not declare one object per piece")
        if len(settings.findall(".//plate/model_instance")) != count:
            raise PuzzleError("The plate does not carry one instance per piece")
        if sorted(int(part.get("id")) for part in settings.findall(".//part")) != declared:
            raise PuzzleError("Bambu part ids do not match the serialized mesh objects")

        project = json.loads(archive.read("Metadata/project_settings.config"))
        if [str(color).upper() for color in project.get("filament_colour", [])] \
                != [color.upper() for color in source.colors]:
            raise PuzzleError(
                f"3MF filament colors {project.get('filament_colour')!r} do not match the "
                f"palette the meshes are indexed against {source.colors!r}")
        sequence = str(project.get("print_sequence", ""))
        if sequence != "by layer":
            raise PuzzleError(
                f"A {count} object plate must print by layer, not {sequence!r}: printing "
                "piece by piece would drive the toolhead through pieces already standing")
        written = print_profile(archive.read("Metadata/project_settings.config"))
        if written.brim_bridges_gap(clearance_mm):
            raise PuzzleError(
                f"The project keeps a {written.brim_width_mm:g} mm brim, which fits the gap "
                f"between two pieces and would weld the puzzle's first layer into one tile")

        with archive.open(member) as handle:
            meshes, colors = read_meshes(handle)
    if [color.upper() for color in colors] != [color.upper() for color in source.colors]:
        raise PuzzleError("The serialized color group does not match the source palette")

    parsed, seen = {}, {}
    for object_id, vertices, triangles in meshes:
        identifier = int(object_id)
        piece, material = expected["owner"][identifier]
        counts = expected["counts"][identifier]
        if (len(vertices), len(triangles)) != counts:
            raise PuzzleError(
                f"Piece {labels[piece]} material {material} serialized "
                f"{len(vertices)} vertices and {len(triangles)} triangles, not {counts}")
        if not np.isfinite(vertices).all():
            raise PuzzleError(
                f"Piece {labels[piece]} material {material} has non-finite vertex coordinates")
        if not len(triangles) or triangles.min() < 0 or triangles.max() >= len(vertices):
            raise PuzzleError(
                f"Piece {labels[piece]} material {material} has out-of-range triangle indices")
        mesh = trimesh.Trimesh(vertices, triangles, process=False)
        zero = int((mesh.area_faces < MINIMUM_FACE_AREA_MM2).sum())
        if not mesh.is_watertight or not mesh.is_winding_consistent or mesh.volume <= 0 or zero:
            raise PuzzleError(
                f"Piece {labels[piece]} material {material} failed geometry validation: "
                f"watertight={mesh.is_watertight}, winding_consistent={mesh.is_winding_consistent}, "
                f"volume_mm3={mesh.volume:g}, zero_area_triangles={zero}")
        low, high = mesh.bounds
        # The outline is a polygon, so a piece's *bounding box* legitimately
        # reaches outside it; what must hold is that the piece stays within the
        # map's own extent and on the plate.
        span = footprint.bounds
        if (low[2] < -PLACEMENT_TOLERANCE_MM
                or low[0] < span[0] - PLACEMENT_TOLERANCE_MM
                or low[1] < span[1] - PLACEMENT_TOLERANCE_MM
                or high[0] > span[2] + PLACEMENT_TOLERANCE_MM
                or high[1] > span[3] + PLACEMENT_TOLERANCE_MM):
            raise PuzzleError(
                f"Piece {labels[piece]} material {material} lies outside the map's "
                f"{span[2] - span[0]:.1f} x {span[3] - span[1]:.1f} mm extent or below the "
                f"plate: bounds={mesh.bounds.tolist()}")
        parsed.setdefault(piece, []).append(to_manifold(vertices, triangles))
        seen.setdefault(piece, []).append(material)

    if sorted(parsed) != list(range(count)):
        raise PuzzleError(f"The archive carries meshes for {len(parsed)} of {count} pieces")

    assemblies, sealed = {}, {}
    progress = Progress(f"Validating {output.name}", total=count, unit="pieces")
    for piece, parts in sorted(parsed.items()):
        assembly = md.Manifold.batch_boolean(parts, md.OpType.Add)
        shells = assembly.decompose()
        volumes = sorted((float(shell.volume()) for shell in shells), reverse=True)
        printable, crumbs, _ = classify_positive_shells(
            volumes, expected["nozzle_mm"], expected["layer_height_mm"])
        cavities, _, _ = classify_cavity_shells(
            shells, expected["nozzle_mm"], expected["layer_height_mm"])
        if len(printable) != 1:
            raise PuzzleError(
                f"Piece {labels[piece]} is {len(printable)} disconnected printable components of "
                f"{printable[:6]} mm3. A straight cut through an elevated road or a bridge can "
                "leave its deck floating; try a different --seed, --pieces or --grid.")
        if cavities:
            sealed.setdefault(piece, []).extend(cavities)
        # An empty layer is a per-object defect, so it can only be judged here,
        # on the piece, and the profile it is judged against is the one written
        # into the archive rather than the one the cut was planned with.
        ceiling = unprintable_ceiling(parts, expected["floor_mm"], written)
        if ceiling is not None:
            top = max(float(part.bounding_box()[5]) for part in parts)
            raise PuzzleError(
                f"Piece {labels[piece]} prints nothing between {ceiling:.2f} mm and its "
                f"{top:.2f} mm top: no section there is {written.minimum_printable_width_mm:.2f} mm "
                "across, which is the narrowest the slicer will lay an extrusion into. Bambu "
                "refuses an object with an empty layer.")
        assemblies[piece] = assembly
        progress.update(len(assemblies), detail=labels[piece])
        report["pieces"][labels[piece]] = {
            "materials": sorted(seen[piece]), "volume_mm3": float(assembly.volume()),
            "printable_components": len(printable), "negligible_shells": crumbs,
            "triangles": sum(counts for counts in
                             (expected["counts"][i][1] for i in expected["by_piece"][piece])),
        }

    progress.close()

    gaps = []
    owner = layout.owner()
    checked = set()
    for (row, col), piece in owner.items():
        here = assemblies[piece]
        for neighbour in ((row, col + 1), (row + 1, col)):
            # Two cells of the same piece are not neighbours to hold apart, and
            # one pair of pieces need only be measured once however many cell
            # boundaries they happen to share.
            if owner.get(neighbour, piece) != piece and (
                    pair := (min(piece, owner[neighbour]), max(piece, owner[neighbour]))
            ) not in checked:
                checked.add(pair)
                other = assemblies[owner[neighbour]]
                overlap = float((here ^ other).volume())
                if overlap > crumb:
                    raise PuzzleError(
                        f"Pieces {labels[piece]} and {labels[owner[neighbour]]} overlap by "
                        f"{overlap:g} mm3; they would print fused")
                # Neighbours meet in two places with two different clearances:
                # side by side across the seam, and a knob lying under the
                # other's surface. The tighter of the two is what has to hold.
                gap = float(here.min_gap(other, 4 * separation_mm))
                gaps.append(gap)
                if gap < separation_mm - PLACEMENT_TOLERANCE_MM:
                    raise PuzzleError(
                        f"Pieces {labels[piece]} and {labels[owner[neighbour]]} come within "
                        f"{gap:g} mm of each other, inside the {separation_mm:g} mm the joint "
                        f"is built to ({clearance_mm:g} mm across the seam, "
                        f"{vertical_clearance_mm:g} mm under the surface); they would print "
                        "welded together")
    report["minimum_neighbour_gap_mm"] = min(gaps) if gaps else None

    # Only now, and only if something turned up, is it worth the one big Boolean
    # that says whether the map was already carrying these.
    if sealed:
        known = (inherited_cavities(solids, expected["nozzle_mm"], expected["layer_height_mm"])
                 if solids else [])
        created = {label: [cavity for cavity in found if not matches_cavity(cavity, known)]
                   for label, found in sealed.items()}
        created = {label: found for label, found in created.items() if found}
        inherited = sum(len(found) for found in sealed.values()) - sum(
            len(found) for found in created.values())
        report["inherited_sealed_chambers"] = inherited
        report["source_sealed_chambers"] = [[round(v, 4), round(t, 4)] for v, t in known]
        if inherited:
            print(f"note: {inherited} sealed chamber(s) in the pieces are already in "
                  f"{source.path.name}; the cut did not make them. Regenerate that model with "
                  "--full-validation to see them reported there.", flush=True)
        if created:
            detail = "; ".join(
                f"{labels[piece]}: {[(round(v, 3), round(t, 3)) for v, t in found]}"
                for piece, found in sorted(created.items()))
            raise PuzzleError(
                f"The cut sealed {sum(len(f) for f in created.values())} chamber(s) that the map "
                f"does not have -- {detail} (volume mm3, effective thickness mm). A cut across a "
                "tunnel can close it off at both ends; try a different --seed, --pieces or "
                "--grid.")
    report["result"] = "passed"
    return report


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def parse_size(text: str):
    match = re.fullmatch(r"\s*([0-9.]+)\s*[xX]\s*([0-9.]+)\s*", text)
    if not match:
        raise argparse.ArgumentTypeError(f"Expected WIDTHxHEIGHT, got {text!r}")
    return float(match.group(1)), float(match.group(2))


def parse_grid(text: str):
    rows, cols = parse_size(text)
    if rows != int(rows) or cols != int(cols) or rows < 1 or cols < 1:
        raise argparse.ArgumentTypeError(f"Expected ROWSxCOLS whole numbers, got {text!r}")
    return int(rows), int(cols)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Generate a printable interlocking jigsaw puzzle from a finished NYC map 3MF.")
    parser.add_argument("input", type=Path, help="Finished map 3MF from generate_3mf.py")
    parser.add_argument("--pieces", type=int, default=25,
                        help="Exact number of puzzle pieces to produce (default 25)")
    parser.add_argument("--grid", type=parse_grid, metavar="ROWSxCOLS",
                        help="Force a division instead of deriving the squarest one from --pieces")
    parser.add_argument("--max-piece-aspect", type=float, default=1.6,
                        help="Longest acceptable piece aspect ratio (default 1.6)")
    parser.add_argument("--min-piece-fill", type=float, default=0.35,
                        help="Least of a grid cell a piece may be, where the map's outline cuts "
                             "across one; a grid that catches anything smaller is rejected "
                             "(default 0.35)")
    parser.add_argument("--plate-mm", type=parse_size, metavar="WIDTHxHEIGHT",
                        help="Printable plate area the assembled cut must fit; the project's "
                             "own printable_area when omitted")
    parser.add_argument("--brim", action=argparse.BooleanOptionalAction, default=None,
                        help="Keep the project's brim; by default it is kept only when a brim "
                             "loop cannot fit in the gap between two pieces")
    parser.add_argument("--plate-margin-mm", type=float,
                        help="Margin kept clear inside the plate; the project's brim width plus "
                             "its object gap when omitted")
    parser.add_argument("--floor-mm", type=float,
                        help="Plane the puzzle joint hides below; one layer beneath the lowest "
                             "colour skin in the model when omitted")
    parser.add_argument("--knob-recess-mm", type=float,
                        help="Vertical gap between a knob and the neighbour's surface above it; "
                             "two layer heights when omitted")
    parser.add_argument("--clearance-mm", type=float,
                        help="Total gap between neighbouring pieces; two outer-wall line widths "
                             "when omitted, the room a bead laid on a sub-bead map detail needs "
                             "on each side of the seam before the slicer calls it a collision")
    parser.add_argument("--tab-size", type=float, default=DEFAULT_TAB_SIZE,
                        help="Knob reach, as a fraction of the edge it sits on "
                             f"(default {DEFAULT_TAB_SIZE:g})")
    parser.add_argument("--tab-neck", type=float, default=DEFAULT_TAB_NECK,
                        help="Knob neck width, as a fraction of its edge "
                             f"(default {DEFAULT_TAB_NECK:g})")
    parser.add_argument("--tab-undercut-mm", type=float,
                        help="How far a knob's head out-reaches its own neck; the clearance plus "
                             "half a nozzle when omitted, so that what is left after the gap "
                             "is a real interference fit rather than a free one")
    parser.add_argument("--foundation-material", type=int,
                        help="Index of the filament carrying the substrate; the one reaching the "
                             "plate when omitted")
    parser.add_argument("--seed", type=int, default=0, help="Knob randomisation seed (default 0)")
    parser.add_argument("--puzzle-id",
                        help="Directory name under the output root; derived from the input "
                             "model and the piece count when omitted")
    parser.add_argument("--output-dir", type=Path, default=Path("output/puzzles"),
                        help="Root the puzzle directory is created under (default output/puzzles)")
    parser.add_argument("--preview", action=argparse.BooleanOptionalAction, default=True,
                        help="Write preview.svg into the puzzle directory: the cut drawn over a "
                             "top-down render of the map it was cut from")
    parser.add_argument("--preview-px-per-mm", type=float, default=8.0,
                        help="Resolution of the preview's rendered basemap (default 8)")
    parser.add_argument("--validate", action=argparse.BooleanOptionalAction, default=True,
                        help="Re-read the written 3MF and audit it the way validate_3mf audits "
                             "a map, piece by piece")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan the cut, write the preview and manifest, and cut no meshes")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    started = time.time()
    print(f"Reading {args.input}", flush=True)
    source = read_project(args.input)
    profile = source.profile
    if not profile.complete():
        raise PuzzleError(
            f"{args.input} does not carry a complete print profile ({profile}), so the cut's "
            "clearances cannot be derived from the machine it was resolved for. Pass "
            "--clearance-mm, --floor-mm, --tab-undercut-mm and --plate-mm explicitly.")
    print(f"Profile: {profile.nozzle_mm:g} mm nozzle, {profile.layer_height_mm:g} mm layers, "
          f"{profile.wall_loops} walls of {profile.wall_line_width_mm:g} mm, "
          f"{profile.plate_mm[0]:g} x {profile.plate_mm[1]:g} mm plate", flush=True)

    clearance = profile.clearance_mm if args.clearance_mm is None else args.clearance_mm
    # The undercut is derived once the knob's size is known, further down.
    undercut = args.tab_undercut_mm
    plate_w, plate_h = args.plate_mm or profile.plate_mm
    project = source.project
    brim = profile.brim_width_mm
    if args.brim and profile.brim_bridges_gap(clearance):
        raise PuzzleError(
            f"A {profile.brim_width_mm:g} mm brim cannot be kept at a {clearance:g} mm gap: "
            f"{clearance - 2 * profile.brim_object_gap_mm:.2f} mm is left between two pieces "
            f"after the {profile.brim_object_gap_mm:g} mm object gap, which fits a "
            f"{profile.outer_wall_line_width_mm:g} mm extrusion, and the first layer would weld "
            "the puzzle into one tile. Drop --brim, or narrow the gap with --clearance-mm.")
    if args.brim is False or (args.brim is None and profile.brim_bridges_gap(clearance)):
        if profile.brim_width_mm:
            reason = ("a brim loop fits the "
                      f"{clearance - 2 * profile.brim_object_gap_mm:.2f} mm left between two "
                      f"pieces after the {profile.brim_object_gap_mm:g} mm object gap, so it "
                      "would weld the first layer of the puzzle together"
                      if args.brim is None else "it was turned off")
            print(f"Brim: disabled because {reason}.", flush=True)
        brim = 0.0
        settings = json.loads(project or b"{}")
        settings["brim_type"] = "no_brim"
        project = json.dumps(settings, indent=2).encode()
    margin = (brim + profile.brim_object_gap_mm if brim else 0.0) \
        if args.plate_margin_mm is None else args.plate_margin_mm
    if not 0 < clearance < 2:
        raise PuzzleError("Clearance must be between 0 and 2 mm")
    if clearance < profile.nozzle_mm - 1e-9:
        print(f"warning: a {clearance:g} mm gap is narrower than the {profile.nozzle_mm:g} mm "
              "nozzle, so the slicer cannot resolve a void between two pieces. They will print "
              "fused and the puzzle will come off the plate as one tile.", flush=True)

    solids = [to_manifold(vertices, triangles) for _, vertices, triangles in source.meshes]
    foundation = (derive_foundation(solids) if args.foundation_material is None
                  else args.foundation_material)
    if not 0 <= foundation < len(solids):
        raise PuzzleError(f"Material {foundation} is not one of this project's "
                          f"{len(solids)} filaments")
    floor = (derive_floor(solids, foundation, profile.layer_height_mm)
             if args.floor_mm is None else args.floor_mm)
    if floor <= 0:
        raise PuzzleError("The floor height must be positive")

    bounds = np.array([solid.bounding_box() for solid in solids])
    low = bounds[:, :3].min(axis=0)
    high = bounds[:, 3:].max(axis=0)
    width, height = float(high[0] - low[0]), float(high[1] - low[1])
    print(f"Model {width:.3f} x {height:.3f} mm, {len(solids)} filaments, "
          f"{sum(solid.num_tri() for solid in solids):,} triangles", flush=True)

    usable_w, usable_h = plate_w - 2 * margin, plate_h - 2 * margin
    if width > usable_w + 1e-6 or height > usable_h + 1e-6:
        raise PuzzleError(
            f"A {width:.1f} x {height:.1f} mm map does not fit {plate_w:g} x {plate_h:g} mm with "
            f"{margin:g} mm of margin ({usable_w:g} x {usable_h:g} mm usable). "
            "Regenerate the chunk smaller, or raise --plate-mm to your printer's real area.")

    footprint = validate_floor(solids, floor, foundation)
    local = shapely.affinity.translate(footprint, -low[0], -low[1])
    fill = footprint.area / (width * height)
    print(f"Outline: {footprint.area:,.0f} mm2, {fill:.1%} of its bounding box, "
          f"{len(shapely.get_parts(footprint))} part(s)", flush=True)

    if args.grid:
        rows, cols = args.grid
        layout, slivers = layout_for(Grid(rows, cols, width, height), local,
                                     args.min_piece_fill, clearance + profile.nozzle_mm)
        if slivers:
            raise PuzzleError(
                f"--grid {rows}x{cols} catches {slivers} slivers of the map too small to be "
                f"pieces; raise --min-piece-fill to keep them or pick another grid")
        if layout.pieces != args.pieces:
            print(f"--grid {rows}x{cols} overrides --pieces {args.pieces}; "
                  f"cutting {layout.pieces} pieces", flush=True)
    else:
        layout = choose_layout(local, width, height, args.pieces, args.max_piece_aspect,
                               args.min_piece_fill, clearance + profile.nozzle_mm)
    grid = layout.grid
    if layout.pieces > MAX_PIECES:
        raise PuzzleError(f"At most {MAX_PIECES} pieces fit this 3MF's object numbering")
    shortest = min(grid.cell_width, grid.cell_height)
    neck_mm = args.tab_neck * shortest
    if neck_mm < profile.narrowest_knob_neck_mm:
        raise PuzzleError(
            f"{grid.rows} x {grid.cols} gives {grid.cell_width:.1f} x {grid.cell_height:.1f} mm "
            f"pieces, whose knobs are only {neck_mm:.2f} mm across the neck. This profile's "
            f"{profile.wall_loops} walls of {profile.wall_line_width_mm:g} mm need "
            f"{profile.narrowest_knob_neck_mm:.2f} mm before a knob is solid rather than two "
            "walls touching. Ask for fewer pieces, or raise --tab-neck.")
    if undercut is None:
        undercut = derive_undercut(neck_mm, clearance, profile.interference_mm)
    interference = undercut - clearance
    head_ratio = printed_head_ratio(neck_mm, clearance, undercut)
    if head_ratio > TAB_MAX_HEAD_RATIO:
        raise PuzzleError(
            f"At {layout.pieces} pieces a {neck_mm:.2f} mm knob neck across a {clearance:g} mm gap "
            f"prints a head {head_ratio:.2f} times its own neck, past {TAB_MAX_HEAD_RATIO:g}. "
            "Both halves of the joint lose half the gap, so on a piece this small the head that "
            "would still lock is a lump on a stalk. Ask for fewer, larger pieces, or accept a "
            "free fit with --tab-undercut-mm 0.")
    if head_ratio > TAB_TARGET_HEAD_RATIO:
        print(f"note: a {neck_mm:.2f} mm knob neck across a {clearance:g} mm gap prints a head "
              f"{head_ratio:.2f} times its own neck, past the {TAB_TARGET_HEAD_RATIO:g} a "
              "cardboard puzzle sits at. It keeps the lock, which is the point, but the knobs "
              "are chunky; fewer, larger pieces slim them.", flush=True)
    if interference <= 0:
        print(f"warning: a {undercut:g} mm undercut across a {clearance:g} mm gap leaves "
              f"{interference:g} mm of lock, so the pieces will locate each other but not hold "
              "together.", flush=True)

    crumb_mm3 = profile.crumb_mm3
    samples = profile.knob_samples(neck_mm)
    recess = (profile.vertical_clearance_mm if args.knob_recess_mm is None
              else args.knob_recess_mm)
    knob_thickness = floor - recess
    if knob_thickness < 2 * profile.layer_height_mm:
        raise PuzzleError(
            f"A {floor:.3g} mm floor less {recess:g} mm of vertical clearance leaves a "
            f"{knob_thickness:.3g} mm knob, under two {profile.layer_height_mm:g} mm layers. "
            "Lower --knob-recess-mm, or raise --floor-mm if you know this map's base.")

    rng = np.random.default_rng(args.seed)
    curves = cut_curves(grid, rng, size=args.tab_size, neck=args.tab_neck,
                        undercut=undercut, samples=samples)
    polygons = floor_polygons(layout, curves, clearance, local, profile.nozzle_mm)
    placed = [shapely.affinity.translate(polygon, low[0], low[1]) for polygon in polygons]
    seats = seat_polygons(layout, low, clearance)
    labels = [piece_label(row, col) for row, col in layout.seeds]
    merged = sum(1 for group in layout.groups if len(group) > 1)
    smallest = min(polygon.area for polygon in polygons)

    print(f"Grid {grid.rows} x {grid.cols} = {layout.pieces} pieces, "
          f"{grid.cell_width:.2f} x {grid.cell_height:.2f} mm cells ({grid.aspect:.2f}:1), "
          f"smallest piece {smallest:,.0f} mm2 "
          f"({smallest / (grid.cell_width * grid.cell_height):.0%} of a cell)"
          + (f", {merged} piece(s) absorbed a clipped neighbour" if merged else ""), flush=True)
    print(f"Joint: {floor:.3g} mm floor ({floor / profile.layer_height_mm:.0f} layers), "
          f"{neck_mm:.2f} mm narrowest knob neck, {clearance:g} mm gap, "
          f"{undercut:g} mm undercut leaving {interference:g} mm of lock", flush=True)
    print(f"Knob: {knob_thickness:.3g} mm thick "
          f"({knob_thickness / profile.layer_height_mm:.0f} layers), recessed {recess:g} mm "
          f"under its neighbour's surface, head {head_ratio:.2f} x its own neck", flush=True)

    puzzle_id = args.puzzle_id or f"{args.input.stem}_{layout.pieces}"
    directory = args.output_dir / puzzle_id
    output = directory / f"{puzzle_id}.3mf"
    plan = {
        "puzzle_id": puzzle_id,
        "schema_version": 1,
        "source_model": str(args.input),
        "command": shlex.join(sys.argv),
        "rows": grid.rows, "cols": grid.cols, "pieces": layout.pieces,
        "groups": [[list(cell) for cell in group] for group in layout.groups],
        "merged_pieces": merged,
        "outline_mm2": footprint.area, "outline_fill": fill,
        "smallest_piece_mm2": smallest,
        "min_piece_fill": args.min_piece_fill,
        "model_mm": [width, height],
        "piece_mm": [grid.cell_width, grid.cell_height],
        "piece_aspect": grid.aspect,
        "floor_mm": floor,
        "clearance_mm": clearance,
        "knob_recess_mm": recess,
        "knob_thickness_mm": knob_thickness,
        "nozzle_mm": profile.nozzle_mm,
        "tab_size": args.tab_size, "tab_neck": args.tab_neck,
        "tab_undercut_mm": undercut,
        "interference_mm": interference,
        "printed_head_ratio": head_ratio,
        "outer_wall_line_width_mm": profile.outer_wall_line_width_mm,
        "narrowest_neck_mm": neck_mm,
        "minimum_neck_mm": profile.narrowest_knob_neck_mm,
        "crumb_volume_mm3": crumb_mm3,
        "layer_height_mm": profile.layer_height_mm,
        "knob_samples": samples,
        "plate_mm": [plate_w, plate_h], "plate_margin_mm": margin,
        "brim_width_mm": brim,
        "seed": args.seed,
        "labels": labels,
    }

    directory.mkdir(parents=True, exist_ok=True)
    basemap = None
    if args.preview:
        basemap = render_basemap(solids, source.colors, low, high, args.preview_px_per_mm)
        write_preview(directory / "preview.svg", grid, placed, low, basemap)
        print(f"Preview {directory / 'preview.svg'} "
              f"({basemap.shape[1]}x{basemap.shape[0]} px, {time.time() - started:.0f}s)",
              flush=True)

    if args.dry_run:
        write_manifest(directory / "puzzle.json", plan)
        print(f"Plan {directory / 'puzzle.json'}", flush=True)
        print(json.dumps({k: v for k, v in plan.items() if k != "labels"}, indent=2), flush=True)
        return 0

    slab, upper = solids[foundation].split_by_plane([0.0, 0.0, -1.0], -floor)
    # One layer of the substrate above the plane too, so each piece's floor and
    # its surface overlap instead of meeting face to face.
    bonded = solids[foundation].trim_by_plane(
        [0.0, 0.0, -1.0], -(floor + profile.layer_height_mm))
    surfaces = {foundation: upper}
    for index, solid in enumerate(solids):
        if index != foundation:
            surfaces[index] = solid
    print(f"Split at z={floor:g}: floor slab {slab.num_tri():,} triangles", flush=True)

    floors = floor_prisms(bonded, placed, seats, floor, recess, profile.layer_height_mm)
    print(f"Floor cut into {len(floors)} knobbed pieces "
          f"({time.time() - started:.0f}s)", flush=True)

    cells = {index: split_cells(solid, grid, low) for index, solid in surfaces.items()}
    print(f"Surfaces split on the {grid.rows} x {grid.cols} grid "
          f"({time.time() - started:.0f}s)", flush=True)
    ceiling = float(high[2]) - floor + 2.0
    seat_prisms = [cross_section(seat).extrude(ceiling).translate([0.0, 0.0, floor])
                   for seat in seats]

    import manifold3d as md
    pieces = []
    discarded = 0
    shaved = []
    progress = Progress("Cutting pieces", total=layout.pieces, unit="pieces")
    for index, group in enumerate(layout.groups):
        built = []
        for material in sorted(surfaces):
            parts = [cells[material][row][col] for row, col in group]
            parts = [part for part in parts if not part.is_empty()]
            if not parts:
                solid = md.Manifold()
            else:
                region = (parts[0] if len(parts) == 1
                          else md.Manifold.batch_boolean(parts, md.OpType.Add))
                # The seat is where this piece's own surface stands. Its knobs
                # reach past it, but only through the floor, which is cut from
                # the slab below rather than from here.
                solid = region ^ seat_prisms[index]
            if material == foundation:
                if solid.is_empty():
                    raise PuzzleError(
                        f"Piece {labels[index]} has no substrate above the floor plane")
                solid = md.Manifold.batch_boolean([solid, floors[index]], md.OpType.Add)
            if solid.is_empty() or solid.num_tri() == 0:
                continue
            solid, dropped, _ = drop_negligible_shells(solid, crumb_mm3)
            discarded += dropped
            if solid.is_empty():
                continue
            built.append((material, solid))
        if not built:
            raise PuzzleError(f"Piece {labels[index]} is empty")
        # A spire too fine for the slicer to lay a bead into is an empty layer
        # of this object, and Bambu will not print an object that has one.
        ceiling = unprintable_ceiling([solid for _, solid in built], floor, profile)
        if ceiling is not None:
            was = max(float(solid.bounding_box()[5]) for _, solid in built)
            trimmed = []
            for material, solid in built:
                solid = solid.trim_by_plane([0.0, 0.0, -1.0], -ceiling)
                # The trim plane crosses whatever the piece happens to hold at
                # that height, so it sheds the same grazing debris a split does.
                solid, dropped, _ = drop_negligible_shells(solid, crumb_mm3)
                discarded += dropped
                if not solid.is_empty() and solid.num_tri():
                    trimmed.append((material, solid))
            built = trimmed
            if not built:
                raise PuzzleError(
                    f"Piece {labels[index]} prints nothing above the {floor:g} mm floor")
            shaved.append((labels[index], was - ceiling))
        if foundation not in {material for material, _ in built}:
            raise PuzzleError(
                f"Piece {labels[index]} carries no substrate, so it has no floor to hold its "
                "knobs")
        assembled = [solid for _, solid in built]
        meshes = [(material, prepare_export_mesh(solid, floor)[0])
                  for material, solid in built]
        # Connectivity is a property of the piece, not of one filament: a
        # lawn, a roof and the ground under them are separate solids that
        # print as one object. This is the rule validate_3mf applies to the
        # whole map, applied to each piece the cut produced.
        loose = [float(shell.volume())
                 for shell in md.Manifold.batch_boolean(assembled, md.OpType.Add).decompose()
                 if shell.volume() > crumb_mm3]
        if len(loose) != 1:
            raise PuzzleError(
                f"Piece {labels[index]} is {len(loose)} disconnected printable components "
                f"of {sorted(loose, reverse=True)[:6]} mm3. A straight cut through an "
                "elevated road or a bridge can leave its deck floating; try a different "
                "--seed, --pieces or --grid.")
        pieces.append({"index": index, "name": f"Piece {labels[index]}", "meshes": meshes})
        progress.update(index + 1, detail=labels[index])
    progress.close()

    plan["piece_triangles"] = [sum(len(mesh.faces) for _, mesh in piece["meshes"])
                               for piece in pieces]
    plan["negligible_shells_dropped"] = discarded
    plan["minimum_printable_width_mm"] = profile.minimum_printable_width_mm
    plan["spires_trimmed"] = [{"piece": name, "mm": depth} for name, depth in shaved]
    if discarded:
        print(f"Dropped {discarded} sub-micron Boolean shells", flush=True)
    if shaved:
        worst, deepest = max(shaved, key=lambda row: row[1])
        print(f"Trimmed spire tips off {len(shaved)} piece(s), at most {deepest:.2f} mm "
              f"(piece {worst}): below {profile.minimum_printable_width_mm:.2f} mm across, the "
              "slicer lays no extrusion and the layer would be an empty one of that object.",
              flush=True)
    thumbnail = (plate_thumbnail(basemap, grid, placed, low) if basemap is not None
                 else source.thumbnail)
    mesh_ids = write_project(output, source, pieces, layout, plan, project, thumbnail)
    print(f"{output} {output.stat().st_size:,} bytes, {len(pieces)} pieces, "
          f"{sum(plan['piece_triangles']):,} triangles ({time.time() - started:.0f}s)", flush=True)

    if args.validate:
        expected = {
            "mesh_ids": list(mesh_ids.values()),
            "owner": {identifier: key for key, identifier in mesh_ids.items()},
            "counts": {mesh_ids[(piece["index"], material)]:
                       (len(mesh.vertices), len(mesh.faces))
                       for piece in pieces for material, mesh in piece["meshes"]},
            "by_piece": {piece["index"]: [mesh_ids[(piece["index"], material)]
                                          for material, _ in piece["meshes"]]
                         for piece in pieces},
            "crumb_mm3": crumb_mm3,
            "nozzle_mm": profile.nozzle_mm,
            "layer_height_mm": profile.layer_height_mm,
            "floor_mm": floor,
        }
        validation = validate_puzzle(output, source, expected, footprint,
                                     clearance_mm=clearance, vertical_clearance_mm=recess,
                                     layout=layout, labels=labels, solids=solids)
        write_manifest(directory / "validation.json", validation)
        print(f"Validation passed: {layout.pieces} pieces, closest neighbours "
              f"{validation['minimum_neighbour_gap_mm']:.3f} mm apart "
              f"({time.time() - started:.0f}s)", flush=True)
        plan["validation"] = {"result": "passed",
                              "minimum_neighbour_gap_mm": validation["minimum_neighbour_gap_mm"]}

    write_manifest(directory / "puzzle.json", plan)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PuzzleError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
