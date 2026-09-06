#!/usr/bin/env python3
"""Validate a multi-chunk plan and optionally compare generated seam heights."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
import shlex
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import shapely
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Polygon, shape


FT = 0.3048006096012192
MIN_FRAME_MM = 20.0
MAX_FRAME_MM = 250.0
KNOWN_SHARED_FLAGS = {"--offline", "--full-validation", "--slice"}
NUMERIC_SHARED_OPTIONS = {
    "--scale", "--terrain-origin-m", "--terrain-relief-factor",
    "--vertical-exaggeration", "--minimum-terrain-levels", "--grid-step-mm",
    "--layer-height", "--source-padding-m",
}
PATH_SHARED_OPTIONS = {"--lidar-cache-dir", "--cache-dir", "--data-dir", "--output-dir"}


@dataclass
class Diagnostics:
    issues: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def add(self, severity: str, code: str, message: str, **context: Any) -> None:
        self.issues.append({
            "severity": severity,
            "code": code,
            "message": message,
            "context": context,
        })

    def error(self, code: str, message: str, **context: Any) -> None:
        self.add("error", code, message, **context)

    def warning(self, code: str, message: str, **context: Any) -> None:
        self.add("warning", code, message, **context)

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [issue for issue in self.issues if issue["severity"] == "error"]

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return [issue for issue in self.issues if issue["severity"] == "warning"]


def _option_values(argv: list[str], name: str) -> list[str | None]:
    return [argv[index + 1] if index + 1 < len(argv) else None
            for index, value in enumerate(argv) if value == name]


def _option(argv: list[str], name: str) -> str | None:
    values = _option_values(argv, name)
    return values[0] if len(values) == 1 else None


def _float_option(argv: list[str], name: str) -> float | None:
    value = _option(argv, name)
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _read_geometry(path: Path):
    payload = json.loads(path.read_text())
    if payload.get("type") == "Feature":
        payload = payload.get("geometry") or {}
    return shape(payload), payload.get("type")


def _polygon_parts(geometry) -> list:
    return [part for part in shapely.get_parts(geometry) if part.geom_type == "Polygon" and not part.is_empty]


def _linear_parts(geometry) -> list:
    return [
        part for part in shapely.get_parts(geometry)
        if part.geom_type in {"LineString", "LinearRing"} and not part.is_empty
    ]


def _frame_geometry(origin: np.ndarray, x_axis: np.ndarray, y_axis: np.ndarray,
                    x0: float, x1: float, y0: float, y1: float) -> Polygon:
    return Polygon([
        origin + x_axis * x0 + y_axis * y0,
        origin + x_axis * x1 + y_axis * y0,
        origin + x_axis * x1 + y_axis * y1,
        origin + x_axis * x0 + y_axis * y1,
    ])


def _normalized_shared_options(plan: dict) -> dict[str, Any]:
    raw = plan.get("generation", {}).get("shared_options", {})
    if not isinstance(raw, dict):
        return {}
    return {
        key if str(key).startswith("--") else "--" + str(key).replace("_", "-"): value
        for key, value in raw.items()
    }


def _normalized_shared_flags(plan: dict) -> dict[str, bool]:
    raw = plan.get("generation", {}).get("shared_flags", {})
    if isinstance(raw, list):
        enabled = {
            value if str(value).startswith("--") else "--" + str(value).replace("_", "-")
            for value in raw
        }
        return {flag: flag in enabled for flag in KNOWN_SHARED_FLAGS | enabled}
    if isinstance(raw, dict):
        result = {}
        for key, value in raw.items():
            option = key if str(key).startswith("--") else "--" + str(key).replace("_", "-")
            result[option] = bool(value)
        return result
    return {}


def _same_serialized_number(actual: str | None, expected: Any) -> bool:
    if actual is None:
        return False
    try:
        expected_float = float(expected)
        actual_float = float(actual)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(actual_float)
        and math.isfinite(expected_float)
        and math.isclose(actual_float, expected_float, rel_tol=5e-15, abs_tol=1e-12)
    )


def _manufacturing_grid_has_cell(
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
    """Independently reproduce the generator's polygon cell-center occupancy."""
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


def _component_summary(geometry, *, source_crs: int = 2263) -> list[dict]:
    parts = _polygon_parts(geometry)
    if not parts:
        return []
    points = gpd.GeoSeries([part.representative_point() for part in parts], crs=source_crs).to_crs(4326)
    return [
        {
            "area_sq_ft": float(part.area),
            "bounds_epsg2263_ft": [float(value) for value in part.bounds],
            "representative_wgs84": [float(points.iloc[index].x), float(points.iloc[index].y)],
        }
        for index, part in enumerate(parts[:10])
    ]


def _graph_components(count: int, edges: list[tuple[int, int]]) -> list[list[int]]:
    neighbors = [set() for _ in range(count)]
    for left, right in edges:
        neighbors[left].add(right)
        neighbors[right].add(left)
    remaining = set(range(count))
    result = []
    while remaining:
        start = min(remaining)
        stack = [start]
        component = []
        remaining.remove(start)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in sorted(neighbors[current]):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)
        result.append(sorted(component))
    return result


