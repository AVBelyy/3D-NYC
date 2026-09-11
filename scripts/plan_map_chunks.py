#!/usr/bin/env python3
"""Plan a large NYC polygon as gap-free neighboring print plates.

The planner partitions one WGS84 Polygon into chunks that each fit a single
build plate, routes every seam over low, uniform ground using cached LiDAR and
Planimetrics evidence, and writes the exact ``generate_3mf.py`` commands that
produce plates which butt together with no gap, overlap, or step at the seam.

Planning writes commands and a preview; it does not generate any 3MF itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shlex
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
import structlog
from shapely.geometry import Polygon

import _chunk_geometry as geometry
from _chunk_cost import (
    LIDAR_DATASETS,
    CostWeights,
    SourceDataError,
    build_cost_surface,
    cache_signature,
)
from _chunk_geometry import Frame, PlanGeometryError
from cache_common import read_tiled_geoparquet
from _material_layers import MATERIAL_NAMES
from generate_3mf import (
    FT,
    NYC_BOUNDS,
    parse_bounding_polygon,
    parse_material_color,
    parse_size,
)
from terrain_relief import choose_terrain_relief


ROOT = Path(__file__).resolve().parents[1]
PLANNER_VERSION = 1
CRS = 2263
# OSM highway classes worth a vote when reading the dominant street bearing,
# weighted by how strongly each defines a neighbourhood's grid.
BEARING_WEIGHTS = {
    "motorway": 6.0, "trunk": 5.0, "primary": 4.0, "secondary": 3.0,
    "tertiary": 2.0, "residential": 1.5, "unclassified": 1.0, "living_street": 1.0,
    "motorway_link": 2.0, "trunk_link": 2.0, "primary_link": 2.0, "secondary_link": 1.5,
}
MINIMUM_SEGMENT_FT = 20.0
# Bearing histogram resolution, and how far from the peak a segment still
# counts as belonging to the same grid.
BEARING_BINS = 360
BEARING_TOLERANCE_DEG = 5.0
# Share of road length that must sit near the peak for a grid to be believed.
MINIMUM_GRID_COHERENCE = 0.25


class PlanError(RuntimeError):
    """The request cannot be satisfied; the message explains what to change."""


# --------------------------------------------------------------------------
# Orientation
# --------------------------------------------------------------------------


def street_bearing(cache_dir: Path, target: Polygon) -> dict:
    """Dominant street bearing inside ``target``, from cached OSM geometry.

    Street directions are 90-degree periodic, so this works on bearings modulo
    a quarter turn.  It takes the *mode* rather than the mean: averaging pulls
    the answer toward every irregular quarter in the target, and the frame only
    has to serve the grid that most of the cuts will run along.  Over all of
    Manhattan the mean lands on 27.3 degrees while the real grid is 28.9, and
    that 1.5-degree error drifts a cut more than a block across the island, so
    no seam can stay in one street.

    The peak is then refined to the circular mean of the segments near it, and
    the share of length within ``BEARING_TOLERANCE_DEG`` of the peak is
    reported as the confidence in a grid existing at all.
    """
    # Fail here with the actionable message rather than deep inside pyogrio:
    # --orientation auto is the one place the planner reads OSM before the
    # cost surface validates every cache.
    cache_signature(cache_dir, "new_york_osm")
    osm = read_tiled_geoparquet(
        cache_dir / "new_york_osm", tuple(target.bounds),
        deduplicate_by=["source_order"], source_order=["source_order"],
    )
    if osm.empty or "highway" not in osm:
        return {"source": "none", "coherence": 0.0, "bearing_rad": None}
    roads = osm[osm["highway"].isin(BEARING_WEIGHTS)
               & osm.geom_type.isin(["LineString", "MultiLineString"])]
    roads = roads[roads.intersects(target)]
    angles: list[np.ndarray] = []
    weights: list[np.ndarray] = []
    for shape_geometry, highway in zip(roads.geometry.values, roads["highway"].values):
        weight = BEARING_WEIGHTS[highway]
        parts = shape_geometry.geoms if shape_geometry.geom_type == "MultiLineString" else [shape_geometry]
        for part in parts:
            coordinates = shapely.get_coordinates(part)
            if len(coordinates) < 2:
                continue
            delta = np.diff(coordinates, axis=0)
            length = np.hypot(delta[:, 0], delta[:, 1])
            keep = length > MINIMUM_SEGMENT_FT
            if not keep.any():
                continue
            delta, length = delta[keep], length[keep]
            angles.append(np.degrees(np.arctan2(delta[:, 0], delta[:, 1])) % 90.0)
            weights.append(weight * length)
    if not angles:
        return {"source": "none", "coherence": 0.0, "bearing_rad": None}
    angle = np.concatenate(angles)
    weight = np.concatenate(weights)
    total = float(weight.sum())
    if total <= 0:
        return {"source": "none", "coherence": 0.0, "bearing_rad": None}

    peak, coherence = dominant_bearing(angle, weight)
    return {
        "source": "osm_streets",
        "coherence": coherence,
        "bearing_rad": float(math.radians(peak)),
        "segments_ft": total,
    }


def dominant_bearing(angle_deg: np.ndarray, weight: np.ndarray) -> tuple[float, float]:
    """Modal bearing of a weighted set of quarter-turn directions, in degrees.

    Returns the peak and the share of weight lying within
    ``BEARING_TOLERANCE_DEG`` of it. Taking the mode rather than the mean is
    what stops one irregular quarter of a target from dragging the frame off
    the grid that most cuts will follow.
    """
    counts, edges = np.histogram(angle_deg, bins=BEARING_BINS, range=(0.0, 90.0), weights=weight)
    # Smooth across the wrap so a grid straddling two bins is not split in half.
    window = np.asarray([1.0, 2.0, 3.0, 4.0, 5.0, 4.0, 3.0, 2.0, 1.0])
    pad = len(window) // 2
    smoothed = np.convolve(
        np.concatenate([counts[-pad:], counts, counts[:pad]]), window / window.sum(), "valid"
    )
    peak = float(edges[int(np.argmax(smoothed))] + 90.0 / BEARING_BINS / 2)
    # Refine to the circular mean of the segments that belong to that peak.
    offset = (angle_deg - peak + 45.0) % 90.0 - 45.0
    near = np.abs(offset) <= BEARING_TOLERANCE_DEG
    if not near.any():
        return peak % 90.0, 0.0
    peak = (peak + float(np.average(offset[near], weights=weight[near]))) % 90.0
    return peak, float(weight[near].sum() / weight.sum())


def envelope_bearing(target: Polygon) -> float:
    """Bearing of the polygon's minimum rotated rectangle long axis."""
    with warnings.catch_warnings():
        # GEOS divides by a zero slope for an axis-aligned rectangle. The
        # result is still correct; only the warning is spurious.
        warnings.filterwarnings("ignore", "divide by zero", RuntimeWarning)
        warnings.filterwarnings("ignore", "invalid value", RuntimeWarning)
        rectangle = target.minimum_rotated_rectangle
    corners = shapely.get_coordinates(rectangle.exterior)[:-1]
    if len(corners) != 4:
        return 0.0
    edges = [corners[(index + 1) % 4] - corners[index] for index in range(4)]
    lengths = [float(np.linalg.norm(edge)) for edge in edges]
    axis = edges[int(np.argmax(lengths))] / max(lengths)
    return math.atan2(axis[0], axis[1]) % math.pi


