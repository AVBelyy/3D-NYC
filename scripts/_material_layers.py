"""Shared material-layer rules for a white substrate and colored surface solids."""

from __future__ import annotations

import math

import numpy as np


# Filament order is the print's material identity: index 0 is the first
# extruder, and every stage -- fields, meshes, packaging, validation -- indexes
# materials by these positions.  The names are the palette's roles, not the
# filament actually loaded, which the operator is free to change.
MATERIAL_NAMES = ["ivory", "green", "blue", "tan"]


def surface_color_depth_mm(layer_height_mm: float, minimum_depth_mm: float = 0.24) -> float:
    """Return a layer-aligned color depth thick enough for a solid surface skin."""
    layer = float(layer_height_mm)
    minimum = float(minimum_depth_mm)
    if not math.isfinite(layer) or layer <= 0:
        raise ValueError("layer height must be finite and positive")
    if not math.isfinite(minimum) or minimum <= 0:
        raise ValueError("minimum surface color depth must be finite and positive")
    requested = max(minimum, 2.0 * layer)
    return round(math.ceil((requested - 1e-9) / layer) * layer, 10)


def white_substrate_top(support_surface_mm, aoi_mask, *, base_mm: float, color_depth_mm: float) -> np.ndarray:
    """Place a continuous white substrate just below the lowest supported surface."""
    ground = np.asarray(support_surface_mm, dtype=float)
    mask = np.asarray(aoi_mask, dtype=bool)
    if ground.ndim != 2 or ground.shape != mask.shape:
        raise ValueError("ground and AOI mask must be same-shaped two-dimensional arrays")
    base = float(base_mm)
    depth = float(color_depth_mm)
    if not math.isfinite(base) or not math.isfinite(depth) or base < 0 or depth <= 0:
        raise ValueError("base and color depth must be finite, with a positive color depth")
    if not np.isfinite(ground[mask]).all():
        raise ValueError("ground inside the AOI must be finite")
    result = np.full(ground.shape, base, dtype=np.float32)
    result[mask] = np.maximum(base, ground[mask] - depth)
    return result


# The coarsest layer height any supported print profile uses.  A drawn map
# symbol has to read as raised on that profile too, so it is the height the
# default symbol is sized against.  ``test_material_layers`` holds it to the
# profiles ``generate_3mf`` actually offers.
COARSEST_LAYER_HEIGHT_MM = 0.24
DRAWN_LINE_RELIEF_MM = 2 * COARSEST_LAYER_HEIGHT_MM


def drawn_line_relief_mm(config: dict) -> float:
    """Return how far a drawn road or trail line stands above the ground below it.

    A road is a printed line, not a painted patch.  Laid flush with the
    pavement pad beside it, an ivory carriageway ribbon is separated from the
    tan around it by colour alone, so the eye reads every junction, marked
    crossing and kerb overlap the two materials meet at as a break in a line
    that is in fact continuous.  Giving the line its own edge fixes that.

    Two bounds meet in the height.  The line is a cartographic symbol, so how
    far it stands proud is a map-design choice that must not shrink when a
    finer layer height is selected -- the default is two layers at the coarsest
    supported profile, the height at which the same symbol still prints raised
    everywhere.  It is also printed, so whatever is asked for is floored at a
    height that survives slicing: two layers above the ground it is drawn on,
    and one layer above the pavement pad it has to out-top.
    """
    pad = float(config["path_relief_mm"])
    layer = float(config["layer_height_mm"])
    if not math.isfinite(pad) or pad < 0:
        raise ValueError("pavement relief must be finite and non-negative")
    if not math.isfinite(layer) or layer <= 0:
        raise ValueError("layer height must be finite and positive")
    requested = config.get("road_line_relief_mm")
    requested = DRAWN_LINE_RELIEF_MM if requested is None else float(requested)
    if not math.isfinite(requested) or requested <= 0:
        raise ValueError("drawn road line relief must be finite and positive")
    return round(max(requested, 2.0 * layer, pad + layer), 10)


# The pavement pad is a cartographic step, not a structural one: it exists so a
# sidewalk, plaza or roadbed reads as raised beside the ground it abuts.  The
# request is therefore a design height, and 0.16 mm is the height the map is
# drawn against.
PAVEMENT_PAD_RELIEF_MM = 0.16


