"""Pure elevation/profile helpers for multi-level transport crossings.

The geographic scene and the printable model have different constraints.  A
five metre underpass is real geographic evidence, but at a small map scale it
can collapse to less than two printed layers.  These helpers preserve measured
anchors and make any hidden-only print adjustment explicit and auditable.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
import shapely


def structural_roof_thickness_mm(nozzle_mm: float, layer_height_mm: float) -> float:
    """Return a layer-aligned permanent roof at least three layers/1.5 nozzles thick."""
    nozzle = float(nozzle_mm)
    layer = float(layer_height_mm)
    if not math.isfinite(nozzle) or nozzle <= 0:
        raise ValueError("nozzle width must be finite and positive")
    if not math.isfinite(layer) or layer <= 0:
        raise ValueError("layer height must be finite and positive")
    requested = max(1.5 * nozzle, 3.0 * layer)
    return round(math.ceil((requested - 1e-9) / layer) * layer, 10)


def minimum_crossing_floor_mm(
    base_mm: float, layer_height_mm: float, surface_color_depth_mm: float = 0.0
) -> float:
    """Keep a lower route and any color skin above one complete white base layer."""
    base = float(base_mm)
    layer = float(layer_height_mm)
    color = float(surface_color_depth_mm)
    if not math.isfinite(base) or base < 0:
        raise ValueError("base height must be finite and non-negative")
    if not math.isfinite(layer) or layer <= 0:
        raise ValueError("layer height must be finite and positive")
    if not math.isfinite(color) or color < 0:
        raise ValueError("surface color depth must be finite and non-negative")
    return base + layer + color


@dataclass(frozen=True)
class LinearElevationProfile:
    start: float
    end: float
    method: str
    samples: int
    rejected_outliers: int = 0


def fit_linear_elevation_profile(line, xy, elevations, fallback: float, method: str) -> LinearElevationProfile:
    """Fit a robust elevation gradient along ``line``.

    ``xy`` and ``elevations`` use the same source CRS and vertical units.  The
    fit is intentionally linear: bridge/tunnel source points are sparse and a
    higher-order curve would invent unsupported vertical detail.
    """
    coordinates = np.asarray(xy, dtype=float)
    z = np.asarray(elevations, dtype=float)
    if coordinates.size == 0 or z.size == 0 or line.is_empty or line.length <= 0:
        return LinearElevationProfile(float(fallback), float(fallback), "fallback", 0)
    coordinates = coordinates.reshape((-1, coordinates.shape[-1]))[:, :2]
    good = np.isfinite(coordinates).all(axis=1) & np.isfinite(z)
    coordinates, z = coordinates[good], z[good]
    if not len(z):
        return LinearElevationProfile(float(fallback), float(fallback), "fallback", 0)
    points = shapely.points(coordinates)
    positions = np.asarray(shapely.line_locate_point(line, points, normalized=True), dtype=float)
    good = np.isfinite(positions)
    positions, z = positions[good], z[good]
    if not len(z):
        return LinearElevationProfile(float(fallback), float(fallback), "fallback", 0)
    if len(z) < 3 or float(np.ptp(positions)) < 0.20:
        value = float(np.median(z))
        return LinearElevationProfile(value, value, method + "; constant median", len(z))

    def solve(mask):
        design = np.column_stack([np.ones(int(mask.sum())), positions[mask]])
        return np.linalg.lstsq(design, z[mask], rcond=None)[0]

    selected = np.ones(len(z), dtype=bool)
    # Start with a Theil-Sen-style median slope so one gross bridge-height
    # outlier cannot drag the initial least-squares line far enough to hide
    # itself from the residual filter.
    delta_t = positions[:, None] - positions[None, :]
    delta_z = z[:, None] - z[None, :]
    pairs = np.triu(np.abs(delta_t) > 1e-9, 1)
    slope = float(np.median(delta_z[pairs] / delta_t[pairs]))
    intercept = float(np.median(z - slope * positions))
    coefficients = np.asarray([intercept, slope])
    residual = z - (coefficients[0] + coefficients[1] * positions)
    median = float(np.median(residual))
    mad = float(np.median(np.abs(residual - median)))
    robust_sigma = 1.4826 * mad
    candidate = np.abs(residual - median) <= max(0.30, 3.5 * robust_sigma)
    if candidate.sum() >= 3 and float(np.ptp(positions[candidate])) >= 0.20:
        selected = candidate
        coefficients = solve(selected)

    start = float(coefficients[0])
    end = float(coefficients[0] + coefficients[1])
    # Bound endpoint extrapolation to the observed elevation range plus a small
    # tolerance.  This still retains real grades while preventing one bad
    # boundary vertex from producing a bridge ramp many metres high.
    observed = z[selected]
    margin = max(0.50, float(np.ptp(observed)) * 0.25)
    low, high = float(observed.min() - margin), float(observed.max() + margin)
    start, end = float(np.clip(start, low, high)), float(np.clip(end, low, high))
    return LinearElevationProfile(start, end, method, int(selected.sum()), int((~selected).sum()))


@dataclass(frozen=True)
class PrintableTunnelProfile:
    accepted: bool
    road: np.ndarray
    ceiling: np.ndarray
    maximum_geographic_separation_mm: float
    maximum_hidden_floor_adjustment_mm: float
    maximum_portal_roof_overcut_mm: float
    reason: str


def printable_tunnel_profile(
    surface_mm,
    road_mm,
    *,
    minimum_cover_mm: float,
    minimum_clearance_mm: float,
    minimum_evidence_mm: float,
    maximum_clearance_mm: float = 1.40,
    portal_fraction: float = 0.15,
    protected_surface_mask=None,
) -> PrintableTunnelProfile:
    """Create a printable hidden road/roof profile without moving its portals.

    A positive measured separation establishes that the mapped topology is
    physically plausible.  Where that real separation becomes sub-layer at the
    requested map scale, only the occluded road floor is lowered.  The
    adjustment reaches zero at both mapped portals, keeping it connected to the
    measured exposed approaches.  The small roof overcut near those endpoints
    is part of the daylight portal opening and is reported separately.
    """
    surface = np.asarray(surface_mm, dtype=float)
    baseline = np.asarray(road_mm, dtype=float)
    if surface.shape != baseline.shape or surface.ndim != 1 or len(surface) < 2:
        raise ValueError("surface_mm and road_mm must be same-length one-dimensional profiles")
    if not np.isfinite(surface).all() or not np.isfinite(baseline).all():
        raise ValueError("crossing profiles must be finite")
    separation = surface - baseline
    maximum = float(separation.max())
    if maximum < minimum_evidence_mm:
        empty = baseline.copy()
        return PrintableTunnelProfile(
            False, empty, empty, maximum, 0.0, 0.0,
            "insufficient positive terrain/structure separation",
        )

    target = float(minimum_cover_mm + minimum_clearance_mm)
    t = np.linspace(0.0, 1.0, len(baseline))
    edge = max(1e-6, float(np.sin(np.pi * np.clip(portal_fraction, 1e-3, 0.49))))
    # Full adjustment through the covered core, smoothly returning to the two
    # surveyed/inferred approach anchors at the portals.
    weight = np.clip(np.sin(np.pi * t) / edge, 0.0, 1.0)
    if protected_surface_mask is not None:
        protected=np.asarray(protected_surface_mask,dtype=bool)
        if protected.shape!=surface.shape:
            raise ValueError("protected_surface_mask must match the elevation profile")
        # A bridge is not a daylight portal: never taper the hidden-floor
        # adjustment while any mapped upper deck must remain above it.
        weight[protected]=1.0
    deficit = np.maximum(0.0, target - separation)
    adjustment = deficit * weight
    road = baseline - adjustment
    roof_limit = surface - minimum_cover_mm
    ceiling = np.minimum(roof_limit, road + maximum_clearance_mm)
    ceiling = np.maximum(ceiling, road + minimum_clearance_mm)
    portal_overcut = np.maximum(0.0, ceiling - roof_limit)
    return PrintableTunnelProfile(
        True,
        road,
        ceiling,
        maximum,
        float(adjustment.max()),
        float(portal_overcut.max()),
        "measured separation with hidden-only printable adjustment",
    )


def minimum_crossing_length_mm(config: dict) -> float:
    """Smallest opening worth constructing, never less than two nozzle widths."""
    configured = float(config.get("minimum_crossing_length_mm", 0.0))
    return max(configured, 2.0 * float(config.get("nozzle_mm", 0.4)))


def constrain_deck_to_visible_surface(planned_surface, visible_surface):
    """Keep a crossing void below the deck actually selected during field fusion.

    Several mapped ways can share/intersect a surveyed structure. Their fitted
    grades need not agree exactly. The cutter may use the lower visible deck as
    a roof constraint, but must never raise it to the grade of a different way.
    NaN means there is no mapped upper-deck sample at that position.
    """
    planned = np.asarray(planned_surface, dtype=float)
    visible = np.asarray(visible_surface, dtype=float)
    if planned.shape != visible.shape or not np.isfinite(planned).all():
        raise ValueError("deck profiles must have matching shapes and finite planned elevations")
    protected = np.isfinite(visible)
    return np.where(protected, np.minimum(planned, visible), planned), protected


def tunnel_surface_masks(tunnel_mask, park_mask, bridge_mask, surface_route_mask):
    """Restore ground over a tunnel without erasing an untagged upper road.

    Surface route masks must exclude the tunnel itself. A mapped road/path
    crossing a tunnel remains an upper surface even without a bridge tag.
    """
    tunnel,park,bridge,surface=(np.asarray(value,dtype=bool) for value in
        (tunnel_mask,park_mask,bridge_mask,surface_route_mask))
    if not (tunnel.shape==park.shape==bridge.shape==surface.shape):
        raise ValueError("crossing masks must have matching shapes")
    protected=tunnel&(bridge|surface)
    return tunnel&park&~protected,protected
