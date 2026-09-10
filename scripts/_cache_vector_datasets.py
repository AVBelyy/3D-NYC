#!/usr/bin/env python3
"""Precompute the remaining NYC vector sources used by the 3MF pipeline."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import requests
import shapely
from pyarrow import parquet as pq
from requests.adapters import HTTPAdapter
from shapely.geometry import box
from urllib3.util.retry import Retry

from cache_common import (
    CACHE_FORMAT_VERSION,
    CRS,
    DATA,
    DEFAULT_CACHE_ROOT,
    DEFAULT_COVERAGE,
    Progress,
    TiledGeoParquetWriter,
    atomic_geoparquet,
    atomic_json,
    directory_signature,
    file_signature,
    finish_manifest,
    load_coverage,
    output_record,
    parse_bounds,
    reusable_manifest,
    spatial_sort,
    start_manifest,
    utc_now,
)


PIPELINE_VERSION = 1
BUILDING_SERVICE = (
    "https://services6.arcgis.com/yG5s3afENB5iO9fj/arcgis/rest/services/"
    "BUILDING_view/FeatureServer/0/query"
)
PLANIMETRIC_LAYERS = [
    "COOLING_TOWERS", "CURB", "CURB_CUT", "ELEVATION", "HYDROGRAPHY",
    "MEDIAN", "PARKING_LOT", "PARK", "PAVEMENT_EDGE", "PLAZA",
    "RETAININGWALL", "ROADBED", "SIDEWALK", "SIDEWALK_LINE",
    "TRANSPORT_STRUCTURE", "WATER_TANK",
]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    value.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    value.add_argument("--bounds", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
    value.add_argument("--planimetrics", type=Path, default=DATA / "raw/nyc_planimetrics_2022/Planimetric_2022.gdb")
    value.add_argument("--tile-span-ft", type=float, default=10000.0)
    value.add_argument("--api-page-size", type=int, default=2000)
    value.add_argument(
        "--api-retries", type=int, default=12,
        help="Retries for transient API connection, timeout, 429 and 5xx failures",
    )
    value.add_argument(
        "--api-backoff-seconds", type=float, default=2.0,
        help="Exponential retry backoff base (capped at 120 seconds)",
    )
    value.add_argument("--hash-sources", action="store_true")
    value.add_argument("--skip-output-hashes", action="store_true")
    value.add_argument("--force", action="store_true")
    value.add_argument("--skip-buildings", action="store_true", help=argparse.SUPPRESS)
    value.add_argument("--limit-building-pages", type=int, help=argparse.SUPPRESS)
    return value


def normalize_columns(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    frame = frame.copy()
    geometry_name = frame.geometry.name
    frame.columns = [
        "geometry" if column == geometry_name
        else re.sub(r"[^a-z0-9]+", "_", str(column).lower()).strip("_")
        for column in frame.columns
    ]
    return frame.set_geometry("geometry")


def api_parameters(bounds: tuple[float, float, float, float], page_size: int) -> dict[str, str | int]:
    return {
        "where": "1=1", "outFields": "*", "returnGeometry": "true",
        "geometry": ",".join(f"{number:.3f}" for number in bounds),
        "geometryType": "esriGeometryEnvelope", "inSR": 2263, "outSR": 2263,
        "spatialRel": "esriSpatialRelIntersects", "f": "geojson",
        "resultRecordCount": page_size, "orderByFields": "OBJECTID",
    }


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def retrying_session(args: argparse.Namespace) -> requests.Session:
    retry = Retry(
        total=args.api_retries,
        connect=args.api_retries,
        read=args.api_retries,
        status=args.api_retries,
        allowed_methods=frozenset(("GET",)),
        status_forcelist=(429, 500, 502, 503, 504),
        backoff_factor=args.api_backoff_seconds,
        backoff_max=120,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=2, pool_maxsize=2)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def building_configuration(
    args: argparse.Namespace,
    coverage,
    source_signature: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": 1,
        "source_signature": source_signature,
        "coverage_bounds": list(map(float, coverage.bounds)),
        "crs": CRS,
        "tile_span_ft": args.tile_span_ft,
        "api_page_size": args.api_page_size,
    }


def valid_output_records(root: Path, outputs: list[dict[str, Any]]) -> bool:
    return all(
        (root / record.get("path", "")).is_file()
        and (root / record["path"]).stat().st_size == record.get("bytes")
        for record in outputs
    )


def reusable_building_cache(
    destination: Path,
    configuration: dict[str, Any],
    *,
    hash_outputs: bool,
) -> tuple[list[dict[str, Any]], int] | None:
    manifest_path = destination / "manifest.json"
    manifest = read_json(manifest_path)
    if not manifest or manifest.get("status") != "complete":
        return None
    outputs = manifest.get("outputs", [])
    if manifest.get("configuration") != configuration or not valid_output_records(destination, outputs):
        return None
    # Staging may remain only if the process stopped after publishing the
    # building manifest but before its final cleanup.  It is now redundant.
    staging = destination / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    result = list(outputs)
    result.append(output_record(manifest_path, destination, with_hash=hash_outputs))
    return result, int(manifest["features"])


def publish_building_cache(
    destination: Path,
    configuration: dict[str, Any],
    outputs: list[dict[str, Any]],
    features: int,
    *,
    hash_outputs: bool,
    production_ready: bool,
) -> tuple[list[dict[str, Any]], int]:
    manifest_path = destination / "manifest.json"
    atomic_json(manifest_path, {
        "status": "complete",
        "component": "nyc_building_footprints",
        "cache_format_version": CACHE_FORMAT_VERSION,
        "configuration": configuration,
        "completed_at": utc_now(),
        "features": features,
        "outputs": outputs,
        "output_bytes": sum(record["bytes"] for record in outputs),
        "production_ready": production_ready,
    })
    # The manifest is the commit marker. Cleanup occurs after it is durable;
    # a restart can therefore either compact from staging or reuse the commit.
    staging = destination / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    (destination / "progress.json").unlink(missing_ok=True)
    result = list(outputs)
    result.append(output_record(manifest_path, destination, with_hash=hash_outputs))
    return result, features


def remove_fragment_batch(staging: Path, batch: int) -> None:
    for path in staging.glob(f"*/part-{batch:08d}*.parquet"):
        path.unlink()


def infer_legacy_api_progress(staging: Path, page_size: int) -> dict[str, int] | None:
    """Recover fragments written by the original non-checkpointing builder.

    The highest batch may have been interrupted while its tile fragments were
    being written, so it is discarded and fetched again.  All earlier batches
    are preserved.
    """
    batches = set()
    pattern = re.compile(r"part-(\d{8})\.parquet$")
    for path in staging.glob("*/part-*.parquet"):
        match = pattern.fullmatch(path.name)
        if match:
            # Opening metadata catches a truncated fragment before we trust it.
            pq.ParquetFile(path)
            batches.add(int(match.group(1)))
    if not batches:
        return None
    maximum = max(batches)
    expected = set(range(maximum + 1))
    if batches != expected:
        raise RuntimeError(
            "Building fragment batches are non-contiguous; refusing an unsafe resume. "
            "Keep the directory for inspection or pass --force to start this component over."
        )
    remove_fragment_batch(staging, maximum)
    return {
        "next_offset": maximum * page_size,
        "retained": maximum * page_size,
        "pages": maximum,
        "next_batch": maximum,
    }


def parquet_row_count(path: Path) -> int | None:
    try:
        metadata = pq.ParquetFile(path).metadata
    except Exception:
        return None
    file_metadata = metadata.metadata or {}
    if b"geo" not in file_metadata:
        return None
    return int(metadata.num_rows)


def planimetrics_resume_compatible(
    previous: dict[str, Any] | None,
    configuration: dict[str, Any],
    sources: dict[str, Any],
) -> bool:
    if not previous:
        return False
    old_configuration = previous.get("configuration", {})
    old_sources = previous.get("sources", {})
    keys = ("crs", "coverage_bounds", "coverage_catalog", "coverage_mode", "planimetric_layers")
    return (
        all(old_configuration.get(key) == configuration.get(key) for key in keys)
        and old_sources.get("planimetrics") == sources.get("planimetrics")
        and old_sources.get("coverage_catalog") == sources.get("coverage_catalog")
    )


def add_building_batch(
    writer: TiledGeoParquetWriter,
    frame: gpd.GeoDataFrame,
    coverage,
    source_offset: int,
) -> int:
    if frame.empty:
        return 0
    frame = normalize_columns(frame.set_crs(CRS, allow_override=True))
    frame = frame[frame.geometry.notna() & frame.intersects(coverage)].copy()
    if frame.empty:
        return 0
    frame["source_order"] = np.arange(source_offset, source_offset + len(frame), dtype=np.int64)
    if "doitt_id" not in frame:
        frame["doitt_id"] = None
    if "height_roof" not in frame:
        frame["height_roof"] = np.nan
    frame["doitt_id"] = frame.doitt_id.astype(str).str.replace(r"\.0$", "", regex=True)
    frame["height_roof"] = pd.to_numeric(frame.height_roof, errors="coerce")
    writer.add(frame)
    return len(frame)


def build_buildings_api(
    destination: Path,
    coverage,
    args: argparse.Namespace,
    configuration: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    destination.mkdir(parents=True, exist_ok=True)
    hash_outputs = not args.skip_output_hashes
    completed = None if args.force else reusable_building_cache(
        destination, configuration, hash_outputs=hash_outputs
    )
    if completed is not None:
        print(f"Reusing completed building cache: {destination}", flush=True)
        return completed

    staging = destination / "staging"
    progress_path = destination / "progress.json"
    if args.force:
        (destination / "manifest.json").unlink(missing_ok=True)
        progress_path.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging)
    checkpoint = read_json(progress_path)
    if checkpoint and checkpoint.get("configuration") != configuration:
        raise RuntimeError(
            f"Building resume state uses a different source/configuration: {progress_path}. "
            "Use the matching command, a different --cache-root, or --force to discard it."
        )
    if not checkpoint and staging.exists():
        inferred = infer_legacy_api_progress(staging, args.api_page_size)
        if inferred:
            checkpoint = {"configuration": configuration, **inferred, "legacy_recovery": True}
            atomic_json(progress_path, checkpoint)
            print(
                f"Recovered legacy API fragments through offset {inferred['next_offset']:,}; "
                "the last uncheckpointed page will be fetched again.",
                flush=True,
            )
    if not checkpoint and staging.exists() and any(staging.iterdir()):
        raise RuntimeError(
            f"Unrecognized building staging data at {staging}; refusing to overwrite it. "
            "Inspect it or pass --force to start over."
        )
    if checkpoint:
        remove_fragment_batch(staging, int(checkpoint["next_batch"]))
    elif staging.exists():
        shutil.rmtree(staging)
    writer = TiledGeoParquetWriter(destination, tile_span_ft=args.tile_span_ft)
    offset = int(checkpoint.get("next_offset", 0)) if checkpoint else 0
    retained = int(checkpoint.get("retained", 0)) if checkpoint else 0
    pages = int(checkpoint.get("pages", 0)) if checkpoint else 0
    writer.batch = int(checkpoint.get("next_batch", 0)) if checkpoint else 0
    download_complete = bool(checkpoint.get("download_complete", False)) if checkpoint else False

    bounds = tuple(map(float, coverage.bounds))
    params = api_parameters(bounds, args.api_page_size)
    session = retrying_session(args)
    total = None
    count_params = {**params, "returnGeometry": "false", "returnCountOnly": "true", "f": "json"}
    count_params.pop("orderByFields", None)
    count_params.pop("resultRecordCount", None)
    count_response = session.get(
        BUILDING_SERVICE,
        params=count_params,
        timeout=120,
    )
    count_response.raise_for_status()
    count_payload = count_response.json()
    if "count" in count_payload:
        total = int(count_payload["count"])
    previous_total = checkpoint.get("total") if checkpoint else None
    if previous_total is not None and total is not None and int(previous_total) != total:
        raise RuntimeError(
            f"Building API count changed during the resumable snapshot "
            f"({int(previous_total):,} -> {total:,}). Resume would mix snapshots; "
            "use --force to deliberately start a fresh snapshot."
        )
    progress = Progress("buildings API", total, unit="features", initial=offset)
    if offset:
        print(
            f"Resuming building API at offset {offset:,} "
            f"({pages:,} committed pages, {retained:,} retained features).",
            flush=True,
        )
    try:
        while not download_complete:
            response = session.get(
                BUILDING_SERVICE,
                params={**params, "resultOffset": offset},
                timeout=120,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                raise RuntimeError(f"Building API error: {payload['error']}")
            features = payload.get("features", [])
            if features:
                frame = gpd.GeoDataFrame.from_features(features)
                retained += add_building_batch(writer, frame, coverage, offset)
            offset += len(features)
            pages += 1
            download_complete = len(features) < args.api_page_size
            if args.limit_building_pages and pages >= args.limit_building_pages:
                download_complete = True
            checkpoint = {
                "configuration": configuration,
                "next_offset": offset,
                "retained": retained,
                "pages": pages,
                "next_batch": writer.batch,
                "total": total,
                "download_complete": download_complete,
                "updated_at": utc_now(),
            }
            atomic_json(progress_path, checkpoint)
            progress.update(offset, detail=f"kept={retained:,} pages={pages:,}")
    finally:
        progress.close(detail=f"kept={retained:,} pages={pages:,}")
    _, outputs = writer.finalize(
        deduplicate_by=["objectid", "doitt_id"],
        source_order=["source_order"],
        hash_outputs=hash_outputs,
        cleanup_staging=False,
    )
    return publish_building_cache(
        destination, configuration, outputs, retained, hash_outputs=hash_outputs,
        production_ready=args.limit_building_pages is None and getattr(args, "bounds", None) is None,
    )


def write_vector(frame: gpd.GeoDataFrame, path: Path, component_dir: Path, hash_outputs: bool):
    if frame.crs is None:
        frame = frame.set_crs(CRS)
    else:
        frame = frame.to_crs(CRS)
    frame = spatial_sort(frame[frame.geometry.notna()].copy())
    atomic_geoparquet(frame, path)
    return output_record(path, component_dir, with_hash=hash_outputs), len(frame)


DATASET_SOURCES = {
    "nyc_parks_trails": DATA / "raw/nyc_parks_trails/parks_trails.csv",
    "nyc_parks_structures": DATA / "raw/nyc_parks_structures/structures.geojson",
    "mta_subway_entrances_2024": DATA / "raw/mta_subway_entrances_2024/subway_entrances.csv",
}
VECTOR_DATASETS = (
    "nyc_building_footprints",
    "nyc_planimetrics_2022",
    *DATASET_SOURCES,
)


def manifest_context(args: argparse.Namespace, coverage, bounds, source: Path) -> tuple[dict, dict]:
    sources = {"source": file_signature(source.resolve(), with_hash=args.hash_sources)}
    if bounds is None:
        sources["coverage_catalog"] = file_signature(args.coverage.resolve())
    configuration = {
        "pipeline_version": PIPELINE_VERSION,
        "cache_format_version": CACHE_FORMAT_VERSION,
        "crs": CRS,
        "coverage_bounds": list(map(float, coverage.bounds)),
        "coverage_catalog": None if bounds else str(args.coverage.resolve()),
        "coverage_mode": "explicit_bounds" if bounds else "catalog_envelope",
    }
    return configuration, sources


def finish_single_dataset(
    component_dir: Path,
    dataset: str,
    configuration: dict,
    sources: dict,
    writer,
    *,
    production_ready: bool,
) -> None:
    existing = reusable_manifest(component_dir, configuration, sources)
    if existing:
        print(f"Cache is current: {component_dir}")
        return
    manifest = start_manifest(component_dir, dataset, configuration, sources)
    started = time.monotonic()
    output, count = writer(component_dir)
    completed = finish_manifest(
        component_dir,
        manifest,
        [output],
        features=count,
        elapsed_seconds=time.monotonic() - started,
        production_ready=production_ready,
    )
    print(
        f"Completed cache: {component_dir}\n"
        f"  features: {count:,}\n"
        f"  size: {completed['output_bytes'] / 1024**2:.1f} MiB"
    )


def main_for(dataset: str) -> None:
    if dataset not in VECTOR_DATASETS:
        raise ValueError(f"Unknown vector dataset: {dataset}")
    args = parser().parse_args()
    if args.tile_span_ft <= 0 or args.api_page_size <= 0 or args.api_retries < 0 or args.api_backoff_seconds < 0:
        raise ValueError(
            "--tile-span-ft and --api-page-size must be positive; retry settings cannot be negative"
        )
    bounds = parse_bounds(args.bounds)
    coverage = box(*load_coverage(args.coverage.resolve(), bounds).bounds)
    component_dir = args.cache_root.resolve() / dataset
    component_dir.mkdir(parents=True, exist_ok=True)
    hash_outputs = not args.skip_output_hashes
    production_ready = bounds is None

    if dataset == "nyc_building_footprints":
        source_signature = {"service": BUILDING_SERVICE, "snapshot_refresh": "use --force"}
        configuration = building_configuration(args, coverage, source_signature)
        _, count = build_buildings_api(component_dir, coverage, args, configuration)
        print(f"Completed cache: {component_dir} ({count:,} features)")
        return

    if dataset == "nyc_planimetrics_2022":
        source = args.planimetrics.resolve()
        if not source.is_dir():
            raise FileNotFoundError(source)
        sources = {"source": directory_signature(source)}
        if bounds is None:
            sources["coverage_catalog"] = file_signature(args.coverage.resolve())
        configuration = {
            "pipeline_version": PIPELINE_VERSION,
            "cache_format_version": CACHE_FORMAT_VERSION,
            "crs": CRS,
            "coverage_bounds": list(map(float, coverage.bounds)),
            "coverage_catalog": None if bounds else str(args.coverage.resolve()),
            "coverage_mode": "explicit_bounds" if bounds else "catalog_envelope",
            "layers": PLANIMETRIC_LAYERS,
        }
        existing = None if args.force else reusable_manifest(component_dir, configuration, sources)
        if existing:
            print(f"Cache is current: {component_dir}")
            return
        manifest = start_manifest(component_dir, dataset, configuration, sources)
        available = {name for name, _ in pyogrio.list_layers(source)}
        missing = sorted(set(PLANIMETRIC_LAYERS) - available)
        if missing:
            raise RuntimeError(f"Missing Planimetrics layers: {', '.join(missing)}")
        progress = Progress(dataset, len(PLANIMETRIC_LAYERS), unit="layers")
        outputs: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for index, name in enumerate(PLANIMETRIC_LAYERS, 1):
            frame = pyogrio.read_dataframe(source, layer=name)
            frame = frame[frame.geometry.notna() & frame.intersects(coverage)].copy()
            record, count = write_vector(
                frame, component_dir / f"{name}.parquet", component_dir, hash_outputs
            )
            outputs.append(record)
            counts[name] = count
            progress.update(index, detail=f"{name} rows={count:,}")
        progress.close()
        finish_manifest(
            component_dir, manifest, outputs, counts=counts, features=sum(counts.values()),
            production_ready=production_ready,
        )
        print(f"Completed cache: {component_dir} ({sum(counts.values()):,} features)")
        return

    source = DATASET_SOURCES[dataset].resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    configuration, sources = manifest_context(args, coverage, bounds, source)

    def write_detail(root: Path):
        if dataset == "nyc_parks_trails":
            frame = pd.read_csv(source, dtype=str)
            frame.columns = [
                re.sub(r"[^a-z0-9]+", "_", str(column).lower()).strip("_")
                for column in frame
            ]
            geometry = shapely.from_wkt(frame["shape"].fillna("").to_numpy(), on_invalid="ignore")
            frame = gpd.GeoDataFrame(
                frame.drop(columns=["shape"]), geometry=geometry, crs=4326
            ).to_crs(CRS)
            frame = frame[frame.geometry.notna() & frame.intersects(coverage)].copy()
            return write_vector(frame, root / "data.parquet", root, hash_outputs)
        if dataset == "nyc_parks_structures":
            frame = gpd.read_file(source).to_crs(CRS)
            frame = frame[frame.geometry.notna() & frame.intersects(coverage)].copy()
            return write_vector(frame, root / "data.parquet", root, hash_outputs)
        if dataset == "mta_subway_entrances_2024":
            frame = pd.read_csv(source)
            frame = gpd.GeoDataFrame(
                frame,
                geometry=gpd.points_from_xy(frame.entrance_longitude, frame.entrance_latitude),
                crs=4326,
            ).to_crs(CRS)
            frame = frame[frame.geometry.notna() & frame.intersects(coverage)].copy()
            return write_vector(frame, root / "data.parquet", root, hash_outputs)
        raise AssertionError(f"Unhandled vector dataset: {dataset}")

    if args.force:
        (component_dir / "manifest.json").unlink(missing_ok=True)
    finish_single_dataset(
        component_dir, dataset, configuration, sources, write_detail,
        production_ready=production_ready,
    )


def main() -> None:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("dataset", choices=VECTOR_DATASETS)
    known, remaining = command.parse_known_args()
    import sys
    sys.argv = [sys.argv[0], *remaining]
    main_for(known.dataset)


if __name__ == "__main__":
    main()