def choose_bearing(cache_dir: Path, target: Polygon, requested: str) -> dict:
    """Resolve --orientation into one bearing shared by every chunk."""
    long_axis = envelope_bearing(target)
    if requested == "north":
        return {"bearing_rad": 0.0, "source": "north", "coherence": None,
                "envelope_bearing_deg": math.degrees(long_axis)}
    if requested != "auto":
        try:
            degrees = float(requested)
        except ValueError as error:
            raise PlanError(
                f"--orientation must be auto, north, or a bearing in degrees, got {requested!r}"
            ) from error
        return {"bearing_rad": math.radians(degrees % 180.0), "source": "explicit",
                "coherence": None, "envelope_bearing_deg": math.degrees(long_axis)}
    grid = street_bearing(cache_dir, target)
    if grid["bearing_rad"] is None or grid["coherence"] < MINIMUM_GRID_COHERENCE:
        return {"bearing_rad": long_axis, "source": "minimum_rotated_rectangle",
                "coherence": grid["coherence"],
                "envelope_bearing_deg": math.degrees(long_axis)}
    # The street grid fixes the axes only up to a quarter turn. Take whichever
    # of the two puts the polygon's long axis on the frame's Y axis, which is
    # what keeps a long, narrow target from wasting plate width.
    candidates = [grid["bearing_rad"], grid["bearing_rad"] + math.pi / 2]
    best = min(candidates, key=lambda bearing: abs(_aligned_extent(target, bearing)[0]))
    return {
        "bearing_rad": float(best % math.pi),
        "source": "osm_street_grid",
        "coherence": grid["coherence"],
        "street_bearing_deg": math.degrees(grid["bearing_rad"]),
        "envelope_bearing_deg": math.degrees(long_axis),
    }


def _aligned_extent(target: Polygon, bearing: float) -> tuple[float, float]:
    y_axis = np.asarray([math.sin(bearing), math.cos(bearing)])
    x_axis = np.asarray([y_axis[1], -y_axis[0]])
    coordinates = shapely.get_coordinates(target)
    return float(np.ptp(coordinates @ x_axis)), float(np.ptp(coordinates @ y_axis))


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def build_frame(target: Polygon, bearing: float, scale: float, grid_step_mm: float) -> Frame:
    """Anchor the shared manufacturing lattice just outside the target."""
    provisional = Frame.from_bearing(bearing, (0.0, 0.0), scale, grid_step_mm)
    coordinates = shapely.get_coordinates(target)
    x_axis, y_axis = np.asarray(provisional.x_axis), np.asarray(provisional.y_axis)
    origin = (
        x_axis * float((coordinates @ x_axis).min())
        + y_axis * float((coordinates @ y_axis).min())
    )
    return Frame.from_bearing(bearing, origin.tolist(), scale, grid_step_mm)


