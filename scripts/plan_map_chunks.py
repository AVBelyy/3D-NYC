#!/usr/bin/env python3
"""Plan seam-aware, gap-free NYC 3MF chunks and emit reproducible commands.

The planner partitions one WGS84 polygon in EPSG:2263 and keeps every piece on
one shared print-space grid.  Rectangles are only maximum printer envelopes:
the default semantic-path router bends the actual shared chunk boundaries
along suitable streets, trails, water and open space while avoiding buildings
and structures.  It does not generate or slice any 3MF itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import shlex
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import shapely
import structlog
from rasterio.features import geometry_mask, geometry_window
from rasterio.windows import Window
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Polygon, box, shape

from download_data import ROOT
from terrain_relief import choose_terrain_relief
from validate_chunk_plan import validate_plan, write_report


FT = 0.3048006096012192
NYC_BOUNDS = (-74.27, 40.47, -73.68, 40.93)
MIN_FRAME_MM = 20.0
MAX_FRAME_MM = 250.0
MAX_ELEVATION_CELLS = 30_000_000
ELEVATION_CELL_M = 0.5
PLAN_VERSION = 3


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class EventRecorder:
    """Retain the same structured events emitted to stdout for the plan log."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, _logger, _method_name, event_dict):
        self.events.append(json.loads(json.dumps(event_dict, default=str)))
        return event_dict

    def write(self, path: Path) -> None:
        atomic_write(path, "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in self.events))


def configure_logging(level: str) -> tuple[object, EventRecorder]:
    """Configure JSONL stdout logging in the same style as generate_3mf.py."""
    recorder = EventRecorder()
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper()))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            recorder,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    return structlog.get_logger("plan_map_chunks"), recorder


class LoggedStage:
    def __init__(self, log, name: str, **context) -> None:
        self.log, self.name, self.context = log, name, context

    def __enter__(self):
        self.started = time.monotonic()
        self.log.info("stage_started", stage=self.name, **self.context)
        return self

    def __exit__(self, exc_type, exc, _tb):
        elapsed = time.monotonic() - self.started
        if exc is None:
            self.log.info("stage_completed", stage=self.name, elapsed_seconds=elapsed, **self.context)
            return False
        self.log.error(
            "stage_failed", stage=self.name, elapsed_seconds=elapsed,
            error_type=exc_type.__name__, error=str(exc), traceback=traceback.format_exc(), **self.context,
        )
        return False


def require_complete_manifest(component: Path, label: str) -> dict:
    """Fail early when a supposedly reusable citywide cache is incomplete."""
    manifest_path = component / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(
            f"{label} cache is missing {manifest_path}. Build the cache before planning reproducible offline jobs."
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"{label} cache manifest is unreadable: {manifest_path}: {error}") from error
    if manifest.get("status") != "complete" or manifest.get("production_ready") is False:
        raise SystemExit(
            f"{label} cache is not complete/production-ready: {manifest_path} "
            f"(status={manifest.get('status')!r}, production_ready={manifest.get('production_ready')!r})"
        )
    missing = []
    for record in manifest.get("outputs", []):
        output = component / str(record.get("path", ""))
        if not output.is_file() or (
            record.get("bytes") is not None and output.stat().st_size != int(record["bytes"])
        ):
            missing.append(str(output))
            if len(missing) == 5:
                break
    if missing:
        raise SystemExit(
            f"{label} cache manifest is complete but listed output files are missing or changed: "
            + ", ".join(missing)
        )
    return manifest


def parse_size(text: str) -> tuple[float, float]:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[xX,]\s*(\d+(?:\.\d+)?)\s*", text)
    if not match:
        raise argparse.ArgumentTypeError("Size must look like 235x235")
    return float(match.group(1)), float(match.group(2))


def parse_polygon(text: str):
    """Read a WGS84 Polygon/MultiPolygon from WKT or GeoJSON.

    A FeatureCollection is dissolved deliberately, which makes an exported
    borough boundary usable as a single planning request.  Generated chunks
    are still individual Polygon files accepted by generate_3mf.py.
    """
    source = text.strip()
    candidate = None
    if source.startswith("@"):
        candidate = Path(source[1:]).expanduser()
    elif len(source) < 1000 and not source.upper().startswith(("POLYGON", "MULTIPOLYGON")) and not source.startswith("{"):
        candidate = Path(source).expanduser()
    if candidate is not None:
        if not candidate.is_file():
            raise argparse.ArgumentTypeError(f"Bounding-polygon file does not exist: {candidate}")
        source = candidate.read_text().strip()
    try:
        if source.startswith("{"):
            payload = json.loads(source)
            if payload.get("type") == "FeatureCollection":
                geometries = [shape(feature["geometry"]) for feature in payload.get("features", [])]
                geometry = shapely.union_all(geometries) if geometries else Polygon()
            else:
                if payload.get("type") == "Feature":
                    payload = payload.get("geometry") or {}
                geometry = shape(payload)
        else:
            geometry = shapely.from_wkt(source)
    except (json.JSONDecodeError, TypeError, ValueError, KeyError, shapely.errors.GEOSException) as error:
        raise argparse.ArgumentTypeError(f"Invalid bounding polygon: {error}") from error
    geometry = shapely.make_valid(geometry)
    polygonal = [part for part in shapely.get_parts(geometry) if part.geom_type == "Polygon"]
    geometry = shapely.normalize(shapely.union_all(polygonal)) if polygonal else Polygon()
    if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise argparse.ArgumentTypeError("Bounding polygon must contain polygonal area")
    if not box(*NYC_BOUNDS).covers(geometry):
        raise argparse.ArgumentTypeError("Bounding polygon extends outside the supported NYC bounds")
    return geometry


def axes_for_orientation(degrees_east_of_north: float) -> tuple[np.ndarray, np.ndarray]:
    angle = math.radians(float(degrees_east_of_north))
    y_axis = np.asarray([math.sin(angle), math.cos(angle)], dtype=float)
    x_axis = np.asarray([y_axis[1], -y_axis[0]], dtype=float)
    return x_axis, y_axis


def normalize_orientation(degrees: float) -> float:
    result = (float(degrees) + 90.0) % 180.0 - 90.0
    return 0.0 if abs(result) < 1e-10 else result


def grid_angle_distance(left: float, right: float) -> float:
    """Smallest angular difference for axes that are equivalent modulo 90°."""
    return abs((float(left) - float(right) + 45.0) % 90.0 - 45.0)


def local_bounds(geometry, x_axis: np.ndarray, y_axis: np.ndarray) -> tuple[float, float, float, float]:
    coordinates = shapely.get_coordinates(geometry)
    return (
        float((coordinates @ x_axis).min()),
        float((coordinates @ y_axis).min()),
        float((coordinates @ x_axis).max()),
        float((coordinates @ y_axis).max()),
    )


def oriented_envelope_orientation(geometry) -> float:
    rectangle = geometry.minimum_rotated_rectangle
    corners = shapely.get_coordinates(rectangle.exterior)[:-1]
    edges = [corners[(index + 1) % 4] - corners[index] for index in range(4)]
    lengths = np.asarray([np.linalg.norm(edge) for edge in edges])
    y_axis = edges[int(np.argmax(lengths))] / float(lengths.max())
    if y_axis[1] < 0 or (abs(y_axis[1]) < 1e-12 and y_axis[0] < 0):
        y_axis = -y_axis
    return normalize_orientation(math.degrees(math.atan2(y_axis[0], y_axis[1])))


def read_vector(path: Path, bounds, clip=None) -> gpd.GeoDataFrame:
    if not path.is_file():
        return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=2263), crs=2263)
    try:
        frame = gpd.read_parquet(path, bbox=tuple(bounds))
    except ValueError:
        frame = gpd.read_parquet(path)
    if frame.crs is None:
        frame = frame.set_crs(2263)
    else:
        frame = frame.to_crs(2263)
    frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
    if clip is not None and len(frame):
        frame = frame[frame.intersects(clip)].copy()
    return frame.reset_index(drop=True)


def dominant_road_orientation(roads: gpd.GeoDataFrame) -> float | None:
    """Estimate the dominant street-grid axis from roadbed boundary bearings."""
    vectors: list[np.ndarray] = []
    weights: list[np.ndarray] = []
    for geometry in roads.geometry:
        for line in shapely.get_parts(geometry.boundary):
            coordinates = shapely.get_coordinates(line)
            if len(coordinates) < 2:
                continue
            delta = np.diff(coordinates[:, :2], axis=0)
            length = np.linalg.norm(delta, axis=1)
            use = (length >= 8.0) & (length <= 1000.0)
            if use.any():
                vectors.append(delta[use])
                weights.append(length[use])
    if not vectors:
        return None
    delta = np.vstack(vectors)
    length = np.concatenate(weights)
    angle_from_east = np.arctan2(delta[:, 1], delta[:, 0])
    moment = np.sum(length * np.exp(4j * angle_from_east))
    if abs(moment) <= 1e-9:
        return None
    grid_axis_from_east = math.atan2(moment.imag, moment.real) / 4.0
    candidates = [
        np.asarray([math.cos(grid_axis_from_east), math.sin(grid_axis_from_east)]),
        np.asarray([-math.sin(grid_axis_from_east), math.cos(grid_axis_from_east)]),
    ]
    y_axis = max(candidates, key=lambda axis: abs(axis[1]))
    if y_axis[1] < 0:
        y_axis = -y_axis
    return normalize_orientation(math.degrees(math.atan2(y_axis[0], y_axis[1])))


def maximum_safe_scale(width_mm: float, height_mm: float, source_padding_m: float) -> float:
    """Largest denominator safe after the generator's per-axis ceil snapping."""
    def cells(scale: float) -> int:
        width_m = width_mm / 1000.0 * scale + 2.0 * source_padding_m
        height_m = height_mm / 1000.0 * scale + 2.0 * source_padding_m
        return math.ceil(width_m / ELEVATION_CELL_M) * math.ceil(height_m / ELEVATION_CELL_M)

    if cells(1000.0) > MAX_ELEVATION_CELLS:
        return 0.0
    if cells(50_000.0) <= MAX_ELEVATION_CELLS:
        return 50_000.0
    low, high = 1000.0, 50_000.0
    for _ in range(64):
        middle = (low + high) / 2.0
        if cells(middle) <= MAX_ELEVATION_CELLS:
            low = middle
        else:
            high = middle
    # Nudge inside the discontinuous ceil boundary so decimal serialization of
    # the chosen scale cannot push a command one source cell over the cap.
    return math.nextafter(low, 0.0)


@dataclass(frozen=True)
class LayoutCandidate:
    orientation_deg: float
    x_axis: np.ndarray
    y_axis: np.ndarray
    local_bounds_ft: tuple[float, float, float, float]
    columns: int
    rows: int
    scale: float
    minimum_scale: float
    cells: int
    road_alignment_deg: float | None


def orientation_candidates(aoi, roads: gpd.GeoDataFrame, requested: float | None) -> tuple[list[float], float | None]:
    road = dominant_road_orientation(roads)
    if requested is not None:
        return [normalize_orientation(requested)], road
    envelope = oriented_envelope_orientation(aoi)
    raw = [0.0, 90.0, envelope, envelope + 90.0]
    if road is not None:
        raw.extend([road, road + 90.0, road - 1.0, road + 1.0])
    result: list[float] = []
    for value in raw:
        value = normalize_orientation(value)
        if not any(abs(normalize_orientation(value - existing)) < 1e-7 for existing in result):
            result.append(value)
    return result, road


