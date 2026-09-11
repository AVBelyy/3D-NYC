#!/usr/bin/env python3
"""Convert the published NYC 2021 DTM/DSM rasters into the canonical LiDAR cache.

The 2021 survey publishes one bare-earth DTM and one DSM per borough -- at the
time of writing 2 US survey foot cells in EPSG:6539, with elevations in US survey
feet, though the converter reads all of that off the rasters rather than assuming
it -- rather than a point cloud. This resamples that pair onto the global grid
`cache_nyc_lidar_2017.py` writes, so the generator and the chunk planner can read
either collection through one contract:

* the published DTM becomes `ground_m`; and
* the published DSM becomes `upper_surface_m`.

Only the *measurements* are equivalent, not their derivation. The 2017 cache bins
individual returns (mean class-2 ground, maximum class 1/2/17/25 upper); these are
vendor-gridded surfaces. The manifest says so -- `source_kind: published rasters`,
naming both published products -- rather than claiming per-class point
aggregation that never happened, so a consumer can tell the two apart.

Four source properties are reconciled here rather than downstream:

* The published projection is reprojected to the cache's EPSG:2263. For
  EPSG:6539 (NAD83(2011)) that is a null transform in PROJ, so it costs nothing.
* Elevations convert from US survey feet to metres with the project's exact
  1200/3937 factor.
* Source cells are resampled to the cache resolution. The DTM is a continuous surface
  and is interpolated; the DSM steps at roof and canopy edges and is sampled
  without blending, so interpolation cannot invent a ramp up a wall for the mesh
  to follow.
* The published DSM is gridded independently of the DTM and dips below it on a
  small fraction of cells, nearly all by single-digit millimetres. The 2017 upper
  surface includes class 2 and so is never below ground; the same invariant is
  restored by raising the upper surface to the ground where the DSM falls below.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
import shapely
from affine import Affine
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window, from_bounds
from scipy.ndimage import distance_transform_edt
from shapely.geometry import box, mapping

from cache_nyc_lidar_2017 import (
    CRS,
    DEFAULT_CHUNK_CELLS,
    DEFAULT_GROUND_FILL_M,
    FT_TO_M,
    PIPELINE_VERSION,
    RESOLUTION_FT,
    RESOLUTION_M,
    atomic_json,
    sha256,
    tile_key,
    write_raster,
)
from download_nyc_lidar_2021 import BOROUGH_ALIASES, DEFAULT_SOURCE, borough_for
from download_data import ROOT

DEFAULT_OUTPUT = ROOT / "data/cache/nyc_lidar_2021"

# The DTM is a continuous surface, so refining 0.6096 m cells to 0.5 m may
# interpolate.  The DSM steps at every roof and canopy edge, and blending across
# one would place cells at heights the survey never measured, on exactly the
# silhouettes the print shows most clearly.
GROUND_RESAMPLING = Resampling.bilinear
UPPER_RESAMPLING = Resampling.nearest


@dataclass(frozen=True)
class BoroughSource:
    name: str
    ground_path: Path
    upper_path: Path
    bounds: tuple[float, float, float, float]
    crs: str
    cell_ft: float
    nodata: float

    def signature(self) -> dict[str, Any]:
        record: dict[str, Any] = {"borough": self.name}
        for role, path in (("ground", self.ground_path), ("upper", self.upper_path)):
            stat = path.stat()
            record[role] = {
                "path": str(path), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            }
        return record


def cache_configuration(chunk_cells: int, fill_cells: int,
                        sources: list[BoroughSource]) -> dict[str, Any]:
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
        "dtype": "float32",
        "vertical_quantization": None,
        "source_kind": "published rasters",
        "source_collection": "NYC 2021 LiDAR",
        # Read off the rasters, so the manifest describes what was converted
        # rather than what the converter expected to be handed.
        "source_crs": sorted({source.crs for source in sources}),
        "source_resolution_ft": sorted({source.cell_ft for source in sources}),
        "source_elevation_units": "US survey feet",
        "ground_measurement": "published bare-earth DTM",
        "upper_measurement": "published DSM, raised to the ground where lower",
        "ground_resampling": GROUND_RESAMPLING.name,
        "upper_resampling": UPPER_RESAMPLING.name,
    }


def open_sources(source_dir: Path, boroughs: list[str] | None) -> list[BoroughSource]:
    """Pair and validate the published borough rasters present on disk.

    Files are matched to a borough and a surface by name, the same synonymy the
    downloader pairs them with, so a renamed publication needs no edit here.
    Every other property -- projection, cell size, nodata -- is read off the
    raster rather than assumed.
    """
    found: dict[str, dict[str, Path]] = {}
    for path in sorted(source_dir.glob("*.tif")):
        borough = borough_for(path.name)
        token = path.stem.lower()
        surface = "ground" if "dtm" in token else "upper" if "dsm" in token else None
        if borough is None or surface is None:
            continue
        if surface in found.get(borough, {}):
            raise SystemExit(
                f"{source_dir} holds two {surface} rasters for {borough}: "
                f"{found[borough][surface].name} and {path.name}"
            )
        found.setdefault(borough, {})[surface] = path

    selected = sorted(boroughs or found)
    sources = []
    for name in selected:
        pair = found.get(name, {})
        missing = [role for role in ("ground", "upper") if role not in pair]
        if missing:
            if boroughs:
                raise SystemExit(
                    f"{name} is missing its {', '.join(missing)} raster under {source_dir}. "
                    "Run scripts/download_nyc_lidar_2021.py first."
                )
            continue
        ground_path, upper_path = pair["ground"], pair["upper"]
        grids = []
        for path in (ground_path, upper_path):
            with rasterio.open(path) as raster:
                if raster.crs is None:
                    raise SystemExit(f"{path} declares no CRS")
                if raster.count != 1:
                    raise SystemExit(f"{path} has {raster.count} bands, expected 1")
                if raster.nodata is None:
                    raise SystemExit(f"{path} declares no nodata value")
                grids.append((tuple(raster.bounds), raster.transform,
                              str(raster.crs), abs(raster.transform.a), raster.nodata))
        # Canopy relief and rooftop-fixture inference read DSM - DTM per cell, so
        # a misaligned pair would shear every one of those differences.
        if grids[0][1] != grids[1][1]:
            raise SystemExit(
                f"{name} DTM and DSM are not on the same grid:\n"
                f"  {grids[0][1]}\n  {grids[1][1]}"
            )
        sources.append(BoroughSource(name, ground_path, upper_path, grids[0][0],
                                     grids[0][2], grids[0][3], grids[0][4]))
    if not sources:
        raise SystemExit(
            f"No published NYC 2021 rasters found under {source_dir}. "
            "Run scripts/download_nyc_lidar_2021.py first."
        )
    return sources


def read_surface(path: Path, transform: Affine, shape: tuple[int, int],
                 resampling: Resampling) -> np.ndarray:
    """Resample one published raster onto a destination cache grid, in metres."""
    destination = np.full(shape, np.nan, np.float32)
    height, width = shape
    bounds = (transform.c, transform.f + height * transform.e,
              transform.c + width * transform.a, transform.f)
    with rasterio.open(path) as source:
        requested = from_bounds(*bounds, transform=source.transform)
        # The interpolator reads a neighbourhood around every destination cell, so
        # the window has to carry one source cell for each destination cell the
        # kernel spans.  Without it the resampling tapers to nodata one cell
        # inside every tile and the cache seams.
        margin = int(math.ceil(abs(transform.a) / abs(source.transform.a))) + 1
        window = Window(
            math.floor(requested.col_off) - margin,
            math.floor(requested.row_off) - margin,
            math.ceil(requested.width) + 2 * margin,
            math.ceil(requested.height) + 2 * margin,
        )
        values = source.read(1, window=window, boundless=True, fill_value=source.nodata)
        reproject(
            values, destination,
            src_transform=source.window_transform(window), src_crs=source.crs,
            src_nodata=source.nodata,
            dst_transform=transform, dst_crs=CRS, dst_nodata=np.nan,
            resampling=resampling,
        )
    finite = np.isfinite(destination)
    destination[finite] = (destination[finite] * FT_TO_M).astype(np.float32)
    return destination


def process_tile(tile_x: int, tile_y: int, sources: list[BoroughSource], output: Path,
                 chunk_cells: int, fill_cells: int,
                 configuration: dict[str, Any]) -> dict[str, Any]:
    """Build one cache tile from every published raster that reaches it."""
    span = chunk_cells * RESOLUTION_FT
    x0, y0 = tile_x * span, tile_y * span
    x1, y1 = x0 + span, y0 + span
    halo = fill_cells
    expanded = chunk_cells + 2 * halo
    expanded_transform = Affine(
        RESOLUTION_FT, 0, x0 - halo * RESOLUTION_FT,
        0, -RESOLUTION_FT, y1 + halo * RESOLUTION_FT,
    )
    reach = box(x0 - halo * RESOLUTION_FT, y0 - halo * RESOLUTION_FT,
                x1 + halo * RESOLUTION_FT, y1 + halo * RESOLUTION_FT)

    shape = (expanded, expanded)
    ground = np.full(shape, np.nan, np.float32)
    upper = np.full(shape, np.nan, np.float32)
    contributing = []
    for source in sources:
        if not box(*source.bounds).intersects(reach):
            continue
        tile_ground = read_surface(source.ground_path, expanded_transform, shape,
                                   GROUND_RESAMPLING)
        tile_upper = read_surface(source.upper_path, expanded_transform, shape,
                                  UPPER_RESAMPLING)
        if not (np.isfinite(tile_ground).any() or np.isfinite(tile_upper).any()):
            continue
        # Boroughs are read in a fixed order and the first valid value wins, so a
        # seam where two published rasters overlap resolves identically on every
        # rebuild rather than depending on which was read last.
        for target, values in ((ground, tile_ground), (upper, tile_upper)):
            take = np.isfinite(values) & ~np.isfinite(target)
            target[take] = values[take]
        contributing.append(source)

    observed = np.isfinite(ground)
    if observed.any():
        distance, nearest = distance_transform_edt(~observed, return_indices=True)
        fill = (~observed) & (distance <= fill_cells)
        ground[fill] = ground[tuple(nearest[:, fill])]
    else:
        fill = np.zeros_like(observed)

    both = np.isfinite(upper) & np.isfinite(ground)
    raised = int(np.count_nonzero(both & (upper < ground)))
    upper[both] = np.fmax(upper[both], ground[both])

    core = np.s_[halo:halo + chunk_cells, halo:halo + chunk_cells]
    key = tile_key(tile_x, tile_y)
    tiles_dir = output / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    transform = Affine(RESOLUTION_FT, 0, x0, 0, -RESOLUTION_FT, y1)
    ground_path = tiles_dir / f"{key}_ground_m.tif"
    upper_path = tiles_dir / f"{key}_upper_surface_m.tif"
    write_raster(ground_path, ground[core], transform)
    write_raster(upper_path, upper[core], transform)

    def output_record(path: Path) -> dict[str, Any]:
        return {"path": f"tiles/{path.name}", "bytes": path.stat().st_size,
                "sha256": sha256(path)}

    record = {
        "status": "completed",
        "key": key,
        "tile_x": tile_x,
        "tile_y": tile_y,
        "bounds_epsg2263_ft": [x0, y0, x1, y1],
        "configuration": configuration,
        "sources": [source.signature() for source in contributing],
        "ground_observed_fraction": float(observed[core].mean()),
        "ground_filled_fraction": float(fill[core].mean()),
        "upper_observed_fraction": float(np.isfinite(upper[core]).mean()),
        "upper_raised_to_ground_cells": raised,
        "ground": output_record(ground_path),
        "upper": output_record(upper_path),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json(tiles_dir / f"{key}.json", record)
    return record


def planned_tiles(sources: list[BoroughSource], requested: shapely.Geometry | None,
                  chunk_cells: int) -> list[tuple[int, int]]:
    """Select every grid tile a published raster reaches."""
    span = chunk_cells * RESOLUTION_FT
    coverage = shapely.union_all([box(*source.bounds) for source in sources])
    area = coverage if requested is None else coverage.intersection(requested)
    if area.is_empty:
        raise SystemExit("No published NYC 2021 coverage intersects the requested bounds")
    xmin, ymin, xmax, ymax = area.bounds
    tiles = []
    for tile_y in range(math.floor(ymin / span), math.ceil(ymax / span)):
        for tile_x in range(math.floor(xmin / span), math.ceil(xmax / span)):
            if box(tile_x * span, tile_y * span,
                   (tile_x + 1) * span, (tile_y + 1) * span).intersects(area):
                tiles.append((tile_x, tile_y))
    return tiles


def write_catalog(output: Path, results: list[dict[str, Any]]) -> None:
    features = []
    for result in results:
        geometry = gpd.GeoSeries([box(*result["bounds_epsg2263_ft"])],
                                 crs=2263).to_crs(4326).iloc[0]
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
    atomic_json(output / "catalog.geojson",
                {"type": "FeatureCollection", "features": features})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--bounds", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
        help="Optional EPSG:2263 bounds; the whole published extent is built without them",
    )
    parser.add_argument(
        "--borough", action="append", dest="boroughs", choices=sorted(BOROUGH_ALIASES),
        help="Restrict to one borough's rasters; repeatable (default: every pair on disk)",
    )
    parser.add_argument("--chunk-cells", type=int, default=DEFAULT_CHUNK_CELLS)
    parser.add_argument(
        "--ground-fill-m", type=float, default=DEFAULT_GROUND_FILL_M,
        help="Nearest-ground fill reach, matching the 2017 builder's contract",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.chunk_cells < 256 or args.chunk_cells % 256:
        raise SystemExit("--chunk-cells must be a multiple of 256 and at least 256")
    output = args.output_dir.resolve()
    sources = open_sources(args.source_dir.resolve(), args.boroughs)
    fill_cells = int(round(args.ground_fill_m / RESOLUTION_M))
    requested = box(*args.bounds) if args.bounds else None
    tiles = planned_tiles(sources, requested, args.chunk_cells)
    configuration = cache_configuration(args.chunk_cells, fill_cells, sources)

    print(f"Published rasters: {', '.join(source.name for source in sources)}")
    print(f"Planned cache tiles: {len(tiles)}")
    if args.dry_run:
        for tile_x, tile_y in tiles:
            print(f"  {tile_key(tile_x, tile_y)}")
        return

    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    atomic_json(manifest_path, {"status": "building", "configuration": configuration})

    results = []
    for number, (tile_x, tile_y) in enumerate(tiles, 1):
        key = tile_key(tile_x, tile_y)
        metadata_path = output / "tiles" / f"{key}.json"
        if metadata_path.is_file() and not args.overwrite:
            existing = json.loads(metadata_path.read_text())
            if existing.get("configuration") == configuration:
                results.append(existing)
                print(f"Tile {number}/{len(tiles)}: {key} (reused)", flush=True)
                continue
        print(f"Tile {number}/{len(tiles)}: {key}", flush=True)
        results.append(process_tile(tile_x, tile_y, sources, output,
                                    args.chunk_cells, fill_cells, configuration))

    write_catalog(output, results)
    complete = args.bounds is None and len(sources) == len(BOROUGH_ALIASES)
    atomic_json(manifest_path, {
        "status": "complete",
        "configuration": configuration,
        "coverage_mode": "all" if args.bounds is None else "bounds",
        "requested_bounds_epsg2263_ft": list(args.bounds) if args.bounds else None,
        "boroughs": [source.name for source in sources],
        "chunks": len(results),
        "ground_bytes": sum(result["ground"]["bytes"] for result in results),
        "upper_bytes": sum(result["upper"]["bytes"] for result in results),
        "catalog": "catalog.geojson",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "tiles": [f"tiles/{result['key']}.json" for result in results],
        "component": "nyc_lidar_2021",
        "production_ready": complete,
    })
    print(f"Cache ready: {output} ({len(results)} tiles)")
    if not complete:
        print(
            "Note: this cache is partial (bounded, or missing boroughs). It is usable "
            "for generation over its own extent, but the chunk planner and the vector "
            "cache builders require a production-ready citywide cache."
        )


if __name__ == "__main__":
    main()
