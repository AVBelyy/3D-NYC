#!/usr/bin/env python3
"""Pure geometry for gap-free multi-plate map plans.

Everything here works in one shared *print frame*: a right-handed EPSG:2263
frame whose axes are the printed model's X and Y.  Frame-local coordinates are
survey feet measured from the frame origin, so a chunk's printed size is just
its frame extent multiplied by ``Frame.k`` millimetres per foot.

The module has no I/O and no dataset dependencies.  Cut placement is delegated
to a :class:`CutChooser`, which lets the planner drive it from real elevation
and land-cover rasters while tests drive it from synthetic geometry.
"""

from __future__ import annotations

import math
import string
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np
import shapely
from shapely.geometry import LineString, Polygon, box
from shapely.ops import split as shapely_split


# Survey foot to metre. Must match generate_3mf.FT; asserted by the test suite.
FT = 0.3048006096012192
# Generator limits restated from generate_3mf.build_config; asserted by tests.
MIN_PRINT_MM = 20.0
MAX_PRINT_MM = 250.0
MIN_SCALE = 1000.0
MAX_SCALE = 50000.0
MAX_ELEVATION_CELLS = 30_000_000
ELEVATION_CELL_M = 0.5

AXIS_X = 0
AXIS_Y = 1
# How many extra pieces a single cut may buy itself to find a clear corridor,
# and how many split positions it tries before paying for one.
EXTRA_PIECE_ATTEMPTS = 3
SPLIT_POSITION_ATTEMPTS = 5


class PlanGeometryError(ValueError):
    """A partition could not be produced under the requested constraints."""


@dataclass(frozen=True)
class Frame:
    """A shared print frame plus the manufacturing lattice derived from it.

    ``origin_ft`` anchors the lattice.  Every chunk's own print-frame origin is
    an integer number of manufacturing cells away from it along both axes,
    which is what keeps adjacent generator jobs co-registered.
    """

    origin_ft: tuple[float, float]
    x_axis: tuple[float, float]
    y_axis: tuple[float, float]
    scale: float
    grid_step_mm: float

    def __post_init__(self) -> None:
        x = np.asarray(self.x_axis, dtype=float)
        y = np.asarray(self.y_axis, dtype=float)
        if abs(np.linalg.norm(x) - 1) > 1e-9 or abs(np.linalg.norm(y) - 1) > 1e-9:
            raise PlanGeometryError("Frame axes must be unit vectors")
        if abs(float(x @ y)) > 1e-9:
            raise PlanGeometryError("Frame axes must be orthogonal")
        if float(np.linalg.det(np.vstack([x, y]))) <= 0:
            raise PlanGeometryError("Frame axes must form a right-handed pair")
        if not MIN_SCALE <= self.scale <= MAX_SCALE:
            raise PlanGeometryError(
                f"Scale denominator must be {MIN_SCALE:g}-{MAX_SCALE:g}, got {self.scale:g}"
            )
        if not 0.1 <= self.grid_step_mm <= 0.5:
            raise PlanGeometryError("Grid step must be between 0.1 and 0.5 mm")

    @classmethod
    def from_bearing(
        cls,
        bearing_rad: float,
        origin_ft: Sequence[float],
        scale: float,
        grid_step_mm: float,
    ) -> "Frame":
        """Build a frame whose Y axis points along ``bearing_rad`` east of north."""
        y = (math.sin(bearing_rad), math.cos(bearing_rad))
        x = (y[1], -y[0])
        return cls(tuple(float(v) for v in origin_ft), x, y, float(scale), float(grid_step_mm))

    @property
    def k(self) -> float:
        """Printed millimetres per survey foot."""
        return FT * 1000.0 / self.scale

    @property
    def cell_ft(self) -> float:
        """Ground footprint of one manufacturing raster cell, in feet."""
        return self.grid_step_mm / self.k

    @property
    def bearing_deg(self) -> float:
        """Frame Y axis bearing in degrees east of north."""
        return math.degrees(math.atan2(self.y_axis[0], self.y_axis[1])) % 360.0

    def mm(self, feet: float) -> float:
        return feet * self.k

    def feet(self, millimetres: float) -> float:
        return millimetres / self.k

    def to_frame(self, geometry):
        """Map EPSG:2263 geometry into frame-local feet."""
        origin = np.asarray(self.origin_ft)
        x, y = np.asarray(self.x_axis), np.asarray(self.y_axis)
        return shapely.affinity.affine_transform(
            geometry, [x[0], x[1], y[0], y[1], -float(origin @ x), -float(origin @ y)]
        )

    def to_world(self, geometry):
        """Map frame-local feet back to EPSG:2263."""
        x, y = np.asarray(self.x_axis), np.asarray(self.y_axis)
        return shapely.affinity.affine_transform(
            geometry, [x[0], y[0], x[1], y[1], self.origin_ft[0], self.origin_ft[1]]
        )

    def snap_down(self, value_ft: float) -> float:
        """Largest lattice coordinate at or below ``value_ft``."""
        return math.floor(value_ft / self.cell_ft + 1e-9) * self.cell_ft

    def snap_up(self, value_ft: float) -> float:
        """Smallest lattice coordinate at or above ``value_ft``."""
        return math.ceil(value_ft / self.cell_ft - 1e-9) * self.cell_ft

    def print_frame(self, chunk_frame_ft: tuple[float, float, float, float]) -> dict:
        """Render a lattice-aligned bbox as a ``generate_3mf --print-frame`` payload."""
        minx, miny, maxx, maxy = chunk_frame_ft
        origin = (
            np.asarray(self.origin_ft)
            + np.asarray(self.x_axis) * minx
            + np.asarray(self.y_axis) * miny
        )
        width = round(self.mm(maxx - minx) / self.grid_step_mm) * self.grid_step_mm
        height = round(self.mm(maxy - miny) / self.grid_step_mm) * self.grid_step_mm
        return {
            "origin_ft": [float(origin[0]), float(origin[1])],
            "x_axis": [float(self.x_axis[0]), float(self.x_axis[1])],
            "y_axis": [float(self.y_axis[0]), float(self.y_axis[1])],
            "size_mm": [float(width), float(height)],
        }