def choose_layout(
    aoi,
    orientations: Iterable[float],
    *,
    road_orientation: float | None,
    maximum_chunks: int,
    chunk_size_mm: tuple[float, float],
    grid_step_mm: float,
    requested_scale: float | None,
    seam_flex_percent: float,
    source_padding_m: float,
) -> tuple[LayoutCandidate, list[dict]]:
    width_mm, height_mm = chunk_size_mm
    max_x_cells = int(math.floor((width_mm + 1e-9) / grid_step_mm))
    max_y_cells = int(math.floor((height_mm + 1e-9) / grid_step_mm))
    safe_scale = maximum_safe_scale(width_mm, height_mm, source_padding_m)
    choices: list[LayoutCandidate] = []
    diagnostics: list[dict] = []
    for orientation in orientations:
        x_axis, y_axis = axes_for_orientation(orientation)
        bounds = local_bounds(aoi, x_axis, y_axis)
        extent_x_ft = bounds[2] - bounds[0]
        extent_y_ft = bounds[3] - bounds[1]
        selected = None
        if requested_scale is not None:
            printed_x = extent_x_ft * FT * 1000.0 / requested_scale
            printed_y = extent_y_ft * FT * 1000.0 / requested_scale
            total_x = int(math.ceil((printed_x - 1e-9) / grid_step_mm))
            total_y = int(math.ceil((printed_y - 1e-9) / grid_step_mm))
            columns = max(1, math.ceil(total_x / max_x_cells))
            rows = max(1, math.ceil(total_y / max_y_cells))
            if columns * rows <= maximum_chunks and requested_scale <= safe_scale + 1e-9:
                selected = (columns, rows, requested_scale, requested_scale)
        else:
            candidates = []
            for columns in range(1, maximum_chunks + 1):
                for rows in range(1, maximum_chunks // columns + 1):
                    required = max(
                        extent_x_ft * FT * 1000.0 / (columns * width_mm),
                        extent_y_ft * FT * 1000.0 / (rows * height_mm),
                        1000.0,
                    )
                    scale = math.ceil(required * (1.0 + seam_flex_percent / 100.0) * 10.0) / 10.0
                    if scale > safe_scale + 1e-9:
                        continue
                    printed_x = extent_x_ft * FT * 1000.0 / scale
                    printed_y = extent_y_ft * FT * 1000.0 / scale
                    total_x = int(math.ceil((printed_x - 1e-9) / grid_step_mm))
                    total_y = int(math.ceil((printed_y - 1e-9) / grid_step_mm))
                    actual_columns = max(1, math.ceil(total_x / max_x_cells))
                    actual_rows = max(1, math.ceil(total_y / max_y_cells))
                    if actual_columns * actual_rows > maximum_chunks:
                        continue
                    if total_x < actual_columns * math.ceil(MIN_FRAME_MM / grid_step_mm):
                        continue
                    if total_y < actual_rows * math.ceil(MIN_FRAME_MM / grid_step_mm):
                        continue
                    candidates.append(
                        (scale, actual_columns * actual_rows, actual_columns, actual_rows, required)
                    )
            if candidates:
                scale, _, columns, rows, required = min(candidates)
                selected = (columns, rows, scale, required)
        diagnostics.append({
            "orientation_deg_east_of_north": orientation,
            "extent_ft": [extent_x_ft, extent_y_ft],
            "feasible": selected is not None,
            "safe_maximum_scale": safe_scale,
            "selection": None if selected is None else {
                "columns": selected[0], "rows": selected[1],
                "scale": selected[2], "minimum_scale": selected[3],
            },
        })
        if selected is None:
            continue
        columns, rows, scale, minimum_scale = selected
        choices.append(LayoutCandidate(
            orientation_deg=orientation,
            x_axis=x_axis,
            y_axis=y_axis,
            local_bounds_ft=bounds,
            columns=columns,
            rows=rows,
            scale=scale,
            minimum_scale=minimum_scale,
            cells=columns * rows,
            road_alignment_deg=(
                grid_angle_distance(orientation, road_orientation)
                if road_orientation is not None else None
            ),
        ))
    if not choices:
        scale_note = f" at scale 1:{requested_scale:g}" if requested_scale is not None else ""
        raise ValueError(
            f"No orientation can cover the polygon with at most {maximum_chunks} chunks{scale_note}. "
            f"The {width_mm:g}x{height_mm:g} mm frame also limits scale to about 1:{safe_scale:,.0f} "
            f"under the per-job {MAX_ELEVATION_CELLS:,}-cell elevation safety cap."
        )
    minimum = min(choice.scale for choice in choices)
    # A near-equal street-aligned layout usually buys much better physical seams
    # for negligible loss of horizontal detail.
    shortlist = [choice for choice in choices if choice.scale <= minimum * 1.03 + 1e-9]
    selected = min(shortlist, key=lambda choice: (
        choice.road_alignment_deg if choice.road_alignment_deg is not None else 90.0,
        choice.scale,
        choice.cells,
        abs(choice.orientation_deg),
    ))
    return selected, diagnostics


def frame_geometry(origin: np.ndarray, x_axis: np.ndarray, y_axis: np.ndarray,
                   x0: float, x1: float, y0: float, y1: float) -> Polygon:
    return Polygon([
        origin + x_axis * x0 + y_axis * y0,
        origin + x_axis * x1 + y_axis * y0,
        origin + x_axis * x1 + y_axis * y1,
        origin + x_axis * x0 + y_axis * y1,
    ])


def manufacturing_grid_has_cell(
    geometry,
    *,
    frame_origin: np.ndarray,
    x_axis: np.ndarray,
    y_axis: np.ndarray,
    scale: float,
    grid_step_mm: float,
    width_cells: int,
    height_cells: int,
) -> bool:
    """Whether polygon rasterization will select at least one grid-cell center."""
    k = FT * 1000.0 / scale
    model = affine_transform(geometry, [
        k * x_axis[0], k * x_axis[1],
        k * y_axis[0], k * y_axis[1],
        -k * float(frame_origin @ x_axis),
        -k * float(frame_origin @ y_axis),
    ])
    representative = model.representative_point()
    center_column = int(math.floor(representative.x / grid_step_mm))
    center_row = int(math.floor(representative.y / grid_step_mm))
    for row in range(max(0, center_row - 1), min(height_cells, center_row + 2)):
        for column in range(max(0, center_column - 1), min(width_cells, center_column + 2)):
            if shapely.intersects_xy(
                model, (column + 0.5) * grid_step_mm, (row + 0.5) * grid_step_mm
            ):
                return True

    minimum_x, minimum_y, maximum_x, maximum_y = model.bounds
    column_start = max(0, int(math.ceil(minimum_x / grid_step_mm - 0.5)))
    column_end = min(width_cells, int(math.floor(maximum_x / grid_step_mm - 0.5)) + 1)
    row_start = max(0, int(math.ceil(minimum_y / grid_step_mm - 0.5)))
    row_end = min(height_cells, int(math.floor(maximum_y / grid_step_mm - 0.5)) + 1)
    if column_start >= column_end or row_start >= row_end:
        return False
    x_values = (np.arange(column_start, column_end) + 0.5) * grid_step_mm
    rows_per_batch = max(1, 1_000_000 // max(1, len(x_values)))
    for batch_start in range(row_start, row_end, rows_per_batch):
        y_values = (
            np.arange(batch_start, min(row_end, batch_start + rows_per_batch)) + 0.5
        ) * grid_step_mm
        xx, yy = np.meshgrid(x_values, y_values)
        if shapely.intersects_xy(model, xx, yy).any():
            return True
    return False


def feasible_ranges(total_cells: int, divisions: int, maximum_cells: int,
                    minimum_cells: int) -> list[tuple[int, int]]:
    return [
        (
            max(index * minimum_cells, total_cells - (divisions - index) * maximum_cells),
            min(index * maximum_cells, total_cells - (divisions - index) * minimum_cells),
        )
        for index in range(1, divisions)
    ]


def load_tiled_buildings(component: Path, corridor, clearance_ft: float) -> gpd.GeoDataFrame:
    catalog_path = component / "catalog.geojson"
    if corridor.is_empty or not catalog_path.is_file():
        return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=2263), crs=2263)
    query = corridor.buffer(clearance_ft)
    catalog = gpd.read_file(catalog_path)
    catalog = catalog.set_crs(2263) if catalog.crs is None else catalog.to_crs(2263)
    selected = catalog[catalog.intersects(query)]
    parts = []
    for relative in selected.path:
        frame = gpd.read_parquet(component / relative)
        frame = frame.set_crs(2263) if frame.crs is None else frame.to_crs(2263)
        frame = frame[frame.geometry.notna() & frame.intersects(query)]
        if len(frame):
            parts.append(frame)
    if not parts:
        return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=2263), crs=2263)
    result = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=2263)
    keys = [column for column in ["objectid", "doitt_id", "source_order"] if column in result]
    if keys:
        result = result.drop_duplicates(keys[:1], keep="first")
    return result.reset_index(drop=True)


def load_tiled_osm_roads(component: Path, query) -> gpd.GeoDataFrame:
    catalog_path = component / "catalog.geojson"
    if query.is_empty or not catalog_path.is_file():
        return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=2263), crs=2263)
    catalog = gpd.read_file(catalog_path)
    catalog = catalog.set_crs(2263) if catalog.crs is None else catalog.to_crs(2263)
    parts = []
    for relative in catalog[catalog.intersects(query)].path:
        frame = gpd.read_parquet(component / relative)
        frame = frame.set_crs(2263) if frame.crs is None else frame.to_crs(2263)
        frame = frame[
            frame.geometry.notna()
            & frame.highway.notna()
            & frame.geom_type.isin(["LineString", "MultiLineString"])
            & frame.intersects(query)
        ]
        if len(frame):
            parts.append(frame)
    if not parts:
        return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=2263), crs=2263)
    result = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=2263)
    keys = [column for column in ["osm_type", "osm_id", "source_order"] if column in result]
    if keys:
        result = result.drop_duplicates(keys, keep="first")
    return result.reset_index(drop=True)


def nearby_road_labels(line, roads: gpd.GeoDataFrame, distance_ft: float = 30.0) -> list[dict]:
    """Return named OSM roads that run along a seam, ordered by overlap."""
    if roads.empty:
        return []
    candidates = roads[roads.name.notna() & roads.intersects(line.buffer(distance_ft))].copy()
    if candidates.empty:
        return []
    overlaps: dict[str, float] = {}
    corridor = line.buffer(distance_ft, cap_style="flat")
    for name, group in candidates.groupby(candidates.name.astype(str)):
        overlap = float(shapely.intersection(shapely.union_all(group.geometry), corridor).length)
        if overlap > 0:
            overlaps[name] = overlap
    return [
        {"name": name, "overlap_ft": overlap}
        for name, overlap in sorted(overlaps.items(), key=lambda item: (-item[1], item[0]))[:6]
    ]