def plan_partition(target_ft: Polygon, frame: Frame, chooser, options) -> dict:
    """Partition, pack the pieces onto as few plates as possible, then report.

    Recursive splitting is what makes the partition exact, but it leaves plates
    that could have shared a build, so the compaction pass runs before any
    sliver handling.
    """
    limits = (frame.feet(options.envelope_mm[0]), frame.feet(options.envelope_mm[1]))
    snap_ft = round(options.cut_snap_mm / frame.grid_step_mm) * frame.cell_ft
    result = geometry.partition(
        target_ft,
        limits_ft=limits,
        deviation_ft=frame.feet(options.cut_deviation_mm),
        chooser=chooser,
        snap_ft=snap_ft,
        min_run_ft=frame.feet(options.min_edge_mm),
        min_jog_ft=frame.feet(options.min_jog_mm),
        sample_step_ft=options.sample_step_ft,
    )
    thresholds = dict(
        min_side_ft=frame.feet(geometry.MIN_PRINT_MM),
        min_area_ft2=limits[0] * limits[1] * options.min_chunk_fill,
        min_fill=options.min_chunk_fill,
    )
    polygons, notes = geometry.compact_chunks(result.polygons, limits_ft=limits)
    polygons, sliver_notes = geometry.merge_small_chunks(
        polygons, limits_ft=limits, **thresholds
    )
    unmergeable = [
        reason for reason in
        (geometry.undersized_reason(polygon, **thresholds) for polygon in polygons)
        if reason is not None
    ]
    unprintable = [reason for reason in unmergeable if "printable minimum" in reason]
    if unprintable:
        raise PlanError(
            "A chunk is too small to print and has no neighbor it can merge with "
            f"({unprintable[0]}). Try a finer --scale, a larger --max-chunk-size-mm, "
            "or a target polygon without that spur."
        )
    return {"polygons": polygons, "cuts": result.cuts,
            "merge_notes": notes + sliver_notes,
            "limits_ft": limits, "snap_ft": snap_ft, "soft_warnings": unmergeable}


def estimate_chunks(target_ft: Polygon, frame: Frame, options) -> int:
    """Cheap straight-cut count, a lower bound used to bracket a scale search."""
    limits = (frame.feet(options.envelope_mm[0]), frame.feet(options.envelope_mm[1]))
    result = geometry.partition(
        target_ft,
        limits_ft=limits,
        deviation_ft=frame.feet(options.cut_deviation_mm),
        sample_step_ft=options.sample_step_ft,
    )
    polygons, _ = geometry.compact_chunks(result.polygons, limits_ft=limits)
    return len(polygons)


def smallest_feasible_scale(target: Polygon, bearing: float, options) -> float | None:
    """Coarsest-detail scale whose straight-cut plan fits the chunk budget.

    Straight cuts are a lower bound on plate count, so this brackets the real
    search rather than answering it.
    """
    ceiling = geometry.max_scale_for_envelope(options.envelope_mm, options.source_padding_m)
    low, high = max(options.scale, geometry.MIN_SCALE), min(ceiling, geometry.MAX_SCALE)
    if low > high:
        return None

    def fits(scale: float) -> bool:
        frame = build_frame(target, bearing, scale, options.grid_step_mm)
        try:
            return estimate_chunks(frame.to_frame(target), frame, options) <= options.max_chunks
        except PlanGeometryError:
            return False

    if not fits(high):
        return None
    for _ in range(24):
        middle = (low + high) / 2
        if fits(middle):
            high = middle
        else:
            low = middle
    return high


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def chunk_records(frame: Frame, polygons, labels, shared: dict) -> list[dict]:
    records = []
    for polygon, label in zip(polygons, labels):
        bounds = geometry.chunk_frame_bounds(frame, polygon)
        print_frame = frame.print_frame(bounds)
        world = frame.to_world(polygon)
        wgs84 = gpd.GeoSeries([world], crs=CRS).to_crs(4326).iloc[0]
        records.append({
            "label": label,
            "polygon_ft": polygon,
            "polygon_wgs84": wgs84,
            "print_frame": print_frame,
            "size_mm": print_frame["size_mm"],
            "area_ft2": float(polygon.area),
            "area_km2": float(polygon.area * FT * FT / 1e6),
            "fill": float(polygon.area / max(
                (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]), 1e-9)),
            "vertices": int(len(shapely.get_coordinates(polygon))),
            "elevation_cells": geometry.elevation_cells(
                *print_frame["size_mm"], frame.scale, shared["source_padding_m"]),
        })
    return records


def command_for(chunk: dict, shared: dict, paths: dict) -> list[str]:
    command = [
        shared["python"], "scripts/generate_3mf.py",
        "--bounding-polygon", f"@{paths['polygon']}",
        "--print-frame", f"@{paths['frame']}",
        "--scale", f"{shared['scale']:.6f}",
        "--grid-step-mm", f"{shared['grid_step_mm']:g}",
        "--layer-height", f"{shared['layer_height']:g}",
        "--vertical-exaggeration", f"{shared['vertical_exaggeration']:g}",
        "--terrain-origin-m", f"{shared['terrain_origin_m']:.4f}",
        "--terrain-relief-factor", f"{shared['terrain_relief_factor']:.6f}",
        "--source-padding-m", f"{shared['source_padding_m']:g}",
        "--prime-tower", shared["prime_tower"],
        "--job-id", f"{shared['plan_id']}_{chunk['label']}",
        "--output", f"{paths['model']}",
    ]
    # Not a cross-plate contract: the substrate is hidden, so plates may be
    # founded on different filament and still fit together. Pin it only when
    # it is not the default, so an ordinary plan's commands stay unchanged.
    foundation = shared.get("foundation_color", 0)
    if foundation:
        command += ["--foundation-color", MATERIAL_NAMES[foundation]]
    if shared["offline"]:
        command.append("--offline")
    if shared["no_preview"]:
        command.append("--no-preview")
    return command


