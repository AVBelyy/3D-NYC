#!/usr/bin/env python3
"""Precompute the NYC 2017 LAZ measurements into a compact raster cache.

The cache preserves the two measurements used by the production 3MF pipeline:

* mean class-2 ground elevation in each 0.5 metre cell; and
* maximum class 1/2/17/25 elevation in each 0.5 metre cell.

Values remain float32 metres.  There is no vertical quantization.  Tiles use a
fixed global EPSG:2263 grid, include a seamless 10 metre nearest-ground fill,
and can be built independently and resumed safely.

This is lossless with respect to the *canonical raster measurements*.  It does
not preserve individual point returns, and an axis-aligned citywide cache
cannot be bit-identical to a point cloud binned directly into an arbitrarily
rotated job grid.  Consumers should mosaic/reproject these canonical tiles.

With --delete-source-after-last-use, missing LAZ files are downloaded lazily
and removed only after the final planned raster chunk that uses each file.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import shutil
import sys
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import laspy
import numpy as np
import rasterio
import shapely
from affine import Affine
from scipy.ndimage import distance_transform_edt
from shapely.geometry import box, mapping

from download_data import MIN_FREE, ROOT, download


PIPELINE_VERSION = 1
FT_TO_M = 0.3048006096012192
RESOLUTION_M = 0.5
RESOLUTION_FT = RESOLUTION_M / FT_TO_M
CRS = "EPSG:2263"
GROUND_CLASSES = (2,)
UPPER_CLASSES = (1, 2, 17, 25)
# The tile grid and fill reach every LiDAR cache publishes, whatever it was built
# from.  A second builder writing the same contract reads them from here rather
# than repeating the numbers, so the two cannot drift apart.
DEFAULT_CHUNK_CELLS = 4096
DEFAULT_GROUND_FILL_M = 10.0
DEFAULT_OUTPUT = ROOT / "data/cache/nyc_lidar_2017"
DEFAULT_INDEX = ROOT / "data/raw/nyc_lidar_2017/index.geojson"
DEFAULT_LAZ = ROOT / "data/raw/nyc_lidar_2017/tiles"
SOURCE_GB = 1_000_000_000


@dataclass(frozen=True)
class SourceSignature:
    tile_id: str
    path: str
    bytes: int
    mtime_ns: int
    url: str | None = None


@dataclass(frozen=True)
class TileTask:
    key: str
    tile_x: int
    tile_y: int
    bounds_ft: tuple[float, float, float, float]
    sources: tuple[SourceSignature, ...]
    output_dir: str
    chunk_cells: int
    fill_cells: int
    point_chunk_size: int
    overwrite: bool
    hash_outputs: bool


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, allow_nan=False))
    os.replace(temporary, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def cache_configuration(chunk_cells: int, fill_cells: int) -> dict[str, Any]:
    return {
        "pipeline_version": PIPELINE_VERSION,
        "crs": CRS,
        "horizontal_units": "US survey feet",
        "elevation_units": "metres NAVD88",
        "resolution_m": RESOLUTION_M,
        "resolution_ft": RESOLUTION_FT,
        "chunk_cells": chunk_cells,
        "chunk_span_m": chunk_cells * RESOLUTION_M,
        "ground_fill_max_distance_m": fill_cells * RESOLUTION_M,
        "ground_classes": list(GROUND_CLASSES),
        "upper_classes": list(UPPER_CLASSES),
        "withheld_points_excluded": True,
        "ground_aggregation": "arithmetic mean",
        "upper_aggregation": "maximum",
        "dtype": "float32",
        "vertical_quantization": None,
    }


def classification_values(points: Any) -> np.ndarray:
    raw = np.asarray(points.classification)
    dimensions = set(points.point_format.dimension_names)
    if "LAS 1.4 classification" not in dimensions:
        return raw
    extended = np.asarray(points["LAS 1.4 classification"])
    return np.where(extended > 31, extended, raw)


def write_raster(path: Path, array: np.ndarray, transform: Affine) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    profile = {
        "driver": "GTiff",
        "width": array.shape[1],
        "height": array.shape[0],
        "count": 1,
        "dtype": "float32",
        "crs": CRS,
        "transform": transform,
        "compress": "deflate",
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        "nodata": np.nan,
    }
    with rasterio.open(temporary, "w", **profile) as target:
        target.write(array.astype(np.float32, copy=False), 1)
    os.replace(temporary, path)


def completed_metadata(task: TileTask, metadata_path: Path) -> dict[str, Any] | None:
    if task.overwrite or not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    output = Path(task.output_dir)
    ground = output / metadata.get("ground", {}).get("path", "")
    upper = output / metadata.get("upper", {}).get("path", "")
    expected_sources = [asdict(source) for source in task.sources]
    expected_config = cache_configuration(task.chunk_cells, task.fill_cells)
    if (
        metadata.get("configuration") == expected_config
        and metadata.get("sources") == expected_sources
        and ground.is_file()
        and upper.is_file()
        and ground.stat().st_size == metadata["ground"]["bytes"]
        and upper.stat().st_size == metadata["upper"]["bytes"]
    ):
        metadata["status"] = "reused"
        return metadata
    return None


def reusable_declared_sources(task: TileTask) -> tuple[SourceSignature, ...] | None:
    """Return sidecar-declared inputs when a completed tile's LAZ was deleted."""
    metadata_path = Path(task.output_dir) / "tiles" / f"{task.key}.json"
    if not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    expected = cache_configuration(task.chunk_cells, task.fill_cells)
    declared = metadata.get("sources", [])
    if metadata.get("status") not in {"completed", "reused"} or metadata.get("configuration") != expected:
        return None
    if sorted(item.get("tile_id") for item in declared) != sorted(source.tile_id for source in task.sources):
        return None
    output = Path(task.output_dir)
    for name in ["ground", "upper"]:
        record = metadata.get(name, {})
        if not (output / record.get("path", "")).is_file():
            return None
    return tuple(SourceSignature(
        tile_id=str(item["tile_id"]), path=str(item["path"]),
        bytes=int(item["bytes"]), mtime_ns=int(item["mtime_ns"]),
        url=item.get("url"),
    ) for item in declared)