def _validate_frame(
    diagnostics: Diagnostics,
    record: dict,
    geometry,
    *,
    scale: float,
    grid_step_mm: float,
    global_origin: np.ndarray,
    common_x: np.ndarray,
    common_y: np.ndarray,
    cut_x: list[int],
    cut_y: list[int],
    grid_columns: int,
    grid_rows: int,
    partition_mode: str,
) -> dict | None:
    chunk_id = record.get("id", "<missing-id>")
    frame_path = Path(record.get("frame_file", ""))
    try:
        frame = json.loads(frame_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        diagnostics.error(
            "FRAME_FILE_UNREADABLE", f"Chunk {chunk_id} print-frame file cannot be read: {error}",
            chunk_id=chunk_id, frame_file=str(frame_path),
        )
        return None
    if frame != record.get("frame"):
        diagnostics.error(
            "FRAME_RECORD_MISMATCH",
            f"Chunk {chunk_id} frame file differs from its plan.json frame record.",
            chunk_id=chunk_id, frame_file=str(frame_path),
        )
    try:
        origin = np.asarray(frame["origin_ft"], dtype=float)
        x_axis = np.asarray(frame["x_axis"], dtype=float)
        y_axis = np.asarray(frame["y_axis"], dtype=float)
        size = np.asarray(frame["size_mm"], dtype=float)
    except (KeyError, TypeError, ValueError) as error:
        diagnostics.error(
            "FRAME_SCHEMA_INVALID", f"Chunk {chunk_id} frame has invalid numeric fields: {error}",
            chunk_id=chunk_id,
        )
        return None
    if any(value.shape != (2,) for value in [origin, x_axis, y_axis, size]) or not all(
        np.isfinite(value).all() for value in [origin, x_axis, y_axis, size]
    ):
        diagnostics.error(
            "FRAME_SCHEMA_INVALID",
            f"Chunk {chunk_id} frame origin, axes and size must be finite numeric pairs.",
            chunk_id=chunk_id,
        )
        return None
    determinant = float(x_axis[0] * y_axis[1] - x_axis[1] * y_axis[0])
    if not (
        abs(np.linalg.norm(x_axis) - 1) <= 1e-8
        and abs(np.linalg.norm(y_axis) - 1) <= 1e-8
        and abs(float(x_axis @ y_axis)) <= 1e-8
        and determinant >= 1 - 1e-8
    ):
        diagnostics.error(
            "FRAME_AXES_INVALID",
            f"Chunk {chunk_id} axes are not an orthonormal right-handed frame.",
            chunk_id=chunk_id, x_axis=x_axis.tolist(), y_axis=y_axis.tolist(), determinant=determinant,
        )
    if not (np.allclose(x_axis, common_x, rtol=0, atol=1e-9) and np.allclose(y_axis, common_y, rtol=0, atol=1e-9)):
        diagnostics.error(
            "FRAME_AXES_DIFFER",
            f"Chunk {chunk_id} uses different XY axes from the plan's shared frame.",
            chunk_id=chunk_id, x_axis=x_axis.tolist(), y_axis=y_axis.tolist(),
            expected_x_axis=common_x.tolist(), expected_y_axis=common_y.tolist(),
        )
    if np.any(size < MIN_FRAME_MM - 1e-8) or np.any(size > MAX_FRAME_MM + 1e-8):
        diagnostics.error(
            "FRAME_SIZE_OUT_OF_GENERATOR_RANGE",
            f"Chunk {chunk_id} frame is {size[0]:g}x{size[1]:g} mm; generator dimensions must be 20-250 mm.",
            chunk_id=chunk_id, size_mm=size.tolist(),
        )
    request_limit = np.asarray(record.get("requested_chunk_size_mm", [MAX_FRAME_MM, MAX_FRAME_MM]), dtype=float)
    if request_limit.shape == (2,) and np.any(size > request_limit + 1e-8):
        diagnostics.error(
            "FRAME_EXCEEDS_REQUESTED_CHUNK_SIZE",
            f"Chunk {chunk_id} frame exceeds the requested {request_limit[0]:g}x{request_limit[1]:g} mm maximum.",
            chunk_id=chunk_id, size_mm=size.tolist(), maximum_mm=request_limit.tolist(),
        )
    cells = size / grid_step_mm
    if not np.allclose(cells, np.rint(cells), rtol=0, atol=1e-6):
        diagnostics.error(
            "FRAME_SIZE_OFF_MANUFACTURING_GRID",
            f"Chunk {chunk_id} dimensions are not multiples of the {grid_step_mm:g} mm grid.",
            chunk_id=chunk_id, size_mm=size.tolist(), grid_step_mm=grid_step_mm,
        )
    step_ft = grid_step_mm * scale / (FT * 1000.0)
    displacement = origin - global_origin
    offset_cells = np.asarray([
        float(displacement @ common_x) / step_ft,
        float(displacement @ common_y) / step_ft,
    ])
    if not np.allclose(offset_cells, np.rint(offset_cells), rtol=0, atol=2e-5):
        diagnostics.error(
            "FRAME_ORIGIN_OFF_SHARED_GRID",
            f"Chunk {chunk_id} origin is not an integer offset on the shared manufacturing grid.",
            chunk_id=chunk_id, offset_grid_cells=offset_cells.tolist(),
        )
    try:
        column = int(record["column"])
        display_row = int(record["row"])
        if not (1 <= column <= grid_columns and 1 <= display_row <= grid_rows):
            raise IndexError("row/column outside declared grid")
        south_row = grid_rows - display_row
        if partition_mode == "semantic_paths":
            declared_bounds = np.asarray(record.get("frame_grid_bounds"), dtype=float)
            actual_bounds = np.asarray([
                offset_cells[0], offset_cells[1],
                offset_cells[0] + cells[0], offset_cells[1] + cells[1],
            ])
            if (
                declared_bounds.shape != (4,) or not np.isfinite(declared_bounds).all()
                or not np.allclose(declared_bounds, np.rint(declared_bounds), rtol=0, atol=1e-8)
                or not np.allclose(declared_bounds, actual_bounds, rtol=0, atol=2e-5)
            ):
                diagnostics.error(
                    "FRAME_GRID_BOUNDS_MISMATCH",
                    f"Free-form chunk {chunk_id} frame_grid_bounds do not match its shared-grid origin and size.",
                    chunk_id=chunk_id, declared_grid_bounds=record.get("frame_grid_bounds"),
                    actual_grid_bounds=actual_bounds.tolist(),
                )
        else:
            expected_offset = np.asarray([cut_x[column - 1], cut_y[south_row]], dtype=float)
            expected_size = np.asarray([
                (cut_x[column] - cut_x[column - 1]) * grid_step_mm,
                (cut_y[south_row + 1] - cut_y[south_row]) * grid_step_mm,
            ])
            if not np.allclose(offset_cells, expected_offset, rtol=0, atol=2e-5):
                diagnostics.error(
                    "FRAME_ORIGIN_WRONG_GRID_CELL",
                    f"Chunk {chunk_id} origin does not match row {display_row}, column {column}.",
                    chunk_id=chunk_id, offset_grid_cells=offset_cells.tolist(),
                    expected_grid_cells=expected_offset.tolist(),
                )
            if not np.allclose(size, expected_size, rtol=0, atol=1e-8):
                diagnostics.error(
                    "FRAME_SIZE_WRONG_GRID_CELL",
                    f"Chunk {chunk_id} frame is {size[0]:g}x{size[1]:g} mm, but row "
                    f"{display_row}, column {column} spans {expected_size[0]:g}x{expected_size[1]:g} mm.",
                    chunk_id=chunk_id, size_mm=size.tolist(), expected_size_mm=expected_size.tolist(),
                    row=display_row, column=column,
                )
    except (KeyError, IndexError, TypeError, ValueError):
        diagnostics.error(
            "CHUNK_GRID_INDEX_INVALID",
            f"Chunk {chunk_id} has an invalid row or column index.",
            chunk_id=chunk_id, row=record.get("row"), column=record.get("column"),
        )
        return None
    coordinates = shapely.get_coordinates(geometry)
    relative = coordinates - origin
    local_x = relative @ x_axis
    local_y = relative @ y_axis
    extent_ft = size / (FT * 1000.0 / scale)
    containment_tolerance = max(step_ft * 2e-5, 1e-6)
    overflow = {
        "west": max(0.0, -float(local_x.min())),
        "south": max(0.0, -float(local_y.min())),
        "east": max(0.0, float(local_x.max() - extent_ft[0])),
        "north": max(0.0, float(local_y.max() - extent_ft[1])),
    }
    if max(overflow.values()) > containment_tolerance:
        diagnostics.error(
            "POLYGON_OUTSIDE_PRINT_FRAME",
            f"Chunk {chunk_id} polygon extends outside its print frame by up to {max(overflow.values()):.6g} ft.",
            chunk_id=chunk_id, overflow_ft=overflow, tolerance_ft=containment_tolerance,
        )
    shape_cells = np.rint(cells).astype(int)
    if np.all(shape_cells > 0) and not _manufacturing_grid_has_cell(
        geometry,
        frame_origin=origin,
        x_axis=x_axis,
        y_axis=y_axis,
        scale=scale,
        grid_step_mm=grid_step_mm,
        width_cells=int(shape_cells[0]),
        height_cells=int(shape_cells[1]),
    ):
        diagnostics.error(
            "CHUNK_HAS_NO_MANUFACTURING_CELLS",
            f"Chunk {chunk_id} contains no manufacturing-grid cell center and would fail during field generation.",
            chunk_id=chunk_id,
            grid_step_mm=grid_step_mm,
            printed_area_sq_mm=float(geometry.area * (FT * 1000.0 / scale) ** 2),
        )
    return {
        "origin": origin,
        "x_axis": x_axis,
        "y_axis": y_axis,
        "size_mm": size,
        "offset_cells": np.rint(offset_cells).astype(int),
        "shape_cells": shape_cells,
        "cell": (south_row, column - 1),
    }


def _validate_command(
    diagnostics: Diagnostics,
    record: dict,
    *,
    expected_scale: float,
    expected_origin: float,
    expected_factor: float,
    expected_grid_step: float,
    generation: dict,
    shared_options: dict[str, Any],
    shared_flags: dict[str, bool],
) -> None:
    chunk_id = record.get("id", "<missing-id>")
    argv = record.get("argv")
    if not isinstance(argv, list) or len(argv) < 3 or not all(isinstance(value, str) for value in argv):
        diagnostics.error(
            "COMMAND_ARGV_INVALID", f"Chunk {chunk_id} does not contain a usable argv list.",
            chunk_id=chunk_id,
        )
        return
    if record.get("command") != shlex.join(argv):
        diagnostics.error(
            "COMMAND_STRING_MISMATCH",
            f"Chunk {chunk_id} shell command is not the shell-quoted form of its argv list.",
            chunk_id=chunk_id,
        )
    expected_python = generation.get("python")
    expected_script = generation.get("script")
    if expected_python is not None and Path(argv[0]).resolve() != Path(str(expected_python)).resolve():
        diagnostics.error(
            "COMMAND_WRONG_PYTHON",
            f"Chunk {chunk_id} uses Python {argv[0]!r}, expected {str(expected_python)!r}.",
            chunk_id=chunk_id, actual=argv[0], expected=str(expected_python),
        )
    if expected_script is not None and Path(argv[1]).resolve() != Path(str(expected_script)).resolve():
        diagnostics.error(
            "COMMAND_WRONG_ENTRYPOINT",
            f"Chunk {chunk_id} invokes {argv[1]!r}, expected {str(expected_script)!r}.",
            chunk_id=chunk_id, entrypoint=argv[1], expected=str(expected_script),
        )
    elif Path(argv[1]).name != "generate_3mf.py":
        diagnostics.error(
            "COMMAND_WRONG_ENTRYPOINT",
            f"Chunk {chunk_id} command does not invoke generate_3mf.py.",
            chunk_id=chunk_id, entrypoint=argv[1],
        )
    required = {
        "--bounding-polygon": record.get("polygon_file"),
        "--print-frame": record.get("frame_file"),
        "--job-id": chunk_id,
        "--output": record.get("output_3mf"),
    }
    for option, expected in required.items():
        values = _option_values(argv, option)
        if len(values) != 1:
            diagnostics.error(
                "COMMAND_OPTION_CARDINALITY",
                f"Chunk {chunk_id} must contain {option} exactly once; found {len(values)} occurrence(s).",
                chunk_id=chunk_id, option=option, occurrences=len(values),
            )
        actual = _option(argv, option)
        if option in {"--bounding-polygon", "--print-frame"} and actual is not None:
            actual = actual.removeprefix("@")
        if actual != str(expected):
            diagnostics.error(
                "COMMAND_PATH_OR_ID_MISMATCH",
                f"Chunk {chunk_id} {option} value does not match its plan record.",
                chunk_id=chunk_id, option=option, actual=actual, expected=str(expected),
            )
    numeric = {
        "--scale": expected_scale,
        "--terrain-origin-m": expected_origin,
        "--terrain-relief-factor": expected_factor,
        "--grid-step-mm": expected_grid_step,
    }
    for option, expected in numeric.items():
        actual_text = _option(argv, option)
        actual = _float_option(argv, option)
        if not _same_serialized_number(actual_text, expected):
            diagnostics.error(
                "COMMAND_SHARED_VALUE_MISMATCH",
                f"Chunk {chunk_id} {option}={actual!r}, expected the shared value {expected:g}.",
                chunk_id=chunk_id, option=option, actual=actual, expected=expected,
            )
    for option, expected in shared_options.items():
        values = _option_values(argv, option)
        if len(values) != 1:
            diagnostics.error(
                "COMMAND_SHARED_OPTION_CARDINALITY",
                f"Chunk {chunk_id} must contain shared option {option} exactly once; found {len(values)} occurrence(s).",
                chunk_id=chunk_id, option=option, occurrences=len(values), expected=expected,
            )
            continue
        actual = values[0]
        if option in NUMERIC_SHARED_OPTIONS:
            matches = _same_serialized_number(actual, expected)
        elif option in PATH_SHARED_OPTIONS:
            matches = actual is not None and Path(actual).resolve() == Path(str(expected)).resolve()
        else:
            matches = actual == str(expected)
        if not matches:
            diagnostics.error(
                "COMMAND_SHARED_OPTION_MISMATCH",
                f"Chunk {chunk_id} {option}={actual!r}, expected {str(expected)!r} from plan.generation.shared_options.",
                chunk_id=chunk_id, option=option, actual=actual, expected=expected,
            )
    for flag, expected_enabled in shared_flags.items():
        occurrences = argv.count(flag)
        if occurrences > 1 or bool(occurrences) != expected_enabled:
            diagnostics.error(
                "COMMAND_SHARED_FLAG_MISMATCH",
                f"Chunk {chunk_id} flag {flag} is present {occurrences} time(s); expected "
                f"{'present once' if expected_enabled else 'absent'}.",
                chunk_id=chunk_id, flag=flag, occurrences=occurrences,
                expected_enabled=expected_enabled,
            )
    if "--length-mm" in argv or "--size-mm" in argv:
        diagnostics.error(
            "COMMAND_PER_CHUNK_SCALE_RISK",
            f"Chunk {chunk_id} command uses --length-mm/--size-mm; multi-chunk polygon jobs must use the shared --scale and --print-frame.",
            chunk_id=chunk_id,
        )
    if _option(argv, "--prime-tower") != "off":
        diagnostics.error(
            "COMMAND_PRIME_TOWER_NOT_OFF",
            f"Chunk {chunk_id} must explicitly disable the prime tower for consistent full-frame placement.",
            chunk_id=chunk_id,
        )


def _load_generated_edges(job_dir: Path) -> dict:
    config_path = job_dir / "config.json"
    fields_path = job_dir / "work/map_fields.npz"
    report_path = job_dir / "work/field_build_report.json"
    config = json.loads(config_path.read_text())
    field_report = json.loads(report_path.read_text())
    with np.load(fields_path, allow_pickle=False) as fields:
        height = fields["height_mm"]
        ground = fields["ground_mm"]
        material = fields["material"]
        mask = fields["aoi_mask"].astype(bool)
        shapes = {tuple(value.shape) for value in [height, ground, material, mask]}
        if len(shapes) != 1 or any(value.ndim != 2 for value in [height, ground, material, mask]):
            raise ValueError(
                "height_mm, ground_mm, material and aoi_mask must be same-shaped 2-D arrays; "
                f"got height={height.shape}, ground={ground.shape}, material={material.shape}, mask={mask.shape}"
            )
        if min(height.shape) < 2:
            raise ValueError(f"field grid {height.shape} is too small for edge extrapolation")
        if not np.isfinite(height[mask]).all() or not np.isfinite(ground[mask]).all():
            raise ValueError("height_mm/ground_mm contain non-finite values inside aoi_mask")
        if not np.isin(material[mask], [0, 1, 2, 3]).all():
            raise ValueError("material contains values outside 0-3 inside aoi_mask")

        def vertical(array, east):
            return np.stack([array[:, -1 if east else 0], array[:, -2 if east else 1]], axis=1).copy()

        def horizontal(array, north):
            return np.stack([array[0 if north else -1], array[1 if north else -2]], axis=1).copy()

        edges = {}
        for side, vertical_side, positive in [
            ("west", True, False), ("east", True, True),
            ("south", False, False), ("north", False, True),
        ]:
            get = vertical if vertical_side else horizontal
            edges[side] = {
                "height": get(height, positive),
                "ground": get(ground, positive),
                "material": get(material, positive),
                "mask": get(mask, positive),
            }
    return {
        "config": config, "field_report": field_report, "edges": edges,
        "shape": list(height.shape), "job_dir": job_dir,
    }


def _edge_values(edge: dict, indices: np.ndarray, *, reverse: bool = False):
    selected = edge[indices]
    if reverse:
        selected = selected[::-1]
    return selected


def _compare_generated_pair(
    left: dict,
    right: dict,
    left_frame: dict,
    right_frame: dict,
) -> tuple[str, np.ndarray, np.ndarray, np.ndarray, int] | None:
    lo = left_frame["offset_cells"]
    ro = right_frame["offset_cells"]
    ls = left_frame["shape_cells"]
    rs = right_frame["shape_cells"]
    if lo[0] + ls[0] == ro[0]:
        first, second, side_first, side_second, axis = left, right, "east", "west", "vertical"
        first_frame, second_frame = left_frame, right_frame
    elif ro[0] + rs[0] == lo[0]:
        first, second, side_first, side_second, axis = right, left, "east", "west", "vertical"
        first_frame, second_frame = right_frame, left_frame
    elif lo[1] + ls[1] == ro[1]:
        first, second, side_first, side_second, axis = left, right, "north", "south", "horizontal"
        first_frame, second_frame = left_frame, right_frame
    elif ro[1] + rs[1] == lo[1]:
        first, second, side_first, side_second, axis = right, left, "north", "south", "horizontal"
        first_frame, second_frame = right_frame, left_frame
    else:
        return None
    varying = 1 if axis == "vertical" else 0
    start = max(first_frame["offset_cells"][varying], second_frame["offset_cells"][varying])
    end = min(
        first_frame["offset_cells"][varying] + first_frame["shape_cells"][varying],
        second_frame["offset_cells"][varying] + second_frame["shape_cells"][varying],
    )
    if end <= start:
        return None

    def indices(frame):
        values = np.arange(start, end) - frame["offset_cells"][varying]
        if axis == "vertical":
            values = frame["shape_cells"][1] - 1 - values
        return values.astype(int)

    first_indices = indices(first_frame)
    second_indices = indices(second_frame)
    first_edge = first["edges"][side_first]
    second_edge = second["edges"][side_second]
    valid = first_edge["mask"][first_indices, 0] & second_edge["mask"][second_indices, 0]
    if not valid.any():
        return axis, np.asarray([]), np.asarray([]), np.asarray([]), 0

    def extrapolate(key):
        a = first_edge[key][first_indices][valid]
        b = second_edge[key][second_indices][valid]
        a_mask = first_edge["mask"][first_indices][valid]
        b_mask = second_edge["mask"][second_indices][valid]
        can_extrapolate = a_mask[:, 1] & b_mask[:, 1]
        a_boundary = np.where(can_extrapolate, 1.5 * a[:, 0] - 0.5 * a[:, 1], a[:, 0])
        b_boundary = np.where(can_extrapolate, 1.5 * b[:, 0] - 0.5 * b[:, 1], b[:, 0])
        return np.abs(a_boundary - b_boundary), can_extrapolate

    ground_delta, extrapolated = extrapolate("ground")
    height_delta, _ = extrapolate("height")
    first_material = first_edge["material"][first_indices, 0][valid]
    second_material = second_edge["material"][second_indices, 0][valid]
    material_match = first_material == second_material
    return axis, ground_delta, height_delta, material_match, int(extrapolated.sum())


def _compare_generated_pair_arbitrary(
    left: dict,
    right: dict,
    left_frame: dict,
    right_frame: dict,
    left_geometry,
    right_geometry,
    *,
    step_ft: float,
) -> tuple[str, np.ndarray, np.ndarray, np.ndarray, int] | None:
    """Compare fields by extrapolating from both sides of a curved shared seam."""
    shared = shapely.intersection(left_geometry.boundary, right_geometry.boundary)
    parts = _linear_parts(shared)
    if not parts:
        return None
    sampled_parts = [shapely.segmentize(part, max_segment_length=step_ft * 2.0) for part in parts]

    def load_fields(generated):
        with np.load(generated["job_dir"] / "work/map_fields.npz", allow_pickle=False) as fields:
            return {
                "height": fields["height_mm"].copy(),
                "ground": fields["ground_mm"].copy(),
                "material": fields["material"].copy(),
                "mask": fields["aoi_mask"].astype(bool, copy=True),
            }

    left_fields = load_fields(left)
    right_fields = load_fields(right)

    def fetch_pair(fields, frame, point_cells, normal, sign):
        found = []
        used = set()
        for depth in (0.65, 1.25, 2.0, 3.0, 4.25, 5.75, 7.5):
            candidate = point_cells + normal * (sign * depth)
            cell_x, cell_y = int(math.floor(candidate[0])), int(math.floor(candidate[1]))
            if (cell_x, cell_y) in used:
                continue
            used.add((cell_x, cell_y))
            local_x = cell_x - int(frame["offset_cells"][0])
            local_y = cell_y - int(frame["offset_cells"][1])
            row = int(frame["shape_cells"][1]) - 1 - local_y
            column = local_x
            if not (0 <= row < fields["mask"].shape[0] and 0 <= column < fields["mask"].shape[1]):
                continue
            if not fields["mask"][row, column]:
                continue
            center = np.asarray([cell_x + 0.5, cell_y + 0.5])
            distance = float((center - point_cells) @ (normal * sign))
            if distance <= 0:
                continue
            found.append((distance, row, column))
            if len(found) == 2:
                break
        if len(found) < 2:
            return None
        found.sort()

        def extrapolate(key):
            d0, r0, c0 = found[0]
            d1, r1, c1 = found[1]
            v0 = float(fields[key][r0, c0])
            v1 = float(fields[key][r1, c1])
            return v0 - d0 * (v1 - v0) / max(d1 - d0, 1e-9)

        _, row, column = found[0]
        return extrapolate("ground"), extrapolate("height"), int(fields["material"][row, column])

    ground_delta, height_delta, material_match = [], [], []
    seen = set()
    for part in sampled_parts:
        coordinates = shapely.get_coordinates(part)
        if len(coordinates) < 2:
            continue
        for index, coordinate in enumerate(coordinates[1:-1], 1):
            tangent_world = coordinates[index + 1] - coordinates[index - 1]
            tangent = np.asarray([
                float(tangent_world @ left_frame["x_axis"]),
                float(tangent_world @ left_frame["y_axis"]),
            ])
            length = float(np.linalg.norm(tangent))
            if length <= 1e-12:
                continue
            tangent /= length
            normal = np.asarray([-tangent[1], tangent[0]])
            relative = coordinate - (
                left_frame["origin"]
                - left_frame["x_axis"] * left_frame["offset_cells"][0] * step_ft
                - left_frame["y_axis"] * left_frame["offset_cells"][1] * step_ft
            )
            point_cells = np.asarray([
                float(relative @ left_frame["x_axis"]) / step_ft,
                float(relative @ left_frame["y_axis"]) / step_ft,
            ])
            key = tuple(np.rint(point_cells * 4).astype(int))
            if key in seen:
                continue
            seen.add(key)
            pair = None
            for sign in (1.0, -1.0):
                left_value = fetch_pair(left_fields, left_frame, point_cells, normal, sign)
                right_value = fetch_pair(right_fields, right_frame, point_cells, normal, -sign)
                if left_value is not None and right_value is not None:
                    pair = left_value, right_value
                    break
            if pair is None:
                continue
            left_value, right_value = pair
            ground_delta.append(abs(left_value[0] - right_value[0]))
            height_delta.append(abs(left_value[1] - right_value[1]))
            material_match.append(left_value[2] == right_value[2])
    return (
        "free_form",
        np.asarray(ground_delta, dtype=float),
        np.asarray(height_delta, dtype=float),
        np.asarray(material_match, dtype=bool),
        len(ground_delta),
    )


def _validate_generated(
    diagnostics: Diagnostics,
    records: list[dict],
    frames: list[dict | None],
    geometries: list,
    adjacency: list[tuple[int, int]],
    *,
    expected_scale: float,
    expected_origin: float,
    expected_factor: float,
    shared_options: dict[str, Any],
    height_tolerance_mm: float,
    maximum_height_tolerance_mm: float,
    require_generated: bool,
    partition_mode: str,
    step_ft: float,
) -> None:
    jobs: list[Path | None] = []
    missing = []
    for record in records:
        argv = record.get("argv", [])
        output_dir = _option(argv, "--output-dir")
        job_id = _option(argv, "--job-id")
        job_dir = Path(output_dir) / "jobs" / job_id if output_dir and job_id else None
        required = ["config.json", "work/map_fields.npz", "work/field_build_report.json"]
        if job_dir is None or any(not (job_dir / relative).is_file() for relative in required):
            jobs.append(None)
            missing.append(record.get("id"))
        else:
            jobs.append(job_dir)
    if missing:
        message = (
            f"Generated field artifacts are missing for {len(missing)} chunk(s); actual edge heights cannot be compared."
        )
        context = {"missing_chunks": missing[:20], "missing_count": len(missing)}
        if require_generated:
            diagnostics.error("GENERATED_CHUNKS_MISSING", message, **context)
        else:
            diagnostics.warning("GENERATED_CHUNKS_MISSING", message, **context)

    generated_records: list[dict | None] = [None] * len(records)
    config_option_keys = {
        "--scale": "scale_denominator",
        "--terrain-origin-m": "terrain_origin_m",
        "--terrain-relief-factor": "terrain_relief_factor",
        "--vertical-exaggeration": "vertical_exaggeration",
        "--minimum-terrain-levels": "minimum_terrain_relief_levels",
        "--grid-step-mm": "grid_step_mm",
        "--layer-height": "layer_height_mm",
        "--source-padding-m": "source_padding_m",
        "--lidar-source": "lidar_source",
        "--lidar-cache-dir": "lidar_cache_dir",
        "--cache-dir": "cache_dir",
    }
    for index, (record, job_dir, frame) in enumerate(zip(records, jobs, frames)):
        if job_dir is None or frame is None:
            continue
        chunk_id = record.get("id", f"index-{index}")
        try:
            generated = _load_generated_edges(job_dir)
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
            diagnostics.error(
                "GENERATED_FIELDS_UNREADABLE",
                f"Could not read generated fields for chunk {chunk_id}: {error}",
                chunk_id=chunk_id, job_directory=str(job_dir),
            )
            continue
        generated_records[index] = generated
        config = generated["config"]
        field_report = generated["field_report"]
        actual = {
            "scale": config.get("scale_denominator"),
            "terrain_origin_m": field_report.get("vertical_origin_m_navd88"),
            "terrain_relief_factor": field_report.get("terrain_relief", {}).get("factor"),
        }
        expected = {
            "scale": expected_scale,
            "terrain_origin_m": expected_origin,
            "terrain_relief_factor": expected_factor,
        }
        mismatches = {
            key: {"actual": actual[key], "expected": expected[key]}
            for key in expected
            if actual[key] is None or not math.isclose(
                float(actual[key]), float(expected[key]), rel_tol=1e-10, abs_tol=1e-9
            )
        }
        for option, expected_value in shared_options.items():
            config_key = config_option_keys.get(option)
            if config_key is None:
                continue
            actual_value = config.get(config_key)
            if option in NUMERIC_SHARED_OPTIONS:
                try:
                    matches = math.isclose(
                        float(actual_value), float(expected_value), rel_tol=1e-10, abs_tol=1e-9
                    )
                except (TypeError, ValueError):
                    matches = False
            elif option in PATH_SHARED_OPTIONS:
                matches = actual_value is not None and Path(str(actual_value)).resolve() == Path(str(expected_value)).resolve()
            else:
                matches = str(actual_value) == str(expected_value)
            if not matches:
                mismatches[option] = {"actual": actual_value, "expected": expected_value}
        config_frame = config.get("frame_epsg2263", {})
        frame_mismatches = {}
        for key, expected_value in {
            "origin_ft": frame["origin"], "x_axis": frame["x_axis"], "y_axis": frame["y_axis"]
        }.items():
            try:
                if not np.allclose(np.asarray(config_frame[key], dtype=float), expected_value, rtol=0, atol=1e-8):
                    frame_mismatches[key] = {"actual": config_frame.get(key), "expected": expected_value.tolist()}
            except (KeyError, TypeError, ValueError):
                frame_mismatches[key] = {"actual": config_frame.get(key), "expected": expected_value.tolist()}
        try:
            if not np.allclose(np.asarray(config.get("size_mm"), dtype=float), frame["size_mm"], rtol=0, atol=1e-8):
                frame_mismatches["size_mm"] = {"actual": config.get("size_mm"), "expected": frame["size_mm"].tolist()}
        except (TypeError, ValueError):
            frame_mismatches["size_mm"] = {"actual": config.get("size_mm"), "expected": frame["size_mm"].tolist()}
        expected_shape = [int(frame["shape_cells"][1]), int(frame["shape_cells"][0])]
        if generated["shape"] != expected_shape:
            frame_mismatches["field_shape"] = {"actual": generated["shape"], "expected": expected_shape}
        if mismatches or frame_mismatches:
            diagnostics.error(
                "GENERATED_CONFIGURATION_MISMATCH",
                f"Generated chunk {chunk_id} does not use the plan's common normalization and print frame.",
                chunk_id=chunk_id, value_mismatches=mismatches, frame_mismatches=frame_mismatches,
            )
        try:
            generated_aoi = shape(config["aoi_wgs84"])
            generated_aoi = gpd.GeoSeries([generated_aoi], crs=4326).to_crs(2263).iloc[0]
            aoi_difference = float(shapely.symmetric_difference(generated_aoi, geometries[index]).area)
            if aoi_difference > 1e-5:
                diagnostics.error(
                    "GENERATED_AOI_MISMATCH",
                    f"Generated chunk {chunk_id} used a different AOI than its emitted polygon file.",
                    chunk_id=chunk_id, symmetric_difference_sq_ft=aoi_difference,
                )
        except (KeyError, TypeError, ValueError, shapely.errors.GEOSException) as error:
            diagnostics.error(
                "GENERATED_AOI_INVALID",
                f"Generated chunk {chunk_id} config has an invalid aoi_wgs84: {error}", chunk_id=chunk_id,
            )

        planned_argv = record.get("argv")
        planned_command = record.get("command")
        metadata_path = job_dir / "generation_command.json"
        try:
            metadata = json.loads(metadata_path.read_text())
            if metadata.get("argv") != planned_argv or metadata.get("shell_command") != planned_command:
                diagnostics.error(
                    "GENERATED_COMMAND_METADATA_MISMATCH",
                    f"Chunk {chunk_id} job metadata does not match the command in plan.json.",
                    chunk_id=chunk_id, metadata_file=str(metadata_path),
                )
        except (OSError, json.JSONDecodeError) as error:
            diagnostics.error(
                "GENERATED_COMMAND_METADATA_UNREADABLE",
                f"Chunk {chunk_id} generation-command metadata cannot be read: {error}",
                chunk_id=chunk_id, metadata_file=str(metadata_path),
            )
        model_path = Path(str(record.get("output_3mf", "")))
        if model_path.is_file():
            try:
                with zipfile.ZipFile(model_path) as archive:
                    embedded = json.loads(archive.read("Metadata/generation_command.json"))
                if embedded.get("argv") != planned_argv or embedded.get("shell_command") != planned_command:
                    diagnostics.error(
                        "EMBEDDED_COMMAND_METADATA_MISMATCH",
                        f"Chunk {chunk_id} 3MF embeds a different generation command than plan.json.",
                        chunk_id=chunk_id, model=str(model_path),
                    )
            except (OSError, KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as error:
                diagnostics.error(
                    "EMBEDDED_COMMAND_METADATA_UNREADABLE",
                    f"Chunk {chunk_id} 3MF generation-command metadata cannot be read: {error}",
                    chunk_id=chunk_id, model=str(model_path),
                )
        else:
            message = f"Chunk {chunk_id} 3MF is not present, so embedded command metadata was not checked."
            if require_generated:
                diagnostics.error("GENERATED_3MF_MISSING", message, chunk_id=chunk_id, model=str(model_path))
            else:
                diagnostics.warning("GENERATED_3MF_MISSING", message, chunk_id=chunk_id, model=str(model_path))

    pair_reports = []
    for left_index, right_index in adjacency:
        if generated_records[left_index] is None or generated_records[right_index] is None:
            continue
        left_id, right_id = records[left_index]["id"], records[right_index]["id"]
        if partition_mode == "semantic_paths":
            comparison = _compare_generated_pair_arbitrary(
                generated_records[left_index], generated_records[right_index],
                frames[left_index], frames[right_index],
                geometries[left_index], geometries[right_index], step_ft=step_ft,
            )
        else:
            comparison = _compare_generated_pair(
                generated_records[left_index], generated_records[right_index],
                frames[left_index], frames[right_index],
            )
        if comparison is None:
            diagnostics.error(
                "GENERATED_FRAME_ADJACENCY_BROKEN",
                f"Chunks {left_id} and {right_id} share geometry but no comparable generated seam samples were found.",
                left_chunk=left_id, right_chunk=right_id,
            )
            continue
        axis, ground_delta, height_delta, material_match, extrapolated_samples = comparison
        if not len(ground_delta):
            diagnostics.error(
                "GENERATED_SEAM_HAS_NO_SAMPLES",
                f"Chunks {left_id} and {right_id} share a seam but no paired AOI cells were found on it.",
                left_chunk=left_id, right_chunk=right_id, axis=axis,
            )
            continue
        report = {
            "left_chunk": left_id,
            "right_chunk": right_id,
            "axis": axis,
            "samples": int(len(ground_delta)),
            "extrapolated_samples": extrapolated_samples,
            "ground_max_abs_mm": float(np.max(ground_delta)),
            "ground_p95_abs_mm": float(np.percentile(ground_delta, 95)),
            "surface_max_abs_mm": float(np.max(height_delta)),
            "surface_p95_abs_mm": float(np.percentile(height_delta, 95)),
            "material_match_fraction": float(material_match.mean()),
        }
        pair_reports.append(report)
        material_mismatch = 1.0 - report["material_match_fraction"]
        if material_mismatch > 0.25:
            diagnostics.error(
                "GENERATED_MATERIAL_SEAM_DISCONTINUITY",
                f"Chunks {left_id} and {right_id} assign different materials to "
                f"{material_mismatch:.1%} of paired seam cells.",
                **report,
            )
        elif material_mismatch > 0.05:
            diagnostics.warning(
                "GENERATED_MATERIAL_SEAM_MISMATCH",
                f"Chunks {left_id} and {right_id} assign different materials to "
                f"{material_mismatch:.1%} of paired seam cells.",
                **report,
            )
        worst_p95 = max(report["ground_p95_abs_mm"], report["surface_p95_abs_mm"])
        worst_max = max(report["ground_max_abs_mm"], report["surface_max_abs_mm"])
        if worst_p95 > height_tolerance_mm or worst_max > maximum_height_tolerance_mm:
            diagnostics.error(
                "GENERATED_HEIGHT_SEAM_DISCONTINUITY",
                f"Chunks {left_id} and {right_id} disagree at their {axis} seam: "
                f"p95={worst_p95:.3f} mm, max={worst_max:.3f} mm "
                f"(limits {height_tolerance_mm:.3f}/{maximum_height_tolerance_mm:.3f} mm).",
                **report,
                p95_tolerance_mm=height_tolerance_mm,
                maximum_tolerance_mm=maximum_height_tolerance_mm,
            )
        elif worst_max > height_tolerance_mm:
            diagnostics.warning(
                "GENERATED_HEIGHT_SEAM_LOCAL_OUTLIER",
                f"Chunks {left_id} and {right_id} have a localized {worst_max:.3f} mm seam outlier, "
                f"while p95 remains {worst_p95:.3f} mm.",
                **report,
                p95_tolerance_mm=height_tolerance_mm,
                maximum_tolerance_mm=maximum_height_tolerance_mm,
            )
    diagnostics.checks["generated_seam_heights"] = {
        "requested": True,
        "require_generated": require_generated,
        "height_p95_tolerance_mm": height_tolerance_mm,
        "height_maximum_tolerance_mm": maximum_height_tolerance_mm,
        "chunks_available": len(records) - len(missing),
        "chunks_missing": len(missing),
        "pairs_compared": len(pair_reports),
        "pairs": pair_reports,
    }


def validate_plan(
    plan_path: Path,
    *,
    check_generated: bool = False,
    require_generated: bool = False,
    height_tolerance_mm: float = 0.24,
    maximum_height_tolerance_mm: float = 0.72,
) -> dict:
    plan_path = Path(plan_path).resolve()
    diagnostics = Diagnostics()
    try:
        plan = json.loads(plan_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        diagnostics.error("PLAN_UNREADABLE", f"Plan file cannot be read: {error}", plan=str(plan_path))
        return _report(plan_path, diagnostics)
    if plan.get("schema_version") not in {1, 2, 3, 4}:
        diagnostics.error(
            "PLAN_SCHEMA_UNSUPPORTED",
            f"Plan schema_version must be 1, 2, 3 or 4; got {plan.get('schema_version')!r}.",
            schema_version=plan.get("schema_version"),
        )
    records = plan.get("chunks")
    if not isinstance(records, list) or not records:
        diagnostics.error("PLAN_HAS_NO_CHUNKS", "Plan must contain a non-empty chunks list.")
        return _report(plan_path, diagnostics)
    request = plan.get("request", {})
    maximum_chunks = request.get("maximum_chunks")
    declared_count = plan.get("chunk_count")
    if declared_count != len(records):
        diagnostics.error(
            "CHUNK_COUNT_RECORD_MISMATCH",
            f"plan.json declares {declared_count!r} chunks but contains {len(records)} records.",
            declared=declared_count, actual=len(records),
        )
    if not isinstance(maximum_chunks, int) or isinstance(maximum_chunks, bool) or maximum_chunks < 1 or len(records) > maximum_chunks:
        diagnostics.error(
            "CHUNK_COUNT_EXCEEDS_MAXIMUM",
            f"Plan contains {len(records)} chunks, exceeding the requested maximum {maximum_chunks!r}.",
            actual=len(records), maximum=maximum_chunks,
        )
    ids = [record.get("id") for record in records]
    id_counts = Counter(value for value in ids if isinstance(value, str) and value)
    duplicates = sorted(value for value, count in id_counts.items() if count > 1)
    missing_ids = sum(not isinstance(value, str) or not value for value in ids)
    if missing_ids or duplicates:
        diagnostics.error(
            "CHUNK_IDS_NOT_UNIQUE", "Every chunk must have one unique non-empty ID.",
            duplicate_ids=duplicates, missing_ids=missing_ids,
        )

    try:
        target_payload = request["bounding_polygon_wgs84"]
        target_wgs = shape(target_payload)
        if target_wgs.geom_type not in {"Polygon", "MultiPolygon"} or not target_wgs.is_valid or target_wgs.is_empty:
            raise ValueError(f"requested geometry must be a non-empty valid Polygon/MultiPolygon, got {target_wgs.geom_type}")
        projected_from_wgs = gpd.GeoSeries([target_wgs], crs=4326).to_crs(2263).iloc[0]
        target_hex = request.get("target_wkb_hex_epsg2263")
        target = shapely.from_wkb(bytes.fromhex(target_hex)) if target_hex else projected_from_wgs
        if target.geom_type not in {"Polygon", "MultiPolygon"} or target.is_empty or not target.is_valid:
            raise ValueError("target_wkb_hex_epsg2263 is empty, invalid, or non-polygonal")
    except (KeyError, TypeError, ValueError, shapely.errors.GEOSException) as error:
        diagnostics.error(
            "REQUEST_GEOMETRY_INVALID",
            f"Plan does not contain a valid request.bounding_polygon_wgs84 geometry: {error}",
        )
        return _report(plan_path, diagnostics)

    layout = plan.get("layout", {})
    try:
        scale = float(layout["scale_denominator"])
        grid_step_mm = float(layout["grid_step_mm"])
        global_origin = np.asarray(layout["global_origin_ft"], dtype=float)
        common_x = np.asarray(layout["x_axis"], dtype=float)
        common_y = np.asarray(layout["y_axis"], dtype=float)
        cut_x = [int(value) for value in layout["cut_grid_cells"]["x"]]
        cut_y = [int(value) for value in layout["cut_grid_cells"]["y"]]
        grid_columns = int(layout["grid"]["columns"])
        grid_rows = int(layout["grid"]["rows"])
        partition_mode = str(layout.get("partition_mode", "straight_grid"))
        expected_origin = float(plan["terrain"]["shared_origin_m_navd88"])
        expected_factor = float(plan["terrain"]["shared_relief"]["factor"])
    except (KeyError, TypeError, ValueError) as error:
        diagnostics.error("LAYOUT_SCHEMA_INVALID", f"Plan layout/terrain schema is invalid: {error}")
        return _report(plan_path, diagnostics)
    structural_error = False
    if not all(math.isfinite(value) for value in [scale, grid_step_mm, expected_origin, expected_factor]):
        diagnostics.error("LAYOUT_VALUES_NONFINITE", "Scale, grid step and terrain normalization values must be finite.")
        structural_error = True
    if not (1000 <= scale <= 50000):
        diagnostics.error("SCALE_OUT_OF_RANGE", f"Shared scale 1:{scale:g} is outside generator range 1:1,000-50,000.")
    if not (0.1 <= grid_step_mm <= 0.5):
        diagnostics.error("GRID_STEP_OUT_OF_RANGE", f"Grid step {grid_step_mm:g} mm is outside generator range 0.1-0.5 mm.")
        structural_error = True
    for name, value in [("global_origin_ft", global_origin), ("x_axis", common_x), ("y_axis", common_y)]:
        if value.shape != (2,) or not np.isfinite(value).all():
            diagnostics.error("LAYOUT_VECTOR_INVALID", f"layout.{name} must be a finite numeric pair.", field=name)
            structural_error = True
    if not structural_error:
        determinant = float(common_x[0] * common_y[1] - common_x[1] * common_y[0])
        if not (
            abs(np.linalg.norm(common_x) - 1) <= 1e-8
            and abs(np.linalg.norm(common_y) - 1) <= 1e-8
            and abs(float(common_x @ common_y)) <= 1e-8
            and determinant >= 1 - 1e-8
        ):
            diagnostics.error("LAYOUT_AXES_INVALID", "Shared layout axes are not an orthonormal right-handed frame.")
            structural_error = True
    if grid_columns < 1 or grid_rows < 1:
        diagnostics.error("GRID_DIMENSIONS_INVALID", "Grid rows and columns must both be positive.", columns=grid_columns, rows=grid_rows)
        structural_error = True
    if partition_mode not in {"straight_grid", "semantic_paths"}:
        diagnostics.error(
            "PARTITION_MODE_INVALID",
            f"layout.partition_mode must be straight_grid or semantic_paths; got {partition_mode!r}.",
            partition_mode=partition_mode,
        )
        structural_error = True
    if isinstance(maximum_chunks, int) and grid_columns * grid_rows > maximum_chunks:
        diagnostics.error(
            "GRID_CELL_COUNT_EXCEEDS_MAXIMUM",
            f"Declared {grid_columns}x{grid_rows} grid has {grid_columns * grid_rows} cells, exceeding --max-chunks={maximum_chunks}.",
            grid_cells=grid_columns * grid_rows, maximum=maximum_chunks,
        )
    if len(cut_x) != grid_columns + 1 or len(cut_y) != grid_rows + 1:
        diagnostics.error(
            "CUT_COUNT_MISMATCH",
            "Cut-coordinate arrays do not match the declared grid dimensions.",
            columns=grid_columns, rows=grid_rows, x_cuts=len(cut_x), y_cuts=len(cut_y),
        )
        structural_error = True
    if not cut_x or not cut_y or (cut_x and cut_x[0] != 0) or (cut_y and cut_y[0] != 0):
        diagnostics.error("CUT_ORIGIN_INVALID", "Both cut-coordinate arrays must start at grid cell 0.")
        structural_error = True
    if any(right <= left for left, right in zip(cut_x, cut_x[1:])) or any(
        right <= left for left, right in zip(cut_y, cut_y[1:])
    ):
        diagnostics.error("CUT_ORDER_INVALID", "Cut coordinates must be strictly increasing on both axes.")
        structural_error = True
    if structural_error:
        return _report(plan_path, diagnostics)

    step_ft = grid_step_mm * scale / (FT * 1000.0)
    requested_area = float(target.area)
    tolerance = max(1e-6, requested_area * 1e-10)
    target_roundtrip_difference = float(shapely.symmetric_difference(target, projected_from_wgs).area)
    if target_roundtrip_difference > tolerance:
        diagnostics.error(
            "REQUEST_GEOMETRY_REPRESENTATIONS_DIFFER",
            "The WGS84 request geometry and exact EPSG:2263 WKB target do not describe the same area.",
            symmetric_difference_sq_ft=target_roundtrip_difference, tolerance_sq_ft=tolerance,
        )

    generation = plan.get("generation", {})
    if not isinstance(generation, dict):
        diagnostics.error("GENERATION_SCHEMA_INVALID", "plan.generation must be an object.")
        generation = {}
    readiness = generation.get("readiness")
    if readiness is not None:
        if not isinstance(readiness, dict) or readiness.get("result") not in {"validated", "not_checked"}:
            diagnostics.error(
                "GENERATION_READINESS_INVALID",
                "plan.generation.readiness.result must be 'validated' or 'not_checked'.",
            )
    readiness_record = readiness if isinstance(readiness, dict) else {}
    shared_options = _normalized_shared_options(plan)
    shared_flags = _normalized_shared_flags(plan)
    if not shared_options:
        diagnostics.error("GENERATION_SHARED_OPTIONS_MISSING", "plan.generation.shared_options must define authoritative generator settings.")
    if not shared_flags:
        diagnostics.error("GENERATION_SHARED_FLAGS_MISSING", "plan.generation.shared_flags must define authoritative generator flags.")

    geometries_wgs: list[Any | None] = [None] * len(records)
    geometries: list[Any | None] = [None] * len(records)
    frame_records: list[dict | None] = [None] * len(records)
    try:
        requested_size = np.asarray(request.get("chunk_size_mm"), dtype=float)
        if requested_size.shape != (2,) or not np.isfinite(requested_size).all() or np.any(requested_size <= 0):
            raise ValueError("must be a positive finite numeric pair")
    except (TypeError, ValueError) as error:
        diagnostics.error("REQUEST_CHUNK_SIZE_INVALID", f"request.chunk_size_mm {error}.")
        requested_size = np.asarray([MAX_FRAME_MM, MAX_FRAME_MM])
    for index, record in enumerate(records):
        chunk_id = record.get("id", f"index-{index}")
        polygon_path = Path(record.get("polygon_file", ""))
        try:
            geometry, payload_type = _read_geometry(polygon_path)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            diagnostics.error(
                "CHUNK_POLYGON_UNREADABLE",
                f"Chunk {chunk_id} polygon file cannot be read: {error}",
                chunk_id=chunk_id, polygon_file=str(polygon_path),
            )
            continue
        if payload_type == "FeatureCollection":
            diagnostics.error(
                "CHUNK_POLYGON_FEATURE_COLLECTION",
                f"Chunk {chunk_id} polygon is a FeatureCollection, which generate_3mf.py rejects.",
                chunk_id=chunk_id, polygon_file=str(polygon_path),
            )
        if geometry.geom_type != "Polygon" or geometry.is_empty or not geometry.is_valid:
            diagnostics.error(
                "CHUNK_POLYGON_INVALID",
                f"Chunk {chunk_id} must be one non-empty valid Polygon; got {geometry.geom_type}, valid={geometry.is_valid}.",
                chunk_id=chunk_id, geometry_type=geometry.geom_type, valid=geometry.is_valid,
            )
            continue
        geometries_wgs[index] = geometry
        record_for_frame = dict(record)
        record_for_frame["requested_chunk_size_mm"] = requested_size.tolist()
        projected = gpd.GeoSeries([geometry], crs=4326).to_crs(2263).iloc[0]
        geometries[index] = projected
        if partition_mode == "semantic_paths":
            try:
                planned_hex = record["planned_polygon_wkb_hex_epsg2263"]
                planned = shapely.from_wkb(bytes.fromhex(planned_hex))
                difference = float(shapely.symmetric_difference(planned, projected).area)
                if difference > tolerance:
                    diagnostics.error(
                        "PLANNED_CHUNK_GEOMETRY_MISMATCH",
                        f"Free-form chunk {chunk_id} polygon file differs from its exact planned partition geometry.",
                        chunk_id=chunk_id, symmetric_difference_sq_ft=difference,
                        tolerance_sq_ft=tolerance,
                    )
            except (KeyError, TypeError, ValueError, shapely.errors.GEOSException) as error:
                diagnostics.error(
                    "PLANNED_CHUNK_GEOMETRY_INVALID",
                    f"Free-form chunk {chunk_id} has invalid planned_polygon_wkb_hex_epsg2263: {error}",
                    chunk_id=chunk_id,
                )
        declared_area = record.get("area_sq_ft")
        try:
            if not math.isclose(float(declared_area), float(projected.area), rel_tol=1e-9, abs_tol=tolerance):
                diagnostics.error(
                    "CHUNK_AREA_RECORD_MISMATCH",
                    f"Chunk {chunk_id} recorded area {declared_area!r} sq ft differs from its polygon file area {projected.area:.9g} sq ft.",
                    chunk_id=chunk_id, declared_area_sq_ft=declared_area, actual_area_sq_ft=float(projected.area),
                )
        except (TypeError, ValueError):
            diagnostics.error("CHUNK_AREA_RECORD_INVALID", f"Chunk {chunk_id} area_sq_ft must be numeric.", chunk_id=chunk_id)
        frame_records[index] = _validate_frame(
            diagnostics, record_for_frame, projected,
            scale=scale, grid_step_mm=grid_step_mm,
            global_origin=global_origin, common_x=common_x, common_y=common_y,
            cut_x=cut_x, cut_y=cut_y, grid_columns=grid_columns, grid_rows=grid_rows,
            partition_mode=partition_mode,
        )
        _validate_command(
            diagnostics, record,
            expected_scale=scale, expected_origin=expected_origin,
            expected_factor=expected_factor, expected_grid_step=grid_step_mm,
            generation=generation, shared_options=shared_options, shared_flags=shared_flags,
        )

    commands_path = Path(str(plan.get("files", {}).get("commands", "")))
    readiness_prefix = (
        "# Source-cache readiness was not checked while creating this plan.\n"
        "# Validate/provision the generated command inputs before running it.\n\n"
        if readiness_record.get("result") == "not_checked" else ""
    )
    expected_commands = (
        "#!/bin/sh\nset -eu\n\n"
        + readiness_prefix
        + shlex.join(generation.get("static_validation_argv", [])) + "\n\n"
        + "\n\n".join(record.get("command", "") for record in records) + "\n\n"
        + shlex.join(generation.get("post_generation_validation_argv", [])) + "\n"
    )
    try:
        actual_commands = commands_path.read_text()
        if actual_commands != expected_commands:
            diagnostics.error(
                "COMMANDS_FILE_MISMATCH",
                "commands.sh is not the exact ordered static-check/generation/post-check script described by plan.json.",
                commands_file=str(commands_path),
            )
    except OSError as error:
        diagnostics.error("COMMANDS_FILE_UNREADABLE", f"commands.sh cannot be read: {error}", commands_file=str(commands_path))

    if any(geometry is None for geometry in geometries):
        diagnostics.checks["geometry_coverage"] = {"result": "not_run", "reason": "one or more chunk polygons are invalid"}
        return _report(plan_path, diagnostics)
    geometries = list(geometries)
    coverage = shapely.union_all(geometries)
    missing = shapely.difference(target, coverage)
    extra = shapely.difference(coverage, target)
    missing_area = float(missing.area)
    extra_area = float(extra.area)
    overlap_total = max(0.0, float(sum(geometry.area for geometry in geometries) - coverage.area))

    tree = shapely.STRtree(geometries)
    pair_indices = tree.query(geometries, predicate="intersects")
    overlap_pairs = []
    exact_adjacency = []
    shared_lines = []
    for left, right in pair_indices.T:
        left, right = int(left), int(right)
        if left >= right:
            continue
        intersection = shapely.intersection(geometries[left], geometries[right])
        area = float(intersection.area)
        if area > tolerance:
            overlap_pairs.append({
                "left_chunk": records[left]["id"], "right_chunk": records[right]["id"],
                "overlap_sq_ft": area,
            })
        shared = shapely.intersection(geometries[left].boundary, geometries[right].boundary)
        line_parts = [part for part in shapely.get_parts(shared) if part.geom_type in {"LineString", "LinearRing"}]
        shared_length = float(sum(part.length for part in line_parts))
        if shared_length > 1e-5:
            exact_adjacency.append((left, right))
            shared_lines.extend(line_parts)
    if missing_area > tolerance:
        diagnostics.error(
            "COVERAGE_GAP",
            f"Chunk union leaves {missing_area:.6g} sq ft uncovered in {len(_polygon_parts(missing))} gap component(s).",
            missing_area_sq_ft=missing_area, tolerance_sq_ft=tolerance,
            gap_components=_component_summary(missing),
        )
    if extra_area > tolerance:
        diagnostics.error(
            "COVERAGE_OUTSIDE_REQUEST",
            f"Chunks extend {extra_area:.6g} sq ft outside the requested polygon.",
            extra_area_sq_ft=extra_area, tolerance_sq_ft=tolerance,
            extra_components=_component_summary(extra),
        )
    if overlap_total > tolerance or overlap_pairs:
        diagnostics.error(
            "CHUNK_INTERIOR_OVERLAP",
            f"Chunk interiors overlap by {overlap_total:.6g} sq ft in aggregate.",
            overlap_area_sq_ft=overlap_total, tolerance_sq_ft=tolerance,
            overlapping_pairs=overlap_pairs[:20],
        )
    diagnostics.checks["geometry_coverage"] = {
        "result": "passed" if missing_area <= tolerance and extra_area <= tolerance and overlap_total <= tolerance else "failed",
        "requested_area_sq_ft": requested_area,
        "covered_area_sq_ft": float(coverage.area),
        "missing_area_sq_ft": missing_area,
        "extra_area_sq_ft": extra_area,
        "overlap_area_sq_ft": overlap_total,
        "tolerance_sq_ft": tolerance,
    }

    total_x = cut_x[-1] * step_ft
    total_y = cut_y[-1] * step_ft
    expected_lines = []
    if partition_mode == "semantic_paths":
        for seam_index, seam in enumerate(plan.get("seams", [])):
            try:
                geometry = shapely.from_wkb(bytes.fromhex(seam["geometry_wkb_hex_epsg2263"]))
                parts = _linear_parts(geometry)
                if not parts:
                    raise ValueError(f"geometry is {geometry.geom_type}, not linear")
                expected_lines.extend(parts)
            except (KeyError, TypeError, ValueError, shapely.errors.GEOSException) as error:
                diagnostics.error(
                    "PLANNED_SEAM_GEOMETRY_INVALID",
                    f"Free-form seam {seam_index + 1} has invalid geometry_wkb_hex_epsg2263: {error}",
                    seam_index=seam_index + 1,
                )
    else:
        for cell in cut_x[1:-1]:
            distance = cell * step_ft
            expected_lines.append(LineString([
                global_origin + common_x * distance,
                global_origin + common_x * distance + common_y * total_y,
            ]))
        for cell in cut_y[1:-1]:
            distance = cell * step_ft
            expected_lines.append(LineString([
                global_origin + common_y * distance,
                global_origin + common_y * distance + common_x * total_x,
            ]))
    coordinate_tolerance = max(step_ft * 2e-5, 2e-6)
    target_edge_band = target.boundary.buffer(coordinate_tolerance)
    expected_internal = shapely.union_all([
        shapely.difference(shapely.intersection(line, target), target_edge_band)
        for line in expected_lines
    ]) if expected_lines else LineString()
    # Validate every grid cell independently. This localizes swapped records,
    # arbitrary in-cell splits, and frame errors that a whole-AOI union can hide.
    cell_members: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, frame in enumerate(frame_records):
        if frame is not None:
            cell_members[frame["cell"]].append(index)
    expected_cells = {}
    actual_cells = {}
    cell_mismatch_count = 0
    for south_row in range(grid_rows):
        for column in range(grid_columns):
            key = (south_row, column)
            member_indices = cell_members.get(key, [])
            if partition_mode == "semantic_paths":
                logical_cells = []
                for member_index in member_indices:
                    try:
                        logical_cells.append(shapely.from_wkb(bytes.fromhex(
                            records[member_index]["logical_cell_wkb_hex_epsg2263"]
                        )))
                    except (KeyError, TypeError, ValueError, shapely.errors.GEOSException) as error:
                        diagnostics.error(
                            "LOGICAL_CELL_GEOMETRY_INVALID",
                            f"Chunk {records[member_index].get('id')} has invalid logical-cell geometry: {error}",
                            chunk_id=records[member_index].get("id"),
                        )
                if logical_cells:
                    cell = logical_cells[0]
                    for other in logical_cells[1:]:
                        if float(shapely.symmetric_difference(cell, other).area) > tolerance:
                            diagnostics.error(
                                "LOGICAL_CELL_GEOMETRY_MISMATCH",
                                f"Components assigned to row {grid_rows - south_row}, column {column + 1} "
                                "do not share one planned logical-cell boundary.",
                                row=grid_rows - south_row, column=column + 1,
                            )
                    expected_cell = shapely.intersection(target, cell)
                else:
                    expected_cell = Polygon()
            else:
                cell = _frame_geometry(
                    global_origin, common_x, common_y,
                    cut_x[column] * step_ft, cut_x[column + 1] * step_ft,
                    cut_y[south_row] * step_ft, cut_y[south_row + 1] * step_ft,
                )
                expected_cell = shapely.intersection(target, cell)
            expected_cells[key] = expected_cell
            actual_cell = shapely.union_all([geometries[index] for index in member_indices]) if member_indices else Polygon()
            actual_cells[key] = actual_cell
            cell_difference = float(shapely.symmetric_difference(expected_cell, actual_cell).area)
            expected_parts = len(_polygon_parts(expected_cell))
            components = sorted(records[index].get("component") for index in member_indices)
            expected_components = list(range(1, len(member_indices) + 1))
            if components != expected_components:
                diagnostics.error(
                    "CHUNK_COMPONENT_INDEX_INVALID",
                    f"Grid row {grid_rows - south_row}, column {column + 1} component indices are {components}; expected {expected_components}.",
                    row=grid_rows - south_row, column=column + 1, components=components,
                )
            if expected_parts != len(member_indices) or cell_difference > tolerance:
                cell_mismatch_count += 1
                diagnostics.error(
                    "GRID_CELL_PARTITION_MISMATCH",
                    f"Grid row {grid_rows - south_row}, column {column + 1} should contain {expected_parts} polygon piece(s), "
                    f"but has {len(member_indices)}; symmetric difference is {cell_difference:.6g} sq ft.",
                    row=grid_rows - south_row, column=column + 1,
                    expected_components=expected_parts, actual_chunks=[records[index]["id"] for index in member_indices],
                    symmetric_difference_sq_ft=cell_difference, tolerance_sq_ft=tolerance,
                )

    # Expected grid-neighbor seams are authoritative. Match each component on
    # both sides within a tiny coordinate buffer, rather than relying on exact
    # equality after the EPSG:2263 -> WGS84 -> EPSG:2263 round trip.
    adjacency_set: set[tuple[int, int]] = set()
    missing_side_length = 0.0
    expected_neighbor_lines = []
    neighbor_keys = []
    for south_row in range(grid_rows):
        for column in range(grid_columns - 1):
            neighbor_keys.append(((south_row, column), (south_row, column + 1)))
    for south_row in range(grid_rows - 1):
        for column in range(grid_columns):
            neighbor_keys.append(((south_row, column), (south_row + 1, column)))
    for left_key, right_key in neighbor_keys:
        expected_seam = shapely.intersection(
            expected_cells[left_key].boundary, expected_cells[right_key].boundary
        )
        expected_seam = shapely.union_all(_linear_parts(expected_seam)) if _linear_parts(expected_seam) else LineString()
        if expected_seam.is_empty or expected_seam.length <= 1e-5:
            continue
        expected_neighbor_lines.extend(_linear_parts(expected_seam))
        left_actual = actual_cells[left_key]
        right_actual = actual_cells[right_key]
        left_missing = shapely.difference(expected_seam, left_actual.boundary.buffer(coordinate_tolerance))
        right_missing = shapely.difference(expected_seam, right_actual.boundary.buffer(coordinate_tolerance))
        missing_side_length += max(float(left_missing.length), float(right_missing.length))
        for left_index in cell_members.get(left_key, []):
            for right_index in cell_members.get(right_key, []):
                near = shapely.intersection(
                    geometries[left_index].boundary,
                    geometries[right_index].boundary.buffer(coordinate_tolerance),
                )
                near = shapely.intersection(near, expected_seam.buffer(coordinate_tolerance))
                if float(near.length) > 1e-5:
                    adjacency_set.add(tuple(sorted((left_index, right_index))))
    adjacency = sorted(adjacency_set)

    actual_shared = shapely.union_all(shared_lines) if shared_lines else LineString()
    expected_neighbor_union = shapely.union_all(expected_neighbor_lines) if expected_neighbor_lines else LineString()
    uncovered_seam = shapely.difference(expected_neighbor_union, actual_shared.buffer(coordinate_tolerance))
    unmatched_planned = shapely.difference(expected_internal, actual_shared.buffer(coordinate_tolerance))
    broken_length = max(float(uncovered_seam.length), float(unmatched_planned.length), missing_side_length)
    seam_length = float(expected_internal.length)
    seam_length_tolerance = max(0.01, seam_length * 1e-7)
    if broken_length > seam_length_tolerance:
        diagnostics.error(
            "BROKEN_INTERNAL_SEAM",
            f"Planned internal cut lines contain {broken_length:.6g} ft without a matching shared chunk boundary.",
            broken_length_ft=broken_length,
            expected_internal_seam_length_ft=seam_length,
            tolerance_ft=seam_length_tolerance,
        )
    unplanned = shapely.difference(actual_shared, expected_internal.buffer(coordinate_tolerance))
    unplanned_length = float(unplanned.length)
    if unplanned_length > seam_length_tolerance:
        diagnostics.error(
            "UNPLANNED_INTERNAL_SEAM",
            f"Chunks contain {unplanned_length:.6g} ft of shared boundary away from a planned grid cut.",
            unplanned_shared_boundary_ft=unplanned_length, tolerance_ft=seam_length_tolerance,
        )
    graph = _graph_components(len(records), adjacency)
    target_components = len(_polygon_parts(target))
    if len(graph) > target_components:
        diagnostics.error(
            "CHUNK_ADJACENCY_GRAPH_DISCONNECTED",
            f"Chunk adjacency has {len(graph)} components for a request with {target_components} polygon component(s).",
            graph_components=[[records[index]["id"] for index in component] for component in graph[:10]],
            target_components=target_components,
        )
    diagnostics.checks["seam_topology"] = {
        "result": "passed" if broken_length <= seam_length_tolerance and unplanned_length <= seam_length_tolerance and len(graph) <= target_components and not cell_mismatch_count else "failed",
        "adjacent_chunk_pairs": len(adjacency),
        "adjacency_components": len(graph),
        "target_components": target_components,
        "expected_internal_seam_length_ft": seam_length,
        "unmatched_internal_seam_length_ft": broken_length,
        "unplanned_internal_seam_length_ft": unplanned_length,
        "grid_cells_with_partition_errors": cell_mismatch_count,
        "tolerance_ft": seam_length_tolerance,
    }
    diagnostics.checks["shared_configuration"] = {
        "result": "passed" if not any(
            issue["code"] in {
                "FRAME_AXES_DIFFER", "FRAME_ORIGIN_OFF_SHARED_GRID", "FRAME_ORIGIN_WRONG_GRID_CELL",
                "FRAME_SIZE_WRONG_GRID_CELL", "FRAME_GRID_BOUNDS_MISMATCH",
                "COMMAND_SHARED_VALUE_MISMATCH", "COMMAND_SHARED_OPTION_MISMATCH",
                "COMMAND_SHARED_FLAG_MISMATCH", "COMMAND_PER_CHUNK_SCALE_RISK",
            }
            for issue in diagnostics.errors
        ) else "failed",
        "scale_denominator": scale,
        "terrain_origin_m_navd88": expected_origin,
        "terrain_relief_factor": expected_factor,
        "grid_step_mm": grid_step_mm,
    }

    if check_generated:
        if any(frame is None for frame in frame_records):
            diagnostics.error(
                "GENERATED_CHECK_BLOCKED_BY_FRAME_ERRORS",
                "Actual seam heights cannot be compared until all frame errors are fixed.",
            )
        else:
            _validate_generated(
                diagnostics, records, frame_records, geometries, adjacency,
                expected_scale=scale, expected_origin=expected_origin, expected_factor=expected_factor,
                shared_options=shared_options,
                height_tolerance_mm=height_tolerance_mm,
                maximum_height_tolerance_mm=maximum_height_tolerance_mm,
                require_generated=require_generated,
                partition_mode=partition_mode, step_ft=step_ft,
            )
    else:
        diagnostics.checks["generated_seam_heights"] = {
            "requested": False,
            "result": "not_run",
            "reason": "Run with --check-generated after all chunk commands finish.",
        }
    return _report(plan_path, diagnostics)


def _report(plan_path: Path, diagnostics: Diagnostics) -> dict:
    return {
        "plan": str(plan_path),
        "result": "failed" if diagnostics.errors else "passed",
        "error_count": len(diagnostics.errors),
        "warning_count": len(diagnostics.warnings),
        "checks": diagnostics.checks,
        "issues": diagnostics.issues,
    }


def write_report(path: Path, report: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(path)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Validate chunk-plan geometry, frames, commands and optional generated seam heights.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    result.add_argument("--plan", type=Path, required=True)
    result.add_argument("--report", type=Path)
    result.add_argument("--check-generated", action="store_true")
    result.add_argument("--require-generated", action="store_true")
    result.add_argument("--height-tolerance-mm", type=float, default=0.24,
                        help="Maximum accepted p95 extrapolated edge mismatch")
    result.add_argument("--maximum-height-tolerance-mm", type=float, default=0.72,
                        help="Maximum accepted single extrapolated edge mismatch")
    return result


def main() -> None:
    args = parser().parse_args()
    if args.require_generated and not args.check_generated:
        raise SystemExit("--require-generated requires --check-generated")
    if args.height_tolerance_mm <= 0 or args.maximum_height_tolerance_mm < args.height_tolerance_mm:
        raise SystemExit("Height tolerances must be positive and maximum >= p95 tolerance")
    report = validate_plan(
        args.plan,
        check_generated=args.check_generated,
        require_generated=args.require_generated,
        height_tolerance_mm=args.height_tolerance_mm,
        maximum_height_tolerance_mm=args.maximum_height_tolerance_mm,
    )
    report_path = args.report or Path(args.plan).resolve().with_name("validation.json")
    write_report(report_path, report)
    summary = {
        "result": report["result"],
        "errors": report["error_count"],
        "warnings": report["warning_count"],
        "report": str(Path(report_path).resolve()),
    }
    if report["issues"]:
        summary["issues"] = [
            {"severity": issue["severity"], "code": issue["code"], "message": issue["message"]}
            for issue in report["issues"][:20]
        ]
    print(json.dumps(summary, indent=2))
    if report["result"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