def write_plan(directory: Path, plan: dict, chunks: list[dict], shared: dict) -> dict:
    """Write the chunk inputs, the command script, and the plan manifest."""
    chunk_dir = directory / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    commands: list[list[str]] = []
    for chunk in chunks:
        stem = f"{shared['plan_id']}_{chunk['label']}"
        polygon_path = chunk_dir / f"{stem}.geojson"
        frame_path = chunk_dir / f"{stem}.frame.json"
        model_path = Path(shared["model_dir"]) / f"{stem}.3mf"
        # shapely.to_geojson round-trips full float precision, so both sides of
        # a seam reproject to identical EPSG:2263 feet in their own jobs.
        polygon_path.write_text(shapely.to_geojson(chunk["polygon_wgs84"]) + "\n")
        frame_path.write_text(json.dumps(chunk["print_frame"], indent=2) + "\n")
        paths = {
            "polygon": polygon_path.relative_to(ROOT) if polygon_path.is_relative_to(ROOT)
            else polygon_path,
            "frame": frame_path.relative_to(ROOT) if frame_path.is_relative_to(ROOT)
            else frame_path,
            "model": model_path.relative_to(ROOT) if model_path.is_relative_to(ROOT)
            else model_path,
        }
        chunk["command"] = command_for(chunk, shared, paths)
        chunk["files"] = {key: str(value) for key, value in paths.items()}
        commands.append(chunk["command"])

    script = directory / "commands.sh"
    lines = [
        "#!/bin/sh",
        "# Generated by scripts/plan_map_chunks.py. Run from the repository root.",
        f"# Plan {shared['plan_id']}: {len(chunks)} plates at 1:{shared['scale']:.0f}.",
        "# Every plate shares one print-frame rotation, terrain datum and relief",
        "# factor; changing any of them for a single plate will break the seams.",
        "set -eu",
        "",
    ]
    for chunk, command in zip(chunks, commands):
        width, height = chunk["size_mm"]
        lines.append(f"# {chunk['label']}: {width:g} x {height:g} mm, "
                     f"{chunk['area_km2']:.2f} km2")
        lines.append(" ".join(shlex.quote(part) for part in command))
        lines.append("")
    script.write_text("\n".join(lines))
    script.chmod(0o755)

    collection = gpd.GeoDataFrame(
        [{
            "label": chunk["label"],
            "width_mm": chunk["size_mm"][0], "height_mm": chunk["size_mm"][1],
            "area_km2": chunk["area_km2"], "fill": chunk["fill"],
            "vertices": chunk["vertices"],
        } for chunk in chunks],
        geometry=[chunk["polygon_wgs84"] for chunk in chunks], crs=4326,
    )
    collection.to_file(directory / "chunks.geojson", driver="GeoJSON")

    manifest = {
        **plan,
        "chunks": [
            {key: value for key, value in chunk.items()
             if key not in {"polygon_ft", "polygon_wgs84"}}
            | {"polygon_wgs84": json.loads(shapely.to_geojson(chunk["polygon_wgs84"]))}
            for chunk in chunks
        ],
    }
    (directory / "plan.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    return manifest


def measure_seams(surface, seams: list[dict], labels: list[str]) -> list[dict]:
    """Score the boundaries the plates are actually cut along.

    Quality must come from the finished seams, not from the cuts the search
    made: compaction merges neighboring pieces afterwards, so part of an
    early cut can end up inside a chunk and never be cut at all. Measuring the
    cuts instead reports crossings that do not exist in the plan.
    """
    for record in seams:
        left, right = record["chunks"]
        record["between"] = [labels[left], labels[right]]
        seam = record["geometry"]
        record["blocked_samples"] = surface.blocked(seam)
        record.update(surface.describe(seam))
    return seams


def summarise(chunks: list[dict], seams: list[dict], scale: float) -> dict:
    """Aggregate seam quality, weighted by the length that is actually printed."""
    total = sum(seam.get("length_ft", 0.0) for seam in seams)
    crossings = [
        {**crossing, "between": seam.get("between", seam.get("chunks")),
         "length_mm": crossing["length_ft"] * FT * 1000 / scale}
        for seam in seams
        for crossing in seam.get("crosses", [])
    ]
    crossings.sort(key=lambda item: -item["length_ft"])

    def weighted(field: str) -> float | None:
        if total <= 0:
            return None
        return sum(seam.get(field, 0.0) * seam.get("length_ft", 0.0) for seam in seams) / total

    return {
        "seams": len(seams),
        "seam_length_ft": total,
        "seam_length_km": total * FT / 1000,
        "building_fraction": weighted("building_fraction"),
        "transport_structure_fraction": weighted("structure_fraction"),
        "paved_surface_fraction": weighted("cheap_surface_fraction"),
        "mean_above_ground_m": weighted("mean_above_ground_m"),
        "nodata_fraction": weighted("nodata_fraction"),
        "blocked_samples": sum(seam.get("blocked_samples", 0) for seam in seams),
        "keep_out_crossings": crossings,
        "keep_out_length_ft": sum(item["length_ft"] for item in crossings),
        "longest_crossing_mm": max((item["length_mm"] for item in crossings), default=0.0),
        "lengthwise_crossings": sum(1 for item in crossings if item.get("along_feature")),
        "chunk_area_km2": {
            "minimum": min((chunk["area_km2"] for chunk in chunks), default=0.0),
            "maximum": max((chunk["area_km2"] for chunk in chunks), default=0.0),
        },
    }


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Plan a large NYC polygon as gap-free neighboring print plates and emit "
            "the exact generate_3mf.py commands that build them."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    result.add_argument(
        "--bounding-polygon", required=True, type=parse_bounding_polygon,
        metavar="WKT|GEOJSON|PATH",
        help="Target WGS84 Polygon, supplied as WKT, GeoJSON, a file path, or @file",
    )
    result.add_argument(
        "--max-chunk-size-mm", type=parse_size, default=(235.0, 235.0), metavar="WIDTHxHEIGHT",
        help="Largest printed plate a chunk may need, inside the 256 mm bed",
    )
    result.add_argument(
        "--max-chunks", type=int, default=32,
        help="Fail rather than emit more plates than this",
    )
    result.add_argument("--scale", type=float, default=6286.5, help="Map scale denominator")
    result.add_argument(
        "--fit-scale", action="store_true",
        help="Search for the most detailed scale whose plan fits --max-chunks",
    )
    result.add_argument(
        "--orientation", default="auto", metavar="auto|north|DEGREES",
        help="Shared print-frame rotation; auto reads the cached OSM street grid",
    )
    result.add_argument(
        "--cut-deviation-mm", type=float, default=35.0,
        help="How far a seam may wander from its straight position to follow a street",
    )
    result.add_argument(
        "--cut-style", choices=["angled", "staircase", "axis"], default="angled",
        help=("angled: the street-following path with its right angles collapsed into "
              "the fewest straight segments that stay clear of every keep-out; "
              "staircase: street-following runs joined by right-angle jogs; "
              "axis: straight and parallel to the frame axis"),
    )
    result.add_argument(
        "--cut-snap-mm", type=float, default=1.0,
        help="Printed lattice that seam vertices are placed on",
    )
    result.add_argument(
        "--min-edge-mm", type=float, default=35.0,
        help="Shortest printed run a seam may hold before it may jog again",
    )
    result.add_argument(
        "--min-jog-mm", type=float, default=4.0,
        help="Smallest printed sideways step a seam may make",
    )
    result.add_argument(
        "--min-chunk-fill", type=float, default=0.10,
        help="Merge a chunk filling less than this fraction of its own plate",
    )
    result.add_argument(
        "--cost-resolution-m", type=float, default=4.0,
        help="Ground sampling of the cut-cost surface",
    )
    result.add_argument(
        "--cut-straightness", type=float, default=3.0,
        help="Price of one seam jog, in units of a minimum run over open pavement",
    )
    result.add_argument(
        "--cut-centering", type=float, default=0.10,
        help="Preference for keeping a seam near its straight position",
    )
    result.add_argument(
        "--keep-out-height-m", type=float, default=0.0,
        help=("Minimum building height a seam is forbidden to cut; 0 forbids every "
              "building, raise it only when a target is too dense to route around"),
    )
    result.add_argument(
        "--keep-out-buffer-m", type=float, default=2.0,
        help="Clearance held around every keep-out structure",
    )
    result.add_argument(
        "--max-keep-out-crossing-mm", type=float, default=3.0,
        help=("Longest printed run a seam may travel lengthwise along a building, "
              "bridge, or tunnel; square crossings are judged against the "
              "feature's own width instead"),
    )
    result.add_argument(
        "--allow-keep-out-crossings", action="store_true",
        help="Report keep-out crossings instead of failing on them",
    )
    result.add_argument(
        "--layer-height", type=float, default=0.16,
        help=("Process layer height shared by every plate, checked against the installed "
              "printer profile when a plate is generated. The default resolves the map's "
              "0.16 mm pavement pad in exactly one layer, so a kerb slices identically "
              "along its whole length"),
    )
    result.add_argument(
        "--foundation-color", type=parse_material_color, metavar="COLOR",
        default="ivory",
        help=("Filament for the hidden substrate on every plate: ivory (default), green, "
              "blue, or tan. Plates may differ without affecting how they fit together"),
    )
    result.add_argument("--grid-step-mm", type=float, default=0.125,
                        help="Manufacturing raster spacing shared by every plate")
    result.add_argument("--vertical-exaggeration", type=float, default=1.0,
                        help="Z multiplier shared by every plate")
    result.add_argument(
        "--terrain-origin-m", type=float,
        help="Override the shared NAVD88 datum; must not exceed any chunk's own minimum",
    )
    result.add_argument(
        "--terrain-origin-margin-m", type=float, default=0.5,
        help="Headroom below the measured minimum ground elevation",
    )
    result.add_argument("--terrain-relief-factor", type=float,
                        help="Override the shared ground-relief multiplier")
    result.add_argument("--source-padding-m", type=float, default=20.0,
                        help="Extra source context each generated chunk reads")
    result.add_argument("--prime-tower", choices=["auto", "on", "off"], default="auto",
                        help="Prime-tower mode passed to every chunk")
    result.add_argument("--plan-id", help="Plan directory name; derived from the request when omitted")
    result.add_argument("--data-dir", type=Path, default=ROOT / "data", help="Shared input-data root")
    result.add_argument("--cache-dir", type=Path, help="Dataset cache root (defaults to <data-dir>/cache)")
    result.add_argument(
        "--lidar-dataset", choices=LIDAR_DATASETS, default=LIDAR_DATASETS[0],
        help="Cached LiDAR collection the cut-cost surface measures height from. "
             "Both publish the same contract; the plan records which one it used.",
    )
    result.add_argument("--output-dir", type=Path, default=ROOT / "output",
                        help="Root for generated plans and models")
    result.add_argument("--no-land-cover", action="store_true",
                        help="Skip the land-cover raster when scoring open ground")
    result.add_argument("--preview", action=argparse.BooleanOptionalAction, default=True,
                        help="Render the chunk layout over a basemap")
    result.add_argument("--diagnostics", action="store_true",
                        help="Also render the cut-cost surface and keep-outs")
    result.add_argument(
        "--preview-format", choices=["svg", "png", "both"], default="svg",
        help="SVG keeps seams, labels and markers vector-sharp at any zoom",
    )
    result.add_argument(
        "--preview-pixels", type=int, default=6000,
        help="Long side of the rendered preview; never below one pixel per cost cell",
    )
    result.add_argument("--offline", action="store_true",
                        help="Add --offline to every emitted generation command")
    result.add_argument("--generate-no-preview", action="store_true",
                        help="Add --no-preview to every emitted generation command")
    result.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING"], default="INFO")
    return result


def resolve(args) -> dict:
    """Validate the request and derive everything that does not need data."""
    if args.max_chunks < 1:
        raise PlanError("--max-chunks must be at least 1")
    for name, value in (("--cut-snap-mm", args.cut_snap_mm),
                        ("--min-edge-mm", args.min_edge_mm),
                        ("--min-jog-mm", args.min_jog_mm)):
        if value <= 0:
            raise PlanError(f"{name} must be positive")
    if args.cut_deviation_mm < 0:
        raise PlanError("--cut-deviation-mm cannot be negative")
    if not 0 <= args.min_chunk_fill < 1:
        raise PlanError("--min-chunk-fill must be at least 0 and below 1")
    if args.cost_resolution_m <= 0:
        raise PlanError("--cost-resolution-m must be positive")
    if not 500 <= args.preview_pixels <= 20000:
        raise PlanError("--preview-pixels must be between 500 and 20000")
    envelope = tuple(float(value) for value in args.max_chunk_size_mm)
    for value in envelope:
        if not geometry.MIN_PRINT_MM <= value <= geometry.MAX_PRINT_MM:
            raise PlanError(
                f"--max-chunk-size-mm sides must be "
                f"{geometry.MIN_PRINT_MM:g}-{geometry.MAX_PRINT_MM:g} mm, got {value:g}"
            )
        if abs(value / args.grid_step_mm - round(value / args.grid_step_mm)) > 1e-6:
            raise PlanError(
                f"--max-chunk-size-mm side {value:g} is not a multiple of "
                f"--grid-step-mm {args.grid_step_mm:g}"
            )
    if not shapely.box(*NYC_BOUNDS).covers(args.bounding_polygon):
        raise PlanError("The target polygon extends outside the supported NYC bounding box")
    ceiling = geometry.max_scale_for_envelope(envelope, args.source_padding_m)
    if args.scale > ceiling:
        raise PlanError(
            f"A {envelope[0]:g}x{envelope[1]:g} mm plate at 1:{args.scale:.0f} with "
            f"{args.source_padding_m:g} m padding exceeds the "
            f"{geometry.MAX_ELEVATION_CELLS:,}-cell elevation limit. "
            f"Use --scale {ceiling:.0f} or finer, or a smaller --max-chunk-size-mm."
        )
    cache_dir = (args.cache_dir or args.data_dir / "cache").resolve()
    return {
        "envelope_mm": envelope,
        "cache_dir": cache_dir,
        "scale_ceiling": ceiling,
    }


def search_scale(target, target_ft, bearing, surface, args, resolved, log):
    """Return the scale actually used, its frame, and the finished partition.

    Without ``--fit-scale`` the requested scale is honoured and a plan over
    budget is an error, so the printed size is never silently changed.  With
    it, the scale is stepped back only until the real cost-guided plan fits.
    """
    scale = args.scale
    attempts: list[tuple[float, int]] = []
    for _ in range(30):
        options = Options(args, resolved, scale)
        frame = build_frame(target, bearing, scale, args.grid_step_mm)
        partition = plan_partition(target_ft, frame, surface, options)
        count = len(partition["polygons"])
        if count <= args.max_chunks:
            if scale != args.scale:
                log.info("scale_fitted", scale=round(scale, 3),
                         requested=args.scale, chunks=count)
            return scale, frame, partition
        attempts.append((scale, count))
        if not args.fit_scale:
            raise PlanError(
                f"The plan needs {count} plates at 1:{scale:.0f}, over the "
                f"--max-chunks budget of {args.max_chunks}. Re-run with --fit-scale, "
                "a coarser --scale, or a smaller --cut-deviation-mm."
            )
        scale = min(scale * 1.06, resolved["scale_ceiling"])
        if scale >= resolved["scale_ceiling"] - 1e-9 and attempts[-1][0] >= scale:
            break
    best = min(attempts, key=lambda item: item[1]) if attempts else (scale, 0)
    raise PlanError(
        f"No scale up to the 1:{resolved['scale_ceiling']:.0f} elevation-grid ceiling "
        f"fits {args.max_chunks} plates; the best tried was {best[1]} plates at "
        f"1:{best[0]:.0f}. Raise --max-chunks, raise --max-chunk-size-mm, or lower "
        "--cut-deviation-mm."
    )


class Options:
    """Resolved planning knobs, in the units the geometry core expects."""

    def __init__(self, args, resolved: dict, scale: float):
        self.envelope_mm = resolved["envelope_mm"]
        self.max_chunks = args.max_chunks
        self.scale = scale
        self.grid_step_mm = args.grid_step_mm
        self.cut_deviation_mm = args.cut_deviation_mm
        self.cut_snap_mm = args.cut_snap_mm
        self.min_edge_mm = args.min_edge_mm
        self.min_jog_mm = args.min_jog_mm
        self.min_chunk_fill = args.min_chunk_fill
        self.source_padding_m = args.source_padding_m
        self.sample_step_ft = args.cost_resolution_m / FT


def main() -> None:
    args = parser().parse_args()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(__import__("logging"), args.log_level)
        )
    )
    log = structlog.get_logger("plan_map_chunks")
    try:
        run(args, log)
    except (PlanError, PlanGeometryError, SourceDataError) as error:
        log.error("planning_failed", error=str(error))
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