def prepare_task_sources(
    task: TileTask,
    laz_dir: Path,
    download_missing: bool,
    max_source_bytes: int | None = None,
    managed_source_ids: set[str] | None = None,
) -> TileTask:
    """Resolve a task's LAZ references immediately before processing it.

    In streaming mode the task plan may contain files that have not been
    downloaded yet.  A completed tile can also be resumed from its sidecar
    after its source LAZ was deleted, so those declared signatures are kept
    as a valid input reference even when the file is absent.
    """
    managed_source_ids = managed_source_ids or {source.tile_id for source in task.sources}
    if max_source_bytes is not None:
        resident = source_bytes_on_disk(managed_source_ids, laz_dir)
        if resident > max_source_bytes:
            raise RuntimeError(
                f"Managed LAZ files already occupy {resident / SOURCE_GB:.1f} GB, "
                f"above --max-source-gb ({max_source_bytes / SOURCE_GB:.1f} GB)."
            )
    declared = None if task.overwrite else reusable_declared_sources(task)
    declared_by_id = {source.tile_id: source for source in declared or ()}
    resolved: list[SourceSignature] = []
    for source in task.sources:
        path = Path(source.path)
        if path.is_file() and path.stat().st_size > 1000:
            stat = path.stat()
            resolved.append(SourceSignature(
                tile_id=source.tile_id,
                path=str(path.resolve()),
                bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                url=source.url,
            ))
            continue
        declared_source = declared_by_id.get(source.tile_id)
        if declared_source is not None:
            resolved.append(declared_source)
            continue
        if download_missing and source.url:
            laz_dir.mkdir(parents=True, exist_ok=True)
            print(f"Download for {task.key}: {source.tile_id}.laz", flush=True)
            downloaded = download(
                str(source.url), f"{laz_dir.name}/{source.tile_id}.laz",
                raw_dir=laz_dir.parent, manifests_dir=laz_dir.parent,
                max_resident_bytes=max_source_bytes,
                resident_bytes_fn=(
                    lambda target: source_bytes_on_disk(
                        managed_source_ids, laz_dir, exclude=target
                    )
                ) if max_source_bytes is not None else None,
            )
            if max_source_bytes is not None:
                resident = source_bytes_on_disk(managed_source_ids, laz_dir)
                if resident > max_source_bytes:
                    # Remove only the file just downloaded; leave its
                    # provenance manifest so the next run can retry cleanly.
                    downloaded = Path(downloaded)
                    if downloaded.is_file():
                        downloaded.unlink()
                    raise RuntimeError(
                        f"Downloading {source.tile_id}.laz would exceed the "
                        f"{max_source_bytes / SOURCE_GB:.1f} GB managed-LAZ cap "
                        f"(resident {resident / SOURCE_GB:.1f} GB)."
                    )
            resolved.append(source_signature(source.tile_id, laz_dir, source.url))
            continue
        raise FileNotFoundError(
            f"{task.key}: required LAZ file is missing: {path}. "
            "Add --download-missing or use a cache sidecar from a completed run."
        )
    if max_source_bytes is not None:
        resident = source_bytes_on_disk(managed_source_ids, laz_dir)
        if resident > max_source_bytes:
            raise RuntimeError(
                f"Managed LAZ files occupy {resident / SOURCE_GB:.1f} GB, "
                f"above --max-source-gb ({max_source_bytes / SOURCE_GB:.1f} GB)."
            )
    return replace(task, sources=tuple(resolved))


