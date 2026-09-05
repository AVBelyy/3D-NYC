#!/usr/bin/env python3
"""Scan the statewide OSM PBF once and cache the NYC features by spatial tile."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import geopandas as gpd
import osmium
import shapely
from shapely.geometry import Point, box

from cache_common import (
    CACHE_FORMAT_VERSION,
    CRS,
    DATA,
    DEFAULT_CACHE_ROOT,
    DEFAULT_COVERAGE,
    Progress,
    TiledGeoParquetWriter,
    clean_staging,
    file_signature,
    finish_manifest,
    load_coverage,
    parse_bounds,
    reusable_manifest,
    start_manifest,
)


PIPELINE_VERSION = 1
KEYS = (
    "highway", "building", "building:part", "natural", "leisure", "landuse",
    "amenity", "barrier", "man_made", "historic", "railway",
)
AREA_KEYS = frozenset((
    "building", "building:part", "natural", "leisure", "landuse",
    "amenity", "man_made", "historic",
))
LINE_KEYS = frozenset(("highway", "barrier", "railway"))
EXTRA_KEYS = ("name", "width", "bridge", "tunnel", "layer", "surface", "sport", "area")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--source", type=Path, default=DATA / "raw/new_york_osm/new-york-latest.osm.pbf")
    value.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    value.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    value.add_argument("--bounds", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
    value.add_argument("--tile-span-ft", type=float, default=10000.0)
    value.add_argument("--batch-rows", type=int, default=50000)
    value.add_argument("--hash-source", action="store_true", help="Hash the PBF in addition to size/mtime")
    value.add_argument("--skip-output-hashes", action="store_true")
    value.add_argument("--force", action="store_true", help="Rebuild even when the manifest is reusable")
    value.add_argument("--limit-objects", type=int, help=argparse.SUPPRESS)
    return value


def main() -> None:
    args = parser().parse_args()
    if args.tile_span_ft <= 0 or args.batch_rows <= 0:
        raise ValueError("--tile-span-ft and --batch-rows must be positive")
    source = args.source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    bounds = parse_bounds(args.bounds)
    component_dir = args.cache_root.resolve() / "new_york_osm"
    component_dir.mkdir(parents=True, exist_ok=True)
    configuration = {
        "pipeline_version": PIPELINE_VERSION,
        "cache_format_version": CACHE_FORMAT_VERSION,
        "crs": CRS,
        "coverage_bounds": list(bounds) if bounds else None,
        "coverage_catalog": None if bounds else str(args.coverage.resolve()),
        "coverage_mode": "catalog_envelope",
        "tile_span_ft": args.tile_span_ft,
        "keys": list(KEYS),
        "area_keys": sorted(AREA_KEYS),
        "line_keys": sorted(LINE_KEYS),
        "limit_objects": args.limit_objects,
    }
    sources = {"pbf": file_signature(source, with_hash=args.hash_source)}
    if bounds is None:
        sources["coverage_catalog"] = file_signature(args.coverage.resolve())
    if not args.force:
        existing = reusable_manifest(component_dir, configuration, sources)
        if existing:
            print(
                f"OSM cache is current: {component_dir} "
                f"({existing['features']:,} features, {existing['output_bytes'] / 1024**3:.2f} GiB)"
            )
            return

    coverage_2263 = box(*load_coverage(args.coverage.resolve(), bounds).bounds)
    coverage_wgs84 = gpd.GeoSeries([coverage_2263], crs=CRS).to_crs(4326).iloc[0]
    prepared = shapely.prepare(coverage_wgs84)
    # shapely.prepare mutates in place and returns None in Shapely 2.
    if prepared is None:
        prepared = coverage_wgs84
    xmin, ymin, xmax, ymax = coverage_wgs84.bounds
    clean_staging(component_dir)
    manifest = start_manifest(component_dir, "new_york_osm", configuration, sources)
    writer = TiledGeoParquetWriter(component_dir, tile_span_ft=args.tile_span_ft)

    processor = (
        osmium.FileProcessor(source)
        .with_locations()
        .with_areas()
        .with_filter(osmium.filter.KeyFilter(*KEYS))
    )
    factory = osmium.geom.WKBFactory()
    progress = Progress("OSM scan", unit="objects")
    rows: list[dict] = []
    scanned = retained = geometry_errors = duplicated_rows = 0
    error_examples: list[dict[str, str | int]] = []

    def flush() -> None:
        nonlocal rows, duplicated_rows
        if not rows:
            return
        frame = gpd.GeoDataFrame(rows, geometry="geometry", crs=4326).to_crs(CRS)
        duplicated_rows += writer.add(frame)
        rows = []

    started = time.monotonic()
    for obj in processor:
        scanned += 1
        if args.limit_objects and scanned > args.limit_objects:
            break
        try:
            if obj.is_node():
                lon, lat = obj.location.lon, obj.location.lat
                if not (xmin <= lon <= xmax and ymin <= lat <= ymax):
                    continue
                geometry = Point(lon, lat)
                if not shapely.intersects(prepared, geometry):
                    continue
                osm_type, osm_id = "node", obj.id
            elif obj.is_area():
                if not any(key in obj.tags for key in AREA_KEYS):
                    continue
                geometry = shapely.from_wkb(factory.create_multipolygon(obj))
                osm_type = "way" if obj.from_way() else "relation"
                osm_id = obj.orig_id()
            elif obj.is_way():
                if not any(key in obj.tags for key in LINE_KEYS) and obj.tags.get("natural") != "tree_row":
                    continue
                geometry = shapely.from_wkb(factory.create_linestring(obj))
                osm_type, osm_id = "way", obj.id
            else:
                continue
            if not shapely.intersects(prepared, geometry):
                continue
            tags = dict(obj.tags)
            rows.append({
                "osm_type": osm_type,
                "osm_id": int(osm_id),
                "tags": json.dumps(tags, separators=(",", ":"), sort_keys=True),
                "source_order": scanned,
                **{key: tags.get(key) for key in KEYS + EXTRA_KEYS},
                "geometry": geometry,
            })
            retained += 1
            if len(rows) >= args.batch_rows:
                flush()
        except (RuntimeError, ValueError) as error:
            geometry_errors += 1
            if len(error_examples) < 100:
                error_examples.append({"id": int(obj.id), "type": type(obj).__name__, "error": str(error)})
        finally:
            progress.update(scanned, detail=f"kept={retained:,} errors={geometry_errors:,}")
    flush()
    progress.close(detail=f"kept={retained:,} errors={geometry_errors:,}")

    _, outputs = writer.finalize(
        # A closed OSM way may legitimately be emitted once as a way and once
        # as a generated area with the same type/id. source_order distinguishes
        # those records while still removing cross-tile copies.
        deduplicate_by=["source_order"],
        source_order=["source_order"],
        hash_outputs=not args.skip_output_hashes,
    )
    completed = finish_manifest(
        component_dir,
        manifest,
        outputs,
        features=retained,
        tiled_feature_rows=duplicated_rows,
        scanned_objects=scanned,
        geometry_errors=geometry_errors,
        error_examples=error_examples,
        elapsed_seconds=time.monotonic() - started,
        production_ready=args.limit_objects is None and bounds is None,
    )
    print(
        f"Completed OSM cache: {component_dir}\n"
        f"  features: {retained:,} ({duplicated_rows:,} tiled rows)\n"
        f"  size: {completed['output_bytes'] / 1024**3:.2f} GiB"
    )


if __name__ == "__main__":
    main()