class SeamScorer:
    """Exact-vector seam score with hard building conflicts dominant."""

    def __init__(
        self, aoi, *, buildings, roads, parks, water, hard_layers,
        trails=None, clearance_ft=12.0,
    ):
        self.aoi = aoi
        self.buildings = buildings
        self.roads = roads
        self.parks = parks
        self.water = water
        self.trails = (
            trails if trails is not None
            else gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=2263), crs=2263)
        )
        self.hard_layers = hard_layers
        self.clearance_ft = float(clearance_ft)

    @staticmethod
    def _hits(frame: gpd.GeoDataFrame, geometry, predicate="intersects", distance=None):
        if frame.empty or geometry.is_empty:
            return frame.iloc[[]]
        try:
            indices = frame.sindex.query(geometry, predicate=predicate, distance=distance)
            return frame.iloc[np.asarray(indices, dtype=int)]
        except (TypeError, ValueError):
            if predicate == "dwithin":
                return frame[frame.distance(geometry) <= float(distance)]
            return frame[frame.intersects(geometry)]

    @classmethod
    def _covered_line(cls, frame: gpd.GeoDataFrame, line):
        hits = cls._hits(frame, line)
        if hits.empty:
            return LineString()
        intersections = shapely.intersection(hits.geometry.to_numpy(), line)
        return shapely.union_all(intersections)

    def details(self, line) -> dict:
        line = shapely.intersection(line, self.aoi)
        length = float(line.length)
        if line.is_empty or length <= 1e-9:
            return {
                "score": 0.0, "seam_length_ft": 0.0, "buildings_cut": 0,
                "building_crossing_ft": 0.0, "near_buildings": 0,
                "hard_conflicts": 0, "road_fraction": 0.0,
                "trail_fraction": 0.0, "park_fraction": 0.0, "water_fraction": 0.0,
            }
        building_hits = self._hits(self.buildings, line)
        building_intersections = (
            shapely.intersection(building_hits.geometry.to_numpy(), line)
            if len(building_hits) else []
        )
        building_length = float(sum(item.length for item in building_intersections))
        heights = pd.to_numeric(
            building_hits.get("height_roof", pd.Series([], dtype=float)), errors="coerce"
        ).fillna(0.0)
        near = self._hits(
            self.buildings, line, predicate="dwithin", distance=self.clearance_ft
        )
        near_only = near.loc[~near.index.isin(building_hits.index)]
        near_penalty = 0.0
        if len(near_only):
            distances = near_only.distance(line).to_numpy()
            near_penalty = float(np.maximum(0.0, self.clearance_ft - distances).sum()) * 250.0
        hard_conflicts = 0
        for frame in self.hard_layers:
            hard_conflicts += len(self._hits(frame, line))
        water_line = self._covered_line(self.water, line)
        road_line = shapely.difference(self._covered_line(self.roads, line), water_line)
        trail_hits = self._hits(self.trails, line.buffer(6.0))
        trail_line = LineString()
        if len(trail_hits):
            trail_zone = shapely.buffer(shapely.union_all(trail_hits.geometry.to_numpy()), 6.0)
            trail_line = shapely.difference(
                shapely.intersection(line, trail_zone), shapely.union_all([water_line, road_line])
            )
        park_line = shapely.difference(
            self._covered_line(self.parks, line), shapely.union_all([water_line, road_line, trail_line])
        )
        safe = shapely.union_all([water_line, road_line, trail_line, park_line])
        other_length = max(0.0, length - float(safe.length))
        surface_cost = (
            other_length
            + 0.20 * float(road_line.length)
            + 0.15 * float(trail_line.length)
            + 0.35 * float(water_line.length)
            + 0.60 * float(park_line.length)
        )
        score = (
            len(building_hits) * 1_000_000_000.0
            + building_length * 10_000_000.0
            + float(heights.sum()) * 100_000.0
            + hard_conflicts * 1_000_000.0
            + near_penalty
            + surface_cost
        )
        return {
            "score": score,
            "seam_length_ft": length,
            "buildings_cut": int(len(building_hits)),
            "building_crossing_ft": building_length,
            "near_buildings": int(len(near_only)),
            "hard_conflicts": int(hard_conflicts),
            "road_fraction": float(road_line.length) / length,
            "trail_fraction": float(trail_line.length) / length,
            "park_fraction": float(park_line.length) / length,
            "water_fraction": float(water_line.length) / length,
        }

    def score(self, line) -> float:
        return float(self.details(line)["score"])


def optimize_axis(
    *,
    axis: str,
    total_cells: int,
    divisions: int,
    maximum_cells: int,
    minimum_cells: int,
    candidate_every_cells: int,
    step_ft: float,
    origin: np.ndarray,
    x_axis: np.ndarray,
    y_axis: np.ndarray,
    other_extent_ft: float,
    scorer: SeamScorer | None,
) -> tuple[list[int], list[dict]]:
    if divisions == 1:
        return [0, total_cells], []
    ranges = feasible_ranges(total_cells, divisions, maximum_cells, minimum_cells)
    layers: list[list[int]] = []
    for index, (low, high) in enumerate(ranges, 1):
        if low > high:
            raise ValueError(f"No feasible {axis}-axis partition")
        values = list(range(low, high + 1, candidate_every_cells))
        values.extend([low, high, int(round(total_cells * index / divisions))])
        layers.append(sorted({min(high, max(low, value)) for value in values}))

    score_cache: dict[int, float] = {}

    def line_for(position: int):
        distance = position * step_ft
        if axis == "x":
            return LineString([
                origin + x_axis * distance,
                origin + x_axis * distance + y_axis * other_extent_ft,
            ])
        return LineString([
            origin + y_axis * distance,
            origin + y_axis * distance + x_axis * other_extent_ft,
        ])

    def score(position: int, layer_index: int) -> float:
        if position not in score_cache:
            score_cache[position] = scorer.score(line_for(position)) if scorer is not None else 0.0
        ideal = total_cells * (layer_index + 1) / divisions
        regularity = 0.01 * ((position - ideal) * step_ft) ** 2
        return score_cache[position] + regularity

    costs: list[dict[int, float]] = []
    parents: list[dict[int, int | None]] = []
    for layer_index, values in enumerate(layers):
        current: dict[int, float] = {}
        parent: dict[int, int | None] = {}
        if layer_index == 0:
            for value in values:
                if minimum_cells <= value <= maximum_cells:
                    current[value] = score(value, layer_index)
                    parent[value] = None
        else:
            previous = costs[-1]
            for value in values:
                options = [
                    (cost, prior) for prior, cost in previous.items()
                    if minimum_cells <= value - prior <= maximum_cells
                ]
                if options:
                    prior_cost, prior = min(options)
                    current[value] = prior_cost + score(value, layer_index)
                    parent[value] = prior
        if not current:
            raise ValueError(f"No feasible dynamic-programming path for {axis}-axis seams")
        costs.append(current)
        parents.append(parent)
    terminal = [
        (cost, value) for value, cost in costs[-1].items()
        if minimum_cells <= total_cells - value <= maximum_cells
    ]
    if not terminal:
        raise ValueError(f"No feasible terminal {axis}-axis span")
    _, value = min(terminal)
    chosen = [value]
    for layer_index in range(len(layers) - 1, 0, -1):
        value = parents[layer_index][value]
        if value is None:
            raise RuntimeError(f"Internal error: missing {axis}-axis dynamic-programming parent")
        chosen.append(value)
    chosen.reverse()
    reports = []
    for index, position in enumerate(chosen, 1):
        details = scorer.details(line_for(position)) if scorer is not None else {
            "score": 0.0, "seam_length_ft": 0.0, "buildings_cut": 0,
            "building_crossing_ft": 0.0, "near_buildings": 0,
            "hard_conflicts": 0, "road_fraction": 0.0,
            "trail_fraction": 0.0, "park_fraction": 0.0, "water_fraction": 0.0,
        }
        details.update(axis=axis, index=index, grid_cell=position, offset_ft=position * step_ft)
        reports.append(details)
    return [0, *chosen, total_cells], reports


def _union_geometry(frame: gpd.GeoDataFrame, *, buffer_ft: float = 0.0):
    if frame is None or frame.empty:
        return Polygon()
    geometry = shapely.union_all(frame.geometry.to_numpy())
    return shapely.buffer(geometry, buffer_ft) if buffer_ft else geometry


class SemanticRouteCost:
    """Vectorized point costs used by monotone semantic seam routing."""

    def __init__(self, aoi, *, buildings, roads, trails, parks, water, hard_layers, sample_ft: float):
        self.aoi = aoi
        self.roads = _union_geometry(roads)
        self.trails = _union_geometry(trails, buffer_ft=6.0)
        self.parks = _union_geometry(parks)
        self.water = _union_geometry(water)
        water_boundary = shapely.boundary(self.water) if not self.water.is_empty else LineString()
        self.water_boundary = shapely.buffer(water_boundary, max(3.0, sample_ft))
        self.buildings = _union_geometry(buildings, buffer_ft=max(sample_ft * 0.55, 0.25))
        self.near_buildings = _union_geometry(buildings, buffer_ft=12.0 + sample_ft * 0.55)
        hard = [_union_geometry(frame, buffer_ft=max(2.0, sample_ft * 0.25)) for frame in hard_layers]
        self.hard = shapely.union_all([item for item in hard if not item.is_empty]) if hard else Polygon()

    @staticmethod
    def _mask(geometry, x_values, y_values):
        if geometry is None or geometry.is_empty:
            return np.zeros(np.broadcast(x_values, y_values).shape, dtype=bool)
        return shapely.intersects_xy(geometry, x_values, y_values)

    def values(self, x_values: np.ndarray, y_values: np.ndarray) -> np.ndarray:
        inside = self._mask(self.aoi, x_values, y_values)
        result = np.where(inside, 100.0, 0.05)
        # Lower values attract the route. The order intentionally lets a trail
        # or street inside a park beat generic open-space routing.
        result = np.where(inside & self._mask(self.parks, x_values, y_values), np.minimum(result, 55.0), result)
        result = np.where(inside & self._mask(self.water, x_values, y_values), np.minimum(result, 28.0), result)
        result = np.where(inside & self._mask(self.water_boundary, x_values, y_values), np.minimum(result, 18.0), result)
        result = np.where(inside & self._mask(self.roads, x_values, y_values), np.minimum(result, 5.0), result)
        result = np.where(inside & self._mask(self.trails, x_values, y_values), np.minimum(result, 3.0), result)
        near = inside & self._mask(self.near_buildings, x_values, y_values)
        result = np.where(near, result + 2_500.0, result)
        hard = inside & self._mask(self.hard, x_values, y_values)
        result = np.where(hard, result + 250_000.0, result)
        building = inside & self._mask(self.buildings, x_values, y_values)
        return np.where(building, result + 1_000_000_000.0, result)


def _local_point(
    origin: np.ndarray, x_axis: np.ndarray, y_axis: np.ndarray,
    x_cells: float, y_cells: float, step_ft: float,
) -> np.ndarray:
    return origin + x_axis * (x_cells * step_ft) + y_axis * (y_cells * step_ft)