def delete_source_after_last_use(
    source: SourceSignature,
    laz_dir: Path,
    deletion_log: Path,
) -> bool:
    """Delete one source only after validating that it is the planned LAZ file."""
    root = laz_dir.resolve()
    path = Path(source.path)
    # A resumed tile may legitimately refer to a source that was already
    # deleted (or lived under an older LAZ directory).  There is nothing to
    # remove in that case; only validate the deletion target when it exists.
    if not path.exists():
        return False
    if path.parent.resolve() != root or path.name != f"{source.tile_id}.laz":
        raise RuntimeError(f"Refusing to delete source outside {root}: {path}")
    if path.is_symlink():
        raise RuntimeError(f"Refusing to delete symlink source: {path}")
    stat = path.stat()
    if stat.st_size != source.bytes or stat.st_mtime_ns != source.mtime_ns:
        raise RuntimeError(
            f"Refusing to delete changed source {path} "
            f"(expected {source.bytes} bytes/{source.mtime_ns}, "
            f"found {stat.st_size} bytes/{stat.st_mtime_ns})"
        )
    path.unlink()
    deletion_log.parent.mkdir(parents=True, exist_ok=True)
    with deletion_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "tile_id": source.tile_id,
            "path": str(path),
            "bytes": source.bytes,
            "url": source.url,
            "deleted_at": datetime.now(timezone.utc).isoformat(),
        }, allow_nan=False) + "\n")
    return True


def source_bytes_on_disk(
    source_ids: set[str], laz_dir: Path, exclude: Path | None = None,
) -> int:
    """Count managed LAZ and resumable-part bytes currently resident."""
    excluded = set()
    if exclude is not None:
        excluded = {exclude.resolve(), Path(str(exclude) + ".part").resolve()}
    total = 0
    for tile_id in source_ids:
        for path in (
            laz_dir / f"{tile_id}.laz",
            laz_dir / f"{tile_id}.laz.part",
        ):
            if path.resolve() not in excluded and path.is_file():
                total += path.stat().st_size
    return total


def streaming_peak_source_count(tasks: list[TileTask]) -> int:
    """Estimate the peak number of simultaneously needed source tiles."""
    ordered = sorted(tasks, key=lambda task: (task.tile_x, task.tile_y))
    remaining = Counter(source.tile_id for task in ordered for source in task.sources)
    active: set[str] = set()
    peak = 0
    for task in ordered:
        active.update(source.tile_id for source in task.sources)
        peak = max(peak, len(active))
        for source in task.sources:
            remaining[source.tile_id] -= 1
        active.difference_update(tile_id for tile_id, uses in remaining.items() if uses == 0)
    return peak


