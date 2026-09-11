#!/usr/bin/env python3
"""Shared primitives for the citywide source caches.

The cache builders deliberately depend only on packages already used by the
3MF pipeline.  Outputs are written through temporary paths and manifests are
published last, so consumers never mistake an interrupted build for a usable
cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import box
from tqdm import tqdm


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DEFAULT_CACHE_ROOT = DATA / "cache"
DEFAULT_COVERAGE = DATA / "cache/nyc_lidar_2021/catalog.geojson"
CRS = "EPSG:2263"
CACHE_FORMAT_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str))
    os.replace(temporary, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def file_signature(path: Path, *, with_hash: bool = False) -> dict[str, Any]:
    path = path.resolve()
    stat = path.stat()
    result: dict[str, Any] = {
        "path": str(path),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if with_hash:
        result["sha256"] = sha256(path)
    return result


def directory_signature(path: Path) -> dict[str, Any]:
    """Cheap, deterministic signature for a publisher FileGDB directory."""
    path = path.resolve()
    records = []
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        stat = item.stat()
        records.append((str(item.relative_to(path)), stat.st_size, stat.st_mtime_ns))
    encoded = json.dumps(records, separators=(",", ":")).encode()
    return {
        "path": str(path),
        "files": len(records),
        "bytes": sum(record[1] for record in records),
        "inventory_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def output_record(path: Path, root: Path, *, with_hash: bool = True) -> dict[str, Any]:
    record = {
        "path": str(path.relative_to(root)),
        "bytes": path.stat().st_size,
    }
    if with_hash:
        record["sha256"] = sha256(path)
    return record


def reusable_manifest(
    component_dir: Path,
    configuration: dict[str, Any],
    sources: dict[str, Any],
) -> dict[str, Any] | None:
    manifest_path = component_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if (
        manifest.get("status") != "complete"
        or manifest.get("configuration") != configuration
        or manifest.get("sources") != sources
    ):
        return None
    for record in manifest.get("outputs", []):
        path = component_dir / record["path"]
        if not path.is_file() or path.stat().st_size != record["bytes"]:
            return None
    return manifest


def start_manifest(
    component_dir: Path,
    component: str,
    configuration: dict[str, Any],
    sources: dict[str, Any],
) -> dict[str, Any]:
    # A consumer only trusts manifest.json.  Remove it before changing any
    # component files so a rebuild can never expose a mixed old/new cache.
    (component_dir / "manifest.json").unlink(missing_ok=True)
    value = {
        "status": "in_progress",
        "component": component,
        "cache_format_version": CACHE_FORMAT_VERSION,
        "started_at": utc_now(),
        "configuration": configuration,
        "sources": sources,
    }
    atomic_json(component_dir / "manifest.in_progress.json", value)
    return value


def finish_manifest(
    component_dir: Path,
    value: dict[str, Any],
    outputs: list[dict[str, Any]],
    **summary: Any,
) -> dict[str, Any]:
    completed = {
        **value,
        "status": "complete",
        "completed_at": utc_now(),
        "outputs": outputs,
        "output_bytes": sum(record["bytes"] for record in outputs),
        **summary,
    }
    atomic_json(component_dir / "manifest.json", completed)
    (component_dir / "manifest.in_progress.json").unlink(missing_ok=True)
    return completed


class Progress:
    """A tqdm terminal bar with periodic machine-readable log fallback."""

    def __init__(
        self, label: str, total: int | None = None, *, unit: str = "items", initial: int = 0
    ):
        self.label = label
        self.total = total
        self.unit = unit
        self.started = time.monotonic()
        self.last_render = 0.0
        self.last_log = 0.0
        self.initial = initial
        self.current = initial
        self.terminal = sys.stderr.isatty()
        self.bar = tqdm(
            total=total, initial=initial, desc=label, unit=unit, dynamic_ncols=True,
            disable=not self.terminal, mininterval=0.2,
        )

    def update(self, current: int, *, detail: str = "", force: bool = False) -> None:
        previous = self.current
        self.current = current
        now = time.monotonic()
        if self.terminal:
            if detail:
                self.bar.set_postfix_str(detail, refresh=False)
            self.bar.update(current - previous)
            if force:
                self.bar.refresh()
            return
        if not force and now - self.last_render < 30.0:
            return
        elapsed = max(now - self.started, 1e-9)
        rate = (current - self.initial) / elapsed
        suffix = f" {detail}" if detail else ""
        if self.total:
            fraction = min(max(current / self.total, 0.0), 1.0)
            eta = (self.total - current) / rate if rate else 0.0
            if force or now - self.last_log >= 30.0:
                print(
                    f"PROGRESS component={self.label} completed={current} total={self.total} "
                    f"unit={self.unit} percent={fraction * 100:.1f} eta={format_duration(eta)}{suffix}",
                    flush=True,
                )
                self.last_log = now
        else:
            if force or now - self.last_log >= 30.0:
                print(
                    f"PROGRESS component={self.label} completed={current} unit={self.unit} "
                    f"rate={rate:.1f}{suffix}", flush=True,
                )
                self.last_log = now
        self.last_render = now

    def close(self, *, detail: str = "") -> None:
        self.update(self.current, detail=detail, force=True)
        self.bar.close()


def format_duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"


def load_coverage(catalog: Path = DEFAULT_COVERAGE, bounds: Iterable[float] | None = None):
    if bounds is not None:
        values = tuple(float(value) for value in bounds)
        if len(values) != 4 or values[0] >= values[2] or values[1] >= values[3]:
            raise ValueError("--bounds must be xmin ymin xmax ymax in EPSG:2263")
        return box(*values)
    if not catalog.exists():
        raise FileNotFoundError(
            f"Coverage catalog is missing: {catalog}. Build the LiDAR cache first or pass --bounds."
        )
    frame = gpd.read_file(catalog)
    if frame.crs is None:
        frame = frame.set_crs(CRS)
    else:
        frame = frame.to_crs(CRS)
    return shapely.union_all(frame.geometry.to_numpy())


def atomic_geoparquet(frame: gpd.GeoDataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.stem + ".in_progress" + path.suffix)
    temporary.unlink(missing_ok=True)
    frame.to_parquet(temporary, compression="zstd", write_covering_bbox=True, index=False)
    os.replace(temporary, path)


def spatial_sort(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if frame.empty:
        return frame
    bounds = frame.geometry.bounds
    order = pd.DataFrame({
        "x": (bounds.minx + bounds.maxx) / 2,
        "y": (bounds.miny + bounds.maxy) / 2,
        "position": range(len(frame)),
    }).sort_values(["x", "y", "position"], kind="stable").position.to_numpy()
    return frame.iloc[order].reset_index(drop=True)


@dataclass
class TileRecord:
    key: str
    path: str
    rows: int
    bytes: int
    bounds: tuple[float, float, float, float]


class TiledGeoParquetWriter:
    """Spill batches to fixed EPSG:2263 tiles, then compact them atomically.

    Features are duplicated into every tile touched by their bounding box.
    Consumers must de-duplicate by the configured identifier after reading the
    intersecting tiles; this guarantees that boundary-crossing geometry is not
    lost.
    """

    def __init__(self, component_dir: Path, *, tile_span_ft: float = 10000.0):
        self.component_dir = component_dir
        self.tile_span_ft = float(tile_span_ft)
        self.staging = component_dir / "staging"
        self.tiles = component_dir / "tiles"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.tiles.mkdir(parents=True, exist_ok=True)
        self.batch = 0

    def _key(self, x: int, y: int) -> str:
        return f"x{x:+06d}_y{y:+06d}"

    def add(self, frame: gpd.GeoDataFrame) -> int:
        if frame.empty:
            return 0
        if frame.crs is None:
            raise ValueError("GeoDataFrame has no CRS")
        frame = frame.to_crs(CRS)
        groups: dict[str, list[int]] = {}
        for position, values in enumerate(frame.geometry.bounds.itertuples(index=False, name=None)):
            minx, miny, maxx, maxy = values
            if not all(map(lambda value: value == value, values)):
                continue
            x0, x1 = int(minx // self.tile_span_ft), int(maxx // self.tile_span_ft)
            y0, y1 = int(miny // self.tile_span_ft), int(maxy // self.tile_span_ft)
            for tile_x in range(x0, x1 + 1):
                for tile_y in range(y0, y1 + 1):
                    groups.setdefault(self._key(tile_x, tile_y), []).append(position)
        for key, positions in groups.items():
            directory = self.staging / key
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"part-{self.batch:08d}.parquet"
            # A page checkpoint is published only after every fragment from
            # this batch exists.  Atomic fragment replacement lets a restart
            # safely discard/rewrite the one uncheckpointed batch.
            atomic_geoparquet(frame.iloc[positions], path)
        self.batch += 1
        return sum(len(positions) for positions in groups.values())

    def finalize(
        self,
        *,
        deduplicate_by: list[str],
        source_order: list[str] | None = None,
        hash_outputs: bool = True,
        cleanup_staging: bool = True,
    ) -> tuple[list[TileRecord], list[dict[str, Any]]]:
        directories = sorted(path for path in self.staging.iterdir() if path.is_dir())
        progress = Progress("compact", len(directories), unit="tiles")
        records: list[TileRecord] = []
        outputs: list[dict[str, Any]] = []
        for index, directory in enumerate(directories, 1):
            parts = [gpd.read_parquet(path) for path in sorted(directory.glob("*.parquet"))]
            frame = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=CRS)
            keys = [column for column in deduplicate_by if column in frame]
            if keys:
                frame = frame.drop_duplicates(keys, keep="first")
            frame = spatial_sort(frame)
            path = self.tiles / f"{directory.name}.parquet"
            atomic_geoparquet(frame, path)
            tile_x = int(directory.name.split("_y")[0][1:])
            tile_y = int(directory.name.split("_y")[1])
            bounds = (
                tile_x * self.tile_span_ft,
                tile_y * self.tile_span_ft,
                (tile_x + 1) * self.tile_span_ft,
                (tile_y + 1) * self.tile_span_ft,
            )
            relative = str(path.relative_to(self.component_dir))
            records.append(TileRecord(directory.name, relative, len(frame), path.stat().st_size, bounds))
            outputs.append(output_record(path, self.component_dir, with_hash=hash_outputs))
            progress.update(index, detail=directory.name)
        progress.close()
        catalog_rows = [
            {"key": record.key, "path": record.path, "rows": record.rows,
             "bytes": record.bytes, "geometry": box(*record.bounds)}
            for record in records
        ]
        catalog = (
            gpd.GeoDataFrame(catalog_rows, geometry="geometry", crs=CRS)
            if catalog_rows else gpd.GeoDataFrame(
                {"key": [], "path": [], "rows": [], "bytes": []},
                geometry=gpd.GeoSeries([], crs=CRS), crs=CRS,
            )
        )
        catalog_path = self.component_dir / "catalog.geojson"
        temporary = catalog_path.with_name("catalog.in_progress.geojson")
        catalog.to_file(temporary, driver="GeoJSON")
        os.replace(temporary, catalog_path)
        outputs.append(output_record(catalog_path, self.component_dir, with_hash=hash_outputs))
        # Keep every source fragment until all compacted tiles and the catalog
        # have been published.  An interruption during compaction can then
        # restart compaction without downloading or parsing source data again.
        if cleanup_staging:
            shutil.rmtree(self.staging)
        return records, outputs


def read_tiled_geoparquet(
    component_dir: Path,
    query_bounds: tuple[float, float, float, float],
    *,
    deduplicate_by: list[str],
    source_order: list[str] | None = None,
) -> gpd.GeoDataFrame:
    catalog = gpd.read_file(component_dir / "catalog.geojson")
    query = box(*query_bounds)
    selected = catalog[catalog.intersects(query)]
    parts = []
    for path in selected.path:
        try:
            parts.append(gpd.read_parquet(component_dir / path, bbox=query_bounds))
        except ValueError:
            parts.append(gpd.read_parquet(component_dir / path))
    if not parts:
        return gpd.GeoDataFrame(geometry=gpd.GeoSeries([], crs=CRS), crs=CRS)
    frame = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=CRS)
    frame = frame[frame.geometry.notna() & frame.intersects(query)].copy()
    keys = [column for column in deduplicate_by if column in frame]
    if keys:
        frame = frame.drop_duplicates(keys, keep="first")
    order = [column for column in (source_order or []) if column in frame]
    if order:
        frame = frame.sort_values(order, kind="stable")
    return frame.reset_index(drop=True)


def clean_staging(component_dir: Path) -> None:
    """Remove only temporary paths owned by an incomplete cache build."""
    for name in ("staging",):
        path = component_dir / name
        if path.exists():
            shutil.rmtree(path)
    for path in component_dir.glob("*.in_progress.*"):
        path.unlink()


def parse_bounds(values: list[float] | None) -> tuple[float, float, float, float] | None:
    if values is None:
        return None
    if len(values) != 4:
        raise ValueError("bounds require four values")
    result = tuple(map(float, values))
    if result[0] >= result[2] or result[1] >= result[3]:
        raise ValueError("bounds must satisfy xmin < xmax and ymin < ymax")
    return result