def run(args, log) -> dict:
    resolved = resolve(args)
    target_wgs = args.bounding_polygon
    target = gpd.GeoSeries([target_wgs], crs=4326).to_crs(CRS).iloc[0]
    orientation = choose_bearing(resolved["cache_dir"], target, args.orientation)
    log.info("orientation_selected", **{
        key: (round(value, 4) if isinstance(value, float) else value)
        for key, value in orientation.items() if value is not None
    })

    scale = args.scale
    options = Options(args, resolved, scale)
    bearing = orientation["bearing_rad"]
    frame = build_frame(target, bearing, scale, args.grid_step_mm)
    target_ft = frame.to_frame(target)

    estimate = estimate_chunks(target_ft, frame, options)
    if estimate > args.max_chunks and not args.fit_scale:
        suggestion = smallest_feasible_scale(target, bearing, options)
        advice = (f"Re-run with --scale {math.ceil(suggestion):d} or coarser, "
                  f"or with --fit-scale." if suggestion
                  else "No supported scale fits that budget; raise --max-chunks.")
        raise PlanError(
            f"A straight-cut plan at 1:{scale:.0f} already needs {estimate} plates, "
            f"over the --max-chunks budget of {args.max_chunks}. {advice}"
        )

    weights = CostWeights(
        keep_out_height_m=args.keep_out_height_m,
        keep_out_buffer_m=args.keep_out_buffer_m,
        straightness=args.cut_straightness,
        centering=args.cut_centering,
    )
    # The cut-cost raster depends on the frame's origin, axes and sampling but
    # not on scale, so one build serves every scale the search below tries.
    surface = build_cost_surface(
        frame, target_ft,
        cache_dir=resolved["cache_dir"],
        lidar_dataset=args.lidar_dataset,
        resolution_m=args.cost_resolution_m,
        weights=weights,
        use_land_cover=not args.no_land_cover,
        log=log,
    )
    surface.style = args.cut_style

    scale, frame, partition = search_scale(
        target, target_ft, bearing, surface, args, resolved, log
    )
    options = Options(args, resolved, scale)

    terrain = surface.terrain_statistics(
        target_ft, origin_margin_m=args.terrain_origin_margin_m
    )
    relief = choose_terrain_relief(
        np.asarray([terrain["relief_p5_m"], terrain["relief_p95_m"]]),
        scale_denominator=scale,
        vertical_exaggeration=args.vertical_exaggeration,
        layer_height_mm=args.layer_height,
        requested_factor=args.terrain_relief_factor,
    )
    terrain_origin = (
        args.terrain_origin_m if args.terrain_origin_m is not None
        else terrain["terrain_origin_m"]
    )
    if args.terrain_origin_m is not None and args.terrain_origin_m > terrain["observed_minimum_m"]:
        raise PlanError(
            f"--terrain-origin-m {args.terrain_origin_m:g} is above the measured minimum "
            f"ground elevation {terrain['observed_minimum_m']:.2f} m in this area; a chunk "
            "containing that low point would fail its base-height check."
        )
    log.info("shared_terrain", terrain_origin_m=round(terrain_origin, 3),
             relief_factor=round(relief.factor, 4), relief_span_m=round(terrain["relief_span_m"], 2))

    polygons = partition["polygons"]
    coverage = geometry.validate_partition(
        target_ft, polygons, tolerance_ft2=max(1.0, target_ft.area * 1e-9)
    )
    plates = geometry.validate_plates(
        frame, polygons, envelope_mm=resolved["envelope_mm"], padding_m=args.source_padding_m
    )
    tolerance = max(1.0, target_ft.area * 1e-9)
    labels = geometry.grid_labels(frame, polygons, partition["limits_ft"])
    seams = measure_seams(
        surface,
        geometry.shared_edges(polygons, area_tolerance_ft2=tolerance),
        labels,
    )

    quality = summarise([], seams, scale)
    # A seam crossing an elevated road at right angles is unavoidable and costs
    # a few printed millimetres. A seam running *along* one is a real defect,
    # and shows up as a long single crossing.
    overlong = [
        item for item in quality["keep_out_crossings"]
        if item.get("along_feature") and item["length_mm"] > args.max_keep_out_crossing_mm
    ]
    if overlong and not args.allow_keep_out_crossings:
        listed = "; ".join(
            f"{item['name']} ({item['kind']}, {item['length_mm']:.1f} mm "
            f"on the {'/'.join(item['between'])} seam)"
            for item in overlong[:6]
        )
        raise PlanError(
            f"{len(overlong)} seam sections run lengthwise along a structure that must "
            f"not be cut, further than {args.max_keep_out_crossing_mm:g} mm: {listed}. "
            "Raise --cut-deviation-mm, change --orientation, raise "
            "--max-keep-out-crossing-mm, or accept them with --allow-keep-out-crossings."
        )

    shared = {
        "plan_id": args.plan_id or default_plan_id(target_wgs, scale, resolved["envelope_mm"]),
        "python": sys.executable,
        "scale": scale,
        "grid_step_mm": args.grid_step_mm,
        "layer_height": args.layer_height,
        "foundation_color": args.foundation_color,
        "vertical_exaggeration": args.vertical_exaggeration,
        "terrain_origin_m": terrain_origin,
        "terrain_relief_factor": relief.factor,
        "source_padding_m": args.source_padding_m,
        "prime_tower": args.prime_tower,
        "offline": args.offline,
        "no_preview": args.generate_no_preview,
        "model_dir": str(args.output_dir / "models"),
    }
    shared["plan_id"] = re.sub(r"[^A-Za-z0-9_.-]+", "_", shared["plan_id"])
    chunks = chunk_records(frame, polygons, labels, shared)
    for chunk, plate in zip(chunks, plates):
        chunk["elevation_cells"] = plate["elevation_cells"]
    quality = summarise(chunks, seams, scale)

    directory = (args.output_dir / "plans" / shared["plan_id"]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    plan = {
        "planner_version": PLANNER_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(shlex.quote(part) for part in sys.argv),
        "target_wgs84": json.loads(shapely.to_geojson(target_wgs)),
        "target_area_km2": float(target.area * FT * FT / 1e6),
        "orientation": orientation,
        "frame": {
            "origin_ft": list(frame.origin_ft), "x_axis": list(frame.x_axis),
            "y_axis": list(frame.y_axis), "bearing_deg": frame.bearing_deg,
            "manufacturing_cell_ft": frame.cell_ft,
        },
        "shared_generation": {key: value for key, value in shared.items() if key != "python"},
        "envelope_mm": list(resolved["envelope_mm"]),
        "assembled_size_mm": list(assembled_size_mm(frame, target_ft)),
        "cut_settings": {
            "deviation_mm": options.cut_deviation_mm, "snap_mm": options.cut_snap_mm,
            "min_edge_mm": options.min_edge_mm, "min_jog_mm": options.min_jog_mm,
            "style": args.cut_style,
            "straightness": args.cut_straightness, "centering": args.cut_centering,
            "cost_resolution_m": args.cost_resolution_m,
        },
        "terrain": {**terrain, "relief_factor": relief.factor, "relief_mode": relief.mode},
        "sources": surface.sources,
        "coverage": coverage,
        "seams": [
            {key: value for key, value in seam.items() if key != "geometry"}
            for seam in seams
        ],
        "cuts": partition["cuts"],
        "quality": quality,
        "merge_notes": partition["merge_notes"],
        "warnings": partition["soft_warnings"],
    }
    manifest = write_plan(directory, plan, chunks, shared)

    if args.preview:
        from _chunk_preview import render_diagnostics, render_plan
        title = (f"{shared['plan_id']} - {len(chunks)} plates at 1:{scale:.0f}, "
                 f"frame bearing {frame.bearing_deg:.1f} deg")
        crossings = quality["keep_out_crossings"]
        suffixes = ["png", "svg"] if args.preview_format == "both" else [args.preview_format]
        written = render_plan(
            surface, frame, target_ft, chunks,
            [directory / f"preview.{suffix}" for suffix in suffixes],
            title=title, crossings=crossings, pixels=args.preview_pixels,
        )
        if args.diagnostics:
            written += render_diagnostics(
                surface, frame, target_ft, chunks,
                [directory / f"preview_cuts.{suffix}" for suffix in suffixes],
                title=f"{title} - cut cost and keep-outs",
                crossings=crossings, pixels=args.preview_pixels,
            )
        log.info("previews_written", files=[path.name for path in written])

    report(directory, chunks, quality, shared, scale,
           assembled_size_mm(frame, target_ft))
    log.info("plan_written", directory=str(directory), chunks=len(chunks))
    return manifest


def default_plan_id(target_wgs: Polygon, scale: float, envelope: tuple[float, float]) -> str:
    digest = hashlib.sha256(
        f"{shapely.to_wkb(target_wgs).hex()}|{scale}|{envelope}".encode()
    ).hexdigest()[:10]
    return f"nyc_plan_{scale:.0f}_{digest}"


def assembled_size_mm(frame: Frame, target_ft: Polygon) -> tuple[float, float]:
    """Printed size of the whole assembled map, across then along the frame."""
    minx, miny, maxx, maxy = target_ft.bounds
    return frame.mm(maxx - minx), frame.mm(maxy - miny)


def report(directory: Path, chunks, quality: dict, shared: dict, scale: float,
           assembled: tuple[float, float]) -> None:
    """Human summary on stdout; plan.json holds the full record."""
    print(f"Plan {shared['plan_id']}: {len(chunks)} plates at 1:{scale:.0f}")
    print(f"  assembled      {assembled[0]:.0f} x {assembled[1]:.0f} mm "
          f"({assembled[0] / 1000:.2f} x {assembled[1] / 1000:.2f} m)")
    print(f"  seams          {quality['seam_length_km']:.1f} km over "
          f"{quality['seams']} plate joints")
    for label, key in (("on paved surface", "paved_surface_fraction"),
                       ("over buildings", "building_fraction"),
                       ("over structures", "transport_structure_fraction")):
        value = quality[key]
        if value is not None:
            print(f"  {label:<14} {value:6.2%}")
    if quality["mean_above_ground_m"] is not None:
        print(f"  mean height    {quality['mean_above_ground_m']:.1f} m above ground")
    if quality["keep_out_crossings"]:
        crossings = quality["keep_out_crossings"]
        print(f"  keep-outs      {len(crossings)} crossings, "
              f"{quality['longest_crossing_mm']:.1f} mm longest:")
        for item in crossings[:8]:
            print(f"    - {item['name']} ({item['kind']}, {item['length_mm']:.1f} mm "
                  f"on the {'/'.join(item['between'])} seam)")
    print(f"  shared datum   {shared['terrain_origin_m']:.2f} m NAVD88, "
          f"relief factor {shared['terrain_relief_factor']:.3f}")
    print(f"  wrote          {directory}")
    print(f"  run            {directory / 'commands.sh'}")


if __name__ == "__main__":
    main()