def process_tile(task: TileTask) -> dict[str, Any]:
    output = Path(task.output_dir)
    tiles_dir = output / "tiles"
    metadata_path = tiles_dir / f"{task.key}.json"
    reused = completed_metadata(task, metadata_path)
    if reused is not None:
        return reused

    estimated_uncompressed = task.chunk_cells * task.chunk_cells * 4 * 2
    if shutil.disk_usage(output).free - estimated_uncompressed < MIN_FREE:
        raise RuntimeError(f"{task.key}: refusing to reduce free storage below 15 GiB")

    halo = task.fill_cells
    expanded_cells = task.chunk_cells + 2 * halo
    expanded_size = expanded_cells * expanded_cells
    ground_sum = np.zeros(expanded_size, np.float64)
    ground_count = np.zeros(expanded_size, np.uint32)
    upper = np.full(expanded_size, -np.inf, np.float32)

    x0, y0, x1, y1 = task.bounds_ft
    # Assign points through integer global cell indices.  Computing local
    # indices from independently rounded floating origins can move a point that
    # lies exactly on a cell boundary when --chunk-cells changes, which would
    # make adjacent cache tiles disagree by a handful of cells.
    expanded_x_cell_min = task.tile_x * task.chunk_cells - halo
    expanded_y_cell_min = task.tile_y * task.chunk_cells - halo
    expanded_y_cell_max = expanded_y_cell_min + expanded_cells - 1
    accepted_points = 0
    ground_points = 0
    upper_points = 0
    class_counts: dict[str, int] = {}

    for signature in task.sources:
        path = Path(signature.path)
        with laspy.open(path) as source:
            for points in source.chunk_iterator(task.point_chunk_size):
                x = np.asarray(points.x)
                y = np.asarray(points.y)
                global_col = np.floor(x / RESOLUTION_FT).astype(np.int64)
                global_y_cell = np.floor(y / RESOLUTION_FT).astype(np.int64)
                col = global_col - expanded_x_cell_min
                row = expanded_y_cell_max - global_y_cell
                inside = (
                    (row >= 0) & (row < expanded_cells)
                    & (col >= 0) & (col < expanded_cells)
                )
                eligible = inside & ~np.asarray(points.withheld, dtype=bool)
                if not eligible.any():
                    continue
                accepted_points += int(eligible.sum())
                classification = classification_values(points)
                values, counts = np.unique(classification[eligible], return_counts=True)
                for value, count in zip(values.tolist(), counts.tolist()):
                    key = str(value)
                    class_counts[key] = class_counts.get(key, 0) + int(count)

                ground_mask = eligible & np.isin(classification, GROUND_CLASSES)
                if ground_mask.any():
                    indices = row[ground_mask] * expanded_cells + col[ground_mask]
                    z = np.asarray(points.z)[ground_mask] * FT_TO_M
                    np.add.at(ground_sum, indices, z)
                    np.add.at(ground_count, indices, 1)
                    ground_points += int(ground_mask.sum())

                upper_mask = eligible & np.isin(classification, UPPER_CLASSES)
                if upper_mask.any():
                    indices = row[upper_mask] * expanded_cells + col[upper_mask]
                    z = (np.asarray(points.z)[upper_mask] * FT_TO_M).astype(np.float32)
                    np.maximum.at(upper, indices, z)
                    upper_points += int(upper_mask.sum())

    ground_count = ground_count.reshape(expanded_cells, expanded_cells)
    observed = ground_count > 0
    ground = np.full((expanded_cells, expanded_cells), np.nan, np.float32)
    if observed.any():
        sums = ground_sum.reshape(expanded_cells, expanded_cells)
        ground[observed] = (sums[observed] / ground_count[observed]).astype(np.float32)
        distance, nearest = distance_transform_edt(~observed, return_indices=True)
        fill = (~observed) & (distance <= task.fill_cells)
        ground[fill] = ground[tuple(nearest[:, fill])]
    else:
        fill = np.zeros_like(observed)

    upper = upper.reshape(expanded_cells, expanded_cells)
    upper[~np.isfinite(upper)] = np.nan
    core = np.s_[halo:halo + task.chunk_cells, halo:halo + task.chunk_cells]
    ground_core = ground[core]
    upper_core = upper[core]
    observed_core = observed[core]
    fill_core = fill[core]

    tiles_dir.mkdir(parents=True, exist_ok=True)
    ground_name = f"{task.key}_ground_m.tif"
    upper_name = f"{task.key}_upper_surface_m.tif"
    ground_path = tiles_dir / ground_name
    upper_path = tiles_dir / upper_name
    transform = Affine(RESOLUTION_FT, 0, x0, 0, -RESOLUTION_FT, y1)
    write_raster(ground_path, ground_core, transform)
    write_raster(upper_path, upper_core, transform)

    def output_record(path: Path) -> dict[str, Any]:
        record: dict[str, Any] = {
            "path": str(path.relative_to(output)),
            "bytes": path.stat().st_size,
        }
        if task.hash_outputs:
            record["sha256"] = sha256(path)
        return record

    metadata = {
        "status": "completed",
        "key": task.key,
        "tile_x": task.tile_x,
        "tile_y": task.tile_y,
        "bounds_epsg2263_ft": list(task.bounds_ft),
        "configuration": cache_configuration(task.chunk_cells, task.fill_cells),
        "sources": [asdict(source) for source in task.sources],
        "accepted_points_with_halo": accepted_points,
        "ground_points_with_halo": ground_points,
        "upper_points_with_halo": upper_points,
        "class_counts_with_halo": dict(sorted(class_counts.items(), key=lambda item: int(item[0]))),
        "ground_observed_fraction": float(observed_core.mean()),
        "ground_filled_fraction": float(fill_core.mean()),
        "upper_observed_fraction": float(np.isfinite(upper_core).mean()),
        "ground": output_record(ground_path),
        "upper": output_record(upper_path),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json(metadata_path, metadata)
    return metadata


def source_signature(
    tile_id: str,
    laz_dir: Path,
    url: str | None = None,
    *,
    allow_missing: bool = False,
) -> SourceSignature:
    path = (laz_dir / f"{tile_id}.laz").resolve()
    if not path.is_file() or path.stat().st_size <= 1000:
        if allow_missing:
            return SourceSignature(tile_id=tile_id, path=str(path), bytes=0, mtime_ns=0, url=url)
        raise FileNotFoundError(path)
    stat = path.stat()
    return SourceSignature(
        tile_id=tile_id, path=str(path), bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns, url=url,
    )


def tile_key(tile_x: int, tile_y: int) -> str:
    return f"x{tile_x:+06d}_y{tile_y:+06d}"


def requested_chunks(frame: gpd.GeoDataFrame, requested: shapely.Geometry, chunk_cells: int) -> list[tuple[int, int]]:
    span = chunk_cells * RESOLUTION_FT
    xmin, ymin, xmax, ymax = requested.bounds
    first_x = math.floor(xmin / span)
    last_x = math.ceil(xmax / span) - 1
    first_y = math.floor(ymin / span)
    last_y = math.ceil(ymax / span) - 1
    spatial_index = frame.sindex
    chunks = []
    for tile_y in range(first_y, last_y + 1):
        for tile_x in range(first_x, last_x + 1):
            bounds = (tile_x * span, tile_y * span, (tile_x + 1) * span, (tile_y + 1) * span)
            geometry = box(*bounds)
            if not geometry.intersects(requested):
                continue
            hits = spatial_index.query(geometry, predicate="intersects")
            if len(hits):
                chunks.append((tile_x, tile_y))
    return chunks


def build_tasks(
    frame: gpd.GeoDataFrame,
    requested: shapely.Geometry,
    laz_dir: Path,
    output: Path,
    chunk_cells: int,
    fill_cells: int,
    point_chunk_size: int,
    overwrite: bool,
    hash_outputs: bool,
    limit_chunks: int | None,
    allow_missing_sources: bool = False,
) -> list[TileTask]:
    span = chunk_cells * RESOLUTION_FT
    halo_ft = fill_cells * RESOLUTION_FT
    chunks = requested_chunks(frame, requested, chunk_cells)
    if limit_chunks is not None:
        chunks = chunks[:limit_chunks]
    spatial_index = frame.sindex
    tasks = []
    for tile_x, tile_y in chunks:
        bounds = (tile_x * span, tile_y * span, (tile_x + 1) * span, (tile_y + 1) * span)
        expanded = box(bounds[0] - halo_ft, bounds[1] - halo_ft, bounds[2] + halo_ft, bounds[3] + halo_ft)
        hits = spatial_index.query(expanded, predicate="intersects")
        candidates = frame.iloc[hits].sort_values("LAS_ID")
        signatures = tuple(
            source_signature(
                str(row.LAS_ID), laz_dir, getattr(row, "azure_url", None),
                allow_missing=allow_missing_sources,
            )
            for row in candidates.itertuples(index=False)
        )
        tasks.append(TileTask(
            key=tile_key(tile_x, tile_y), tile_x=tile_x, tile_y=tile_y,
            bounds_ft=bounds, sources=signatures, output_dir=str(output.resolve()),
            chunk_cells=chunk_cells, fill_cells=fill_cells,
            point_chunk_size=point_chunk_size, overwrite=overwrite,
            hash_outputs=hash_outputs,
        ))
    return tasks


def write_catalog(output: Path, results: list[dict[str, Any]]) -> None:
    features = []
    for result in results:
        geometry = gpd.GeoSeries([box(*result["bounds_epsg2263_ft"])], crs=2263).to_crs(4326).iloc[0]
        features.append({
            "type": "Feature",
            "geometry": mapping(geometry),
            "properties": {
                "key": result["key"],
                "ground": result["ground"]["path"],
                "upper": result["upper"]["path"],
                "ground_observed_fraction": result["ground_observed_fraction"],
                "upper_observed_fraction": result["upper_observed_fraction"],
            },
        })
    atomic_json(output / "catalog.geojson", {"type": "FeatureCollection", "features": features})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tile-index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--laz-dir", type=Path, default=DEFAULT_LAZ)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--coverage", choices=["available", "all"], default="available",
        help="Use cached LAZ files only, or require every indexed LAZ intersecting the request",
    )
    parser.add_argument(
        "--bounds", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
        help="Optional EPSG:2263 bounds; output chunks are expanded to the fixed cache grid",
    )
    parser.add_argument("--download-missing", action="store_true")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--chunk-cells", type=int, default=DEFAULT_CHUNK_CELLS)
    parser.add_argument("--ground-fill-m", type=float, default=DEFAULT_GROUND_FILL_M)
    parser.add_argument("--point-chunk-size", type=int, default=1_000_000)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--delete-source-after-last-use", action="store_true",
        help=(
            "Stream missing LAZ downloads and delete each source after its "
            "last raster-chunk use; requires --workers 1"
        ),
    )
    parser.add_argument(
        "--max-source-gb", type=float, default=50.0,
        help=(
            "Maximum resident bytes for managed LAZ files in streaming-delete "
            "mode (default: 50 GB; the download reserve still applies)"
        ),
    )
    parser.add_argument("--limit-chunks", type=int, help="Development/smoke-test limit")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip-output-hashes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.chunk_cells < 256 or args.chunk_cells % 256:
        raise ValueError("--chunk-cells must be a multiple of 256 and at least 256")
    if args.ground_fill_m < 0 or args.ground_fill_m > 100:
        raise ValueError("--ground-fill-m must be between 0 and 100")
    if args.workers < 1:
        raise ValueError("--workers must be positive")
    if args.point_chunk_size < 10_000:
        raise ValueError("--point-chunk-size must be at least 10,000")
    if args.max_source_gb <= 0:
        raise ValueError("--max-source-gb must be positive")
    if args.delete_source_after_last_use and args.workers != 1:
        raise ValueError("--delete-source-after-last-use requires --workers 1")
    if args.delete_source_after_last_use and args.download_only:
        raise ValueError("--delete-source-after-last-use cannot be combined with --download-only")

    tile_index = args.tile_index.resolve()
    laz_dir = args.laz_dir.resolve()
    output = args.output_dir.resolve()
    if args.delete_source_after_last_use and output.is_relative_to(laz_dir):
        raise ValueError("--output-dir must not be inside --laz-dir when deleting sources")
    if not tile_index.is_file():
        raise FileNotFoundError(tile_index)
    frame = gpd.read_file(tile_index).to_crs(2263)
    frame["LAS_ID"] = frame.LAS_ID.astype(str)
    if frame.LAS_ID.duplicated().any():
        raise RuntimeError("LiDAR tile index contains duplicate LAS_ID values")
    full_frame = frame.copy()
    requested = box(*args.bounds) if args.bounds else box(*frame.total_bounds)
    frame = frame[frame.intersects(requested)].copy()
    if frame.empty:
        raise RuntimeError("Requested bounds do not intersect the LiDAR tile index")

    if args.coverage == "available":
        frame = frame[frame.LAS_ID.map(lambda tile: (laz_dir / f"{tile}.laz").stat().st_size > 1000
                                      if (laz_dir / f"{tile}.laz").exists() else False)].copy()
        if frame.empty:
            raise RuntimeError(f"No usable LAZ files found in {laz_dir}")
    else:
        # Include every source intersecting any fixed output chunk, including
        # the fill halo and the part of a boundary chunk outside --bounds.
        span = args.chunk_cells * RESOLUTION_FT
        xmin, ymin, xmax, ymax = requested.bounds
        expanded_request = box(
            math.floor(xmin / span) * span - args.ground_fill_m / FT_TO_M,
            math.floor(ymin / span) * span - args.ground_fill_m / FT_TO_M,
            math.ceil(xmax / span) * span + args.ground_fill_m / FT_TO_M,
            math.ceil(ymax / span) * span + args.ground_fill_m / FT_TO_M,
        )
        frame = full_frame[full_frame.intersects(expanded_request)].copy()

    missing = [tile for tile in frame.LAS_ID if not (laz_dir / f"{tile}.laz").is_file()
               or (laz_dir / f"{tile}.laz").stat().st_size <= 1000]
    if args.dry_run:
        chunks = requested_chunks(frame, requested, args.chunk_cells)
        if args.limit_chunks is not None:
            chunks = chunks[:args.limit_chunks]
        estimated_uncompressed = len(chunks) * args.chunk_cells**2 * 4 * 2
        print(
            f"Plan: {len(chunks)} raster chunks, up to {len(frame)} LAZ inputs "
            f"({len(missing)} missing), {estimated_uncompressed / 1024**3:.1f} GiB "
            "uncompressed raster values",
            flush=True,
        )
        if args.delete_source_after_last_use:
            fill_cells = math.ceil(args.ground_fill_m / RESOLUTION_M)
            planned_tasks = build_tasks(
                frame, requested, laz_dir, output, args.chunk_cells, fill_cells,
                args.point_chunk_size, args.overwrite, False, args.limit_chunks,
                allow_missing_sources=True,
            )
            print(
                f"Streaming order keeps at most {streaming_peak_source_count(planned_tasks)} "
                f"source tiles live at once; resident-LAZ cap: {args.max_source_gb:.1f} GB",
                flush=True,
            )
        return
    # In streaming-delete mode, missing files are resolved immediately before
    # the chunk that needs them.  A completed tile sidecar may satisfy a
    # missing source on resume, so do not reject the whole plan up front.
    if missing and not args.download_missing and not args.delete_source_after_last_use:
        preview = ", ".join(missing[:10])
        raise FileNotFoundError(
            f"{len(missing)} required LAZ files are missing ({preview}). "
            "Add --download-missing or use --coverage available."
        )
    if missing and not args.delete_source_after_last_use:
        laz_dir.mkdir(parents=True, exist_ok=True)
        lookup = frame.set_index("LAS_ID")
        for number, tile in enumerate(missing, 1):
            print(f"Download {number}/{len(missing)}: {tile}.laz", flush=True)
            download(
                str(lookup.loc[tile].azure_url), f"{laz_dir.name}/{tile}.laz",
                raw_dir=laz_dir.parent, manifests_dir=laz_dir.parent,
            )
    if args.download_only:
        print(f"Required LAZ files ready: {len(frame)}")
        return

    output.mkdir(parents=True, exist_ok=True)

    fill_cells = math.ceil(args.ground_fill_m / RESOLUTION_M)
    # After downloads, use the full cache-grid candidates. In available mode,
    # this intentionally captures the current source snapshot in each tile's
    # input signatures; adding a neighboring LAZ invalidates that tile later.
    if args.coverage == "available":
        full_frame = full_frame[full_frame.LAS_ID.map(
            lambda tile: (laz_dir / f"{tile}.laz").is_file() and (laz_dir / f"{tile}.laz").stat().st_size > 1000
        )].copy()
    tasks = build_tasks(
        full_frame if args.coverage == "available" else frame,
        requested, laz_dir, output, args.chunk_cells, fill_cells,
        args.point_chunk_size, args.overwrite, not args.skip_output_hashes,
        args.limit_chunks,
        allow_missing_sources=args.delete_source_after_last_use,
    )
    source_ids = sorted({source.tile_id for task in tasks for source in task.sources})
    source_id_set = set(source_ids)
    max_source_bytes = int(args.max_source_gb * SOURCE_GB) if args.delete_source_after_last_use else None
    estimated_uncompressed = len(tasks) * args.chunk_cells**2 * 4 * 2
    print(
        f"Plan: {len(tasks)} raster chunks, {len(source_ids)} LAZ inputs, "
        f"{estimated_uncompressed / 1024**3:.1f} GiB uncompressed raster values",
        flush=True,
    )
    configuration = cache_configuration(args.chunk_cells, fill_cells)
    atomic_json(output / "manifest.in_progress.json", {
        "status": "in_progress",
        "configuration": configuration,
        "coverage_mode": args.coverage,
        "requested_bounds_epsg2263_ft": list(requested.bounds),
        "planned_chunks": len(tasks),
        "source_laz_count": len(source_ids),
        "delete_source_after_last_use": args.delete_source_after_last_use,
        "max_source_gb": args.max_source_gb if args.delete_source_after_last_use else None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    results = []
    runtime_tasks: list[TileTask] = []
    deleted_sources: list[str] = []
    if args.delete_source_after_last_use:
        # Column-major traversal keeps the active spatial frontier narrow.
        # A source is retained until the final planned chunk that references
        # it, then removed before the next download can grow the working set.
        tasks = sorted(tasks, key=lambda task: (task.tile_x, task.tile_y))
        remaining_uses = Counter(
            source.tile_id for task in tasks for source in task.sources
        )
        deletion_log = output / "source_deletions.jsonl"
        for number, planned_task in enumerate(tasks, 1):
            runtime_task = prepare_task_sources(
                planned_task, laz_dir, args.download_missing,
                max_source_bytes=max_source_bytes,
                managed_source_ids=source_id_set,
            )
            result = process_tile(runtime_task)
            runtime_tasks.append(runtime_task)
            results.append(result)
            for source in runtime_task.sources:
                remaining_uses[source.tile_id] -= 1
                if remaining_uses[source.tile_id] == 0:
                    if delete_source_after_last_use(source, laz_dir, deletion_log):
                        deleted_sources.append(source.tile_id)
            resident = source_bytes_on_disk(source_id_set, laz_dir)
            if max_source_bytes is not None and resident > max_source_bytes:
                raise RuntimeError(
                    f"Resident managed LAZ grew to {resident / SOURCE_GB:.1f} GB "
                    f"above the {args.max_source_gb:.1f} GB cap"
                )
            print(f"{number}/{len(tasks)} {result['key']} {result['status']}", flush=True)
    elif args.workers == 1:
        iterator = map(process_tile, tasks)
        for number, result in enumerate(iterator, 1):
            results.append(result)
            print(f"{number}/{len(tasks)} {result['key']} {result['status']}", flush=True)
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process_tile, task): task for task in tasks}
            for number, future in enumerate(concurrent.futures.as_completed(futures), 1):
                result = future.result()
                results.append(result)
                print(f"{number}/{len(tasks)} {result['key']} {result['status']}", flush=True)
    if not runtime_tasks:
        runtime_tasks = tasks

    results.sort(key=lambda result: (result["tile_y"], result["tile_x"]))
    write_catalog(output, results)
    final_manifest = {
        "status": "complete",
        "configuration": configuration,
        "coverage_mode": args.coverage,
        "requested_bounds_epsg2263_ft": list(requested.bounds),
        "chunks": len(results),
        "source_laz_count": len(source_ids),
        "source_laz_bytes": sum({source.path: source.bytes for task in runtime_tasks for source in task.sources}.values()),
        "delete_source_after_last_use": args.delete_source_after_last_use,
        "max_source_gb": args.max_source_gb if args.delete_source_after_last_use else None,
        "deleted_source_laz_count": len(deleted_sources),
        "source_deletions_log": (
            "source_deletions.jsonl"
            if args.delete_source_after_last_use and (output / "source_deletions.jsonl").is_file()
            else None
        ),
        "ground_bytes": sum(result["ground"]["bytes"] for result in results),
        "upper_bytes": sum(result["upper"]["bytes"] for result in results),
        "catalog": "catalog.geojson",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "tiles": [f"tiles/{result['key']}.json" for result in results],
    }
    atomic_json(output / "manifest.json", final_manifest)
    in_progress = output / "manifest.in_progress.json"
    if in_progress.exists():
        in_progress.unlink()
    print(output / "manifest.json")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted; completed tiles remain resumable", file=sys.stderr)
        raise SystemExit(130)