def pavement_pad_relief_mm(layer_height_mm: float, requested_mm: float = PAVEMENT_PAD_RELIEF_MM) -> float:
    """Return the pavement pad rounded up to a whole number of printed layers.

    A pad shallower than one layer does not print shallower -- it prints
    intermittently.  The slicer resolves the pad's top edge only on the layers
    whose z-plane happens to fall above the ground beside it, so a step of 0.16
    mm under 0.24 mm layers appears on roughly two thirds of the boundary and
    vanishes on the rest, and the tan/ivory seam it defines breaks up into
    slots that look like missing extrusion.

    Rounding up is the whole fix: on a layer-aligned pad every point of the
    boundary slices the same way, so the step either exists everywhere or --
    for a pad deliberately set to zero, which is not this -- nowhere.  It is
    rounded up rather than to nearest because a map symbol may grow to stay
    printable and must never silently shrink out of the print.
    """
    layer = float(layer_height_mm)
    requested = float(requested_mm)
    if not math.isfinite(layer) or layer <= 0:
        raise ValueError("layer height must be finite and positive")
    if not math.isfinite(requested) or requested <= 0:
        raise ValueError("pavement pad relief must be finite and positive")
    return round(math.ceil((requested - 1e-9) / layer) * layer, 10)


# The height of the plate's first printed layer.  It sets the phase of every
# slicing plane above it, so the mesh cannot be aligned to the slicer without
# it, and ``package_3mf`` cannot write a profile that disagrees with the mesh.
# One definition, tested by ``test_material_layers``.
FIRST_LAYER_HEIGHT_MM = 0.2


def surface_layer_plane_mm(level, layer_height_mm, first_layer_height_mm=FIRST_LAYER_HEIGHT_MM):
    """Return the printed top of layer ``level`` counting from the plate."""
    return float(first_layer_height_mm) + np.asarray(level) * float(layer_height_mm)


def printable_surface_mm(surface_mm, mask, *, layer_height_mm,
                         first_layer_height_mm=FIRST_LAYER_HEIGHT_MM):
    """Quantize visible surfaces onto the slicer's layer planes, without inventing steps.

    A printed surface exists only at a layer plane.  The map's drawn symbols
    already know this -- ``surface_color_depth_mm``, ``pavement_pad_relief_mm``
    and ``drawn_line_relief_mm`` all round their relief up to whole layers --
    but the terrain those symbols stand on is a continuous elevation field, and
    nothing rounds it.  The slicer therefore decides each cell's level on its
    own, by comparing a float height against a plane it happens to sit near.

    Where a surface is flatter than one layer that decision is noise.  A car
    park level to 0.04 mm whose height lands on a slicing plane is torn into
    two levels in a random speckle; a road ribbon 0.875 mm wide is split along
    its length by a cross-fall the print cannot show.  Both read as missing
    material rather than as relief, because there is no relief to read.

    Two rules fix it, and both come from the print rather than from the map:

    * A surface is snapped to the plane it prints at, so it stands half a layer
      clear of the planes on either side.  Nothing decided by micrometres.
    * A step between two levels survives only when the surfaces it separates
      really are a layer apart.  Neighbouring regions whose combined elevation
      span is under one layer are one surface that quantization split, so they
      are merged and printed at one level.

    The second rule is the same measure ``pavement_pad_relief_mm`` applies to a
    symbol: relief thinner than a layer does not print thinner, it prints
    intermittently.  It follows that every deliberate symbol survives -- a
    pavement pad stands exactly one layer, a carriageway three -- because the
    pipeline already sizes them at a layer or more, and a step backed by a
    whole layer of relief is never merged.  No cell moves by as much as a
    layer, and only cells inside a region flatter than a layer move at all.
    """
    surface = np.asarray(surface_mm, dtype=float)
    inside = np.asarray(mask, dtype=bool)
    if surface.ndim != 2 or surface.shape != inside.shape:
        raise ValueError("surface and mask must be same-shaped two-dimensional arrays")
    layer = float(layer_height_mm)
    first = float(first_layer_height_mm)
    if not math.isfinite(layer) or layer <= 0:
        raise ValueError("layer height must be finite and positive")
    if not math.isfinite(first) or first <= 0:
        raise ValueError("first layer height must be finite and positive")
    if not inside.any():
        return surface.copy()
    if not np.isfinite(surface[inside]).all():
        raise ValueError("surface inside the mask must be finite")
    levels = np.rint((surface - first) / layer).astype(np.int64)
    levels = _settle_flat_zones(surface, levels, inside, layer=layer, first=first)
    result = surface.copy()
    result[inside] = first + levels[inside] * layer
    return result