def route_semantic_edge(
    *,
    axis: str,
    anchor_cells: int,
    major_start_cells: int,
    major_end_cells: int,
    minimum_deviation_cells: int,
    maximum_deviation_cells: int,
    sample_every_cells: int,
    origin: np.ndarray,
    x_axis: np.ndarray,
    y_axis: np.ndarray,
    step_ft: float,
    grid_step_mm: float,
    cost_surface: SemanticRouteCost,
) -> tuple[LineString, dict]:
    """Route one grid-edge segment while pinning both grid-junction endpoints."""
    if axis not in {"x", "y"}:
        raise ValueError(f"Semantic route axis must be x or y, got {axis!r}")
    if major_end_cells <= major_start_cells:
        raise ValueError("Semantic route segment has non-positive length")
    low = min(0, int(minimum_deviation_cells))
    high = max(0, int(maximum_deviation_cells))
    sample = max(1, int(sample_every_cells))
    majors = list(range(major_start_cells, major_end_cells, sample))
    majors.append(major_end_cells)
    majors = np.asarray(sorted(set(majors)), dtype=int)
    states = list(range(low, high + 1, sample))
    states.extend([low, 0, high])
    states = np.asarray(sorted(set(states)), dtype=int)
    zero_index = int(np.flatnonzero(states == 0)[0])

    major_grid, state_grid = np.meshgrid(majors, states, indexing="ij")
    if axis == "x":
        local_x = anchor_cells + state_grid
        local_y = major_grid
    else:
        local_x = major_grid
        local_y = anchor_cells + state_grid
    world_x = origin[0] + x_axis[0] * local_x * step_ft + y_axis[0] * local_y * step_ft
    world_y = origin[1] + x_axis[1] * local_x * step_ft + y_axis[1] * local_y * step_ft
    point_cost = cost_surface.values(world_x, world_y)

    predecessors = np.full((len(majors), len(states)), -1, dtype=np.int32)
    previous = np.full(len(states), np.inf, dtype=float)
    previous[zero_index] = point_cost[0, zero_index]
    for major_index in range(1, len(majors)):
        delta_major = int(majors[major_index] - majors[major_index - 1])
        maximum_lateral = max(sample, int(math.ceil(delta_major * 1.25)))
        current = np.full(len(states), np.inf, dtype=float)
        for state_index, state in enumerate(states):
            eligible = np.flatnonzero(np.abs(states - state) <= maximum_lateral)
            if not len(eligible):
                continue
            transition = (
                previous[eligible]
                + np.abs(states[eligible] - state) * grid_step_mm * 0.35
                + abs(state) * grid_step_mm * 0.002
            )
            best_local = int(np.argmin(transition))
            prior_index = int(eligible[best_local])
            current[state_index] = (
                float(transition[best_local])
                + float(point_cost[major_index, state_index]) * delta_major * grid_step_mm
            )
            predecessors[major_index, state_index] = prior_index
        if major_index == len(majors) - 1:
            current[np.arange(len(states)) != zero_index] = np.inf
        if not np.isfinite(current).any():
            raise ValueError(
                f"No connected semantic {axis}-seam route exists inside deviation range "
                f"[{low * grid_step_mm:g}, {high * grid_step_mm:g}] mm"
            )
        previous = current

    state_index = int(np.argmin(previous))
    chosen = [state_index]
    for major_index in range(len(majors) - 1, 0, -1):
        state_index = int(predecessors[major_index, state_index])
        if state_index < 0:
            raise RuntimeError("Semantic seam route has a broken dynamic-programming parent chain")
        chosen.append(state_index)
    chosen.reverse()
    deviations = states[np.asarray(chosen, dtype=int)]
    points = []
    for major, deviation in zip(majors, deviations):
        if axis == "x":
            points.append(_local_point(origin, x_axis, y_axis, anchor_cells + deviation, major, step_ft))
        else:
            points.append(_local_point(origin, x_axis, y_axis, major, anchor_cells + deviation, step_ft))
    # Dropping collinear samples keeps city-scale polygons compact while every
    # retained vertex stays exactly on the common manufacturing grid.
    line = shapely.remove_repeated_points(LineString(points))
    line = shapely.simplify(line, tolerance=step_ft * 1e-6, preserve_topology=True)
    deviations_mm = deviations.astype(float) * grid_step_mm
    return line, {
        "routing_mode": "semantic_path",
        "path_vertices": int(len(shapely.get_coordinates(line))),
        "minimum_deviation_mm": float(deviations_mm.min()),
        "maximum_deviation_mm": float(deviations_mm.max()),
        "maximum_absolute_deviation_mm": float(np.abs(deviations_mm).max()),
        "mean_absolute_deviation_mm": float(np.abs(deviations_mm).mean()),
        "routing_cost": float(previous[zero_index]),
    }


def _straight_local_edge(
    *, axis: str, anchor: int, major_start: int, major_end: int,
    origin: np.ndarray, x_axis: np.ndarray, y_axis: np.ndarray, step_ft: float,
) -> LineString:
    if axis == "x":
        values = [(anchor, major_start), (anchor, major_end)]
    else:
        values = [(major_start, anchor), (major_end, anchor)]
    return LineString([_local_point(origin, x_axis, y_axis, x, y, step_ft) for x, y in values])


def _cell_internal_sides(index: int, count: int) -> int:
    return int(index > 0) + int(index < count - 1)


