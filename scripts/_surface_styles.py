"""Pure raster styling helpers for printable NYC surface materials."""

from __future__ import annotations

import numpy as np
from scipy import ndimage

# NYC Land Cover survey classes.  Only 1 and 2 record standing vegetation; 3 is
# unvegetated natural ground, and every remaining class is built or water.
LAND_COVER_VEGETATION = (1, 2)
LAND_COVER_BARE_SOIL = 3


def vegetated_ground_mask(
    landcover,
    *,
    vegetation_classes=LAND_COVER_VEGETATION,
    bare_soil_class=LAND_COVER_BARE_SOIL,
):
    """Return the land-cover cells that are unbuilt vegetated ground.

    Bare soil is not pavement.  The survey records it wherever a field is worn,
    shaded, seasonal or freshly graded, so a lawn is routinely delivered as
    vegetation stippled with unvegetated ground.  Reading those stipples as
    built surface punches ivory holes through green that no mapped park polygon
    happens to cover, which is exactly where land cover is the only evidence
    the model has.

    A bare patch is therefore the same ground as the field it lies in, and only
    that: bare soil is green where it is continuous with a region the survey
    calls mostly vegetation.  Ground that is predominantly unvegetated -- a
    construction site, a dirt lot, a beach -- stays ivory even when it abuts a
    lawn, because the region it belongs to is not a field.  Contiguity is
    judged on shared edges, matching the manufacturable contact the material
    topology cleanup enforces later.
    """
    grid = np.asarray(landcover)
    if grid.ndim != 2:
        raise ValueError("land cover must be a two-dimensional raster")
    vegetation = np.isin(grid, list(vegetation_classes))
    bare = grid == bare_soil_class
    if not bare.any():
        return vegetation
    regions, count = ndimage.label(vegetation | bare)
    if not count:
        return vegetation
    share = np.atleast_1d(np.asarray(
        ndimage.mean(vegetation, regions, np.arange(1, count + 1)), dtype=float))
    vegetated_share = np.concatenate([[0.], share])
    return vegetation | (bare & (vegetated_share[regions] > .5))


def apply_street_palette(
    material,
    top,
    ground,
    *,
    sidewalk_mask,
    tan_pavement_mask,
    road_mask,
    relief_source: float,
) -> dict:
    """Paint tan sidewalks/paved places and ivory carriageways.

    Road priority is deliberate: small source overlaps at curb boundaries must
    not create intermittent tan holes inside an otherwise ivory road surface.
    """
    arrays = [
        np.asarray(material), np.asarray(top), np.asarray(ground),
        np.asarray(sidewalk_mask, dtype=bool), np.asarray(tan_pavement_mask, dtype=bool),
        np.asarray(road_mask, dtype=bool),
    ]
    shape = arrays[0].shape
    if arrays[0].ndim != 2 or any(array.shape != shape for array in arrays[1:]):
        raise ValueError("street palette arrays must be same-shaped and two-dimensional")
    material_array, top_array, ground_array, sidewalks, tan_pavement, roads = arrays
    tan = sidewalks | tan_pavement
    top_array[tan] = ground_array[tan] + float(relief_source)
    material_array[tan] = 3
    top_array[roads] = ground_array[roads] + float(relief_source)
    material_array[roads] = 0
    return {
        "sidewalk_cells": int(sidewalks.sum()),
        "tan_pavement_cells": int(tan_pavement.sum()),
        "ivory_road_cells": int(roads.sum()),
        "road_overlaps_repainted_ivory": int((roads & tan).sum()),
    }


def paint_trail_ribbons(
    material,
    top,
    ground,
    *,
    trail_mask,
    road_mask,
    relief_source: float,
) -> dict:
    """Paint tan trail ribbons without punching holes in an ivory carriageway.

    A mapped footway ribbon routinely overlaps the roadway beside it: marked
    crossings run kerb to kerb, and a sidewalk drawn one printable width wide
    spills into a street of the same order. Painting trails after the street
    palette lets those overlaps scatter tan cells through an otherwise ivory
    road -- the same defect ``apply_street_palette`` avoids at kerbs and
    ``paint_bridge_decks`` avoids on a shared viaduct deck. Rank by class here
    too, so road-over-tan priority holds wherever the two ribbons meet.

    A trail keeps its surface relief along its whole length: the carriageway
    cells it yields already carry the identical relief from the street palette.
    """
    arrays = [
        np.asarray(material), np.asarray(top), np.asarray(ground),
        np.asarray(trail_mask, dtype=bool), np.asarray(road_mask, dtype=bool),
    ]
    shape = arrays[0].shape
    if arrays[0].ndim != 2 or any(array.shape != shape for array in arrays[1:]):
        raise ValueError("trail ribbon arrays must be same-shaped and two-dimensional")
    material_array, top_array, ground_array, trails, roads = arrays
    relief = float(relief_source)
    if not np.isfinite(relief):
        raise ValueError("trail relief must be finite")
    top_array[trails] = ground_array[trails] + relief
    material_array[trails & ~roads] = 3
    return {
        "trail_cells": int(trails.sum()),
        "trail_cells_yielded_to_carriageways": int((trails & roads).sum()),
    }


def paint_bridge_decks(material, top, decks, *, relief_source: float) -> dict:
    """Paint mapped bridge decks so a carriageway always outranks a trail.

    Several mapped ways share one surveyed deck: a viaduct's roadway, the
    footway and cycleway beside it, and the approach ribbons that join them.
    Painting in source order lets whichever way happens to come last decide the
    colour of the whole deck, so an ivory carriageway is silently restyled tan
    by the sidewalk next to it. Rank by class instead, matching the deliberate
    road-over-tan priority ``apply_street_palette`` already applies at kerbs.
    """
    material_array = np.asarray(material)
    top_array = np.asarray(top)
    if material_array.ndim != 2 or material_array.shape != top_array.shape:
        raise ValueError("deck painting needs same-shaped two-dimensional material and top arrays")
    relief = float(relief_source)
    if not np.isfinite(relief):
        raise ValueError("deck relief must be finite")
    decks = list(decks)
    selections = []
    for deck in decks:
        rows = np.asarray(deck["rows"], dtype=np.intp)
        cols = np.asarray(deck["cols"], dtype=np.intp)
        values = np.asarray(deck["values"], dtype=float)
        if rows.ndim != 1 or rows.shape != cols.shape or rows.shape != values.shape:
            raise ValueError("each deck needs matching one-dimensional rows, cols, and values")
        selections.append((rows, cols, values, bool(deck["ivory"])))
    trail_painted = np.zeros(material_array.shape, dtype=bool)
    # Stable ordering, so ways of the same class keep their source order.
    for rows, cols, values, ivory in sorted(selections, key=lambda deck: deck[3]):
        top_array[rows, cols] = values + relief
        material_array[rows, cols] = 0 if ivory else 3
        if not ivory:
            trail_painted[rows, cols] = True
    carriageway_cells = sum(int(len(rows)) for rows, _, _, ivory in selections if ivory)
    return {
        "decks": len(selections),
        "carriageway_decks": int(sum(1 for deck in selections if deck[3])),
        "trail_decks": int(sum(1 for deck in selections if not deck[3])),
        "carriageway_deck_cells": carriageway_cells,
        "trail_deck_cells_reclaimed_by_carriageways": int(
            (trail_painted & (material_array == 0)).sum()
        ),
    }