def chunk_frame_bounds(frame: Frame, polygon: Polygon) -> tuple[float, float, float, float]:
    """Smallest lattice-aligned frame bbox that covers ``polygon`` (frame feet)."""
    minx, miny, maxx, maxy = polygon.bounds
    return (
        frame.snap_down(minx),
        frame.snap_down(miny),
        frame.snap_up(maxx),
        frame.snap_up(maxy),
    )


def elevation_cells(width_mm: float, height_mm: float, scale: float, padding_m: float) -> float:
    """Elevation-grid cell count generate_3mf will demand for this plate."""
    width_m = width_mm / 1000.0 * scale + 2 * padding_m
    height_m = height_mm / 1000.0 * scale + 2 * padding_m
    return (width_m / ELEVATION_CELL_M) * (height_m / ELEVATION_CELL_M)


def max_scale_for_envelope(envelope_mm: tuple[float, float], padding_m: float) -> float:
    """Coarsest scale a full envelope can use without exceeding the cell limit."""
    width_mm, height_mm = envelope_mm
    lo, hi = MIN_SCALE, MAX_SCALE
    if elevation_cells(width_mm, height_mm, lo, padding_m) > MAX_ELEVATION_CELLS:
        raise PlanGeometryError(
            f"A {width_mm:g}x{height_mm:g} mm plate exceeds the "
            f"{MAX_ELEVATION_CELLS:,} elevation-cell limit at every supported scale"
        )
    for _ in range(200):
        mid = (lo + hi) / 2
        if elevation_cells(width_mm, height_mm, mid, padding_m) > MAX_ELEVATION_CELLS:
            hi = mid
        else:
            lo = mid
    return lo


# --------------------------------------------------------------------------
# Cut selection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CutRequest:
    """One guillotine cut to place inside a region, in frame-local feet.

    ``axis`` names the coordinate the cut varies slowly in: ``AXIS_X`` places a
    roughly-vertical cut whose across-coordinate is ``x`` and whose along-
    coordinate is ``y``.  ``v_nominal`` is the ideal straight position and
    ``deviation_ft`` how far the cut may wander either side of it.

    The regularization limits travel with the request so a chooser can satisfy
    them while searching, rather than having them imposed afterwards where
    straightening could push a cut back onto the building it avoided.
    """

    axis: int
    u_start: float
    u_end: float
    v_nominal: float
    deviation_ft: float
    step_ft: float
    snap_ft: float
    min_run_ft: float
    min_jog_ft: float
    # Only the part of a cut inside its region becomes a printed seam. The
    # chooser needs the region so terrain beyond it neither attracts nor
    # repels the cut.
    region: Polygon | None = None

    @property
    def samples(self) -> np.ndarray:
        count = max(2, int(math.ceil((self.u_end - self.u_start) / self.step_ft)) + 1)
        return np.linspace(self.u_start, self.u_end, count)


class CutChooser:
    """Chooses where a cut runs. Subclasses add real terrain evidence.

    ``choose`` returns the finished vertex polyline rather than a dense trace,
    so a chooser owns its own regularization.  Straightening afterwards would
    be free to undo the routing the chooser did on purpose.
    """

    style = "nominal"

    def choose(self, request: CutRequest) -> np.ndarray:
        """Return ``(N, 2)`` monotone (along, across) vertices in frame feet."""
        return np.asarray([
            [request.u_start, request.v_nominal],
            [request.u_end, request.v_nominal],
        ])

    def blocked(self, seam) -> int:
        """Count samples of a frame-local seam that fall in a hard keep-out."""
        return 0

    def describe(self, seam) -> dict:
        """Quality metrics for a finished seam. Empty when no evidence exists."""
        return {}