def build_semantic_path_cells(
    *,
    aoi,
    x_cuts: list[int],
    y_cuts: list[int],
    maximum_x_cells: int,
    maximum_y_cells: int,
    maximum_deviation_cells: int,
    sample_every_cells: int,
    origin: np.ndarray,
    x_axis: np.ndarray,
    y_axis: np.ndarray,
    step_ft: float,
    grid_step_mm: float,
    scorer: SeamScorer,
    cost_surface: SemanticRouteCost,
) -> tuple[list[dict], list[dict]]:
    """Build a topologically shared free-form grid from routed edge segments."""
    columns, rows = len(x_cuts) - 1, len(y_cuts) - 1
    horizontal: dict[tuple[int, int], LineString] = {}
    vertical: dict[tuple[int, int], LineString] = {}
    reports = []

    for boundary in range(1, rows):
        south = boundary - 1
        north = boundary
        south_slack = maximum_y_cells - (y_cuts[south + 1] - y_cuts[south])
        north_slack = maximum_y_cells - (y_cuts[north + 1] - y_cuts[north])
        low = -min(maximum_deviation_cells, north_slack // max(1, _cell_internal_sides(north, rows)))
        high = min(maximum_deviation_cells, south_slack // max(1, _cell_internal_sides(south, rows)))
        for column in range(columns):
            line, route = route_semantic_edge(
                axis="y", anchor_cells=y_cuts[boundary],
                major_start_cells=x_cuts[column], major_end_cells=x_cuts[column + 1],
                minimum_deviation_cells=low, maximum_deviation_cells=high,
                sample_every_cells=sample_every_cells, origin=origin,
                x_axis=x_axis, y_axis=y_axis, step_ft=step_ft,
                grid_step_mm=grid_step_mm, cost_surface=cost_surface,
            )
            horizontal[(boundary, column)] = line
            details = scorer.details(line)
            details.update(route, axis="y", index=boundary, segment=column + 1,
                           south_row=boundary - 1, column=column + 1)
            reports.append({**details, "geometry": line})

    for boundary in range(1, columns):
        west = boundary - 1
        east = boundary
        west_slack = maximum_x_cells - (x_cuts[west + 1] - x_cuts[west])
        east_slack = maximum_x_cells - (x_cuts[east + 1] - x_cuts[east])
        low = -min(maximum_deviation_cells, east_slack // max(1, _cell_internal_sides(east, columns)))
        high = min(maximum_deviation_cells, west_slack // max(1, _cell_internal_sides(west, columns)))
        for south_row in range(rows):
            line, route = route_semantic_edge(
                axis="x", anchor_cells=x_cuts[boundary],
                major_start_cells=y_cuts[south_row], major_end_cells=y_cuts[south_row + 1],
                minimum_deviation_cells=low, maximum_deviation_cells=high,
                sample_every_cells=sample_every_cells, origin=origin,
                x_axis=x_axis, y_axis=y_axis, step_ft=step_ft,
                grid_step_mm=grid_step_mm, cost_surface=cost_surface,
            )
            vertical[(south_row, boundary)] = line
            details = scorer.details(line)
            details.update(route, axis="x", index=boundary, segment=south_row + 1,
                           south_row=south_row, column=boundary)
            reports.append({**details, "geometry": line})

    cells = []
    for south_row in range(rows):
        for column in range(columns):
            bottom = (
                horizontal[(south_row, column)] if south_row > 0 else
                _straight_local_edge(axis="y", anchor=y_cuts[0], major_start=x_cuts[column],
                                     major_end=x_cuts[column + 1], origin=origin,
                                     x_axis=x_axis, y_axis=y_axis, step_ft=step_ft)
            )
            top = (
                horizontal[(south_row + 1, column)] if south_row + 1 < rows else
                _straight_local_edge(axis="y", anchor=y_cuts[-1], major_start=x_cuts[column],
                                     major_end=x_cuts[column + 1], origin=origin,
                                     x_axis=x_axis, y_axis=y_axis, step_ft=step_ft)
            )
            left = (
                vertical[(south_row, column)] if column > 0 else
                _straight_local_edge(axis="x", anchor=x_cuts[0], major_start=y_cuts[south_row],
                                     major_end=y_cuts[south_row + 1], origin=origin,
                                     x_axis=x_axis, y_axis=y_axis, step_ft=step_ft)
            )
            right = (
                vertical[(south_row, column + 1)] if column + 1 < columns else
                _straight_local_edge(axis="x", anchor=x_cuts[-1], major_start=y_cuts[south_row],
                                     major_end=y_cuts[south_row + 1], origin=origin,
                                     x_axis=x_axis, y_axis=y_axis, step_ft=step_ft)
            )
            ring = list(bottom.coords)
            ring.extend(list(right.coords)[1:])
            ring.extend(list(top.coords)[::-1][1:])
            ring.extend(list(left.coords)[::-1][1:])
            cell = Polygon(ring)
            if not cell.is_valid or cell.is_empty:
                raise ValueError(
                    f"Semantic paths form an invalid logical cell at south-row {south_row + 1}, "
                    f"column {column + 1}; reduce --max-seam-deviation-mm or use --seam-mode straight."
                )
            cells.append({"south_row": south_row, "column": column + 1, "geometry": cell})

    frame = frame_geometry(
        origin, x_axis, y_axis,
        x_cuts[0] * step_ft, x_cuts[-1] * step_ft,
        y_cuts[0] * step_ft, y_cuts[-1] * step_ft,
    )
    union = shapely.union_all([item["geometry"] for item in cells])
    gap = float(shapely.difference(frame, union).area)
    overlap = max(0.0, float(sum(item["geometry"].area for item in cells) - union.area))
    tolerance = max(1e-6, float(frame.area) * 1e-10)
    if gap > tolerance or overlap > tolerance:
        raise ValueError(
            f"Semantic path topology is not a partition: gap={gap:.6g} sq ft, "
            f"overlap={overlap:.6g} sq ft, tolerance={tolerance:.6g}."
        )
    return cells, reports


def tight_frame_grid_bounds(
    geometry, *, origin: np.ndarray, x_axis: np.ndarray, y_axis: np.ndarray,
    step_ft: float, minimum_cells: int,
) -> tuple[int, int, int, int]:
    coordinates = shapely.get_coordinates(geometry) - origin
    x_values = (coordinates @ x_axis) / step_ft
    y_values = (coordinates @ y_axis) / step_ft
    x0 = int(math.floor(float(x_values.min()) + 1e-6))
    y0 = int(math.floor(float(y_values.min()) + 1e-6))
    x1 = int(math.ceil(float(x_values.max()) - 1e-6))
    y1 = int(math.ceil(float(y_values.max()) - 1e-6))

    def expand(low: int, high: int) -> tuple[int, int]:
        missing = max(0, minimum_cells - (high - low))
        return low - missing // 2, high + missing - missing // 2

    x0, x1 = expand(x0, x1)
    y0, y1 = expand(y0, y1)
    return x0, y0, x1, y1


def seam_corridors(
    *,
    origin: np.ndarray,
    x_axis: np.ndarray,
    y_axis: np.ndarray,
    total_x_cells: int,
    total_y_cells: int,
    columns: int,
    rows: int,
    maximum_x_cells: int,
    maximum_y_cells: int,
    minimum_cells: int,
    step_ft: float,
):
    total_x = total_x_cells * step_ft
    total_y = total_y_cells * step_ft
    bands = []
    for low, high in feasible_ranges(total_x_cells, columns, maximum_x_cells, minimum_cells):
        bands.append(frame_geometry(origin, x_axis, y_axis, low * step_ft, high * step_ft, 0, total_y))
    for low, high in feasible_ranges(total_y_cells, rows, maximum_y_cells, minimum_cells):
        bands.append(frame_geometry(origin, x_axis, y_axis, 0, total_x, low * step_ft, high * step_ft))
    return shapely.union_all(bands) if bands else Polygon()


def terrain_statistics(
    aoi,
    lidar_cache: Path,
    *,
    sample_stride: int,
    requested_origin: float | None,
    requested_factor: float | None,
    scale: float,
    vertical_exaggeration: float,
    layer_height_mm: float,
    minimum_levels: float,
) -> dict:
    catalog_path = lidar_cache / "catalog.geojson"
    if not catalog_path.is_file():
        raise FileNotFoundError(f"Completed LiDAR catalog is missing: {catalog_path}")
    catalog = gpd.read_file(catalog_path)
    catalog = catalog.set_crs(4326) if catalog.crs is None else catalog
    catalog = catalog.to_crs(2263)
    selected = catalog[catalog.intersects(aoi)]
    minimum = math.inf
    samples = []
    finite_cells = 0
    for row in selected.itertuples(index=False):
        path = lidar_cache / row.ground
        with rasterio.open(path) as source:
            try:
                raw_window = geometry_window(source, [aoi])
            except rasterio.errors.WindowError:
                continue
            full = Window(0, 0, source.width, source.height)
            window = raw_window.intersection(full).round_offsets().round_lengths()
            values = source.read(1, window=window)
            inside = geometry_mask(
                [aoi], out_shape=values.shape, transform=source.window_transform(window),
                invert=True, all_touched=False,
            )
            valid = inside & np.isfinite(values)
            if not valid.any():
                continue
            finite_cells += int(valid.sum())
            minimum = min(minimum, float(values[valid].min()))
            sampled_values = values[::sample_stride, ::sample_stride]
            sampled_valid = valid[::sample_stride, ::sample_stride]
            if sampled_valid.any():
                samples.append(sampled_values[sampled_valid].astype(np.float32, copy=False))
    if not samples or not math.isfinite(minimum):
        raise ValueError("The bounding polygon contains no finite cached ground elevations")
    sampled = np.concatenate(samples)
    origin = float(requested_origin) if requested_origin is not None else math.floor(minimum) - 5.0
    decision = choose_terrain_relief(
        sampled,
        scale_denominator=scale,
        vertical_exaggeration=vertical_exaggeration,
        layer_height_mm=layer_height_mm,
        minimum_levels=minimum_levels,
        requested_factor=requested_factor,
    )
    return {
        "mode": "exact minimum plus stratified percentile sample",
        "tiles_read": int(len(selected)),
        "finite_cells_scanned": finite_cells,
        "sample_cells": int(len(sampled)),
        "sample_stride": sample_stride,
        "exact_minimum_m_navd88": minimum,
        "shared_origin_m_navd88": origin,
        "shared_relief": decision.__dict__,
    }


def polygon_parts(geometry) -> list[Polygon]:
    valid = shapely.make_valid(geometry)
    return [
        part for part in shapely.get_parts(valid)
        if part.geom_type == "Polygon" and not part.is_empty and part.area > 1e-8
    ]


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def write_preview(path: Path, aoi, chunks: list[dict], seams: list[dict]) -> None:
    """Write a dependency-free plan-view SVG for quick visual inspection."""
    minx, miny, maxx, maxy = aoi.bounds
    span_x, span_y = max(maxx - minx, 1e-9), max(maxy - miny, 1e-9)
    if span_y >= span_x:
        height = 1000.0
        width = max(360.0, min(1000.0, 90.0 + 910.0 * span_x / span_y))
    else:
        width = 1000.0
        height = max(360.0, min(1000.0, 90.0 + 910.0 * span_y / span_x))
    margin = 45.0
    scale = min((width - 2 * margin) / span_x, (height - 2 * margin) / span_y)

    def point(coordinate):
        return (
            margin + (coordinate[0] - minx) * scale,
            height - margin - (coordinate[1] - miny) * scale,
        )

    def paths(geometry):
        values = []
        for poly in polygon_parts(geometry):
            rings = [poly.exterior, *poly.interiors]
            commands = []
            for ring in rings:
                coordinates = [point(value) for value in ring.coords]
                commands.append("M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in coordinates) + " Z")
            values.append(" ".join(commands))
        return values

    palette = ["#d8e8f5", "#f6d7b0", "#d8efd2", "#ead6ef", "#f3e6a7"]
    body = []
    for index, chunk in enumerate(chunks):
        for value in paths(chunk["geometry_2263"]):
            body.append(
                f'<path d="{value}" fill="{palette[index % len(palette)]}" '
                'fill-rule="evenodd" stroke="#334155" stroke-width="1.2"/>'
            )
        center = chunk["geometry_2263"].representative_point()
        x, y = point(center.coords[0])
        label = f'r{int(chunk["row"]):02d} c{int(chunk["column"]):02d}'
        if int(chunk.get("component_count_in_cell", 1)) > 1:
            label += f' p{int(chunk["component"]):02d}'
        body.append(
            f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="middle" font-size="12" '
            'font-family="sans-serif" fill="#111827" stroke="white" stroke-width="3" '
            f'paint-order="stroke">{label}</text>'
        )
    for seam in seams:
        line = seam.get("geometry")
        if line is None:
            continue
        for part in shapely.get_parts(shapely.intersection(line, aoi)):
            coordinates = [point(value) for value in part.coords]
            body.append(
                '<polyline points="' + " ".join(f"{x:.2f},{y:.2f}" for x, y in coordinates)
                + '" fill="none" stroke="#dc2626" stroke-width="2" stroke-dasharray="7 4"/>'
            )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}"><rect width="100%" height="100%" fill="white"/>'
        + "".join(body)
        + "</svg>\n"
    )
    atomic_write(path, svg)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Plan gap-free, low-dissonance NYC 3MF chunks and emit generation commands.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    result.add_argument("--bounding-polygon", required=True, type=parse_polygon, metavar="WKT|GEOJSON|PATH")
    result.add_argument("--max-chunks", required=True, type=int)
    result.add_argument("--chunk-size-mm", type=parse_size, default=(235.0, 235.0), metavar="WIDTHxHEIGHT")
    result.add_argument("--scale", type=float, help="Fixed common scale; omitted chooses the most detailed feasible scale")
    result.add_argument("--orientation-deg", type=float, help="Chunk Y axis in degrees east of north; omitted searches cached road/envelope axes")
    result.add_argument(
        "--seam-mode", choices=["semantic-paths", "straight"], default="semantic-paths",
        help="Route free-form semantic boundaries or retain straight global grid cuts",
    )
    result.add_argument("--seam-flex-percent", type=float, default=4.0, help="Automatic-scale detail traded for movable seam corridors")
    result.add_argument("--candidate-step-mm", type=float, default=0.5, help="Spacing between exact-vector seam candidates")
    result.add_argument("--path-step-mm", type=float, default=0.25, help="Sampling interval for free-form semantic seam routing")
    result.add_argument("--max-seam-deviation-mm", type=float, default=20.0, help="Maximum routed departure from each straight scaffold edge")
    result.add_argument("--grid-step-mm", type=float, default=0.125)
    result.add_argument("--source-padding-m", type=float, default=20.0)
    result.add_argument("--vertical-exaggeration", type=float, default=1.0)
    result.add_argument("--layer-height", type=float, choices=[0.08, 0.12, 0.16, 0.20, 0.24], default=0.24)
    result.add_argument("--minimum-terrain-levels", type=float, default=6.0)
    result.add_argument("--terrain-origin-m", type=float)
    result.add_argument("--terrain-relief-factor", type=float)
    result.add_argument("--terrain-sample-stride", type=int, default=32)
    result.add_argument("--skip-terrain-scan", action="store_true", help="Use a conservative shared datum and factor 1 instead of reading cached ground")
    result.add_argument("--geometric-only", action="store_true", help="Do not read semantic vector layers when placing seams")
    result.add_argument("--plan-id", help="Stable file/job prefix; derived from the request when omitted")
    result.add_argument("--output-dir", type=Path, help="Plan directory; defaults under output/plans")
    result.add_argument("--model-dir", type=Path, help="3MF destination directory; defaults under output/models/<plan-id>")
    result.add_argument(
        "--generation-output-dir", type=Path, default=ROOT / "output",
        help="Root passed to generate_3mf.py for generated jobs and default plan/model output",
    )
    result.add_argument("--data-dir", type=Path, default=ROOT / "data")
    result.add_argument("--cache-dir", type=Path, help="Dataset cache root; defaults to <data-dir>/cache")
    result.add_argument("--lidar-cache-dir", type=Path)
    result.add_argument("--offline", action=argparse.BooleanOptionalAction, default=True)
    result.add_argument("--full-validation", action="store_true")
    result.add_argument("--slice", action="store_true")
    result.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    return result


def main() -> None:
    args = parser().parse_args()
    log, event_recorder = configure_logging(args.log_level)
    log = log.bind(plan_id=args.plan_id)
    log.info(
        "planner_started", maximum_chunks=args.max_chunks,
        chunk_size_mm=list(args.chunk_size_mm), requested_scale=args.scale,
        requested_orientation_deg=args.orientation_deg, seam_mode=args.seam_mode,
    )
    if args.max_chunks < 1:
        raise SystemExit("--max-chunks must be positive")
    width_mm, height_mm = args.chunk_size_mm
    if not (MIN_FRAME_MM <= width_mm <= MAX_FRAME_MM and MIN_FRAME_MM <= height_mm <= MAX_FRAME_MM):
        raise SystemExit(f"Chunk dimensions must each be {MIN_FRAME_MM:g}-{MAX_FRAME_MM:g} mm")
    if not (0.1 <= args.grid_step_mm <= 0.5):
        raise SystemExit("--grid-step-mm must be 0.1-0.5 mm")
    for dimension in args.chunk_size_mm:
        if abs(dimension / args.grid_step_mm - round(dimension / args.grid_step_mm)) > 1e-7:
            raise SystemExit("Chunk dimensions must be exact multiples of --grid-step-mm")
    if not math.isfinite(args.candidate_step_mm) or args.candidate_step_mm <= 0:
        raise SystemExit("--candidate-step-mm must be a positive finite number")
    if args.candidate_step_mm < args.grid_step_mm:
        raise SystemExit("--candidate-step-mm cannot be smaller than --grid-step-mm")
    if not math.isfinite(args.path_step_mm) or args.path_step_mm <= 0:
        raise SystemExit("--path-step-mm must be a positive finite number")
    if args.path_step_mm < args.grid_step_mm:
        raise SystemExit("--path-step-mm cannot be smaller than --grid-step-mm")
    if abs(args.path_step_mm / args.grid_step_mm - round(args.path_step_mm / args.grid_step_mm)) > 1e-7:
        raise SystemExit("--path-step-mm must be an exact multiple of --grid-step-mm")
    if not math.isfinite(args.max_seam_deviation_mm) or not (0 <= args.max_seam_deviation_mm <= 100):
        raise SystemExit("--max-seam-deviation-mm must be between 0 and 100 mm")
    if abs(args.max_seam_deviation_mm / args.grid_step_mm - round(args.max_seam_deviation_mm / args.grid_step_mm)) > 1e-7:
        raise SystemExit("--max-seam-deviation-mm must be an exact multiple of --grid-step-mm")
    if not (0 <= args.seam_flex_percent <= 25):
        raise SystemExit("--seam-flex-percent must be between 0 and 25")
    if args.terrain_sample_stride < 1:
        raise SystemExit("--terrain-sample-stride must be positive")
    if args.scale is not None and not (1000 <= args.scale <= 50000):
        raise SystemExit("--scale must be between 1,000 and 50,000")
    if args.orientation_deg is not None and not math.isfinite(args.orientation_deg):
        raise SystemExit("--orientation-deg must be finite")
    if not (0.25 <= args.vertical_exaggeration <= 5.0):
        raise SystemExit("--vertical-exaggeration must be between 0.25 and 5.0")
    if not (0 <= args.minimum_terrain_levels <= 50):
        raise SystemExit("--minimum-terrain-levels must be between 0 and 50")
    if not (0 <= args.source_padding_m <= 500):
        raise SystemExit("--source-padding-m must be between 0 and 500 metres")
    if args.terrain_origin_m is not None and not math.isfinite(args.terrain_origin_m):
        raise SystemExit("--terrain-origin-m must be finite")
    if args.terrain_relief_factor is not None and not (0.25 <= args.terrain_relief_factor <= 10.0):
        raise SystemExit("--terrain-relief-factor must be between 0.25 and 10.0")

    data_dir = args.data_dir.resolve()
    cache_dir = args.cache_dir.resolve() if args.cache_dir else (data_dir / "cache").resolve()
    lidar = (
        args.lidar_cache_dir.resolve() if args.lidar_cache_dir
        else (cache_dir / "nyc_lidar_2017").resolve()
    )
    stage_started = time.monotonic()
    log.info("stage_started", stage="validate_caches", data_dir=str(data_dir))
    cache_manifests = {
        "lidar": require_complete_manifest(lidar, "LiDAR"),
        "building_footprints": require_complete_manifest(cache_dir / "nyc_building_footprints", "Building footprints"),
        "planimetrics": require_complete_manifest(cache_dir / "nyc_planimetrics_2022", "Planimetrics"),
        "parks_trails": require_complete_manifest(cache_dir / "nyc_parks_trails", "Parks trails"),
        "parks_structures": require_complete_manifest(cache_dir / "nyc_parks_structures", "Parks structures"),
        "land_cover": require_complete_manifest(cache_dir / "nyc_land_cover_2017", "Land cover"),
        "openstreetmap": require_complete_manifest(cache_dir / "new_york_osm", "OpenStreetMap"),
        "buildings_3d": require_complete_manifest(cache_dir / "nyc_3d_buildings_2014", "3D buildings"),
    }
    log.info(
        "stage_completed", stage="validate_caches",
        elapsed_seconds=time.monotonic() - stage_started,
        caches={name: manifest.get("completed_at") for name, manifest in cache_manifests.items()},
    )
    lidar_catalog_path = lidar / "catalog.geojson"
    if not lidar_catalog_path.is_file():
        raise SystemExit(f"LiDAR cache catalog is missing: {lidar_catalog_path}")
    aoi_wgs = args.bounding_polygon
    aoi = gpd.GeoSeries([aoi_wgs], crs=4326).to_crs(2263).iloc[0]
    try:
        lidar_catalog = gpd.read_file(lidar_catalog_path)
    except Exception as error:
        raise SystemExit(f"LiDAR cache catalog is unreadable: {lidar_catalog_path}: {error}") from error
    lidar_catalog = lidar_catalog.set_crs(2263) if lidar_catalog.crs is None else lidar_catalog.to_crs(2263)
    relevant_lidar = lidar_catalog[lidar_catalog.intersects(aoi.buffer(args.source_padding_m / FT))]
    if relevant_lidar.empty:
        raise SystemExit(
            f"LiDAR cache {lidar} has no tiles intersecting the requested polygon and source padding."
        )
    missing_lidar = []
    for _, record in relevant_lidar.iterrows():
        for column in ("ground", "upper"):
            cached = lidar / str(record.get(column, ""))
            if not cached.is_file():
                missing_lidar.append(str(cached))
                if len(missing_lidar) == 5:
                    break
        if len(missing_lidar) == 5:
            break
    if missing_lidar:
        raise SystemExit(
            "LiDAR cache catalog references missing raster tiles needed by this request: "
            + ", ".join(missing_lidar)
        )
    log.info(
        "lidar_tiles_selected", tiles=int(len(relevant_lidar)),
        source_padding_m=args.source_padding_m, lidar_cache=str(lidar),
    )
    request_components = len(polygon_parts(aoi))
    if request_components > args.max_chunks:
        raise SystemExit(
            f"The requested polygon has {request_components} disconnected components, so it cannot be "
            f"represented by at most --max-chunks={args.max_chunks} Polygon-only generation jobs."
        )
    # Each disconnected request component needs at least one generated Polygon.
    # Reserve those unavoidable extra jobs while allowing the main component to
    # use the remaining rectangular grid budget.  A final exact component count
    # below catches unusually convoluted polygons where this bound is insufficient.
    layout_chunk_budget = (
        args.max_chunks if args.scale is not None
        else args.max_chunks - request_components + 1
    )
    planimetrics_root = cache_dir / "nyc_planimetrics_2022"
    road_path = planimetrics_root / "ROADBED.parquet"
    if not args.geometric_only:
        required_semantic = [
            road_path,
            planimetrics_root / "PARK.parquet",
            planimetrics_root / "HYDROGRAPHY.parquet",
            planimetrics_root / "TRANSPORT_STRUCTURE.parquet",
            planimetrics_root / "RETAININGWALL.parquet",
            cache_dir / "nyc_parks_structures/data.parquet",
            cache_dir / "nyc_parks_trails/data.parquet",
            cache_dir / "nyc_building_footprints/catalog.geojson",
        ]
        missing_semantic = [str(path) for path in required_semantic if not path.is_file()]
        if missing_semantic:
            raise SystemExit(
                "Semantic seam planning cache is incomplete; missing: "
                + ", ".join(missing_semantic)
                + ". Use --geometric-only only when a non-semantic fallback is intentional."
            )
    roads_for_orientation = (
        read_vector(road_path, aoi.bounds, aoi) if not args.geometric_only
        else gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=2263), crs=2263)
    )
    orientations, road_orientation = orientation_candidates(aoi, roads_for_orientation, args.orientation_deg)
    try:
        layout, orientation_report = choose_layout(
            aoi,
            orientations,
            road_orientation=road_orientation,
            maximum_chunks=layout_chunk_budget,
            chunk_size_mm=args.chunk_size_mm,
            grid_step_mm=args.grid_step_mm,
            requested_scale=args.scale,
            seam_flex_percent=args.seam_flex_percent,
            source_padding_m=args.source_padding_m,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    log.info(
        "layout_selected", scale_denominator=layout.scale,
        orientation_deg_east_of_north=layout.orientation_deg,
        columns=layout.columns, rows=layout.rows,
        road_alignment_deg=layout.road_alignment_deg,
    )

    k = FT * 1000.0 / layout.scale
    minimum_x, minimum_y, maximum_x, maximum_y = layout.local_bounds_ft
    printed_x_mm = (maximum_x - minimum_x) * k
    printed_y_mm = (maximum_y - minimum_y) * k
    total_x_cells = max(
        math.ceil(MIN_FRAME_MM / args.grid_step_mm),
        int(math.ceil((printed_x_mm - 1e-9) / args.grid_step_mm)),
    )
    total_y_cells = max(
        math.ceil(MIN_FRAME_MM / args.grid_step_mm),
        int(math.ceil((printed_y_mm - 1e-9) / args.grid_step_mm)),
    )
    maximum_x_cells = int(math.floor((width_mm + 1e-9) / args.grid_step_mm))
    maximum_y_cells = int(math.floor((height_mm + 1e-9) / args.grid_step_mm))
    minimum_cells = int(math.ceil(MIN_FRAME_MM / args.grid_step_mm))
    columns = max(1, math.ceil(total_x_cells / maximum_x_cells))
    rows = max(1, math.ceil(total_y_cells / maximum_y_cells))
    if columns * rows > args.max_chunks:
        raise SystemExit("Internal error: snapped layout exceeds --max-chunks")
    if total_x_cells < columns * minimum_cells or total_y_cells < rows * minimum_cells:
        raise SystemExit("The selected grid would create a print frame below 20 mm")
    step_ft = args.grid_step_mm / k
    frame_width_ft = total_x_cells * step_ft
    frame_height_ft = total_y_cells * step_ft
    margin_x = (frame_width_ft - (maximum_x - minimum_x)) / 2.0
    margin_y = (frame_height_ft - (maximum_y - minimum_y)) / 2.0
    origin = layout.x_axis * (minimum_x - margin_x) + layout.y_axis * (minimum_y - margin_y)

    corridors = seam_corridors(
        origin=origin,
        x_axis=layout.x_axis,
        y_axis=layout.y_axis,
        total_x_cells=total_x_cells,
        total_y_cells=total_y_cells,
        columns=columns,
        rows=rows,
        maximum_x_cells=maximum_x_cells,
        maximum_y_cells=maximum_y_cells,
        minimum_cells=minimum_cells,
        step_ft=step_ft,
    )
    scorer = None
    layer_counts = {}
    if not args.geometric_only and not corridors.is_empty:
        clearance = 12.0
        corridor_query = shapely.intersection(corridors.buffer(clearance), aoi.buffer(clearance))
        buildings = load_tiled_buildings(cache_dir / "nyc_building_footprints", corridor_query, clearance)
        roads = roads_for_orientation[roads_for_orientation.intersects(corridor_query)].copy()
        trails = read_vector(cache_dir / "nyc_parks_trails/data.parquet", aoi.bounds, corridor_query)
        parks = read_vector(planimetrics_root / "PARK.parquet", aoi.bounds, corridor_query)
        water = read_vector(planimetrics_root / "HYDROGRAPHY.parquet", aoi.bounds, corridor_query)
        transport = read_vector(planimetrics_root / "TRANSPORT_STRUCTURE.parquet", aoi.bounds, corridor_query)
        walls = read_vector(planimetrics_root / "RETAININGWALL.parquet", aoi.bounds, corridor_query)
        park_structures = read_vector(cache_dir / "nyc_parks_structures/data.parquet", aoi.bounds, corridor_query)
        scorer = SeamScorer(
            aoi,
            buildings=buildings,
            roads=roads,
            trails=trails,
            parks=parks,
            water=water,
            hard_layers=[transport, walls, park_structures],
            clearance_ft=clearance,
        )
        layer_counts = {
            "buildings": len(buildings), "roadbeds": len(roads), "trails": len(trails), "parks": len(parks),
            "hydrography": len(water), "transport_structures": len(transport),
            "retaining_walls": len(walls), "park_structures": len(park_structures),
        }
        log.info("semantic_layers_loaded", **layer_counts)

    candidate_every = max(1, int(round(args.candidate_step_mm / args.grid_step_mm)))
    x_cuts, x_reports = optimize_axis(
        axis="x", total_cells=total_x_cells, divisions=columns,
        maximum_cells=maximum_x_cells, minimum_cells=minimum_cells,
        candidate_every_cells=candidate_every, step_ft=step_ft, origin=origin,
        x_axis=layout.x_axis, y_axis=layout.y_axis, other_extent_ft=frame_height_ft,
        scorer=scorer,
    )
    y_cuts, y_reports = optimize_axis(
        axis="y", total_cells=total_y_cells, divisions=rows,
        maximum_cells=maximum_y_cells, minimum_cells=minimum_cells,
        candidate_every_cells=candidate_every, step_ft=step_ft, origin=origin,
        x_axis=layout.x_axis, y_axis=layout.y_axis, other_extent_ft=frame_width_ft,
        scorer=scorer,
    )
    log.info(
        "straight_scaffold_optimized", x_cut_grid_cells=x_cuts, y_cut_grid_cells=y_cuts,
        candidate_step_mm=args.candidate_step_mm,
        scaffold_buildings_cut=sum(item["buildings_cut"] for item in [*x_reports, *y_reports]),
        scaffold_hard_conflicts=sum(item["hard_conflicts"] for item in [*x_reports, *y_reports]),
    )

    partition_parts = []
    seam_geometries = []
    partition_mode = (
        "semantic_paths"
        if args.seam_mode == "semantic-paths" and scorer is not None and (columns > 1 or rows > 1)
        else "straight_grid"
    )
    if args.seam_mode == "semantic-paths" and partition_mode != "semantic_paths":
        log.warning(
            "semantic_path_routing_disabled", reason=(
                "semantic layers are unavailable in --geometric-only mode"
                if args.geometric_only else "the plan has no internal seams"
            ), fallback="straight_grid",
        )
    if partition_mode == "semantic_paths":
        route_started = time.monotonic()
        sample_every_cells = max(1, int(round(args.path_step_mm / args.grid_step_mm)))
        maximum_deviation_cells = int(round(args.max_seam_deviation_mm / args.grid_step_mm))
        cost_surface = SemanticRouteCost(
            aoi, buildings=buildings, roads=roads, trails=trails, parks=parks, water=water,
            hard_layers=[transport, walls, park_structures], sample_ft=sample_every_cells * step_ft,
        )
        try:
            routed_cells, seam_geometries = build_semantic_path_cells(
                aoi=aoi, x_cuts=x_cuts, y_cuts=y_cuts,
                maximum_x_cells=maximum_x_cells, maximum_y_cells=maximum_y_cells,
                maximum_deviation_cells=maximum_deviation_cells,
                sample_every_cells=sample_every_cells, origin=origin,
                x_axis=layout.x_axis, y_axis=layout.y_axis, step_ft=step_ft,
                grid_step_mm=args.grid_step_mm, scorer=scorer, cost_surface=cost_surface,
            )
        except ValueError as error:
            raise SystemExit(f"Semantic seam routing failed: {error}") from error
        for cell_record in routed_cells:
            south_row = cell_record["south_row"]
            column_index = cell_record["column"]
            display_row = rows - south_row
            parts = polygon_parts(shapely.intersection(aoi, cell_record["geometry"]))
            for part_index, part in enumerate(parts, 1):
                frame_bounds = tight_frame_grid_bounds(
                    part, origin=origin, x_axis=layout.x_axis, y_axis=layout.y_axis,
                    step_ft=step_ft, minimum_cells=minimum_cells,
                )
                partition_parts.append({
                    "south_row": south_row, "display_row": display_row,
                    "column": column_index, "component": part_index,
                    "component_count_in_cell": len(parts),
                    "x0_cells": frame_bounds[0], "y0_cells": frame_bounds[1],
                    "x1_cells": frame_bounds[2], "y1_cells": frame_bounds[3],
                    "logical_cell_wkb_hex_epsg2263": shapely.to_wkb(cell_record["geometry"], hex=True),
                    "geometry": part,
                })
        for seam in seam_geometries:
            log.info(
                "seam_segment_routed", axis=seam["axis"], index=seam["index"],
                segment=seam["segment"], path_vertices=seam["path_vertices"],
                maximum_absolute_deviation_mm=seam["maximum_absolute_deviation_mm"],
                buildings_cut=seam["buildings_cut"], hard_conflicts=seam["hard_conflicts"],
                road_fraction=seam["road_fraction"], trail_fraction=seam["trail_fraction"],
                water_fraction=seam["water_fraction"], park_fraction=seam["park_fraction"],
            )
        log.info(
            "semantic_paths_routed", elapsed_seconds=time.monotonic() - route_started,
            routed_segments=len(seam_geometries), chunks_before_component_split=len(routed_cells),
            maximum_allowed_deviation_mm=args.max_seam_deviation_mm,
            buildings_cut=sum(item["buildings_cut"] for item in seam_geometries),
            hard_conflicts=sum(item["hard_conflicts"] for item in seam_geometries),
        )
    else:
        for south_row, (y0_cells, y1_cells) in enumerate(zip(y_cuts[:-1], y_cuts[1:])):
            display_row = rows - south_row
            for column_index, (x0_cells, x1_cells) in enumerate(zip(x_cuts[:-1], x_cuts[1:]), 1):
                x0, x1 = x0_cells * step_ft, x1_cells * step_ft
                y0, y1 = y0_cells * step_ft, y1_cells * step_ft
                cell = frame_geometry(origin, layout.x_axis, layout.y_axis, x0, x1, y0, y1)
                parts = polygon_parts(shapely.intersection(aoi, cell))
                for part_index, part in enumerate(parts, 1):
                    partition_parts.append({
                        "south_row": south_row,
                        "display_row": display_row,
                        "column": column_index,
                        "component": part_index,
                        "component_count_in_cell": len(parts),
                        "x0_cells": x0_cells,
                        "x1_cells": x1_cells,
                        "y0_cells": y0_cells,
                        "y1_cells": y1_cells,
                        "logical_cell_wkb_hex_epsg2263": shapely.to_wkb(cell, hex=True),
                        "geometry": part,
                    })
    if len(partition_parts) > args.max_chunks:
        raise SystemExit(
            f"The selected {columns}x{rows} grid intersects the disconnected/concave request as "
            f"{len(partition_parts)} printable Polygon pieces, exceeding --max-chunks={args.max_chunks}. "
            "Increase --max-chunks, provide a connected land envelope, choose a coarser --scale, or try "
            "a different --orientation-deg. No plan files were written."
        )
    unprintable_parts = []
    oversized_parts = []
    for item in partition_parts:
        frame_origin = (
            origin
            + layout.x_axis * item["x0_cells"] * step_ft
            + layout.y_axis * item["y0_cells"] * step_ft
        )
        width_cells = item["x1_cells"] - item["x0_cells"]
        height_cells = item["y1_cells"] - item["y0_cells"]
        if width_cells > maximum_x_cells or height_cells > maximum_y_cells:
            oversized_parts.append({
                "row": item["display_row"], "column": item["column"],
                "component": item["component"],
                "size_mm": [width_cells * args.grid_step_mm, height_cells * args.grid_step_mm],
            })
            continue
        if not manufacturing_grid_has_cell(
            item["geometry"],
            frame_origin=frame_origin,
            x_axis=layout.x_axis,
            y_axis=layout.y_axis,
            scale=layout.scale,
            grid_step_mm=args.grid_step_mm,
            width_cells=width_cells,
            height_cells=height_cells,
        ):
            unprintable_parts.append({
                "row": item["display_row"],
                "column": item["column"],
                "component": item["component"],
                "area_sq_ft": float(item["geometry"].area),
                "printed_area_sq_mm": float(item["geometry"].area * k * k),
            })
    if oversized_parts:
        examples = ", ".join(
            f"r{item['row']}c{item['column']}p{item['component']}="
            f"{item['size_mm'][0]:g}x{item['size_mm'][1]:g} mm"
            for item in oversized_parts[:8]
        )
        raise SystemExit(
            f"Semantic seam routing made {len(oversized_parts)} chunk frame(s) exceed the requested "
            f"{width_mm:g}x{height_mm:g} mm maximum: {examples}. Reduce --max-seam-deviation-mm, "
            "choose a coarser --scale, or increase --max-chunks. No plan files were written."
        )
    if unprintable_parts:
        examples = ", ".join(
            f"r{item['row']}c{item['column']}p{item['component']} "
            f"({item['printed_area_sq_mm']:.6g} mm²)"
            for item in unprintable_parts[:8]
        )
        raise SystemExit(
            f"{len(unprintable_parts)} polygon component(s) contain no manufacturing-grid cell center "
            f"at 1:{layout.scale:g} with a {args.grid_step_mm:g} mm grid: {examples}. "
            "These sub-cell islands/slivers cannot be generated without leaving geometry empty; use a more "
            "detailed scale, a finer --grid-step-mm, or simplify the input polygon. No plan files were written."
        )
    log.info(
        "partition_validated", partition_mode=partition_mode,
        printable_polygon_jobs=len(partition_parts), maximum_chunks=args.max_chunks,
        oversized_frames=0, empty_manufacturing_grids=0,
    )

    request_identity = {
        "version": PLAN_VERSION,
        "aoi_wkb": shapely.to_wkb(aoi_wgs, hex=True),
        "maximum_chunks": args.max_chunks,
        "chunk_size_mm": args.chunk_size_mm,
        "scale": layout.scale,
        "orientation": layout.orientation_deg,
        "grid_step_mm": args.grid_step_mm,
        "x_cuts": x_cuts,
        "y_cuts": y_cuts,
        "partition_mode": partition_mode,
        "path_step_mm": args.path_step_mm,
        "max_seam_deviation_mm": args.max_seam_deviation_mm,
    }
    suffix = hashlib.sha256(canonical(request_identity).encode()).hexdigest()[:10]
    plan_id = args.plan_id or f"nyc_chunks_{suffix}"
    plan_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", plan_id)
    generation_output_dir = args.generation_output_dir.resolve()
    output_dir = (args.output_dir or generation_output_dir / "plans" / plan_id).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(f"Output directory is not empty: {output_dir}")
    chunks_dir = output_dir / "chunks"
    frames_dir = output_dir / "frames"
    model_dir = (args.model_dir or generation_output_dir / "models" / plan_id).resolve()

    if args.skip_terrain_scan:
        terrain_origin = args.terrain_origin_m if args.terrain_origin_m is not None else -50.0
        terrain_factor = args.terrain_relief_factor if args.terrain_relief_factor is not None else 1.0
        terrain_report = {
            "mode": "conservative fallback; cached terrain not scanned",
            "shared_origin_m_navd88": terrain_origin,
            "shared_relief": {"factor": terrain_factor},
        }
        log.warning(
            "terrain_scan_skipped", shared_origin_m_navd88=terrain_origin,
            terrain_relief_factor=terrain_factor,
        )
    else:
        terrain_started = time.monotonic()
        log.info("stage_started", stage="scan_shared_terrain", lidar_tiles=int(len(relevant_lidar)))
        terrain_report = terrain_statistics(
            aoi, lidar,
            sample_stride=args.terrain_sample_stride,
            requested_origin=args.terrain_origin_m,
            requested_factor=args.terrain_relief_factor,
            scale=layout.scale,
            vertical_exaggeration=args.vertical_exaggeration,
            layer_height_mm=args.layer_height,
            minimum_levels=args.minimum_terrain_levels,
        )
        terrain_origin = terrain_report["shared_origin_m_navd88"]
        terrain_factor = terrain_report["shared_relief"]["factor"]
        log.info(
            "stage_completed", stage="scan_shared_terrain",
            elapsed_seconds=time.monotonic() - terrain_started,
            tiles_read=terrain_report["tiles_read"],
            finite_cells_scanned=terrain_report["finite_cells_scanned"],
            exact_minimum_m_navd88=terrain_report["exact_minimum_m_navd88"],
            shared_origin_m_navd88=terrain_origin, terrain_relief_factor=terrain_factor,
        )

    # Delay all output creation until the geometry, component budget, caches and
    # terrain normalization have succeeded. A failed request is therefore safe
    # to correct and rerun with the same output directory.
    chunks_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    chunk_records = []
    coverage_parts = []
    root_python = (ROOT / ".venv/bin/python").absolute()
    generator = (ROOT / "scripts/generate_3mf.py").absolute()
    number_arg = lambda value: repr(float(value))
    shared_options = {
        "--scale": number_arg(layout.scale),
        "--terrain-origin-m": number_arg(terrain_origin),
        "--terrain-relief-factor": number_arg(terrain_factor),
        "--vertical-exaggeration": number_arg(args.vertical_exaggeration),
        "--minimum-terrain-levels": number_arg(args.minimum_terrain_levels),
        "--grid-step-mm": number_arg(args.grid_step_mm),
        "--layer-height": number_arg(args.layer_height),
        "--prime-tower": "off",
        "--source-padding-m": number_arg(args.source_padding_m),
        "--lidar-source": "cache",
        "--lidar-cache-dir": str(lidar),
        "--cache-dir": str(cache_dir),
        "--data-dir": str(data_dir),
        "--output-dir": str(generation_output_dir),
    }
    shared_flags = {
        "--offline": bool(args.offline),
        "--full-validation": bool(args.full_validation),
        "--slice": bool(args.slice),
    }
    for item in partition_parts:
        display_row = item["display_row"]
        column_index = item["column"]
        part_index = item["component"]
        x0_cells, x1_cells = item["x0_cells"], item["x1_cells"]
        y0_cells, y1_cells = item["y0_cells"], item["y1_cells"]
        x0, y0 = x0_cells * step_ft, y0_cells * step_ft
        part = item["geometry"]
        part_suffix = f"_p{part_index:02d}" if item["component_count_in_cell"] > 1 else ""
        chunk_id = f"{plan_id}_r{display_row:02d}_c{column_index:02d}{part_suffix}"
        chunk_wgs = gpd.GeoSeries([part], crs=2263).to_crs(4326).iloc[0]
        chunk_path = (chunks_dir / f"{chunk_id}.geojson").resolve()
        frame_path = (frames_dir / f"{chunk_id}.json").resolve()
        frame_origin = origin + layout.x_axis * x0 + layout.y_axis * y0
        frame = {
            "origin_ft": [float(value) for value in frame_origin],
            "x_axis": [float(value) for value in layout.x_axis],
            "y_axis": [float(value) for value in layout.y_axis],
            "size_mm": [
                round((x1_cells - x0_cells) * args.grid_step_mm, 9),
                round((y1_cells - y0_cells) * args.grid_step_mm, 9),
            ],
        }
        atomic_write(chunk_path, json.dumps(json.loads(shapely.to_geojson(chunk_wgs)), indent=2) + "\n")
        atomic_write(frame_path, json.dumps(frame, indent=2) + "\n")
        model_path = (model_dir / f"{chunk_id}.3mf").resolve()
        argv = [
            str(root_python), str(generator),
            "--bounding-polygon", "@" + str(chunk_path),
            "--print-frame", "@" + str(frame_path),
        ]
        for option, value in shared_options.items():
            argv.extend([option, value])
        argv.extend(["--job-id", chunk_id, "--output", str(model_path)])
        argv.extend(option for option, enabled in shared_flags.items() if enabled)
        coverage_parts.append(part)
        chunk_records.append({
            "id": chunk_id,
            "row": display_row,
            "column": column_index,
            "component": part_index,
            "component_count_in_cell": item["component_count_in_cell"],
            "area_sq_ft": float(part.area),
            "planned_polygon_wkb_hex_epsg2263": shapely.to_wkb(part, hex=True),
            "logical_cell_wkb_hex_epsg2263": item["logical_cell_wkb_hex_epsg2263"],
            "frame_grid_bounds": [x0_cells, y0_cells, x1_cells, y1_cells],
            "geometry_2263": part,
            "geometry_wgs84": chunk_wgs,
            "frame": frame,
            "polygon_file": str(chunk_path),
            "frame_file": str(frame_path),
            "output_3mf": str(model_path),
            "argv": argv,
            "command": shlex.join(argv),
        })
    if len(chunk_records) != len(partition_parts):
        raise RuntimeError("Internal error: not every planned polygon piece received a generation command")
    # Parse every emitted argv through the real generator before publishing the
    # plan. This catches option drift that an independent structural validator
    # cannot know about until the generator changes.
    from generate_3mf import build_config as build_generator_config
    from generate_3mf import parser as generator_parser
    for record in chunk_records:
        try:
            generated_args = generator_parser().parse_args(record["argv"][2:])
            generated_config, generated_job_id, generated_output = build_generator_config(generated_args)
        except (SystemExit, Exception) as error:
            raise SystemExit(
                f"Generator preflight rejected chunk {record['id']}: {type(error).__name__}: {error}"
            ) from error
        if (
            generated_job_id != record["id"]
            or generated_output != Path(record["output_3mf"])
            or generated_config["size_mm"] != record["frame"]["size_mm"]
        ):
            raise SystemExit(
                f"Generator preflight changed chunk {record['id']} identity, output, or frame size; "
                "the emitted command is not reproducible."
            )
        log.info(
            "generator_command_preflighted", chunk_id=record["id"],
            frame_size_mm=record["frame"]["size_mm"], output_3mf=record["output_3mf"],
        )
    coverage = shapely.union_all(coverage_parts)
    missing_area = float(shapely.difference(aoi, coverage).area)
    extra_area = float(shapely.difference(coverage, aoi).area)
    overlap_area = max(0.0, sum(part.area for part in coverage_parts) - coverage.area)
    tolerance = max(1e-6, float(aoi.area) * 1e-10)
    if missing_area > tolerance or extra_area > tolerance or overlap_area > tolerance:
        raise RuntimeError(
            f"Coverage verification failed: missing={missing_area:g}, extra={extra_area:g}, overlap={overlap_area:g} sq ft"
        )

    if partition_mode == "straight_grid":
        for report in x_reports:
            distance = report["offset_ft"]
            seam_geometries.append({
                **report, "routing_mode": "straight",
                "geometry": LineString([origin + layout.x_axis * distance,
                                        origin + layout.x_axis * distance + layout.y_axis * frame_height_ft]),
            })
        for report in y_reports:
            distance = report["offset_ft"]
            seam_geometries.append({
                **report, "routing_mode": "straight",
                "geometry": LineString([origin + layout.y_axis * distance,
                                        origin + layout.y_axis * distance + layout.x_axis * frame_width_ft]),
            })

    if seam_geometries and not args.geometric_only:
        seam_query = shapely.union_all([item["geometry"].buffer(35.0) for item in seam_geometries])
        named_roads = load_tiled_osm_roads(cache_dir / "new_york_osm", seam_query)
        for seam in seam_geometries:
            seam["nearby_named_roads"] = nearby_road_labels(
                shapely.intersection(seam["geometry"], aoi), named_roads
            )

    overview_features = []
    for record in chunk_records:
        overview_features.append({
            "type": "Feature",
            "geometry": json.loads(shapely.to_geojson(record["geometry_wgs84"])),
            "properties": {
                "id": record["id"], "row": record["row"], "column": record["column"],
                "component": record["component"], "area_sq_ft": record["area_sq_ft"],
                "size_mm": record["frame"]["size_mm"], "output_3mf": record["output_3mf"],
            },
        })
    overview_path = output_dir / "chunks.geojson"
    atomic_write(overview_path, json.dumps({"type": "FeatureCollection", "features": overview_features}, indent=2) + "\n")

    commands_path = output_dir / "commands.sh"
    preview_path = output_dir / "preview.svg"
    write_preview(preview_path, aoi, chunk_records, seam_geometries)

    serializable_chunks = [
        {key: value for key, value in record.items() if not key.startswith("geometry_")}
        for record in chunk_records
    ]
    serializable_seams = []
    for seam in seam_geometries:
        record = {key: value for key, value in seam.items() if key != "geometry"}
        record["geometry_wkb_hex_epsg2263"] = shapely.to_wkb(seam["geometry"], hex=True)
        serializable_seams.append(record)
    plan_path = output_dir / "plan.json"
    validation_path = output_dir / "validation.json"
    post_validation_path = output_dir / "post_generation_validation.json"
    validator = (ROOT / "scripts/validate_chunk_plan.py").absolute()
    static_validation_argv = [
        str(root_python), str(validator), "--plan", str(plan_path.resolve()),
        "--report", str(validation_path.resolve()),
    ]
    post_validation_argv = [
        str(root_python), str(validator), "--plan", str(plan_path.resolve()),
        "--check-generated", "--require-generated",
        "--report", str(post_validation_path.resolve()),
    ]
    commands_text = (
        "#!/bin/sh\nset -eu\n\n"
        + shlex.join(static_validation_argv)
        + "\n\n"
        + "\n\n".join(record["command"] for record in chunk_records)
        + "\n\n"
        + shlex.join(post_validation_argv)
        + "\n"
    )
    atomic_write(commands_path, commands_text)
    commands_path.chmod(commands_path.stat().st_mode | 0o111)

    plan = {
        "schema_version": PLAN_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plan_id": plan_id,
        "request": {
            "maximum_chunks": args.max_chunks,
            "chunk_size_mm": list(args.chunk_size_mm),
            "requested_scale": args.scale,
            "geometric_only": args.geometric_only,
            "seam_mode": args.seam_mode,
            "path_step_mm": args.path_step_mm,
            "max_seam_deviation_mm": args.max_seam_deviation_mm,
            "bounding_polygon_wgs84": json.loads(shapely.to_geojson(aoi_wgs)),
            "target_wkb_hex_epsg2263": shapely.to_wkb(aoi, hex=True),
        },
        "generation": {
            "python": str(root_python),
            "script": str(generator),
            "shared_options": shared_options,
            "shared_flags": shared_flags,
            "static_validation_argv": static_validation_argv,
            "post_generation_validation_argv": post_validation_argv,
        },
        "validated_caches": {
            name: {
                "status": manifest.get("status"),
                "production_ready": manifest.get("production_ready"),
                "completed_at": manifest.get("completed_at"),
                "cache_format_version": manifest.get("cache_format_version"),
            }
            for name, manifest in cache_manifests.items()
        },
        "layout": {
            "scale_denominator": layout.scale,
            "minimum_feasible_scale": layout.minimum_scale,
            "orientation_deg_east_of_north": layout.orientation_deg,
            "dominant_road_orientation_deg_east_of_north": road_orientation,
            "x_axis": layout.x_axis.tolist(), "y_axis": layout.y_axis.tolist(),
            "global_origin_ft": origin.tolist(),
            "grid_step_mm": args.grid_step_mm,
            "partition_mode": partition_mode,
            "grid": {"columns": columns, "rows": rows},
            "cut_grid_cells": {"x": x_cuts, "y": y_cuts},
            "orientation_candidates": orientation_report,
        },
        "semantic_layers_loaded": layer_counts,
        "terrain": terrain_report,
        "seams": serializable_seams,
        "coverage": {
            "requested_area_sq_ft": float(aoi.area),
            "covered_area_sq_ft": float(coverage.area),
            "missing_area_sq_ft": missing_area,
            "extra_area_sq_ft": extra_area,
            "overlap_area_sq_ft": overlap_area,
            "tolerance_sq_ft": tolerance,
            "result": "passed",
        },
        "generator_preflight": {
            "result": "passed",
            "commands_parsed": len(chunk_records),
            "entrypoint": str(generator),
        },
        "chunk_count": len(chunk_records),
        "chunks": serializable_chunks,
        "files": {
            "plan": str(plan_path.resolve()),
            "commands": str(commands_path.resolve()),
            "overview_geojson": str(overview_path.resolve()),
            "preview_svg": str(preview_path.resolve()),
            "validation": str(validation_path.resolve()),
            "post_generation_validation": str(post_validation_path.resolve()),
            "planner_log": str((output_dir / "logs/planner.jsonl").resolve()),
        },
    }
    atomic_write(plan_path, json.dumps(plan, indent=2) + "\n")
    validation = validate_plan(plan_path)
    write_report(validation_path, validation)
    if validation["result"] != "passed":
        details = "; ".join(
            f"{issue['code']}: {issue['message']}" for issue in validation["issues"][:8]
        )
        raise SystemExit(
            f"Chunk-plan validation failed with {validation['error_count']} error(s). "
            f"See {validation_path.resolve()}. {details}"
        )
    log.info(
        "static_validation_completed", result=validation["result"],
        errors=validation["error_count"], warnings=validation["warning_count"],
        validation=str(validation_path.resolve()),
    )
    log.info(
        "planner_completed", plan_id=plan_id, chunks=len(chunk_records),
        scale=layout.scale, orientation_deg_east_of_north=layout.orientation_deg,
        partition_mode=partition_mode,
        buildings_cut=sum(item["buildings_cut"] for item in serializable_seams),
        coverage="passed", plan=str(plan_path.resolve()),
        commands=str(commands_path.resolve()), preview=str(preview_path.resolve()),
        validation=str(validation_path.resolve()),
    )
    event_recorder.write(output_dir / "logs/planner.jsonl")


if __name__ == "__main__":
    main()