def _settle_flat_zones(surface, levels, inside, *, layer, first):
    """Print each region flatter than one layer at one level.

    A step between two layers is something the model asserts about the ground:
    that it rises a layer here.  Rounding a float height against a plane it
    happens to sit on asserts that without evidence -- a car park level to
    0.04 mm whose height lands on a slicing plane is torn into two levels in a
    random speckle, and a carriageway ribbon 0.875 mm wide is split down its
    length by a cross-fall no layer can show.  Both read as missing material,
    because there is no relief there to read as relief.

    So the surface is partitioned into the largest regions that rise less than
    one layer end to end, and each is printed at a single plane.  The partition
    is grown from the smallest height differences outward, so a region stops
    growing exactly where a layer of measured relief appears: a kerb standing
    one layer and a carriageway standing three are never absorbed, and a
    hillside becomes the contour bands its own relief defines rather than the
    bands a rounding phase happens to cut.

    No surface moves a whole layer.  A region spans under a layer and prints at
    the plane nearest its middle, so every cell in it stays inside the pair of
    planes that already bracketed it.
    """
    from scipy import ndimage

    zones, count = _quasi_flat_zones(surface, inside, layer)
    if count < 1:
        return levels
    index = np.arange(1, count + 1)
    low = np.asarray(ndimage.minimum(surface, zones, index), dtype=float)
    high = np.asarray(ndimage.maximum(surface, zones, index), dtype=float)
    plane = np.rint(((low + high) / 2.0 - first) / layer).astype(np.int64)
    settled = levels.copy()
    spot = inside & (zones > 0)
    settled[spot] = plane[zones[spot] - 1]
    return settled


def _quasi_flat_zones(surface, inside, tolerance):
    """Partition the surface into maximal edge-connected regions spanning under ``tolerance``.

    Neighbouring cells are joined in order of how little they differ, and a
    join is refused once it would make the region as tall as the tolerance.
    Growing from the smallest differences is what makes the partition depend on
    the ground rather than on where the rounding phase fell: the boundaries it
    keeps are the largest height changes on the map, which are its kerbs, its
    carriageways and its slopes.
    """
    height, width = surface.shape
    node = np.full(surface.shape, -1, dtype=np.int64)
    node[inside] = np.arange(int(inside.sum()))
    count = int(inside.sum())
    if not count:
        return np.zeros(surface.shape, dtype=np.int64), 0
    starts, ends, weights = [], [], []
    for axis in (0, 1):
        here = node.take(np.arange((height, width)[axis] - 1), axis=axis)
        there = node.take(np.arange(1, (height, width)[axis]), axis=axis)
        near = surface.take(np.arange((height, width)[axis] - 1), axis=axis)
        far = surface.take(np.arange(1, (height, width)[axis]), axis=axis)
        joined = (here >= 0) & (there >= 0)
        starts.append(here[joined])
        ends.append(there[joined])
        weights.append(np.abs(near[joined] - far[joined]))
    start = np.concatenate(starts)
    end = np.concatenate(ends)
    weight = np.concatenate(weights)
    order = np.argsort(weight, kind="stable")
    parent = np.arange(count, dtype=np.int64)
    low = surface[inside].astype(float)
    high = low.copy()
    _grow_quasi_flat_zones(parent, low, high, start[order], end[order], float(tolerance))
    roots = np.array([_find_root(parent, item) for item in range(count)], dtype=np.int64)
    labels = np.zeros(surface.shape, dtype=np.int64)
    unique, packed = np.unique(roots, return_inverse=True)
    labels[inside] = packed + 1
    return labels, len(unique)


def _find_root(parent, item):
    while parent[item] != item:
        parent[item] = parent[parent[item]]
        item = parent[item]
    return item


def _grow_quasi_flat_zones(parent, low, high, start, end, tolerance):
    """Join cells cheapest difference first, refusing any join a layer tall."""
    find = _find_root
    for index in range(len(start)):
        left = find(parent, int(start[index]))
        right = find(parent, int(end[index]))
        if left == right:
            continue
        bottom = low[left] if low[left] < low[right] else low[right]
        top = high[left] if high[left] > high[right] else high[right]
        if top - bottom >= tolerance - 1e-9:
            continue
        parent[right] = left
        low[left] = bottom
        high[left] = top