class StraightCuts(CutChooser):
    """The default: every cut is the straight nominal line."""


def to_points(axis: int, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Interleave along/across coordinates into frame-local (x, y) points."""
    return np.column_stack([v, u] if axis == AXIS_X else [u, v])


def from_points(axis: int, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Inverse of :func:`to_points`."""
    points = np.asarray(points, dtype=float)
    return (points[:, 1], points[:, 0]) if axis == AXIS_X else (points[:, 0], points[:, 1])


# --------------------------------------------------------------------------
# Cut regularization
# --------------------------------------------------------------------------


def rectilinear_path(
    u: np.ndarray,
    v: np.ndarray,
    *,
    snap_ft: float,
    min_run_ft: float,
    min_jog_ft: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Turn a wandering cut into an axis-parallel staircase.

    In a street-aligned frame the along-runs follow one street and the jogs
    follow the cross-street, so the result reads as a deliberate boundary
    rather than a jagged trace.  The output is monotone in ``u``, every
    along-run is at least ``min_run_ft`` long, every jog at least
    ``min_jog_ft``, and every across-coordinate sits on the ``snap_ft``
    lattice.
    """
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    if u.shape != v.shape or u.ndim != 1 or len(u) < 2:
        raise PlanGeometryError("A cut path needs matching 1-D coordinate arrays")
    quantised = np.round(v / snap_ft) * snap_ft
    runs = _runs(u, quantised)
    runs = _absorb_short_runs(runs, min_run_ft)
    runs = _merge_small_jogs(runs, min_jog_ft, snap_ft)
    runs = _absorb_short_runs(runs, min_run_ft)
    points: list[tuple[float, float]] = [(runs[0][0], runs[0][2])]
    for index, (start, end, level) in enumerate(runs):
        if index:
            # A vertical jog at u=start, from the previous level to this one.
            points.append((start, level))
        points.append((end, level))
    deduped = [points[0]]
    for point in points[1:]:
        if abs(point[0] - deduped[-1][0]) > 1e-9 or abs(point[1] - deduped[-1][1]) > 1e-9:
            deduped.append(point)
    if len(deduped) < 2:
        deduped = [(float(u[0]), deduped[0][1]), (float(u[-1]), deduped[0][1])]
    array = np.asarray(deduped, dtype=float)
    return array[:, 0], array[:, 1]


def _runs(u: np.ndarray, quantised: np.ndarray) -> list[list[float]]:
    """Group equal across-levels into ``[u_start, u_end, level]`` runs."""
    runs: list[list[float]] = []
    start = 0
    for index in range(1, len(quantised) + 1):
        if index == len(quantised) or quantised[index] != quantised[start]:
            runs.append([float(u[start]), float(u[min(index, len(u) - 1)]), float(quantised[start])])
            start = index
    runs[0][0] = float(u[0])
    runs[-1][1] = float(u[-1])
    for index in range(1, len(runs)):
        runs[index][0] = runs[index - 1][1]
    return runs


def _absorb_short_runs(runs: list[list[float]], min_run_ft: float) -> list[list[float]]:
    """Repeatedly fold the shortest sub-minimum run into a neighbor."""
    runs = [list(run) for run in runs]
    while len(runs) > 1:
        lengths = [run[1] - run[0] for run in runs]
        index = int(np.argmin(lengths))
        if lengths[index] >= min_run_ft:
            break
        left = runs[index - 1] if index > 0 else None
        right = runs[index + 1] if index + 1 < len(runs) else None
        # Prefer the longer neighbor so a single narrow street cannot drag a
        # long, well-placed run off its level.
        take_left = right is None or (
            left is not None and (left[1] - left[0]) >= (right[1] - right[0])
        )
        if take_left:
            left[1] = runs[index][1]
        else:
            right[0] = runs[index][0]
        runs.pop(index)
    return _coalesce(runs)


def _merge_small_jogs(runs: list[list[float]], min_jog_ft: float, snap_ft: float) -> list[list[float]]:
    """Flatten steps too small to read as an intentional jog."""
    runs = [list(run) for run in runs]
    while len(runs) > 1:
        jogs = [abs(runs[i + 1][2] - runs[i][2]) for i in range(len(runs) - 1)]
        index = int(np.argmin(jogs))
        if jogs[index] >= min_jog_ft:
            break
        first, second = runs[index], runs[index + 1]
        weight_a, weight_b = first[1] - first[0], second[1] - second[0]
        total = weight_a + weight_b
        level = (first[2] * weight_a + second[2] * weight_b) / total if total else first[2]
        first[2] = round(level / snap_ft) * snap_ft
        first[1] = second[1]
        runs.pop(index + 1)
    return _coalesce(runs)


def _coalesce(runs: list[list[float]]) -> list[list[float]]:
    """Join neighboring runs that ended up on the same level."""
    merged: list[list[float]] = []
    for run in runs:
        if merged and merged[-1][2] == run[2]:
            merged[-1][1] = run[1]
        else:
            merged.append(list(run))
    return merged


def cut_line(
    axis: int,
    u: np.ndarray,
    v: np.ndarray,
    *,
    overshoot_ft: float,
) -> LineString:
    """Build the splitter, extended past the region so the split is clean."""
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    u = np.concatenate([[u[0] - overshoot_ft], u, [u[-1] + overshoot_ft]])
    v = np.concatenate([[v[0]], v, [v[-1]]])
    return LineString(to_points(axis, u, v))


# --------------------------------------------------------------------------
# Partition
# --------------------------------------------------------------------------


@dataclass
class PartitionResult:
    polygons: list[Polygon]
    cuts: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _extent(polygon: Polygon, axis: int) -> float:
    minx, miny, maxx, maxy = polygon.bounds
    return (maxx - minx) if axis == AXIS_X else (maxy - miny)


def _components(geometry) -> list[Polygon]:
    """Every non-degenerate Polygon part, largest first."""
    if geometry.is_empty:
        return []
    parts = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
    polygons = [part for part in parts if part.geom_type == "Polygon" and part.area > 0]
    return sorted(polygons, key=lambda part: part.area, reverse=True)


def split_region(region: Polygon, line: LineString, axis: int) -> tuple[list[Polygon], list[Polygon]]:
    """Split ``region`` with ``line``, returning the low-side and high-side parts.

    Both sides are derived from one noded splitter, so the shared boundary
    carries identical coordinates in every resulting polygon.
    """
    pieces = _components(shapely_split(region, line))
    if len(pieces) < 2:
        return ([region], []) if pieces else ([], [])
    line_u, line_v = from_points(axis, np.asarray(line.coords))
    order = np.argsort(line_u)
    line_u, line_v = line_u[order], line_v[order]
    low: list[Polygon] = []
    high: list[Polygon] = []
    for piece in pieces:
        point = piece.representative_point()
        piece_u, piece_v = from_points(axis, np.asarray([[point.x, point.y]]))
        boundary = float(np.interp(piece_u[0], line_u, line_v))
        (low if piece_v[0] < boundary else high).append(piece)
    return low, high


def plan_cut_count(length_ft: float, limit_ft: float, deviation_ft: float) -> int:
    """Pieces needed along one axis, leaving room for the wiggle budget.

    A cut may wander ``deviation_ft`` either side of its nominal position, so
    each interior piece can grow by twice that.  Sizing against the reduced
    limit guarantees every finished piece still fits the printer envelope.
    """
    usable = limit_ft - 2 * deviation_ft
    if usable <= 0:
        raise PlanGeometryError(
            "The cut deviation budget leaves no usable plate width; "
            "lower --cut-deviation-mm or raise --max-chunk-size-mm"
        )
    return max(1, int(math.ceil(length_ft / usable - 1e-9)))


def partition(
    target: Polygon,
    *,
    limits_ft: tuple[float, float],
    deviation_ft: float,
    chooser: CutChooser | None = None,
    snap_ft: float = 0.0,
    min_run_ft: float = 0.0,
    min_jog_ft: float = 0.0,
    sample_step_ft: float = 20.0,
    max_regions: int = 4096,
) -> PartitionResult:
    """Recursively guillotine ``target`` into envelope-sized single polygons.

    Every split reuses one splitter for both sides, so the union of the result
    is exactly ``target`` and the interiors never overlap: correctness here is
    structural rather than something checked afterwards.  Disconnected pieces
    become separate chunks because ``generate_3mf --bounding-polygon`` accepts
    only a single Polygon.
    """
    chooser = chooser or StraightCuts()
    snap_ft = snap_ft or sample_step_ft
    result = PartitionResult(polygons=[])
    queue: list[Polygon] = _components(target)
    if not queue:
        raise PlanGeometryError("The target polygon is empty")
    guard = 0
    while queue:
        guard += 1
        if guard > max_regions:
            raise PlanGeometryError(
                f"Partition exceeded {max_regions} regions; the constraints are unsatisfiable"
            )
        region = queue.pop(0)
        width, height = _extent(region, AXIS_X), _extent(region, AXIS_Y)
        over_x, over_y = width / limits_ft[0], height / limits_ft[1]
        if over_x <= 1 + 1e-9 and over_y <= 1 + 1e-9:
            result.polygons.append(region)
            continue
        axis = AXIS_X if over_x >= over_y else AXIS_Y
        low, high, record = _apply_cut(
            region, axis, limits_ft, deviation_ft, chooser,
            snap_ft=snap_ft, min_run_ft=min_run_ft, min_jog_ft=min_jog_ft,
            sample_step_ft=sample_step_ft,
        )
        result.cuts.append(record)
        queue = low + high + queue
    result.polygons = _order_polygons(result.polygons)
    return result


def _apply_cut(
    region: Polygon,
    axis: int,
    limits_ft: tuple[float, float],
    deviation_ft: float,
    chooser: CutChooser,
    *,
    snap_ft: float,
    min_run_ft: float,
    min_jog_ft: float,
    sample_step_ft: float,
) -> tuple[list[Polygon], list[Polygon], dict]:
    minx, miny, maxx, maxy = region.bounds
    v_lo, v_hi = (minx, maxx) if axis == AXIS_X else (miny, maxy)
    u_lo, u_hi = (miny, maxy) if axis == AXIS_X else (minx, maxx)
    length = v_hi - v_lo
    limit = limits_ft[axis]
    planned = plan_cut_count(length, limit, deviation_ft)

    def attempt(pieces: int, index: int) -> tuple[CutRequest, np.ndarray, np.ndarray, int]:
        # The requested budget is a ceiling, not a target. Handing a cut all
        # the unused plate width instead lets it roam hundreds of metres for
        # negligible savings, and roaming is what turns a straight seam into a
        # staircase. Spare width is only ever used to stay inside the envelope.
        v_nominal = v_lo + length * index / pieces
        allowance = min(
            deviation_ft,
            (limit - length / pieces) / 2,
            (v_nominal - v_lo) * 0.45,
            (v_hi - v_nominal) * 0.45,
        )
        request = CutRequest(
            axis, u_lo, u_hi, v_nominal, max(allowance, 0.0), sample_step_ft,
            snap_ft=snap_ft, min_run_ft=min_run_ft, min_jog_ft=min_jog_ft, region=region,
        )
        path_u, path_v = _validated_path(chooser.choose(request), request)
        trial = LineString(to_points(axis, path_u, path_v)).intersection(region)
        return request, path_u, path_v, chooser.blocked(trial)

    # A band with no clear street corridor would otherwise force the cut
    # through a building. Try a different split position first, since that is
    # free, then more pieces, which widens the allowance at the price of one
    # extra plate — spent only where the geometry demands it.
    best = None
    for extra in range(EXTRA_PIECE_ATTEMPTS + 1):
        pieces = planned + extra
        balanced = max(1, pieces // 2)
        offsets = sorted(range(1, pieces), key=lambda index: (abs(index - balanced), index))
        for index in offsets[:SPLIT_POSITION_ATTEMPTS]:
            candidate = attempt(pieces, index)
            if best is None or candidate[3] < best[3]:
                best = candidate
            if candidate[3] == 0:
                break
        if best[3] == 0:
            break
    request, path_u, path_v, _ = best
    v_nominal = request.v_nominal
    overshoot = max(limits_ft) * 0.5 + sample_step_ft
    line = cut_line(axis, path_u, path_v, overshoot_ft=overshoot)
    low, high = split_region(region, line, axis)
    if not low or not high:
        raise PlanGeometryError(
            f"A cut at {v_nominal:.1f} ft on axis {axis} did not divide its region; "
            "the target polygon may be degenerate at that position"
        )
    seam = line.intersection(region)
    record = {
        "axis": "x" if axis == AXIS_X else "y",
        "nominal_ft": float(v_nominal),
        "deviation_allowance_ft": float(request.deviation_ft),
        "deviation_used_ft": float(np.max(np.abs(path_v - v_nominal))) if len(path_v) else 0.0,
        "vertices": int(len(path_u)),
        "seam_length_ft": float(seam.length),
        "style": getattr(chooser, "style", "nominal"),
        "blocked_samples": int(chooser.blocked(seam)),
        **chooser.describe(seam),
    }
    return low, high, record


def _validated_path(vertices, request: CutRequest) -> tuple[np.ndarray, np.ndarray]:
    """Check a chooser's polyline and hold it inside the deviation allowance.

    A cut that strayed outside the allowance would grow a neighboring plate
    past the printer envelope, so this is a hard clamp rather than a warning.
    """
    path = np.asarray(vertices, dtype=float)
    if path.ndim != 2 or path.shape[1] != 2 or len(path) < 2:
        raise PlanGeometryError("A cut chooser must return at least two (along, across) vertices")
    if not np.isfinite(path).all():
        raise PlanGeometryError("A cut chooser returned a non-finite vertex")
    if np.any(np.diff(path[:, 0]) < -1e-6):
        raise PlanGeometryError("A cut must not double back along its own axis")
    path[0, 0], path[-1, 0] = request.u_start, request.u_end
    low = request.v_nominal - request.deviation_ft
    high = request.v_nominal + request.deviation_ft
    path[:, 1] = np.clip(path[:, 1], low, high)
    return path[:, 0], path[:, 1]


def _order_polygons(polygons: Iterable[Polygon]) -> list[Polygon]:
    """Deterministic reading order: top row first, then left to right."""
    items = list(polygons)
    keys = []
    for polygon in items:
        point = polygon.representative_point()
        keys.append((-point.y, point.x))
    return [items[index] for index in np.lexsort((
        [key[1] for key in keys], [key[0] for key in keys]
    ))]


# --------------------------------------------------------------------------
# Post-passes
# --------------------------------------------------------------------------


def _fits(polygon: Polygon, limits_ft: tuple[float, float]) -> bool:
    return (_extent(polygon, AXIS_X) <= limits_ft[0] + 1e-9
            and _extent(polygon, AXIS_Y) <= limits_ft[1] + 1e-9)


def _neighbours(polygons: Sequence[Polygon]) -> list[tuple[int, int]]:
    """Index pairs whose boundaries touch, from a spatial index."""
    tree = shapely.STRtree(list(polygons))
    pairs: set[tuple[int, int]] = set()
    for index, polygon in enumerate(polygons):
        for other in tree.query(polygon):
            other = int(other)
            if other != index:
                pairs.add((min(index, other), max(index, other)))
    return sorted(pairs)


def compact_chunks(
    polygons: Sequence[Polygon],
    *,
    limits_ft: tuple[float, float],
    maximum_passes: int = 200,
) -> tuple[list[Polygon], list[str]]:
    """Greedily combine neighbors that still fit one plate.

    Splitting recursively is what makes the partition exact, but it also leaves
    plates that could have shared one build. Each pass takes the merge that
    packs the plate best, so the plan converges on fewer, fuller plates without
    ever producing a chunk the generator would reject.
    """
    working = list(polygons)
    notes: list[str] = []
    for _ in range(maximum_passes):
        best = None
        best_fill = -1.0
        for left, right in _neighbours(working):
            if working[left].intersection(working[right]).length <= 0:
                continue
            union = shapely.union_all([working[left], working[right]])
            if union.geom_type != "Polygon" or not _fits(union, limits_ft):
                continue
            envelope = _extent(union, AXIS_X) * _extent(union, AXIS_Y)
            fill = union.area / envelope if envelope > 0 else 0.0
            if fill > best_fill:
                best, best_fill = (left, right, union), fill
        if best is None:
            break
        left, right, union = best
        notes.append(
            f"combined two neighboring chunks onto one plate ({best_fill:.0%} filled)"
        )
        working = [
            polygon for index, polygon in enumerate(working) if index not in {left, right}
        ] + [union]
    return _order_polygons(working), notes


def undersized_reason(
    polygon: Polygon,
    *,
    min_side_ft: float,
    min_area_ft2: float,
    min_fill: float,
) -> str | None:
    """Why a chunk should be folded into a neighbor, or None if it is fine."""
    width, height = _extent(polygon, AXIS_X), _extent(polygon, AXIS_Y)
    if min(width, height) < min_side_ft:
        return f"plate side {min(width, height):,.0f} ft is under the printable minimum"
    if polygon.area < min_area_ft2:
        return f"area {polygon.area:,.0f} sq ft is under the minimum"
    envelope = width * height
    if envelope > 0 and polygon.area / envelope < min_fill:
        return f"fills only {polygon.area / envelope:.1%} of its plate"
    return None


def merge_small_chunks(
    polygons: Sequence[Polygon],
    *,
    limits_ft: tuple[float, float],
    min_side_ft: float,
    min_area_ft2: float,
    min_fill: float,
) -> tuple[list[Polygon], list[str]]:
    """Fold slivers into the neighbor sharing the longest boundary.

    A merge is only taken when the union is still one Polygon that fits the
    envelope, so the partition stays exact and every chunk stays printable.
    Chunks that cannot be merged are returned unchanged and reported, so the
    caller can fail with a specific reason rather than emit a bad plate.
    """
    working = list(polygons)
    notes: list[str] = []
    thresholds = dict(min_side_ft=min_side_ft, min_area_ft2=min_area_ft2, min_fill=min_fill)
    changed = True
    while changed and len(working) > 1:
        changed = False
        for index, polygon in enumerate(working):
            reason = undersized_reason(polygon, **thresholds)
            if reason is None:
                continue
            best, best_share = None, 0.0
            for other, candidate in enumerate(working):
                if other == index:
                    continue
                share = polygon.intersection(candidate).length
                if share <= best_share:
                    continue
                union = shapely.union_all([polygon, candidate])
                if union.geom_type != "Polygon":
                    continue
                if (_extent(union, AXIS_X) > limits_ft[0] + 1e-9
                        or _extent(union, AXIS_Y) > limits_ft[1] + 1e-9):
                    continue
                best, best_share = other, share
            if best is None:
                continue
            union = shapely.union_all([polygon, working[best]])
            notes.append(f"merged a chunk into its neighbor: {reason}")
            working = [
                geometry for position, geometry in enumerate(working)
                if position not in {index, best}
            ] + [union]
            changed = True
            break
    return _order_polygons(working), notes


def grid_labels(frame: Frame, polygons: Sequence[Polygon], limits_ft: tuple[float, float]) -> list[str]:
    """Wall-assembly labels: column letter plus row number, top-left first.

    Chunk centers are clustered rather than binned on a fixed pitch, because
    cut positions vary with the target's shape and fixed bins collide.
    """
    if not polygons:
        return []
    bounds = np.asarray([polygon.bounds for polygon in polygons])
    columns = _bands(bounds[:, 0], bounds[:, 2], descending=False)
    rows = _bands(bounds[:, 1], bounds[:, 3], descending=True)
    labels: list[str] = []
    seen: dict[str, int] = {}
    for column, row in zip(columns, rows):
        base = f"{_column_name(int(column))}{int(row) + 1}"
        seen[base] = seen.get(base, 0) + 1
        labels.append(base if seen[base] == 1 else f"{base}.{seen[base]}")
    return labels


def _bands(low: np.ndarray, high: np.ndarray, *, descending: bool) -> np.ndarray:
    """Group chunks into rows or columns by overlapping extent.

    Grouping on overlap rather than a fixed pitch keeps the labels meaningful
    when cut positions vary with the target's shape: two chunks share a band
    only if their extents actually line up.
    """
    centers = (low + high) / 2
    order = np.argsort(-centers if descending else centers, kind="stable")
    indices = np.zeros(len(low), dtype=int)
    current = 0
    band_low, band_high = low[order[0]], high[order[0]]
    for position in order:
        overlap = min(band_high, high[position]) - max(band_low, low[position])
        extent = min(band_high - band_low, high[position] - low[position])
        if overlap < 0.5 * max(extent, 1e-9):
            current += 1
            band_low, band_high = low[position], high[position]
        else:
            band_low = max(band_low, low[position])
            band_high = min(band_high, high[position])
        indices[position] = current
    return indices


def _column_name(index: int) -> str:
    letters = string.ascii_uppercase
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, len(letters))
        name = letters[remainder] + name
    return name


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_partition(
    target: Polygon,
    polygons: Sequence[Polygon],
    *,
    tolerance_ft2: float,
) -> dict:
    """Prove the plan is a true partition; raise with detail when it is not."""
    problems: list[str] = []
    for index, polygon in enumerate(polygons):
        if polygon.geom_type != "Polygon":
            problems.append(f"chunk {index} is a {polygon.geom_type}, not a Polygon")
        if not polygon.is_valid:
            problems.append(f"chunk {index} is not a valid polygon")
        if polygon.is_empty or polygon.area <= 0:
            problems.append(f"chunk {index} is empty")
    union = shapely.union_all(list(polygons)) if polygons else Polygon()
    missing = target.difference(union).area
    excess = union.difference(target).area
    overlap = 0.0
    worst: tuple[int, int, float] | None = None
    tree = shapely.STRtree(list(polygons))
    for index, polygon in enumerate(polygons):
        for other in tree.query(polygon):
            other = int(other)
            if other <= index:
                continue
            area = polygon.intersection(polygons[other]).area
            if area > overlap:
                overlap, worst = area, (index, other, area)
    if missing > tolerance_ft2:
        problems.append(f"chunks miss {missing:,.3f} sq ft of the target")
    if excess > tolerance_ft2:
        problems.append(f"chunks cover {excess:,.3f} sq ft outside the target")
    if overlap > tolerance_ft2 and worst is not None:
        problems.append(
            f"chunks {worst[0]} and {worst[1]} overlap by {worst[2]:,.3f} sq ft"
        )
    if problems:
        raise PlanGeometryError("Partition is not gap-free and non-overlapping: " + "; ".join(problems))
    return {
        "chunks": len(polygons),
        "target_area_ft2": float(target.area),
        "covered_area_ft2": float(union.area),
        "uncovered_area_ft2": float(missing),
        "excess_area_ft2": float(excess),
        "maximum_pairwise_overlap_ft2": float(overlap),
        "tolerance_ft2": float(tolerance_ft2),
    }


def validate_plates(
    frame: Frame,
    polygons: Sequence[Polygon],
    *,
    envelope_mm: tuple[float, float],
    padding_m: float,
) -> list[dict]:
    """Restate every generate_3mf plate precondition, per chunk."""
    plates: list[dict] = []
    problems: list[str] = []
    for index, polygon in enumerate(polygons):
        bounds = chunk_frame_bounds(frame, polygon)
        payload = frame.print_frame(bounds)
        width, height = payload["size_mm"]
        cells = elevation_cells(width, height, frame.scale, padding_m)
        plates.append({"index": index, "print_frame": payload,
                       "size_mm": [width, height], "elevation_cells": cells})
        if not (MIN_PRINT_MM <= width <= MAX_PRINT_MM and MIN_PRINT_MM <= height <= MAX_PRINT_MM):
            problems.append(
                f"chunk {index} is {width:g}x{height:g} mm; each side must be "
                f"{MIN_PRINT_MM:g}-{MAX_PRINT_MM:g} mm"
            )
        if width > envelope_mm[0] + 1e-9 or height > envelope_mm[1] + 1e-9:
            problems.append(
                f"chunk {index} is {width:g}x{height:g} mm, larger than the "
                f"{envelope_mm[0]:g}x{envelope_mm[1]:g} mm envelope"
            )
        for value in (width, height):
            if abs(value / frame.grid_step_mm - round(value / frame.grid_step_mm)) > 1e-6:
                problems.append(f"chunk {index} size {value:g} mm is not a grid-step multiple")
        if cells > MAX_ELEVATION_CELLS:
            problems.append(
                f"chunk {index} needs {cells:,.0f} elevation cells, over the "
                f"{MAX_ELEVATION_CELLS:,} limit"
            )
        if not box(*bounds).buffer(frame.cell_ft * 1e-6).covers(polygon):
            problems.append(f"chunk {index} is not covered by its own print frame")
        origin = np.asarray(payload["origin_ft"]) - np.asarray(frame.origin_ft)
        for axis_name, axis in (("x", frame.x_axis), ("y", frame.y_axis)):
            steps = float(origin @ np.asarray(axis)) / frame.cell_ft
            if abs(steps - round(steps)) > 1e-6:
                problems.append(
                    f"chunk {index} origin is {steps - round(steps):+.4g} cells off the "
                    f"shared {axis_name} lattice"
                )
    if problems:
        raise PlanGeometryError("Chunk plates are not printable: " + "; ".join(problems))
    return plates


def shared_edges(
    polygons: Sequence[Polygon],
    *,
    minimum_length_ft: float = 1e-6,
    tolerance_ft: float = 1e-6,
    area_tolerance_ft2: float = 1e-6,
) -> list[dict]:
    """Report neighbors and measure how far each seam sits off both boundaries.

    Both sides of every seam come from one noded splitter, so a seam should lie
    on both polygons' rings.  It is measured rather than asserted exact because
    a node GEOS computes on a slanted segment is only collinear to within
    floating point; ``tolerance_ft`` is what separates that from a real
    mismatch that would leave a hairline gap between printed plates.
    """
    records: list[dict] = []
    problems: list[str] = []
    tree = shapely.STRtree(list(polygons))
    for index, polygon in enumerate(polygons):
        for other in tree.query(polygon):
            other = int(other)
            if other <= index:
                continue
            neighbor = polygons[other]
            seam = polygon.intersection(neighbor)
            if seam.is_empty or seam.length <= minimum_length_ft:
                continue
            # Distance from each seam vertex to each ring, rather than a set
            # comparison: a hair of non-collinearity makes GEOS drop a whole
            # component from an intersection, which would report the gap
            # between components instead of the offset being looked for.
            points = shapely.points(shapely.get_coordinates(seam))
            offset = max(
                float(shapely.distance(points, side.boundary).max())
                for side in (polygon, neighbor)
            )
            if seam.area > area_tolerance_ft2:
                problems.append(
                    f"chunks {index} and {other} overlap by {seam.area:.3g} sq ft "
                    "rather than only sharing a seam"
                )
            if offset > tolerance_ft:
                problems.append(
                    f"the seam between chunks {index} and {other} sits up to "
                    f"{offset:.3g} ft off a boundary"
                )
            records.append({
                "chunks": [index, other],
                "length_ft": float(seam.length),
                "boundary_offset_ft": offset,
                # The seam geometry itself, so quality is measured on what the
                # plates will actually be cut along.
                "geometry": seam,
            })
    if problems:
        raise PlanGeometryError("Seams are not exact: " + "; ".join(problems))
    return records
