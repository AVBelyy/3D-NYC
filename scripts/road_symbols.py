"""Scale-aware, semantic road symbols for the four-colour map.

The ivory road colour is a cartographic symbol, not an erosion of the source
roadbed polygon.  This module deliberately separates the questions:

* is this linear feature physically a carriageway or a trail?
* how wide must its tan casing and ivory centre be to print reliably?

All widths below are model-space millimetres.  OSM access restrictions are not
used as a proxy for physical form: a car-free former drive may still be a
carriageway, while an asphalt footway remains a trail.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString, Polygon


# These values are categorical exclusions.  They must never be promoted to an
# ivory road, even when a trail is paved, named, or overlaps mapped pavement.
TRAIL_HIGHWAYS = frozenset({
    "footway", "path", "steps", "bridleway", "cycleway", "track",
})
STRONG_CARRIAGEWAYS = frozenset({
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
    "tertiary_link",
})
MAJOR_CARRIAGEWAYS = frozenset({
    "motorway", "trunk", "primary", "secondary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
})
NON_LINEAR_HIGHWAYS = frozenset({
    "bus_stop", "crossing", "elevator", "give_way", "milestone",
    "platform", "proposed", "construction", "raceway", "rest_area",
    "services", "speed_camera", "stop", "street_lamp", "traffic_signals",
})
EXCLUDED_SERVICE_TYPES = frozenset({
    "alley", "driveway", "drive-through", "emergency_access",
    "parking_aisle", "slipway",
})
PAVED_SURFACES = frozenset({
    "asphalt", "concrete", "concrete:lanes", "concrete:plates", "paved",
    "paving_stones", "sett", "chipseal",
})
SOURCE_FT_TO_M = 0.3048006096012192
DEFAULT_ROAD_WIDTH_M = {
    "motorway": 24.0, "motorway_link": 10.0,
    "trunk": 20.0, "trunk_link": 10.0,
    "primary": 17.0, "primary_link": 9.0,
    "secondary": 13.0, "secondary_link": 8.0,
    "tertiary": 11.0, "tertiary_link": 8.0,
    "residential": 9.0, "unclassified": 9.0, "living_street": 7.0,
    "pedestrian": 8.0, "service": 7.0, "road": 9.0,
}


@dataclass(frozen=True)
class SymbolWidths:
    core_mm: float
    outer_mm: float


def trail_width_mm(highway: str, tags: dict, config: dict) -> float:
    """Return the same printable width for a trail in fields and crossing meshes."""
    try:
        source_width_m = float(str(tags.get("width", "")).replace(" m", ""))
    except ValueError:
        source_width_m = 0.0
    if not 0.3 < source_width_m < 20.0:
        source_width_m = 3.0 if highway in {"bridleway", "cycleway", "pedestrian"} else 1.8
    return max(
        float(config["minimum_path_width_mm"]),
        source_width_m * 1000.0 / float(config["scale_denominator"]),
    )


def _ceil_to(value: float, increment: float) -> float:
    return math.ceil((value - 1e-9) / increment) * increment


def _round_to(value: float, increment: float) -> float:
    return math.floor(value / increment + 0.5) * increment


def printable_widths(
    config: dict,
    major: bool = False,
    physical_width_m: float | None = None,
    scale_denominator: float | None = None,
) -> SymbolWidths:
    """Return scale-adaptive widths which remain printable.

    The minimum symbol is used at small scales. Once the measured road becomes
    wider than that minimum in model space, the ivory core grows with it while
    retaining the configured tan-to-ivory ratio.
    """
    grid = float(config.get("grid_step_mm", 0.125))
    nozzle = float(config.get("nozzle_mm", 0.4))
    minimum_bead = nozzle * float(config.get("road_minimum_bead_ratio", 0.85))
    minimum_core = _ceil_to(max(nozzle * 1.125, grid * 4), grid)
    casing = _ceil_to(max(minimum_bead, grid * 3), grid)
    minimum_outer = minimum_core + 2 * casing
    if major:
        minimum_core = _ceil_to(max(minimum_core, grid * 5), grid)
        minimum_outer = _ceil_to(max(minimum_outer, grid * 12), grid)
        casing = (minimum_outer - minimum_core) / 2
    scaled_outer = 0.0
    if physical_width_m is not None and scale_denominator:
        scaled_outer = float(physical_width_m) * 1000 / float(scale_denominator)
    outer = _ceil_to(max(minimum_outer, scaled_outer), grid)
    fraction = float(config.get("road_core_fraction", 0.40))
    if not 0.20 <= fraction <= 0.70:
        raise ValueError("road_core_fraction must be between 0.20 and 0.70")
    target_core = max(minimum_core, outer * fraction)
    core = min(max(minimum_core, _round_to(target_core, grid)), outer - 2 * casing)
    return SymbolWidths(core_mm=core, outer_mm=outer)


def _numeric_tag(value) -> float | None:
    text = str(value or "").strip().lower()
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", text)
    if not match:
        return None
    number = float(match.group())
    if "ft" in text or "feet" in text or "'" in text:
        number *= 0.3048
    return number if math.isfinite(number) and number > 0 else None


def tagged_road_width_m(tags: dict) -> tuple[float | None, str | None]:
    width = _numeric_tag(tags.get("width"))
    if width is not None and 2.5 <= width <= 60:
        return width, "OSM width tag"
    lanes = _numeric_tag(tags.get("lanes"))
    if lanes is not None and 1 <= lanes <= 16:
        return lanes * 3.2, "OSM lane-count fallback"
    return None, None


def sample_roadbed_widths_m(
    geometry,
    roadbed_union,
    interval_m: float = 15.0,
    maximum_width_m: float = 60.0,
) -> list[float]:
    """Measure roadbed cross-sections normal to a centerline.

    Multiple samples and a low percentile later suppress intersection flares.
    The connected cross-section nearest the centerline is used, rather than a
    polygon area/length estimate that would be distorted by nearby streets.
    """
    if geometry.is_empty or roadbed_union.is_empty:
        return []
    values = []
    interval_source = interval_m / SOURCE_FT_TO_M
    half_source = maximum_width_m / SOURCE_FT_TO_M
    match_source = 1.5 / SOURCE_FT_TO_M
    for part in shapely.get_parts(geometry):
        if part.geom_type != "LineString" or part.length <= 0:
            continue
        count = max(3, min(30, int(math.ceil(part.length / interval_source))))
        for fraction in np.linspace(0.10, 0.90, count):
            position = float(fraction * part.length)
            delta = min(5 / SOURCE_FT_TO_M, max(part.length * 0.05, 0.2 / SOURCE_FT_TO_M))
            before = part.interpolate(max(0.0, position - delta))
            after = part.interpolate(min(part.length, position + delta))
            point = part.interpolate(position)
            dx, dy = after.x - before.x, after.y - before.y
            norm = math.hypot(dx, dy)
            if norm <= 0:
                continue
            px, py = -dy / norm, dx / norm
            cross = LineString([
                (point.x - px * half_source, point.y - py * half_source),
                (point.x + px * half_source, point.y + py * half_source),
            ])
            cut = cross.intersection(roadbed_union)
            parts = [
                item for item in shapely.get_parts(cut)
                if item.geom_type == "LineString" and item.distance(point) <= match_source
            ]
            if not parts:
                continue
            width = max(item.length for item in parts) * SOURCE_FT_TO_M
            if 2.5 <= width <= maximum_width_m:
                values.append(float(width))
    return values


def _default_width_m(highways: list[str]) -> float:
    return max((DEFAULT_ROAD_WIDTH_M.get(value, 9.0) for value in highways), default=9.0)


def parse_tags(value) -> dict:
    if isinstance(value, dict):
        return value
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return {}
    try:
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _present(tags: dict, key: str) -> bool:
    return str(tags.get(key, "")).strip().lower() not in {"", "no", "none", "nan"}


def _normalized_name(tags: dict) -> str:
    value = tags.get("ref") or tags.get("name") or tags.get("official_name") or ""
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def grade_key(tags: dict) -> str:
    bridge = str(tags.get("bridge", "no")).lower() not in {"", "no", "none", "nan"}
    tunnel = str(tags.get("tunnel", "no")).lower() not in {"", "no", "none", "nan"}
    try:
        layer = int(float(tags.get("layer", 0) or 0))
    except (TypeError, ValueError):
        layer = 0
    return f"{'bridge' if bridge else 'tunnel' if tunnel else 'surface'}:{layer}"


def is_surface_grade(tags: dict) -> bool:
    return grade_key(tags) == "surface:0"


def roadbed_support(line, roadbed_union, tolerance_source: float = 0.0) -> float:
    if line.is_empty or line.length <= 0 or roadbed_union.is_empty:
        return 0.0
    support_geometry = roadbed_union.buffer(tolerance_source) if tolerance_source else roadbed_union
    covered = line.intersection(support_geometry)
    return float(min(1.0, covered.length / line.length))


def classify_highway(highway: str, tags: dict, support: float) -> tuple[str, str]:
    """Classify one OSM way by physical form, conservatively.

    ``trail`` means tan-only. ``carriageway`` may receive an ivory core.
    ``other`` is ignored by the surface symbolizer.
    """
    highway = str(highway or "").strip().lower()
    if highway in TRAIL_HIGHWAYS:
        return "trail", f"categorical trail class highway={highway}"
    if highway in STRONG_CARRIAGEWAYS:
        return "carriageway", f"established carriageway class highway={highway}"
    if highway in NON_LINEAR_HIGHWAYS or not highway:
        return "other", f"non-road highway={highway or 'missing'}"
    if str(tags.get("area", "")).lower() == "yes":
        return "trail" if highway == "pedestrian" else "other", "area feature, not a road centreline"

    paved = str(tags.get("surface", "")).lower() in PAVED_SURFACES
    named = bool(_normalized_name(tags))
    lane_form = _present(tags, "lanes")
    speed_form = _present(tags, "maxspeed")
    direction_form = str(tags.get("oneway", "")).lower() in {"yes", "1", "true", "-1"}
    route_form = _present(tags, "ref") or _present(tags, "destination")
    physical_evidence = sum([lane_form, speed_form, direction_form, route_form])

    if highway == "pedestrian":
        # Examples include pedestrian plazas and formerly motorized park drives.
        # Requiring multiple road-form tags keeps plazas/walks tan, while lanes +
        # maxspeed/oneway identify East and West Drive despite access restrictions.
        if paved and physical_evidence >= 2 and (named or support >= 0.70):
            return "carriageway", "pedestrian access with strong carriageway-form metadata"
        return "trail", "pedestrian feature lacks strong carriageway-form metadata"

    if highway == "service":
        service = str(tags.get("service", "")).strip().lower()
        if service in EXCLUDED_SERVICE_TYPES:
            return "other", f"minor service={service}"
        if physical_evidence >= 1 and (paved or support >= 0.50):
            return "carriageway", "service way with carriageway-form metadata"
        if named and support >= 0.70:
            return "carriageway", "named service way supported by measured roadbed"
        return "other", "service way lacks sufficient carriageway evidence"

    if highway == "road" and support >= 0.70:
        return "carriageway", "generic road supported by measured roadbed"
    return "other", f"unsupported highway={highway}"


def _polygon_parts(geometry) -> list:
    if geometry.is_empty:
        return []
    valid = shapely.make_valid(geometry)
    return [part for part in shapely.get_parts(valid) if part.geom_type == "Polygon" and not part.is_empty]


def _gdf(rows: list[dict], crs=2263) -> gpd.GeoDataFrame:
    if rows:
        return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)
    return gpd.GeoDataFrame({"geometry": gpd.GeoSeries([], crs=crs)}, geometry="geometry", crs=crs)


def build_road_symbols(
    osm: gpd.GeoDataFrame,
    roadbed: gpd.GeoDataFrame,
    aoi,
    scale_denominator: float,
    config: dict,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame, dict]:
    """Build normalized routes, minimum tan casings, and flush ivory cores."""
    crs = osm.crs or roadbed.crs or 2263
    k = 0.3048006096012192 * 1000 / float(scale_denominator)
    tolerance_source = float(config.get("road_surface_match_tolerance_mm", 0.20)) / k
    roadbed_geoms = [shapely.make_valid(g) for g in roadbed.geometry if g is not None and not g.is_empty]
    roadbed_union = shapely.union_all(roadbed_geoms) if roadbed_geoms else Polygon()
    support_geometry = roadbed_union.buffer(tolerance_source) if not roadbed_union.is_empty else roadbed_union

    rows = []
    counts: dict[str, int] = {}
    candidates = osm[osm.geometry.notna() & osm.geom_type.isin(["LineString", "MultiLineString"])].copy()
    candidates = candidates[candidates.get("highway", pd.Series(index=candidates.index, dtype=object)).notna()]
    for _, row in candidates.iterrows():
        tags = parse_tags(row.get("tags"))
        highway = str(row.get("highway") or tags.get("highway") or "").lower()
        geometry = shapely.make_valid(row.geometry).intersection(aoi)
        line_parts = [part for part in shapely.get_parts(geometry) if part.geom_type == "LineString" and part.length > 0]
        if not line_parts:
            continue
        geometry = shapely.union_all(line_parts)
        support = roadbed_support(geometry, support_geometry)
        classification, reason = classify_highway(highway, tags, support)
        eligible = classification == "carriageway"
        grade = grade_key(tags)
        route_key = _normalized_name(tags) or f"osm:{int(row.osm_id)}"
        counts[classification] = counts.get(classification, 0) + 1
        record = {
            "osm_id": int(row.osm_id), "highway": highway,
            "name": str(tags.get("name") or ""), "route_key": route_key,
            "classification": classification, "classification_reason": reason,
            "core_eligible": bool(eligible), "grade_key": grade,
            "bridge": grade.startswith("bridge:"), "tunnel": grade.startswith("tunnel:"),
            "surface_support_ratio": support,
            "_tags": tags, "_major": highway in MAJOR_CARRIAGEWAYS,
            "geometry": geometry,
        }
        rows.append(record)

    # Estimate widths per route rather than per OSM fragment. This keeps a
    # street continuous across tag splits, bridges, and short intersection
    # ways. Unnamed ways retain their stable OSM-id route key.
    groups: dict[str, list[dict]] = {}
    for record in rows:
        if record["core_eligible"]:
            groups.setdefault(record["route_key"], []).append(record)
    percentile = float(config.get("road_width_percentile", 30.0))
    if not 10 <= percentile <= 50:
        raise ValueError("road_width_percentile must be between 10 and 50")
    interval_m = float(config.get("road_width_sample_interval_m", 15.0))
    maximum_width_m = float(config.get("road_maximum_physical_width_m", 60.0))
    for route_key, group in groups.items():
        surface_group = [record for record in group if record["grade_key"] == "surface:0"]
        samples = []
        for record in surface_group:
            samples.extend(sample_roadbed_widths_m(
                record["geometry"], roadbed_union, interval_m, maximum_width_m
            ))
        tagged = [tagged_road_width_m(record["_tags"])[0] for record in group]
        tagged = [value for value in tagged if value is not None]
        if samples:
            physical_width_m = float(np.percentile(samples, percentile))
            method = f"NYC roadbed normal cross-sections, p{percentile:g}"
        elif tagged:
            physical_width_m = float(np.median(tagged))
            method = "OSM width/lane metadata fallback"
        else:
            physical_width_m = _default_width_m([record["highway"] for record in group])
            method = "road-class physical-width fallback"
        physical_width_m = float(np.clip(physical_width_m, 2.5, maximum_width_m))
        widths = printable_widths(
            config, major=any(record["_major"] for record in group),
            physical_width_m=physical_width_m, scale_denominator=scale_denominator,
        )
        for record in group:
            record.update({
                "estimated_road_width_m": physical_width_m,
                "width_estimation_method": method,
                "width_sample_count": len(samples),
                "core_width_mm": widths.core_mm,
                "outer_width_mm": widths.outer_mm,
                "core_fraction": widths.core_mm / widths.outer_mm,
            })

    eligible_buffers = []
    core_buffers = []
    for record in rows:
        if record["core_eligible"] and record["grade_key"] == "surface:0":
            eligible_buffers.append(record["geometry"].buffer(record["outer_width_mm"] / (2 * k), quad_segs=6))
            core_buffers.append(record["geometry"].buffer(record["core_width_mm"] / (2 * k), quad_segs=6))
        record.pop("_tags", None)
        record.pop("_major", None)

    outer_union = shapely.union_all(eligible_buffers).intersection(aoi) if eligible_buffers else Polygon()
    core_union = shapely.union_all(core_buffers).intersection(outer_union).intersection(aoi) if core_buffers else Polygon()
    outer_parts = _polygon_parts(outer_union)
    core_parts = _polygon_parts(core_union)
    outer = _gdf([
        {"source": "OSM carriageway centerlines", "style": "minimum tan casing", "geometry": part}
        for part in outer_parts
    ], crs)
    core = _gdf([
        {"source": "OSM carriageway centerlines", "style": "flush ivory core", "geometry": part}
        for part in core_parts
    ], crs)
    routes = _gdf(rows, crs)

    forbidden_eligible = routes[
        routes.get("core_eligible", pd.Series(dtype=bool)).fillna(False)
        & routes.get("highway", pd.Series(dtype=object)).isin(TRAIL_HIGHWAYS)
    ] if len(routes) else routes
    if len(forbidden_eligible):
        raise RuntimeError("Categorical trail classes were incorrectly made ivory-core eligible")
    outside = core_union.difference(outer_union)
    if not outside.is_empty and outside.area * k * k > 1e-6:
        raise RuntimeError("Ivory road core escapes its tan casing")

    eligible_routes = routes[routes.core_eligible] if len(routes) else routes
    estimated_widths = eligible_routes.estimated_road_width_m.to_numpy() if len(eligible_routes) else np.array([])
    core_fractions = eligible_routes.core_fraction.to_numpy() if len(eligible_routes) else np.array([])
    report = {
        "algorithm": "centerline-derived classic cased ribbon",
        "scale_denominator": float(scale_denominator),
        "classification_counts": counts,
        "candidate_routes": int(len(routes)),
        "eligible_routes": int(len(eligible_routes)),
        "categorical_trails_eligible": int(len(forbidden_eligible)),
        "surface_eligible_routes": int(sum(bool(r["core_eligible"]) and r["grade_key"] == "surface:0" for r in rows)),
        "outer_polygons": len(outer), "core_polygons": len(core),
        "outer_area_mm2": float(outer.area.sum() * k * k) if len(outer) else 0.0,
        "core_area_mm2": float(core.area.sum() * k * k) if len(core) else 0.0,
        "minimum_print_widths_mm": {
            "ordinary": printable_widths(config).__dict__,
            "major": printable_widths(config, major=True).__dict__,
        },
        "target_core_fraction": float(config.get("road_core_fraction", 0.40)),
        "estimated_physical_width_m_range": [float(estimated_widths.min()), float(estimated_widths.max())] if len(estimated_widths) else None,
        "actual_core_fraction_range": [float(core_fractions.min()), float(core_fractions.max())] if len(core_fractions) else None,
        "surface_match_tolerance_mm": float(config.get("road_surface_match_tolerance_mm", 0.20)),
        "height_relationship": "ivory core is flush with tan casing",
        "trail_policy": "footway/path/steps/bridleway/cycleway/track and Parks trails are tan-only",
    }
    return routes, outer, core, report
