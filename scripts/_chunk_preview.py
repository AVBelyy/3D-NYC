#!/usr/bin/env python3
"""Preview images for a multi-plate map plan.

The basemap is composed from the same cached evidence layers the cut search
used, so what you see is what the planner reasoned about — there is no second,
possibly disagreeing, rendering path and no extra dataset read.  Colors follow
the four print materials so the preview reads like the finished map.

Seams, labels and crossing markers are drawn as vectors, so an SVG render stays
sharp at any zoom while the basemap behind it keeps the cost surface's own
sampling.  Inspecting exactly where a seam sits relative to a kerb is the whole
point of the preview, so SVG is the default output.

Images are drawn in frame coordinates rather than north-up, because that is the
layout the plates take on a wall.  A north arrow carries the true bearing.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence

import matplotlib
import numpy as np
import shapely
from shapely.geometry import Polygon

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import to_rgb  # noqa: E402
from matplotlib.patches import Circle  # noqa: E402
from matplotlib.patches import Polygon as PolygonPatch  # noqa: E402

from _chunk_cost import CostSurface  # noqa: E402
from _chunk_geometry import FT, Frame  # noqa: E402


# generate_3mf.MATERIAL_COLORS, so a preview reads like the printed plate.
IVORY, GREEN, BLUE, TAN = "#F2F0E8", "#5FAA72", "#A9D5DF", "#C79A61"
OUTSIDE = "#FFFFFF"
STRUCTURE = "#8C8177"
SEAM = "#C0392B"
KEEP_OUT = "#E74C3C"
# Seams do cross elevated roads square; only lengthwise runs are defects.
CROSSING_SQUARE = "#F39C12"
CROSSING_ALONG = "#D81B60"
# The page is drawn at a fixed fraction of the printed map's own size, so a
# plan of a smaller area gets a smaller page rather than a closer view of
# less map, and two plans of one map open at the same scale and can be
# compared by flipping between them.
PAGE_MAGNIFICATION = 0.75
# No plan opens smaller than this on its long side, however little it covers.
MINIMUM_PAGE_INCHES = 6.0
MILLIMETRES_PER_INCH = 25.4
# Annotation sizes are physical, and the page scale is fixed, so they hold
# the same size relative to a plate on every plan. This is the multiplier
# they were chosen against.
ANNOTATION_SCALE = 1.4
DEFAULT_PIXELS = 6000


def basemap(surface: CostSurface, inside: np.ndarray) -> np.ndarray:
    """Compose an RGB basemap from the cached evidence layers."""
    layers = surface.layers
    shape = surface.grid.shape
    image = np.zeros(shape + (3,), dtype=float)
    image[:] = to_rgb(OUTSIDE)
    image[inside] = to_rgb(IVORY)

    def paint(mask: np.ndarray, color: str) -> None:
        selected = mask & inside
        image[selected] = to_rgb(color)

    green = layers.get("park", np.zeros(shape, bool)) | layers.get(
        "vegetation", np.zeros(shape, bool)
    )
    paint(green, GREEN)
    paint(layers.get("water", np.zeros(shape, bool)), BLUE)
    paint(layers.get("cheap_surface", np.zeros(shape, bool)), TAN)

    # Buildings darken with measured height, which is what makes a dense block
    # legible next to an open one.
    buildings = layers.get("building", np.zeros(shape, bool)) & inside
    if buildings.any():
        height = np.nan_to_num(surface.height_m, nan=0.0)
        shade = np.clip(height / 60.0, 0.0, 1.0)
        light, dark = np.asarray(to_rgb(IVORY)), np.asarray(to_rgb("#6E6A63"))
        blend = light + (dark - light) * shade[..., None]
        image[buildings] = blend[buildings]
    paint(layers.get("structure", np.zeros(shape, bool)), STRUCTURE)
    return image


def _extent(surface: CostSurface) -> tuple[float, float, float, float]:
    grid = surface.grid
    return (
        grid.origin_ft[0], grid.origin_ft[0] + grid.width * grid.resolution_ft,
        grid.origin_ft[1], grid.origin_ft[1] + grid.height * grid.resolution_ft,
    )


def _figure(surface: CostSurface, image: np.ndarray, title: str, pixels: int):
    """A page the size of the map it shows, at the resolution asked for.

    Those are two different things and the canvas must not conflate them. A
    vector page has an intrinsic size, and sizing it by a pixel budget gave
    every plan the same 60-inch page whatever it covered: a two-metre island
    and a half-metre corner of it opened at the same size, so the corner was
    drawn three times closer and its labels read three times smaller. The
    page therefore follows the ground the grid covers, and ``pixels`` sets
    the density of the basemap inside it instead.
    """
    grid = surface.grid
    printed_mm = (
        grid.frame.mm(grid.width * grid.resolution_ft),
        grid.frame.mm(grid.height * grid.resolution_ft),
    )
    inches = tuple(
        millimetres / MILLIMETRES_PER_INCH * PAGE_MAGNIFICATION for millimetres in printed_mm
    )
    longest = max(inches)
    if longest < MINIMUM_PAGE_INCHES:
        inches = tuple(side * MINIMUM_PAGE_INCHES / longest for side in inches)
        longest = MINIMUM_PAGE_INCHES
    # One pixel per cost cell is the floor the basemap must not go below;
    # above it the request decides, and the page size is not involved.
    across = max(pixels, grid.width, grid.height)
    figure, axes = plt.subplots(figsize=inches, dpi=across / longest)
    axes.imshow(image, extent=_extent(surface), origin="upper", interpolation="nearest")
    axes.set_xlim(_extent(surface)[:2])
    axes.set_ylim(_extent(surface)[2:])
    axes.set_axis_off()
    zoom = ANNOTATION_SCALE
    axes.set_title(title, fontsize=9 * zoom, loc="left", pad=6 * zoom)
    return figure, axes, zoom


def _outline(axes, polygon: Polygon, **style) -> None:
    for ring in [polygon.exterior, *polygon.interiors]:
        axes.add_patch(PolygonPatch(_drawable(np.asarray(ring.coords)), closed=True, **style))


def _drawable(vertices: np.ndarray) -> np.ndarray:
    """Drop apexes where a ring doubles straight back on itself.

    A cut running down part of a region's own outline leaves a sliver of
    that line in one piece: real boundary, but thinner than the
    manufacturing raster, so nothing is printed either side of it. Drawn, it
    reads as a seam striking out into open ground, which is the one thing it
    is not. This is for the picture only -- the geometry keeps it, because
    the ground it encloses has to belong to some plate.
    """
    kept = vertices[:-1]
    while len(kept) >= 3:
        incoming = kept - np.roll(kept, 1, axis=0)
        outgoing = np.roll(kept, -1, axis=0) - kept
        scale = np.hypot(*incoming.T) * np.hypot(*outgoing.T)
        with np.errstate(invalid="ignore", divide="ignore"):
            cosine = np.where(scale > 0, np.sum(incoming * outgoing, axis=1) / scale, 0.0)
        spikes = cosine < -1 + 1e-6
        if not spikes.any():
            break
        kept = np.delete(kept, int(np.argmax(spikes)), axis=0)
    return np.vstack([kept, kept[:1]])


def _save(figure, paths: Sequence[Path]) -> list[Path]:
    written = []
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        figure.savefig(path, bbox_inches="tight", pad_inches=0.15)
        written.append(path)
    plt.close(figure)
    return written


def _annotate(axes, frame: Frame, surface: CostSurface, zoom: float) -> None:
    """North arrow and a bar labelled in both printed and ground units."""
    minx, maxx, miny, maxy = _extent(surface)
    width, height = maxx - minx, maxy - miny
    inset = min(width, height) * 0.04
    reference = min(width, height)

    # Frame Y is the plan's bearing, so true north sits a quarter turn from it.
    bearing = math.radians(frame.bearing_deg)
    arrow = np.asarray([-math.sin(bearing), math.cos(bearing)]) * reference * 0.07
    tail = np.asarray([minx + inset + abs(arrow[0]), maxy - inset - abs(arrow[1])])
    axes.annotate(
        "", xy=tuple(tail + arrow), xytext=tuple(tail),
        arrowprops=dict(arrowstyle="-|>", color="#333333", linewidth=1.4 * zoom),
    )
    axes.text(*(tail + arrow * 1.35), "N", color="#333333", fontsize=10 * zoom,
              ha="center", va="center", fontweight="bold")

    # A round number of printed millimetres, so the bar is directly useful.
    bar_mm = next(
        (candidate for candidate in (10.0, 25.0, 50.0, 100.0, 250.0)
         if frame.feet(candidate) > width * 0.15),
        500.0,
    )
    bar = min(frame.feet(bar_mm), width - 2 * inset)
    x0, y0 = maxx - inset - bar, miny + inset + height * 0.012
    axes.plot([x0, x0 + bar], [y0, y0], color="#333333", linewidth=2.5 * zoom,
              solid_capstyle="butt")
    axes.text(
        x0 + bar / 2, y0 + height * 0.006,
        f"{bar_mm:g} mm printed = {frame.feet(bar_mm) * FT / 1000:.2f} km",
        ha="center", va="bottom", fontsize=8 * zoom, color="#333333",
        bbox=dict(boxstyle="square,pad=0.2", facecolor="white", alpha=0.75,
                  edgecolor="none"),
    )


def mark_crossings(
    axes, surface: CostSurface, crossings: Sequence[dict], extent, zoom: float
) -> int:
    """Ring every building or structure a seam passes through.

    On a two-metre map a clipped building is well under a pixel, so each one
    gets a circle sized in figure space rather than only a filled outline.
    Lengthwise runs — the ones that fail the plan by default — are drawn in a
    heavier style than unavoidable square crossings.
    """
    features = surface.keep_out_features
    if features is None or features.empty or not crossings:
        return 0
    minx, maxx, miny, maxy = extent
    radius = max(maxx - minx, maxy - miny) * 0.006
    drawn = 0
    for crossing in crossings:
        index = crossing.get("feature")
        if index is None or index >= len(features):
            continue
        shape = surface.grid.frame.to_frame(features.geometry.iloc[int(index)])
        lengthwise = bool(crossing.get("along_feature"))
        color = CROSSING_ALONG if lengthwise else CROSSING_SQUARE
        for part in (shape.geoms if hasattr(shape, "geoms") else [shape]):
            if part.geom_type != "Polygon":
                continue
            axes.add_patch(PolygonPatch(
                np.asarray(part.exterior.coords), closed=True, facecolor=color,
                edgecolor=color, alpha=0.9, linewidth=0.6 * zoom, zorder=5,
            ))
        point = shape.representative_point()
        axes.add_patch(Circle(
            (point.x, point.y), radius, fill=False, edgecolor=color,
            linewidth=(1.8 if lengthwise else 1.1) * zoom,
            linestyle="-" if lengthwise else (0, (3, 2)), zorder=6,
        ))
        drawn += 1
    return drawn


def render_plan(
    surface: CostSurface,
    frame: Frame,
    target_ft: Polygon,
    chunks: Sequence[dict],
    paths: Sequence[Path],
    *,
    title: str,
    crossings: Sequence[dict] = (),
    pixels: int = DEFAULT_PIXELS,
) -> list[Path]:
    """Draw the chunk layout over a basemap built from the local caches."""
    inside = shapely.contains_xy(
        target_ft,
        *np.meshgrid(
            surface.grid.x_at(np.arange(surface.grid.width)),
            surface.grid.y_at(np.arange(surface.grid.height)),
        ),
    )
    figure, axes, zoom = _figure(surface, basemap(surface, inside), title, pixels)
    marked = mark_crossings(axes, surface, crossings, _extent(surface), zoom)
    if marked:
        axes.add_patch(PolygonPatch(
            np.zeros((3, 2)), closed=True, facecolor=CROSSING_ALONG, edgecolor="none",
            label=f"{marked} keep-outs crossed by a seam",
        ))
        axes.legend(loc="upper right", fontsize=8 * zoom, framealpha=0.85)
    for chunk in chunks:
        polygon = chunk["polygon_ft"]
        _outline(axes, polygon, facecolor="none", edgecolor=SEAM,
                 linewidth=1.4 * zoom, zorder=3)
        minx, miny, maxx, maxy = polygon.bounds
        width, height = chunk["size_mm"]
        axes.text(
            (minx + maxx) / 2, (miny + maxy) / 2,
            f"{chunk['label']}\n{width:g} x {height:g} mm",
            ha="center", va="center", fontsize=10 * zoom, zorder=4, color="#1A1A1A",
            bbox=dict(boxstyle="round,pad=0.35", facecolor="white", alpha=0.82,
                      edgecolor=SEAM, linewidth=0.8 * zoom),
        )
    _annotate(axes, frame, surface, zoom)
    return _save(figure, paths)


def render_diagnostics(
    surface: CostSurface,
    frame: Frame,
    target_ft: Polygon,
    chunks: Sequence[dict],
    paths: Sequence[Path],
    *,
    title: str,
    crossings: Sequence[dict] = (),
    pixels: int = DEFAULT_PIXELS,
) -> list[Path]:
    """Show the cut-cost field and keep-outs the seams were routed around."""
    finite = surface.cost[np.isfinite(surface.cost)]
    ceiling = float(np.percentile(finite, 99)) if finite.size else 1.0
    normalized = np.clip(np.nan_to_num(surface.cost, posinf=ceiling), 0, ceiling) / max(ceiling, 1e-9)
    image = plt.get_cmap("magma")(np.sqrt(normalized))[..., :3]
    image[surface.keep_out] = to_rgb(KEEP_OUT)
    figure, axes, zoom = _figure(surface, image, title, pixels)
    _outline(axes, target_ft, facecolor="none", edgecolor="white",
             linewidth=1.0 * zoom, zorder=2)
    for chunk in chunks:
        _outline(axes, chunk["polygon_ft"], facecolor="none", edgecolor="#39D3F5",
                 linewidth=1.2 * zoom, zorder=3)
    mark_crossings(axes, surface, crossings, _extent(surface), zoom)
    _annotate(axes, frame, surface, zoom)
    return _save(figure, paths)
