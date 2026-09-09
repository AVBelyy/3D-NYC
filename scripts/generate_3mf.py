#!/usr/bin/env python3
"""Generate a detailed four-color NYC map 3MF from one command.

Reusable inputs are shared under ``data/cache``.  Every generated region is
isolated under ``output/jobs/<job-id>`` and can be resumed safely.  The script
does not connect to a printer; optional slicing is offline Bambu Studio CLI
validation only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import geopandas as gpd
import laspy
import numpy as np
import pandas as pd
import pyogrio
import rasterio
import requests
import shapely
import structlog
from lxml import etree
from affine import Affine
from rasterio.warp import Resampling, reproject
from scipy.ndimage import distance_transform_edt
from shapely.geometry import Point, Polygon, box, shape

from _datasets import DATASETS
from download_data import MIN_FREE, ROOT, download
from cache_common import read_tiled_geoparquet
from crossings import structural_roof_thickness_mm
from _material_layers import (
    DRAWN_LINE_RELIEF_MM, FIRST_LAYER_HEIGHT_MM, MATERIAL_NAMES, pavement_pad_relief_mm,
    printable_width_mm,DRAWN_LINE_BEADS,MINIMUM_FEATURE_BEADS,
    surface_color_depth_mm)
from road_symbols import TRAIL_HIGHWAYS


PIPELINE_VERSION = 25
DETAIL_PIPELINE_VERSION = 2
FIELD_PIPELINE_VERSION = 9
CROSSING_VALIDATION_VERSION = 2
MESH_PIPELINE_VERSION = 24
PACKAGE_PIPELINE_VERSION = 9
VALIDATION_PIPELINE_VERSION = 4
SLICE_PIPELINE_VERSION = 2
FT = 0.3048006096012192
PLATE_MM = 256.0
PLATE_EDGE_CLEARANCE_MM = 0.1
MODEL_BRIM_GAP_MM = 0.1
MODEL_MAX_BRIM_MM = 3.0
PRIME_TOWER_WIDTH_MM = 35.0
PRIME_TOWER_BRIM_MM = 2.0
PRIME_TOWER_POSITION_MM = (214.0, 80.0)
# Keep at least one nozzle width between generated brim envelopes.  Expressed
# as a bead rather than a millimetre count so it follows --nozzle-mm.
PRIME_TOWER_CLEARANCE_BEADS = 1.0
SCRIPT_DIR = Path(__file__).resolve().parent
# Bambu names its 0.4 mm presets without a suffix and every other size with one,
# and each nozzle offers its own layer heights: a 0.6 mm nozzle cannot lay a
# 0.08 mm layer and a 0.2 mm one cannot lay 0.24.  These are the P2S presets
# Bambu Studio ships, so --layer-height is checked against the selected nozzle's
# row rather than against one global list.
PROCESS_PRESETS = {
    0.2: {
        0.08: "0.08mm High Quality @BBL P2S 0.2 nozzle",
        0.10: "0.10mm Standard @BBL P2S 0.2 nozzle",
        0.12: "0.12mm Balanced Quality @BBL P2S 0.2 nozzle",
    },
    0.4: {
        0.08: "0.08mm High Quality @BBL P2S",
        0.12: "0.12mm High Quality @BBL P2S",
        0.16: "0.16mm Standard @BBL P2S",
        0.20: "0.20mm Standard @BBL P2S",
        0.24: "0.24mm Standard @BBL P2S",
    },
    0.6: {
        0.18: "0.18mm Balanced Quality @BBL P2S 0.6 nozzle",
        0.24: "0.24mm Balanced Quality @BBL P2S 0.6 nozzle",
        0.30: "0.30mm Standard @BBL P2S 0.6 nozzle",
    },
    0.8: {
        0.24: "0.24mm Balanced Quality @BBL P2S 0.8 nozzle",
        0.32: "0.32mm Balanced Quality @BBL P2S 0.8 nozzle",
        0.40: "0.40mm Standard @BBL P2S 0.8 nozzle",
    },
}
DEFAULT_NOZZLE_MM = 0.4


def machine_preset(nozzle_mm: float) -> str:
    """Return the installed P2S machine preset for a nozzle size."""
    return f"Bambu Lab P2S {nozzle_mm:g} nozzle"
NYC_BOUNDS = (-74.27, 40.47, -73.68, 40.93)
MATERIAL_COLORS = ["#F2F0E8", "#5FAA72", "#A9D5DF", "#C79A61"]
BUILDING_COLOR_ALIASES = {
    "ivory": 0,
    "white": 0,
    "green": 1,
    "blue": 2,
    "tan": 3,
    "brown": 3,
    **{color.lower(): index for index, color in enumerate(MATERIAL_COLORS)},
}
GEOSEARCH_SERVICE = "https://geosearch.planninglabs.nyc/v2/search"
CORE_DOWNLOADS = {
    "citygml": DATASETS["nyc_3d_buildings_2014"],
    "buildings": DATASETS["nyc_building_footprints"],
    "planimetrics": DATASETS["nyc_planimetrics_2022"],
    "trails": DATASETS["nyc_parks_trails"],
    "landcover": DATASETS["nyc_land_cover_2017"],
    "osm": DATASETS["new_york_osm"],
    "parks_structures": DATASETS["nyc_parks_structures"],
    "mta_entrances": DATASETS["mta_subway_entrances_2024"],
}
LIDAR_GRID_SERVICE = (
    "https://services6.arcgis.com/yG5s3afENB5iO9fj/arcgis/rest/services/"
    "NYC_2017_LiDAR_TopoBathymetric_LAS_Tile_Grid_Index/FeatureServer"
)
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


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def configure_logging(path: Path, level: str):
    class StructuredEventFilter(logging.Filter):
        def filter(self, record):
            return record.getMessage().lstrip().startswith("{")

    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper()))
    formatter = logging.Formatter("%(message)s")
    for handler in [logging.StreamHandler(sys.stdout), logging.FileHandler(path)]:
        handler.setFormatter(formatter)
        handler.addFilter(StructuredEventFilter())
        root.addHandler(handler)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger("generate_3mf")


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str))
    temporary.replace(path)


def normalize_columns(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    frame = frame.copy()
    frame.columns = [
        "geometry" if column == frame.geometry.name else re.sub(r"[^a-z0-9]+", "_", str(column).lower()).strip("_")
        for column in frame.columns
    ]
    frame = frame.set_geometry("geometry")
    return frame


def empty_geodata(columns: Iterable[str], crs=2263) -> gpd.GeoDataFrame:
    data = {column: pd.Series(dtype="object") for column in columns if column != "geometry"}
    return gpd.GeoDataFrame(data, geometry=gpd.GeoSeries([], crs=crs), crs=crs)


def read_geoparquet_bbox(path: Path, bounds) -> gpd.GeoDataFrame:
    """Read a spatially sorted GeoParquet subset, with old-PyArrow fallback."""
    try:
        return gpd.read_parquet(path, bbox=tuple(bounds))
    except ValueError:
        return gpd.read_parquet(path)


def count_osm_semantic_features(
    osm: gpd.GeoDataFrame, source_aoi_2263,
) -> dict[str, int]:
    """Count OSM QA references inside the actual buffered source polygon.

    ``extract_osm.py`` reads by bounding box, which can contain a large amount
    of unrelated geometry around a rotated or irregular crop.  Semantic QA
    must compare sources over the same footprint, not over that bounding box.
    """
    if osm.crs is None:
        raise ValueError("OSM semantic validation requires a declared CRS")
    source_aoi = gpd.GeoSeries([source_aoi_2263], crs=2263).to_crs(osm.crs).iloc[0]
    usable = osm.geometry.notna() & ~osm.geometry.is_empty
    in_source_aoi = usable & osm.geometry.intersects(source_aoi)
    relevant = osm.loc[in_source_aoi]

    building_mask = osm.building.notna() & osm.geom_type.isin(["Polygon", "MultiPolygon"])
    motor_road_mask = (
        osm.highway.notna()
        & ~osm.highway.isin(TRAIL_HIGHWAYS | {"pedestrian"})
        & osm.geom_type.eq("LineString")
    )
    relevant_buildings = relevant.building.notna() & relevant.geom_type.isin(["Polygon", "MultiPolygon"])
    relevant_motor_roads = (
        relevant.highway.notna()
        & ~relevant.highway.isin(TRAIL_HIGHWAYS | {"pedestrian"})
        & relevant.geom_type.eq("LineString")
    )
    return {
        "osm_building_footprints": int(relevant_buildings.sum()),
        "osm_building_footprints_unfiltered": int(building_mask.sum()),
        "osm_motor_road_segments": int(relevant_motor_roads.sum()),
        "osm_motor_road_segments_unfiltered": int(motor_road_mask.sum()),
    }


class Pipeline:
    def __init__(self, args, config: dict, job: Path, output: Path, reference: Path, profiles: Path, slicer: Path):
        self.args = args
        self.config = config
        self.job = job
        self.output = output
        self.processed = job / "processed"
        self.work = job / "work"
        self.analysis = job / "analysis"
        self.validation = job / "validation"
        self.logs = job / "logs"
        self.stages = job / "stages"
        self.data_dir = args.data_dir.resolve()
        self.raw = self.data_dir / "raw"
        self.cache_dir = (
            Path(args.cache_dir).resolve() if args.cache_dir is not None
            else (self.data_dir / "cache").resolve()
        )
        self.lidar_source = args.lidar_source
        configured_lidar_cache = config.get("lidar_cache_dir")
        if configured_lidar_cache:
            self.lidar_cache_dir = Path(configured_lidar_cache).resolve()
        elif args.lidar_cache_dir is not None:
            self.lidar_cache_dir = Path(args.lidar_cache_dir).resolve()
        else:
            self.lidar_cache_dir = (self.cache_dir / "nyc_lidar_2017").resolve()
        self.download_manifests = self.raw
        self.reference = reference.resolve()
        self.profiles = profiles.resolve()
        self.slicer = slicer.resolve()
        for folder in [self.processed, self.work, self.analysis, self.validation, self.logs, self.stages]:
            folder.mkdir(parents=True, exist_ok=True)
        self.log = configure_logging(self.logs / "pipeline.jsonl", args.log_level).bind(job_id=job.name)
        self.config_hash = hashlib.sha256(canonical(config).encode()).hexdigest()
        if "aoi_wgs84" in config:
            exact_wgs = shape(config["aoi_wgs84"])
            exact = gpd.GeoSeries([exact_wgs], crs=4326).to_crs(2263).iloc[0]
        else:
            # Compatibility with configurations generated before polygon AOIs.
            projected_center = gpd.GeoSeries(
                [Point(*config["center_wgs84"])], crs=4326
            ).to_crs(2263).iloc[0]
            scale = config["scale_denominator"]
            k = FT * 1000 / scale
            width, height = config["size_mm"]
            exact = box(
                projected_center.x - width / (2 * k),
                projected_center.y - height / (2 * k),
                projected_center.x + width / (2 * k),
                projected_center.y + height / (2 * k),
            )
        self.exact_aoi_2263 = exact
        self.source_aoi_2263 = exact.buffer(args.source_padding_m / FT)
        self.source_aoi_wgs = gpd.GeoSeries([self.source_aoi_2263], crs=2263).to_crs(4326).iloc[0]
        self.env = os.environ.copy()
        self.env.update(
            MAP_CONFIG=str(job / "config.json"),
            MAP_WORK_DIR=str(self.work),
            NYC_PROCESSED_DIR=str(self.processed),
            NYC_ANALYSIS_DIR=str(self.analysis),
            NYC_VALID_DIR=str(self.validation),
            NYC_DATA_DIR=str(self.data_dir),
            NYC_RAW_DIR=str(self.raw),
            NYC_DOWNLOAD_MANIFEST_DIR=str(self.download_manifests),
            NYC_CACHE_DIR=str(self.cache_dir),
            NYC_OUTPUT_DIR=str(args.output_dir.resolve()),
            PYTHONPATH=os.pathsep.join(filter(None, [str(SCRIPT_DIR), os.environ.get("PYTHONPATH", "")])),
        )

    def cached_dataset(self, name: str) -> tuple[Path, dict] | None:
        component = self.cache_dir / name
        manifest_path = component / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Could not read cache manifest: {manifest_path}") from error
        if manifest.get("status") != "complete" or manifest.get("production_ready") is False:
            raise RuntimeError(f"Cache is not production-ready: {manifest_path}")
        for record in manifest.get("outputs", []):
            path = component / record.get("path", "")
            if not path.is_file() or path.stat().st_size != record.get("bytes"):
                raise RuntimeError(f"Cache output is missing or changed: {path}")
        return component, manifest

    def cache_identity(self, name: str):
        result = self.cached_dataset(name)
        if result is None:
            return None
        component, manifest = result
        manifest_path = component / "manifest.json"
        return {
            "path": str(component),
            "completed_at": manifest.get("completed_at"),
            "bytes": manifest.get("output_bytes"),
            "manifest_mtime_ns": manifest_path.stat().st_mtime_ns,
        }

    def stage(
        self,
        name: str,
        outputs: Iterable[Path],
        action: Callable[[], None],
        variant=None,
        cacheable: bool = True,
    ):
        outputs = list(outputs)
        output_names = [str(path) for path in outputs]
        state_path = self.stages / f"{name}.json"
        if cacheable and not self.args.force and state_path.exists():
            state = json.loads(state_path.read_text())
            if (
                state.get("status") == "completed"
                and state.get("config_sha256") == self.config_hash
                and state.get("variant") == variant
                and state.get("outputs") == output_names
                and all(path.exists() for path in outputs)
            ):
                self.log.info("stage_skipped", stage=name, reason="valid_cached_outputs")
                return
        started = time.monotonic()
        state = {
            "stage": name,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "config_sha256": self.config_hash,
            "variant": variant,
            "outputs": output_names,
        }
        atomic_json(state_path, state)
        self.log.info("stage_started", stage=name, outputs=len(outputs))
        try:
            action()
            missing = [str(path) for path in outputs if not path.exists()]
            if missing:
                raise RuntimeError(f"Stage completed without required outputs: {missing}")
            state.update(
                status="completed",
                elapsed_seconds=time.monotonic() - started,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            atomic_json(state_path, state)
            self.log.info("stage_completed", stage=name, elapsed_seconds=state["elapsed_seconds"])
        except BaseException as error:
            state.update(
                status="failed",
                elapsed_seconds=time.monotonic() - started,
                finished_at=datetime.now(timezone.utc).isoformat(),
                error_type=type(error).__name__,
                error=str(error),
                traceback=traceback.format_exc(),
            )
            atomic_json(state_path, state)
            self.log.error("stage_failed", stage=name, elapsed_seconds=state["elapsed_seconds"], exc_info=True)
            raise

    def ensure_download(self, key: str) -> Path:
        url, relative = CORE_DOWNLOADS[key]
        path = self.raw / relative
        if path.exists() and path.stat().st_size > 0:
            self.log.info("download_cache_hit", dataset=key, path=str(path), bytes=path.stat().st_size)
            return path
        if self.args.offline:
            raise FileNotFoundError(f"Offline mode: missing required dataset {key}: {path}")
        self.log.info("download_started", dataset=key, url=url, target=str(path))
        result = download(url, relative, raw_dir=self.raw, manifests_dir=self.download_manifests)
        self.log.info("download_completed", dataset=key, path=str(result), bytes=result.stat().st_size)
        return result

    def ensure_planimetrics(self) -> Path:
        archive = self.ensure_download("planimetrics")
        folder = self.raw / "nyc_planimetrics_2022/Planimetric_2022.gdb"
        if folder.exists():
            return folder
        with zipfile.ZipFile(archive) as source:
            expanded = sum(item.file_size for item in source.infolist())
            if shutil.disk_usage(archive.parent).free - expanded < MIN_FREE:
                raise RuntimeError("Insufficient disk space to extract Planimetrics while retaining 15 GiB")
            for item in source.infolist():
                if not (archive.parent / item.filename).resolve().is_relative_to(archive.parent.resolve()):
                    raise RuntimeError(f"Unsafe Planimetrics archive member: {item.filename}")
            source.extractall(archive.parent)
        return folder

    def ensure_lidar_index(self) -> gpd.GeoDataFrame:
        path = self.raw / "nyc_lidar_2017/index.geojson"
        if path.exists():
            return gpd.read_file(path)
        if self.args.offline:
            raise FileNotFoundError(f"Offline mode: missing LiDAR index {path}")
        metadata = requests.get(LIDAR_GRID_SERVICE, params={"f": "json"}, timeout=60).json()
        layer = LIDAR_GRID_SERVICE + "/" + str(metadata["layers"][0]["id"])
        features = []
        offset = 0
        while True:
            response = requests.get(
                layer + "/query",
                params={
                    "where": "1=1", "outFields": "*", "f": "geojson", "outSR": 4326,
                    "resultOffset": offset, "resultRecordCount": 1000, "orderByFields": "OBJECTID",
                }, timeout=90,
            )
            response.raise_for_status()
            batch = response.json().get("features", [])
            features.extend(batch)
            offset += len(batch)
            if len(batch) < 1000:
                break
        path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        return gpd.read_file(path)

    def ensure_lidar_cache(self) -> tuple[Path, dict, gpd.GeoDataFrame]:
        """Validate and open the canonical LiDAR raster cache."""
        cache = self.lidar_cache_dir
        manifest_path = cache / "manifest.json"
        catalog_path = cache / "catalog.geojson"
        if not manifest_path.is_file() or not catalog_path.is_file():
            raise FileNotFoundError(
                "LiDAR cache is missing or incomplete: "
                f"expected {manifest_path} and {catalog_path}. "
                "Run scripts/cache_nyc_lidar_2017.py first, or use --lidar-source laz."
            )
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Could not read LiDAR cache manifest: {manifest_path}") from error
        if manifest.get("status") != "complete":
            raise RuntimeError(
                f"LiDAR cache manifest is not complete ({manifest.get('status')!r}): {manifest_path}"
            )
        configuration = manifest.get("configuration", {})
        required = {
            "pipeline_version": 1,
            "crs": "EPSG:2263",
            "resolution_m": 0.5,
            "elevation_units": "metres NAVD88",
            "dtype": "float32",
            "ground_fill_max_distance_m": 10.0,
            "ground_classes": [2],
            "upper_classes": [1, 2, 17, 25],
            "ground_aggregation": "arithmetic mean",
            "upper_aggregation": "maximum",
            "withheld_points_excluded": True,
            "vertical_quantization": None,
        }
        incompatible = {
            key: (configuration.get(key), expected)
            for key, expected in required.items()
            if configuration.get(key) != expected
        }
        if incompatible:
            raise RuntimeError(
                f"LiDAR cache configuration is incompatible with generate_3mf: {incompatible}"
            )
        try:
            catalog = gpd.read_file(catalog_path)
        except Exception as error:
            raise RuntimeError(f"Could not read LiDAR cache catalog: {catalog_path}") from error
        if catalog.empty:
            raise RuntimeError(f"LiDAR cache catalog contains no raster tiles: {catalog_path}")
        if catalog.crs is None:
            catalog = catalog.set_crs(4326)
        catalog = catalog.to_crs(2263)
        return cache, manifest, catalog

    def download_sources(self):
        # Validate a local cache before downloading any of the other shared
        # inputs.  An in-progress cache should fail fast instead of leaving a
        # large partially prepared generation job behind.
        if self.lidar_source == "cache":
            cache, manifest, catalog = self.ensure_lidar_cache()
            if catalog[catalog.intersects(self.source_aoi_2263)].empty:
                raise RuntimeError(
                    f"LiDAR cache has no tiles intersecting the requested source area: {cache}"
                )
        cached_sources = {
            "citygml": "nyc_3d_buildings_2014",
            "buildings": "nyc_building_footprints",
            "planimetrics": "nyc_planimetrics_2022",
            "trails": "nyc_parks_trails",
            "landcover": "nyc_land_cover_2017",
            "osm": "new_york_osm",
            "parks_structures": "nyc_parks_structures",
            "mta_entrances": "mta_subway_entrances_2024",
        }
        available_components = {
            component for component in set(cached_sources.values())
            if self.cached_dataset(component) is not None
        }
        for key in CORE_DOWNLOADS:
            component = cached_sources.get(key)
            if component in available_components:
                self.log.info("cached_source_ready", dataset=key, component=component)
                continue
            self.ensure_download(key)
        if "nyc_planimetrics_2022" not in available_components:
            self.ensure_planimetrics()
        if self.lidar_source == "cache":
            self.log.info(
                "lidar_cache_ready",
                path=str(cache),
                tiles=len(catalog),
                source_laz_count=manifest.get("source_laz_count"),
            )
            atomic_json(
                self.job / "raw_ready.json",
                {
                    "datasets": list(CORE_DOWNLOADS),
                    "lidar_source": "cache",
                    "lidar_cache_dir": str(cache),
                    "lidar_cache_tiles": len(catalog),
                },
            )
            return
        index = self.ensure_lidar_index()
        selected = index[index.intersects(self.source_aoi_wgs)].copy()
        if selected.empty:
            raise RuntimeError("No NYC 2017 LiDAR tiles intersect the requested area")
        selected.to_file(self.processed / "lidar_grid_aoi.geojson", driver="GeoJSON")
        self.log.info("lidar_tiles_selected", count=len(selected), ids=selected.LAS_ID.astype(str).tolist())
        for _, row in selected.iterrows():
            name = str(row.LAS_ID) + ".laz"
            path = self.raw / "nyc_lidar_2017/tiles" / name
            if path.exists() and path.stat().st_size > 1000:
                self.log.info("download_cache_hit", dataset="lidar_tile", tile=name, bytes=path.stat().st_size)
                continue
            if self.args.offline:
                raise FileNotFoundError(f"Offline mode: missing LiDAR tile {path}")
            download(
                row.azure_url, f"nyc_lidar_2017/tiles/{name}",
                raw_dir=self.raw, manifests_dir=self.download_manifests,
            )
        atomic_json(
            self.job / "raw_ready.json",
            {"datasets": list(CORE_DOWNLOADS), "lidar_tiles": selected.LAS_ID.astype(str).tolist()},
        )

    def fetch_buildings_api(self) -> gpd.GeoDataFrame:
        cache = self.job / "source_cache/buildings.geojson"
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            payload = json.loads(cache.read_text())
            if not payload.get("features"):
                return empty_geodata(["doitt_id", "height_roof", "geometry"])
            return normalize_columns(gpd.read_file(cache))
        bounds = self.source_aoi_2263.bounds
        features = []
        offset = 0
        while True:
            response = requests.get(
                BUILDING_SERVICE,
                params={
                    "where": "1=1", "outFields": "*", "returnGeometry": "true",
                    "geometry": ",".join(f"{number:.3f}" for number in bounds),
                    "geometryType": "esriGeometryEnvelope", "inSR": 2263, "outSR": 2263,
                    "spatialRel": "esriSpatialRelIntersects", "f": "geojson",
                    "resultOffset": offset, "resultRecordCount": 2000,
                }, timeout=120,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                raise RuntimeError(f"Building API error: {payload['error']}")
            batch = payload.get("features", [])
            features.extend(batch)
            offset += len(batch)
            if len(batch) < 2000:
                break
        cache.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        if not features:
            return empty_geodata(["doitt_id", "height_roof", "geometry"])
        frame = normalize_columns(gpd.read_file(cache))
        # RFC 7946 makes GeoJSON readers label the response as WGS84 even when
        # ArcGIS honors outSR=2263 and serializes projected feet.  The numeric
        # coordinates are around (1,000,000, 200,000), so transforming that
        # incorrectly labelled frame yields no NYC intersections.
        return frame.set_crs(2263, allow_override=True)

    def buildings_from_csv(self) -> gpd.GeoDataFrame:
        path = self.ensure_download("buildings")
        bounds = self.source_aoi_wgs.bounds
        parts = []
        for chunk in pd.read_csv(path, dtype=str, chunksize=100000):
            chunk.columns = [re.sub(r"[^a-z0-9]+", "_", str(c).lower()).strip("_") for c in chunk]
            geom = shapely.from_wkt(chunk["the_geom"].fillna("").to_numpy(), on_invalid="ignore")
            valid = ~pd.isna(geom)
            if not valid.any():
                continue
            candidate = gpd.GeoDataFrame(chunk.loc[valid].drop(columns=["the_geom"]), geometry=geom[valid], crs=4326)
            parts.append(candidate[candidate.intersects(box(*bounds))])
        if not parts:
            return empty_geodata(["doitt_id", "height_roof", "geometry"])
        frame = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=4326).to_crs(2263)
        for column in ["height_roof", "ground_elevation", "construction_year"]:
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        # The downloaded WGS84 CSV has a dataset-wide transform offset relative
        # to the publisher's EPSG:2263 service.  The earlier citywide audit found
        # an exceptionally stable median CSV-minus-service delta of
        # (-0.4005 ft, +3.0135 ft); remove it before matching 2014 CityGML roofs.
        frame["geometry"] = frame.geometry.translate(xoff=0.4005, yoff=-3.0135)
        self.log.warning(
            "building_api_fallback",
            reason="using downloaded CSV with audited dataset-wide EPSG:2263 correction",
            correction_ft={"x": 0.4005, "y": -3.0135},
        )
        return frame

    def geocode_address(self, address: str) -> dict:
        """Resolve and cache one NYC address using the public Planning GeoSearch API."""
        key = hashlib.sha256(address.strip().casefold().encode()).hexdigest()
        cache = self.cache_dir / "nyc_geosearch" / f"{key}.json"
        if cache.exists():
            payload = json.loads(cache.read_text())
        else:
            if self.args.offline:
                raise FileNotFoundError(
                    f"Offline mode: no cached NYC GeoSearch result exists for {address!r} at {cache}"
                )
            try:
                response = requests.get(
                    GEOSEARCH_SERVICE,
                    params={"text": address, "size": 5},
                    headers={"User-Agent": "3D-NYC generate_3mf address resolver"},
                    timeout=60,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, requests.JSONDecodeError) as error:
                raise RuntimeError(f"NYC GeoSearch failed for address {address!r}: {error}") from error
            atomic_json(cache, payload)
        features = payload.get("features", [])
        usable = [
            feature for feature in features
            if feature.get("geometry", {}).get("type") == "Point"
            and len(feature.get("geometry", {}).get("coordinates", [])) >= 2
        ]
        if not usable:
            raise ValueError(f"NYC GeoSearch returned no point result for address {address!r}")
        # Prefer an authoritative PAD result carrying a BIN.  The API ranking
        # still determines which result wins among equally useful candidates.
        feature = next(
            (
                item for item in usable
                if (item.get("properties", {}).get("addendum", {}).get("pad", {}).get("bin")
                    or item.get("properties", {}).get("pad_bin"))
            ),
            usable[0],
        )
        properties = feature.get("properties", {})
        pad = properties.get("addendum", {}).get("pad", {})
        longitude, latitude = map(float, feature["geometry"]["coordinates"][:2])
        if not (
            NYC_BOUNDS[0] <= longitude <= NYC_BOUNDS[2]
            and NYC_BOUNDS[1] <= latitude <= NYC_BOUNDS[3]
        ):
            raise ValueError(
                f"NYC GeoSearch resolved {address!r} outside the supported NYC bounds: "
                f"{latitude:.7f}, {longitude:.7f}"
            )
        return {
            "address": address,
            "label": properties.get("label") or properties.get("name") or address,
            "latitude": latitude,
            "longitude": longitude,
            "bin": pad.get("bin") or properties.get("pad_bin"),
            "bbl": pad.get("bbl") or properties.get("pad_bbl"),
            "confidence": properties.get("confidence"),
            "cache": str(cache),
        }

    @staticmethod
    def identifier_mask(buildings: gpd.GeoDataFrame, selector: str, value: str) -> pd.Series:
        columns = {
            "doitt_id": ["doitt_id"],
            "bin": ["bin"],
            "bbl": ["base_bbl", "mappluto_bbl", "map_pluto_bbl"],
        }[selector]
        result = pd.Series(False, index=buildings.index)
        for column in columns:
            if column not in buildings:
                continue
            normalized = buildings[column].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
            result |= normalized.eq(value)
        return result

    @staticmethod
    def matched_building_records(buildings: gpd.GeoDataFrame) -> list[dict]:
        records = []
        for _, row in buildings.iterrows():
            record = {}
            for column in ["doitt_id", "bin", "base_bbl", "mappluto_bbl", "map_pluto_bbl"]:
                if column not in buildings or pd.isna(row[column]):
                    continue
                record[column] = re.sub(r"\.0$", "", str(row[column]).strip())
            record["centroid_epsg2263_ft"] = [float(row.geometry.centroid.x), float(row.geometry.centroid.y)]
            records.append(record)
        return records

    def buildings_at_coordinate(
        self, buildings: gpd.GeoDataFrame, latitude: float, longitude: float
    ) -> gpd.GeoDataFrame:
        point = gpd.GeoSeries([Point(longitude, latitude)], crs=4326).to_crs(2263).iloc[0]
        matches = buildings[buildings.geometry.covers(point)].copy()
        if matches.empty:
            if buildings.empty:
                detail = "there are no building footprints in the model area"
            else:
                distances = buildings.geometry.distance(point)
                nearest = buildings.loc[[distances.idxmin()]]
                detail = (
                    f"the nearest footprint is {float(distances.min()) * FT:.2f} m away "
                    f"({self.matched_building_records(nearest)[0]})"
                )
            raise ValueError(
                f"Building coordinate {latitude:.7f}, {longitude:.7f} does not fall inside a "
                f"building footprint in the exact model area; {detail}"
            )
        # Boundary points can touch adjacent polygons.  Accept several matches
        # only when they share one stable building identifier.
        if len(matches) > 1:
            identities = []
            for _, row in matches.iterrows():
                identity = next(
                    (
                        (column, re.sub(r"\.0$", "", str(row[column]).strip()))
                        for column in ["doitt_id", "bin"]
                        if column in matches and not pd.isna(row[column])
                    ),
                    None,
                )
                identities.append(identity)
            if len(set(identities)) > 1:
                raise ValueError(
                    f"Building coordinate {latitude:.7f}, {longitude:.7f} touches multiple "
                    f"footprints: {self.matched_building_records(matches)}; use BIN or DoITT ID"
                )
        seed = matches.iloc[0]
        for selector in ["doitt_id", "bin"]:
            if selector in matches and not pd.isna(seed[selector]):
                value = re.sub(r"\.0$", "", str(seed[selector]).strip())
                related = buildings[self.identifier_mask(buildings, selector, value)]
                if not related.empty:
                    return related.copy()
        return matches

    def apply_building_color_overrides(self, buildings: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        buildings = buildings.reset_index(drop=True).copy()
        buildings["material_override"] = np.full(len(buildings), -1, dtype=np.int8)
        requested = self.config.get("building_color_overrides", [])
        visible = buildings[buildings.intersects(self.exact_aoi_2263)].copy()
        resolutions = []
        claimed: dict[int, tuple[int, int]] = {}
        for request_index, request in enumerate(requested):
            geocoded = None
            if "address" in request:
                geocoded = self.geocode_address(request["address"])
                matches = empty_geodata(buildings.columns, crs=buildings.crs)
                if geocoded.get("bin"):
                    value = re.sub(r"\.0$", "", str(geocoded["bin"]).strip())
                    matches = visible[self.identifier_mask(visible, "bin", value)].copy()
                    method = "NYC GeoSearch PAD BIN"
                else:
                    method = "NYC GeoSearch coordinate"
                if matches.empty:
                    matches = self.buildings_at_coordinate(
                        visible, geocoded["latitude"], geocoded["longitude"]
                    )
                    method = "NYC GeoSearch coordinate"
            elif "latitude" in request:
                matches = self.buildings_at_coordinate(
                    visible, request["latitude"], request["longitude"]
                )
                method = "WGS84 coordinate"
            else:
                selector = next(key for key in ["bin", "doitt_id", "bbl"] if key in request)
                matches = visible[self.identifier_mask(visible, selector, request[selector])].copy()
                method = selector.upper().replace("DOITT", "DoITT")
                if matches.empty:
                    raise ValueError(
                        f"Building color request {request_index + 1} matched no visible footprint "
                        f"for {selector}={request[selector]!r}"
                    )
            material = int(request["material"])
            for row_index in matches.index:
                previous = claimed.get(int(row_index))
                if previous is not None and previous[0] != material:
                    raise ValueError(
                        f"Building color requests {previous[1] + 1} and {request_index + 1} "
                        f"assign different colors to the same footprint"
                    )
                claimed[int(row_index)] = (material, request_index)
                buildings.loc[row_index, "material_override"] = material
            resolution = {
                "request_index": request_index + 1,
                "selector": {
                    key: value for key, value in request.items()
                    if key not in ["material", "color"]
                },
                "material": material,
                "material_name": MATERIAL_NAMES[material],
                "resolved_via": method,
                "matched_footprints": self.matched_building_records(matches),
            }
            if geocoded is not None:
                resolution["geocoded"] = geocoded
            resolutions.append(resolution)
            self.log.info(
                "building_color_override_resolved",
                request_index=request_index + 1,
                material=MATERIAL_NAMES[material],
                method=method,
                footprints=len(matches),
            )
        atomic_json(
            self.analysis / "building_color_overrides.json",
            {
                "requested": len(requested),
                "matched_footprints": int((buildings.material_override >= 0).sum()),
                "default_material": "ivory",
                "resolutions": resolutions,
            },
        )
        return buildings

    def prepare_vectors(self):
        planimetrics_cache = self.cached_dataset("nyc_planimetrics_2022")
        buildings_cache = self.cached_dataset("nyc_building_footprints")
        trails_cache = self.cached_dataset("nyc_parks_trails")
        if planimetrics_cache is not None:
            planimetrics_dir, _ = planimetrics_cache
            for name in PLANIMETRIC_LAYERS:
                frame = read_geoparquet_bbox(
                    planimetrics_dir / f"{name}.parquet", self.source_aoi_2263.bounds
                )
                frame = frame[frame.geometry.notna() & frame.intersects(self.source_aoi_2263)].copy()
                frame.to_parquet(self.processed / f"planimetrics_{name}_aoi.parquet")
                self.log.info("vector_cache_subset", dataset=f"planimetrics_{name}", rows=len(frame))
        else:
            planimetrics = self.ensure_planimetrics()
            available = {name for name, _ in pyogrio.list_layers(planimetrics)}
            for name in PLANIMETRIC_LAYERS:
                if name not in available:
                    raise RuntimeError(f"Required Planimetrics layer is absent: {name}")
                frame = pyogrio.read_dataframe(planimetrics, layer=name, bbox=self.source_aoi_2263.bounds)
                if frame.empty:
                    info = pyogrio.read_info(planimetrics, layer=name)
                    layer_bounds = box(*info["total_bounds"])
                    if layer_bounds.intersects(self.source_aoi_2263):
                        complete = pyogrio.read_dataframe(planimetrics, layer=name)
                        if complete.crs is None:
                            complete = complete.set_crs(2263)
                        frame = complete[complete.intersects(self.source_aoi_2263)].copy()
                        self.log.warning(
                            "planimetrics_spatial_index_fallback", layer=name,
                            scanned_rows=len(complete), retained_rows=len(frame),
                        )
                if frame.crs is None:
                    frame = frame.set_crs(2263)
                frame.to_parquet(self.processed / f"planimetrics_{name}_aoi.parquet")
                self.log.info("vector_subset", dataset=f"planimetrics_{name}", rows=len(frame))

        if buildings_cache is not None:
            buildings_dir, _ = buildings_cache
            buildings = read_tiled_geoparquet(
                buildings_dir, tuple(self.source_aoi_2263.bounds),
                deduplicate_by=["objectid", "doitt_id"], source_order=["source_order"],
            )
            self.log.info("vector_cache_subset", dataset="nyc_building_footprints", rows=len(buildings))
        else:
            try:
                if self.args.offline:
                    raise RuntimeError("offline")
                buildings = self.fetch_buildings_api()
                if buildings.empty:
                    raise RuntimeError("building API returned an empty feature collection")
            except Exception as error:
                self.log.warning("building_api_failed", error=str(error))
                buildings = self.buildings_from_csv()
            buildings = buildings[buildings.intersects(self.source_aoi_2263)].copy()
            self.log.info("vector_subset", dataset="buildings", rows=len(buildings))
        for column in ["doitt_id", "height_roof"]:
            if column not in buildings:
                buildings[column] = np.nan
        buildings["doitt_id"] = buildings.doitt_id.astype(str).str.replace(r"\.0$", "", regex=True)
        buildings["height_roof"] = pd.to_numeric(buildings.height_roof, errors="coerce")
        buildings = self.apply_building_color_overrides(buildings)
        buildings.to_parquet(self.processed / "buildings_projected_aoi.parquet")
        buildings.to_parquet(self.processed / "buildings_aoi.parquet")

        if trails_cache is not None:
            trails_dir, _ = trails_cache
            trails = read_geoparquet_bbox(trails_dir / "data.parquet", self.source_aoi_2263.bounds)
            trails = trails[trails.geometry.notna() & trails.intersects(self.source_aoi_2263)].copy()
            self.log.info("vector_cache_subset", dataset="nyc_parks_trails", rows=len(trails))
        else:
            trail_path = self.ensure_download("trails")
            trail = pd.read_csv(trail_path, dtype=str)
            trail.columns = [re.sub(r"[^a-z0-9]+", "_", str(c).lower()).strip("_") for c in trail]
            geometry = shapely.from_wkt(trail["shape"].fillna("").to_numpy(), on_invalid="ignore")
            trails = gpd.GeoDataFrame(trail.drop(columns=["shape"]), geometry=geometry, crs=4326)
            trails = trails[trails.geometry.notna() & trails.intersects(self.source_aoi_wgs)].to_crs(2263)
            self.log.info("vector_subset", dataset="parks_trails", rows=len(trails))
        trails.to_parquet(self.processed / "trails_aoi.parquet")

    def ensure_citygml_index(self) -> Path:
        candidates = [self.cache_dir / "nyc_3d_buildings_2014/raw_index.csv"]
        for path in candidates:
            if path.exists() and path.stat().st_size > 0:
                return path
        target_parent = candidates[-1].parent
        target_parent.mkdir(parents=True, exist_ok=True)
        target = candidates[-1]
        archive = zipfile.ZipFile(self.ensure_download("citygml"))
        with target.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["doitt_id", "file"])
            for name in archive.namelist():
                if not name.endswith(".gml"):
                    continue
                with archive.open(name) as source:
                    for _, element in etree.iterparse(source, events=("end",), tag="{*}Building", huge_tree=True):
                        attrs = {a.get("name"): a.findtext("{*}value") for a in element.findall("{*}stringAttribute")}
                        writer.writerow([attrs.get("DOITT_ID"), name])
                        element.clear()
                        while element.getprevious() is not None:
                            del element.getparent()[0]
                self.log.info("citygml_index_progress", archive_member=name)
        return target

    @staticmethod
    def citygml_coordinates(element):
        namespace = "http://www.opengis.net/gml"
        return [
            np.fromstring(pos.text, sep=" ").reshape(-1, 3)
            for pos in element.iter("{" + namespace + "}posList") if pos.text
        ]

    def extract_citygml(self):
        cached = self.cached_dataset("nyc_3d_buildings_2014")
        if cached is not None:
            cache, manifest = cached
            catalog = gpd.read_file(cache / "catalog.geojson").to_crs(2263)
            catalog = catalog[catalog.intersects(self.source_aoi_2263)]
            building_parts, roof_parts = [], []
            for row in catalog.itertuples():
                building_parts.append(read_geoparquet_bbox(
                    cache / row.building_path, self.source_aoi_2263.bounds
                ))
                roof_parts.append(read_geoparquet_bbox(
                    cache / row.roof_path, self.source_aoi_2263.bounds
                ))
            if building_parts:
                building_frame = gpd.GeoDataFrame(
                    pd.concat(building_parts, ignore_index=True), crs=2263
                )
                building_frame = building_frame[
                    building_frame.geometry.notna() & building_frame.intersects(self.source_aoi_2263)
                ].copy()
                building_frame = building_frame.drop_duplicates(
                    [column for column in ["source_member_order", "source_building_order"] if column in building_frame]
                )
                order = [column for column in ["source_member_order", "source_building_order"] if column in building_frame]
                if order:
                    building_frame = building_frame.sort_values(order, kind="stable")
            else:
                building_frame = empty_geodata([
                    "gml_id", "doitt_id", "bin", "roof_count", "roof_levels",
                    "z_min_ft", "z_max_ft", "geometry",
                ])
            if roof_parts:
                surface_frame = gpd.GeoDataFrame(pd.concat(roof_parts, ignore_index=True), crs=2263)
                surface_frame = surface_frame[
                    surface_frame.geometry.notna() & surface_frame.intersects(self.source_aoi_2263)
                ].copy()
                surface_frame = surface_frame.drop_duplicates([
                    column for column in [
                        "source_member_order", "source_building_order", "source_surface_order"
                    ] if column in surface_frame
                ])
                order = [column for column in [
                    "source_member_order", "source_building_order", "source_surface_order"
                ] if column in surface_frame]
                if order:
                    surface_frame = surface_frame.sort_values(order, kind="stable")
            else:
                surface_frame = empty_geodata([
                    "gml_id", "doitt_id", "bin", "kind", "z_min_ft", "z_max_ft", "geometry",
                ])
            building_frame.to_parquet(self.processed / "citygml_buildings_aoi.parquet")
            surface_frame.to_parquet(self.processed / "citygml_surfaces_aoi.parquet")
            self.log.info(
                "citygml_cache_subset", buildings=len(building_frame), roofs=len(surface_frame),
                members=len(catalog), cache=str(cache), completed_at=manifest.get("completed_at"),
            )
            return
        building_ids = set(gpd.read_parquet(self.processed / "buildings_projected_aoi.parquet").doitt_id.astype(str))
        index = self.ensure_citygml_index()
        members = set()
        for chunk in pd.read_csv(index, dtype=str, usecols=["doitt_id", "file"], chunksize=250000):
            members.update(chunk.loc[chunk.doitt_id.isin(building_ids), "file"].dropna())
        if not members:
            self.log.warning("citygml_no_matching_archive_members", building_ids=len(building_ids))
        buildings, surfaces = [], []
        archive = zipfile.ZipFile(self.ensure_download("citygml"))
        gml_namespace = "http://www.opengis.net/gml"
        xmin, ymin, xmax, ymax = self.source_aoi_2263.bounds
        for name in sorted(members):
            with archive.open(name) as source:
                for _, element in etree.iterparse(source, events=("end",), tag="{*}Building", huge_tree=True):
                    namespace = etree.QName(element).namespace
                    attrs = {a.get("name"): a.findtext("{*}value") for a in element.findall("{*}stringAttribute")}
                    roofs = element.findall(".//{" + namespace + "}RoofSurface")
                    grounds = element.findall(".//{" + namespace + "}GroundSurface")
                    walls = element.findall(".//{" + namespace + "}WallSurface")
                    ground_coords = [coords for ground in grounds for coords in self.citygml_coordinates(ground)]
                    near = False
                    if ground_coords:
                        coordinates = np.concatenate(ground_coords)
                        near = (
                            coordinates[:, 0].max() >= xmin and coordinates[:, 0].min() <= xmax
                            and coordinates[:, 1].max() >= ymin and coordinates[:, 1].min() <= ymax
                        )
                    if near:
                        gid = element.get("{" + gml_namespace + "}id")
                        roof_z, all_z, footprints = [], [], []
                        for kind, objects in [("roof", roofs), ("ground", grounds), ("wall", walls)]:
                            for obj in objects:
                                for poly in obj.iter("{" + gml_namespace + "}Polygon"):
                                    exterior = poly.find(".//{" + gml_namespace + "}exterior//{" + gml_namespace + "}posList")
                                    if exterior is None or not exterior.text:
                                        continue
                                    outer = np.fromstring(exterior.text, sep=" ").reshape(-1, 3)
                                    holes = [
                                        np.fromstring(pos.text, sep=" ").reshape(-1, 3)
                                        for pos in poly.findall(".//{" + gml_namespace + "}interior//{" + gml_namespace + "}posList")
                                    ]
                                    geometry = Polygon(outer, holes)
                                    all_z.extend(outer[:, 2].tolist())
                                    if kind == "roof":
                                        roof_z.extend(outer[:, 2].tolist())
                                    if kind == "ground":
                                        footprints.append(shapely.make_valid(Polygon(outer[:, :2], [h[:, :2] for h in holes])))
                                    surfaces.append({
                                        "gml_id": gid, "doitt_id": attrs.get("DOITT_ID"), "bin": attrs.get("BIN"),
                                        "kind": kind, "z_min_ft": float(outer[:, 2].min()),
                                        "z_max_ft": float(outer[:, 2].max()), "geometry": geometry,
                                    })
                        if footprints and all_z:
                            buildings.append({
                                "gml_id": gid, "doitt_id": attrs.get("DOITT_ID"), "bin": attrs.get("BIN"),
                                "roof_count": len(roofs), "roof_levels": len(set(np.round(roof_z, 1))),
                                "z_min_ft": min(all_z), "z_max_ft": max(all_z),
                                "geometry": shapely.union_all(footprints),
                            })
                    element.clear()
                    while element.getprevious() is not None:
                        del element.getparent()[0]
            self.log.info("citygml_member_processed", archive_member=name)
        building_frame = (
            gpd.GeoDataFrame(buildings, geometry="geometry", crs=2263)
            if buildings else empty_geodata(["gml_id", "doitt_id", "bin", "roof_count", "roof_levels", "z_min_ft", "z_max_ft", "geometry"])
        )
        surface_frame = (
            gpd.GeoDataFrame(surfaces, geometry="geometry", crs=2263)
            if surfaces else empty_geodata(["gml_id", "doitt_id", "bin", "kind", "z_min_ft", "z_max_ft", "geometry"])
        )
        building_frame.to_parquet(self.processed / "citygml_buildings_aoi.parquet")
        surface_frame.to_parquet(self.processed / "citygml_surfaces_aoi.parquet")
        self.log.info("citygml_subset", buildings=len(building_frame), surfaces=len(surface_frame), members=len(members))

    def lidar_destination_grid(self) -> dict:
        """Return the rotated 0.5 m destination grid used by the job."""
        resolution_ft = 0.5 / FT
        frame = self.config.get("frame_epsg2263")
        if frame:
            x_axis = np.asarray(frame["x_axis"], dtype=float)
            y_axis = np.asarray(frame["y_axis"], dtype=float)
        else:
            x_axis = np.asarray([1.0, 0.0])
            y_axis = np.asarray([0.0, 1.0])
        coordinates = shapely.get_coordinates(self.source_aoi_2263)
        local_x = coordinates @ x_axis
        local_y = coordinates @ y_axis
        xmin = math.floor(float(local_x.min()) / resolution_ft) * resolution_ft
        ymin = math.floor(float(local_y.min()) / resolution_ft) * resolution_ft
        xmax = math.ceil(float(local_x.max()) / resolution_ft) * resolution_ft
        ymax = math.ceil(float(local_y.max()) / resolution_ft) * resolution_ft
        width = math.ceil((xmax - xmin) / resolution_ft)
        height = math.ceil((ymax - ymin) / resolution_ft)
        if width * height > 30_000_000:
            raise RuntimeError(f"Regional elevation grid is unexpectedly large: {height}x{width}")
        top_left = x_axis * xmin + y_axis * ymax
        transform = Affine(
            x_axis[0] * resolution_ft, -y_axis[0] * resolution_ft, top_left[0],
            x_axis[1] * resolution_ft, -y_axis[1] * resolution_ft, top_left[1],
        )
        return {
            "resolution_ft": resolution_ft,
            "x_axis": x_axis,
            "y_axis": y_axis,
            "xmin": xmin,
            "ymin": ymin,
            "xmax": xmax,
            "ymax": ymax,
            "width": width,
            "height": height,
            "transform": transform,
        }

    def mosaic_lidar_cache(self, catalog: gpd.GeoDataFrame, cache: Path, name: str, grid: dict):
        """Reproject one canonical cache field into this job's rotated grid."""
        destination = np.full((grid["height"], grid["width"]), np.nan, np.float32)
        cache_root = cache.resolve()
        selected = catalog[catalog.intersects(self.source_aoi_2263)]
        for _, row in selected.iterrows():
            relative = row.get(name)
            if not isinstance(relative, str) or not relative:
                raise RuntimeError(f"LiDAR cache catalog row {row.get('key')} has no {name} raster")
            path = (cache / relative).resolve()
            if not path.is_relative_to(cache_root) or not path.is_file():
                raise FileNotFoundError(f"LiDAR cache raster is missing or unsafe: {path}")
            temporary = np.full(destination.shape, np.nan, np.float32)
            with rasterio.open(path) as source:
                if source.crs is None:
                    raise RuntimeError(f"LiDAR cache raster has no CRS: {path}")
                reproject(
                    source.read(1), temporary,
                    src_transform=source.transform, src_crs=source.crs,
                    src_nodata=source.nodata if source.nodata is not None else np.nan,
                    dst_transform=grid["transform"], dst_crs="EPSG:2263",
                    dst_nodata=np.nan, resampling=Resampling.nearest,
                )
            valid = np.isfinite(temporary)
            destination[valid] = temporary[valid]
        return destination

    def prepare_lidar_from_cache(self):
        cache, manifest, catalog = self.ensure_lidar_cache()
        selected = catalog[catalog.intersects(self.source_aoi_2263)].copy()
        if selected.empty:
            raise RuntimeError(
                f"LiDAR cache has no tiles intersecting the requested source area: {cache}"
            )
        grid = self.lidar_destination_grid()
        ground = self.mosaic_lidar_cache(catalog, cache, "ground", grid)
        upper = self.mosaic_lidar_cache(catalog, cache, "upper", grid)
        observed = np.isfinite(ground)
        if not observed.any():
            raise RuntimeError(
                "Selected LiDAR cache tiles contain no finite class-2 ground values "
                f"in the requested area: {cache}"
            )
        distance, nearest = distance_transform_edt(~observed, return_indices=True)
        fill = (~observed) & (distance * 0.5 <= 10)
        ground[fill] = ground[tuple(nearest[:, fill])]
        upper[~np.isfinite(upper)] = np.nan

        rasters = self.processed / "rasters"
        rasters.mkdir(exist_ok=True)
        profile = {
            "driver": "GTiff", "width": grid["width"], "height": grid["height"],
            "count": 1, "dtype": "float32", "crs": "EPSG:2263",
            "transform": grid["transform"], "compress": "deflate", "tiled": True,
            "nodata": np.nan,
        }
        for name, array in {"ground_m": ground, "upper_surface_m": upper}.items():
            with rasterio.open(rasters / f"{name}.tif", "w", **profile) as target:
                target.write(array.astype(np.float32, copy=False), 1)
        # These diagnostics are only available when binning individual LAZ
        # returns.  Remove stale files from an earlier LAZ-mode run rather than
        # leaving them to be mistaken for cache-derived measurements.
        for name in ["ground_distance_m", "return_count", "ground_return_count"]:
            (rasters / f"{name}.tif").unlink(missing_ok=True)
        atomic_json(self.analysis / "lidar.json", {
            "source": "cache",
            "cache_dir": str(cache),
            "cache_manifest": str(cache / "manifest.json"),
            "tiles": selected.key.astype(str).tolist(),
            "shape": [grid["height"], grid["width"]],
            "ground_observed_fraction": float(observed.mean()),
            "ground_filled_fraction": float(fill.mean()),
            "classes": {},
            "return_counts_available": False,
            "cache_source_laz_count": manifest.get("source_laz_count"),
        })

    def prepare_lidar(self):
        if self.lidar_source == "cache":
            self.prepare_lidar_from_cache()
            return
        selected = gpd.read_file(self.processed / "lidar_grid_aoi.geojson")
        grid = self.lidar_destination_grid()
        resolution_ft = grid["resolution_ft"]
        x_axis = grid["x_axis"]
        y_axis = grid["y_axis"]
        xmin = grid["xmin"]
        ymin = grid["ymin"]
        xmax = grid["xmax"]
        ymax = grid["ymax"]
        width = grid["width"]
        height = grid["height"]
        size = width * height
        ground_sum = np.zeros(size, np.float64)
        ground_count = np.zeros(size, np.uint32)
        upper = np.full(size, -np.inf, np.float32)
        return_count = np.zeros(size, np.uint32)
        class_counts = Counter()
        for tile in selected.LAS_ID.astype(str):
            path = self.raw / "nyc_lidar_2017/tiles" / f"{tile}.laz"
            with laspy.open(path) as source:
                accepted = 0
                for points in source.chunk_iterator(1_000_000):
                    raw_class = np.asarray(points.classification)
                    extended = np.asarray(points["LAS 1.4 classification"])
                    classification = np.where(extended > 31, extended, raw_class)
                    x = np.asarray(points.x)
                    y = np.asarray(points.y)
                    z = np.asarray(points.z) * FT
                    point_x = x * x_axis[0] + y * x_axis[1]
                    point_y = x * y_axis[0] + y * y_axis[1]
                    col = np.floor((point_x - xmin) / resolution_ft).astype(np.int32)
                    row = np.floor((ymax - point_y) / resolution_ft).astype(np.int32)
                    inside = (row >= 0) & (row < height) & (col >= 0) & (col < width)
                    eligible = inside & ~np.asarray(points.withheld, dtype=bool)
                    accepted += int(eligible.sum())
                    values, counts = np.unique(classification[eligible], return_counts=True)
                    class_counts.update(dict(zip(values.tolist(), counts.tolist())))
                    ground = eligible & (classification == 2)
                    indices = row[ground] * width + col[ground]
                    np.add.at(ground_sum, indices, z[ground])
                    np.add.at(ground_count, indices, 1)
                    surface = eligible & np.isin(classification, [1, 2, 17, 25])
                    indices = row[surface] * width + col[surface]
                    np.maximum.at(upper, indices, z[surface].astype(np.float32))
                    np.add.at(return_count, indices, 1)
            self.log.info("lidar_tile_processed", tile=tile, accepted_points=accepted)
        ground_count = ground_count.reshape(height, width)
        upper = upper.reshape(height, width)
        return_count = return_count.reshape(height, width)
        observed = ground_count > 0
        if not observed.any():
            raise RuntimeError("Selected LiDAR tiles contain no class-2 ground returns in this area")
        ground = np.full((height, width), np.nan, np.float32)
        ground[observed] = (
            ground_sum.reshape(height, width)[observed] / ground_count[observed]
        ).astype(np.float32)
        distance, nearest = distance_transform_edt(~observed, return_indices=True)
        fill = (~observed) & (distance * 0.5 <= 10)
        ground[fill] = ground[tuple(nearest[:, fill])]
        upper[~np.isfinite(upper)] = np.nan
        rasters = self.processed / "rasters"
        rasters.mkdir(exist_ok=True)
        top_left = x_axis * xmin + y_axis * ymax
        transform = Affine(
            x_axis[0] * resolution_ft, -y_axis[0] * resolution_ft, top_left[0],
            x_axis[1] * resolution_ft, -y_axis[1] * resolution_ft, top_left[1],
        )
        arrays = {
            "ground_m": ground,
            "upper_surface_m": upper,
            "ground_distance_m": (distance * 0.5).astype(np.float32),
            "return_count": return_count,
            "ground_return_count": ground_count,
        }
        for name, array in arrays.items():
            profile = {
                "driver": "GTiff", "width": width, "height": height, "count": 1,
                "dtype": str(array.dtype), "crs": "EPSG:2263", "transform": transform,
                "compress": "deflate", "tiled": True,
                "nodata": np.nan if array.dtype.kind == "f" else 0,
            }
            with rasterio.open(rasters / f"{name}.tif", "w", **profile) as target:
                target.write(array, 1)
        atomic_json(self.analysis / "lidar.json", {
            "source": "laz",
            "tiles": selected.LAS_ID.astype(str).tolist(), "shape": [height, width],
            "ground_observed_fraction": float(observed.mean()),
            "ground_filled_fraction": float(fill.mean()), "classes": dict(class_counts),
        })

    def prepare_landcover(self):
        reference_path = self.processed / "rasters/ground_m.tif"
        with rasterio.open(reference_path) as reference:
            shape, transform, crs = reference.shape, reference.transform, reference.crs
            profile = reference.profile.copy()
        cached = self.cached_dataset("nyc_land_cover_2017")
        if cached is not None:
            cache, _ = cached
            source_path = str(cache / "landcover_native.tif")
            source_kind = "cache"
        else:
            archive = self.ensure_download("landcover")
            source_path = f"/vsizip/{archive}/Land_Cover/NYC_2017_LiDAR_LandCover.img"
            source_kind = "source_zip"
        with rasterio.Env(GDAL_CACHEMAX=256 * 1024**2), rasterio.open(source_path) as source:
            classes = np.zeros(shape, dtype=np.uint8)
            reproject(
                rasterio.band(source, 1), classes,
                src_transform=source.transform, src_crs=source.crs,
                dst_transform=transform, dst_crs=crs,
                resampling=Resampling.nearest, dst_nodata=0,
            )
        profile.update(dtype="uint8", nodata=0, count=1)
        with rasterio.open(self.processed / "rasters/landcover.tif", "w", **profile) as target:
            target.write(classes.astype(np.uint8), 1)
        values, counts = np.unique(classes, return_counts=True)
        atomic_json(self.analysis / "landcover.json", {
            "classes": dict(zip(values.tolist(), counts.tolist())),
            "source": source_kind,
            "cache": str(cached[0]) if cached else None,
        })

    def reap(self, process: subprocess.Popen, stage: str) -> None:
        """Take the child's whole process group down with an abandoned stage.

        A stage that unwinds early -- an interrupt, or a failure raised while
        reading output -- used to leave its child running.  A long boolean pass
        does not reach a Python signal handler until it returns from C++, so ask
        once and then insist; both waits are bounded because the next attempt
        must not have to compete with the abandoned one for CPU and memory.
        """
        if process.poll() is not None:
            return
        self.log.warning("command_abandoned", stage=stage, pid=process.pid)
        for number, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
            try:
                os.killpg(process.pid, number)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                process.wait(timeout=grace)
                self.log.info("command_terminated", stage=stage, pid=process.pid,
                              signal=number.name, return_code=process.returncode)
                return
            except subprocess.TimeoutExpired:
                continue
        self.log.error("command_termination_failed", stage=stage, pid=process.pid)

    def run_command(self, stage: str, command: list[str]):
        log_path = self.logs / f"{stage}.log"
        self.log.info("command_started", stage=stage, command=command, log=str(log_path))
        started = time.monotonic()
        with log_path.open("w") as output:
            # Own session so the child leads its own process group: the whole
            # group is then addressable as one unit in ``reap``, including any
            # workers the stage forks for itself.
            process = subprocess.Popen(
                command, cwd=ROOT, env=self.env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                start_new_session=True,
            )
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    output.write(line)
                    output.flush()
                    if line.startswith("PROGRESS "):
                        self.log.info("command_progress", stage=stage, message=line.strip()[9:])
                process.wait()
            finally:
                self.reap(process, stage)
        elapsed = time.monotonic() - started
        if process.returncode:
            tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-40:])
            self.log.error("command_failed", stage=stage, return_code=process.returncode, elapsed_seconds=elapsed, log_tail=tail)
            raise subprocess.CalledProcessError(process.returncode, command)
        self.log.info("command_completed", stage=stage, return_code=0, elapsed_seconds=elapsed)

    def build_fields(self, python: Path):
        self.run_command("build_fields", [str(python), str(SCRIPT_DIR / "build_map_fields.py")])
        report = json.loads((self.work / "field_build_report.json").read_text())
        buildings = report["layers"]["buildings"]
        osm = gpd.read_parquet(self.work / "osm_detail.parquet")
        osm_counts = count_osm_semantic_features(osm, self.source_aoi_2263)
        osm_buildings = osm_counts["osm_building_footprints"]
        osm_motor_roads = osm_counts["osm_motor_road_segments"]
        current = int(buildings["current_footprints"])
        historic = int(buildings["historic_objects"])
        roadbeds = int(report["layers"]["paths"]["roadbed_polygons"])
        semantic = {
            **osm_counts,
            "current_nyc_building_footprints": current,
            "historic_citygml_buildings": historic,
            "historic_roof_polygons": int(buildings["roof_polygons"]),
            "nyc_planimetric_roadbed_polygons": roadbeds,
        }
        report["semantic_validation"] = semantic
        atomic_json(self.work / "field_build_report.json", report)
        self.log.info("semantic_building_validation", **semantic)
        if osm_buildings >= 10 and current < max(1, math.floor(osm_buildings * 0.25)):
            raise RuntimeError(
                f"Building coverage failed semantic QA: NYC footprints={current}, "
                f"OSM footprints={osm_buildings}; refusing to generate a building-free map"
            )
        if current >= 10 and historic < max(1, math.floor(current * 0.05)):
            raise RuntimeError(
                f"CityGML roof coverage failed semantic QA: historic buildings={historic}, "
                f"current footprints={current}"
            )
        if osm_motor_roads >= 10 and roadbeds == 0:
            raise RuntimeError(
                f"Road coverage failed semantic QA: Planimetrics roadbeds={roadbeds}, "
                f"OSM motor-road segments={osm_motor_roads}"
            )
        maximum = float(report["z_range_mm"][1])
        if maximum > 250:
            raise RuntimeError(
                f"Generated relief is {maximum:.2f} mm tall; increase --scale or reduce "
                "--vertical-exaggeration to stay within the 250 mm model-height limit"
            )

    def write_manifest(self):
        report = json.loads((self.validation / "3mf_validation.json").read_text())
        manifest = {
            "pipeline_version": PIPELINE_VERSION,
            "config_sha256": self.config_hash,
            "config": self.config,
            "model": str(self.output),
            "model_bytes": self.output.stat().st_size,
            "model_sha256": digest(self.output),
            "geometry_validation": report,
            "job_directory": str(self.job),
            "physical_print_tested": False,
            "data_api_cost_usd": 0,
        }
        result_path = self.validation / "slice/result.json"
        if self.args.slice and result_path.exists():
            result = json.loads(result_path.read_text())
            plate = result["sliced_plates"][0]
            manifest["slice"] = {
                "seconds": plate["total_predication"],
                "filament_changes": plate["filament_change_times"],
                "total_filament_g": sum(item["total_used_g"] for item in plate["filaments"]),
                "warning": plate["warning_message"],
            }
        atomic_json(self.job / "manifest.json", manifest)
        (self.output.with_suffix(".sha256")).write_text(f"{manifest['model_sha256']}  {self.output.name}\n")

    def run(self):
        atomic_json(self.job / "config.json", self.config)
        atomic_json(self.job / "request.json", vars(self.args))
        generation_metadata = {
            "schema_version": 1,
            "argv": [str(Path(sys.executable).absolute()), str(Path(__file__).absolute()), *sys.argv[1:]],
            "shell_command": shlex.join(
                [str(Path(sys.executable).absolute()), str(Path(__file__).absolute()), *sys.argv[1:]]
            ),
            "working_directory": str(Path.cwd().resolve()),
        }
        atomic_json(self.job / "generation_command.json", generation_metadata)
        gpd.GeoDataFrame(
            {"name": [self.config["name"]]}, geometry=[self.exact_aoi_2263], crs=2263
        ).to_crs(4326).to_file(self.job / "source_aoi.geojson", driver="GeoJSON")
        self.log.info("pipeline_started", output=str(self.output), config=self.config)
        addresses = list(dict.fromkeys(
            request["address"] for request in self.config.get("building_color_overrides", [])
            if "address" in request
        ))
        if addresses:
            self.log.info("building_address_geocoding_started", count=len(addresses))
            for address in addresses:
                self.geocode_address(address)
            self.log.info("building_address_geocoding_completed", count=len(addresses))
        # Always validate the selected shared LiDAR source. Cache validation is
        # local; LAZ mode also checks the publisher-backed raw tile set.
        self.stage(
            "download_sources",
            [self.job / "raw_ready.json"],
            self.download_sources,
            cacheable=False,
        )
        vector_outputs = [self.processed / f"planimetrics_{name}_aoi.parquet" for name in PLANIMETRIC_LAYERS]
        vector_outputs += [
            self.processed / "buildings_projected_aoi.parquet",
            self.processed / "buildings_aoi.parquet",
            self.processed / "trails_aoi.parquet",
            self.analysis / "building_color_overrides.json",
        ]
        self.stage(
            "prepare_vectors", vector_outputs, self.prepare_vectors,
            variant={"caches": {
                name: self.cache_identity(name) for name in (
                    "nyc_planimetrics_2022", "nyc_building_footprints", "nyc_parks_trails",
                )
            }},
        )
        self.stage("extract_citygml", [
            self.processed / "citygml_buildings_aoi.parquet",
            self.processed / "citygml_surfaces_aoi.parquet",
        ], self.extract_citygml, variant={"cache": self.cache_identity("nyc_3d_buildings_2014")})
        self.stage("prepare_lidar", [
            self.processed / "rasters/ground_m.tif",
            self.processed / "rasters/upper_surface_m.tif",
        ], self.prepare_lidar)
        self.stage(
            "prepare_landcover", [self.processed / "rasters/landcover.tif"], self.prepare_landcover,
            variant={"cache": self.cache_identity("nyc_land_cover_2017")},
        )
        python = Path(sys.executable)
        self.stage("extract_osm", [self.work / "osm_detail.parquet"], lambda: self.run_command(
            "extract_osm", [str(python), str(SCRIPT_DIR / "extract_osm.py")]
        ), variant={"cache": self.cache_identity("new_york_osm")})
        self.stage("prepare_details", [
            self.processed / "ivory_road_surface.parquet",
            self.processed / "road_symbol_routes.parquet",
            self.processed / "parks_structures.parquet",
            self.processed / "mta_subway_entrances.parquet",
        ], lambda: self.run_command("prepare_details", [str(python), str(SCRIPT_DIR / "prepare_details.py")]),
            variant={"detail_pipeline_version": DETAIL_PIPELINE_VERSION, "caches": {
                name: self.cache_identity(name) for name in (
                    "nyc_parks_structures", "mta_subway_entrances_2024",
                )
            }})
        self.stage(
            "build_fields",
            [self.work / "map_fields.npz", self.work / "field_build_report.json"],
            lambda: self.build_fields(python),
            variant={"field_pipeline_version": FIELD_PIPELINE_VERSION,
                "details": "parks structures and subway entrances"},
        )
        self.stage("validate_crossing_fields", [self.work / "crossing_field_validation.json"],
            lambda: self.run_command("validate_crossing_fields", [
                str(python), str(SCRIPT_DIR / "validate_bridge_surfaces.py"), "--fields-only",
                "--report", str(self.work / "crossing_field_validation.json"),
            ]), variant={"crossing_validation_version": CROSSING_VALIDATION_VERSION,
                "coverage": "tagged_bridges_and_surface_routes_over_tunnels"})
        mesh_variant = {"mesh_pipeline_version": MESH_PIPELINE_VERSION}
        self.stage("build_meshes", [self.work / "mesh/mesh_report.json"], lambda: self.run_command(
            "build_meshes", [str(python), str(SCRIPT_DIR / "build_map_meshes.py")]
        ), variant=mesh_variant)
        preview = self.output.with_name(self.output.stem + "_preview.png")
        if not self.args.no_preview:
            self.stage("render_preview", [preview], lambda: self.run_command(
                "render_preview", [
                    str(python), str(SCRIPT_DIR / "render_map.py"), "--mesh-dir", str(self.work / "mesh"),
                    "--output", str(preview),
                ]
            ), variant=mesh_variant)
        package_command = [
            str(python), str(SCRIPT_DIR / "package_3mf.py"), "--mesh-dir", str(self.work / "mesh"),
            "--output", str(self.output),
            "--project-settings-template", str(self.reference), "--profiles-dir", str(self.profiles),
            "--generation-metadata", str(self.job / "generation_command.json"),
        ]
        if not self.args.no_preview and preview.exists():
            package_command += ["--preview", str(preview)]
        self.stage(
            "package_3mf",
            [self.output],
            lambda: self.run_command("package_3mf", package_command),
            variant={"package_pipeline_version":PACKAGE_PIPELINE_VERSION,
                "embedded_preview": not self.args.no_preview,
                "generation_metadata": generation_metadata, **mesh_variant},
        )
        validation_command = [
            str(python), str(SCRIPT_DIR / "validate_3mf.py"), "--model", str(self.output),
            "--report-dir", str(self.validation),
            "--expected-generation-metadata", str(self.job / "generation_command.json"),
        ]
        if not self.args.full_validation:
            validation_command.append("--skip-booleans")
        self.stage(
            "validate_3mf",
            [self.validation / "3mf_validation.json"],
            lambda: self.run_command("validate_3mf", validation_command),
            variant={"validation_pipeline_version":VALIDATION_PIPELINE_VERSION,
                "cross_material_booleans": self.args.full_validation,
                "generation_metadata": generation_metadata, **mesh_variant},
        )
        if self.args.slice:
            self.stage("slice", [self.validation / "slice/result.json"], lambda: self.run_command(
                "slice", [str(python), str(SCRIPT_DIR / "slice_3mf.py"), "--model", str(self.output),
                          "--slicer", str(self.slicer), "--name", "slice", "--replace"]
            ), variant={"slice_pipeline_version":SLICE_PIPELINE_VERSION,
                "package_pipeline_version":PACKAGE_PIPELINE_VERSION, **mesh_variant})
        self.write_manifest()
        self.log.info("pipeline_completed", output=str(self.output), sha256=digest(self.output), manifest=str(self.job / "manifest.json"))


def parse_size(text: str) -> tuple[float, float]:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[xX,]\s*(\d+(?:\.\d+)?)\s*", text)
    if not match:
        raise argparse.ArgumentTypeError("Size must look like 200x200")
    return float(match.group(1)), float(match.group(2))


def parse_bounding_polygon(text: str):
    """Read one WGS84 Polygon from WKT, GeoJSON, or an @file/path argument."""
    source = text.strip()
    candidate = None
    if source.startswith("@"):
        candidate = Path(source[1:]).expanduser()
        if not candidate.is_file():
            raise argparse.ArgumentTypeError(f"Bounding-polygon file does not exist: {candidate}")
    elif len(source) < 1000 and not source.upper().startswith("POLYGON") and not source.startswith("{"):
        possible = Path(source).expanduser()
        if possible.is_file():
            candidate = possible
    if candidate is not None:
        source = candidate.read_text().strip()
    try:
        if source.startswith("{"):
            payload = json.loads(source)
            if payload.get("type") == "Feature":
                payload = payload.get("geometry") or {}
            if payload.get("type") == "FeatureCollection":
                raise ValueError("a FeatureCollection is ambiguous; provide one Polygon")
            geometry = shape(payload)
        else:
            geometry = shapely.from_wkt(source)
    except (json.JSONDecodeError, TypeError, ValueError, shapely.errors.GEOSException) as error:
        raise argparse.ArgumentTypeError(f"Invalid bounding polygon: {error}") from error
    geometry = shapely.make_valid(geometry)
    if geometry.is_empty or geometry.geom_type != "Polygon":
        raise argparse.ArgumentTypeError("Bounding polygon must resolve to one non-empty Polygon")
    return geometry


def parse_print_frame(text: str) -> dict:
    """Read a planner-supplied EPSG:2263 print frame from JSON or a file."""
    source = text.strip()
    candidate = None
    if source.startswith("@"):
        candidate = Path(source[1:]).expanduser()
    elif not source.startswith("{"):
        candidate = Path(source).expanduser()
    if candidate is not None:
        if not candidate.is_file():
            raise argparse.ArgumentTypeError(f"Print-frame file does not exist: {candidate}")
        source = candidate.read_text().strip()
    try:
        payload = json.loads(source)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError(f"Invalid print-frame JSON: {error}") from error
    if not isinstance(payload, dict):
        raise argparse.ArgumentTypeError("Print frame must be a JSON object")
    expected = {"origin_ft", "x_axis", "y_axis", "size_mm"}
    missing = expected - payload.keys()
    unknown = payload.keys() - expected
    if missing:
        raise argparse.ArgumentTypeError(
            f"Print frame is missing: {', '.join(sorted(missing))}"
        )
    if unknown:
        raise argparse.ArgumentTypeError(
            f"Print frame has unknown fields: {', '.join(sorted(unknown))}"
        )
    try:
        origin = np.asarray(payload["origin_ft"], dtype=float)
        x_axis = np.asarray(payload["x_axis"], dtype=float)
        y_axis = np.asarray(payload["y_axis"], dtype=float)
        size = np.asarray(payload["size_mm"], dtype=float)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("Print-frame values must be numeric pairs") from error
    if any(values.shape != (2,) for values in [origin, x_axis, y_axis, size]):
        raise argparse.ArgumentTypeError(
            "origin_ft, x_axis, y_axis and size_mm must each contain two numbers"
        )
    if not all(np.isfinite(values).all() for values in [origin, x_axis, y_axis, size]):
        raise argparse.ArgumentTypeError("Print-frame values must be finite")
    if not (
        abs(np.linalg.norm(x_axis) - 1.0) <= 1e-8
        and abs(np.linalg.norm(y_axis) - 1.0) <= 1e-8
        and abs(float(x_axis @ y_axis)) <= 1e-8
        and float(np.linalg.det(np.vstack([x_axis, y_axis]))) > 1.0 - 1e-8
    ):
        raise argparse.ArgumentTypeError(
            "x_axis and y_axis must be orthonormal unit vectors in a right-handed frame"
        )
    if np.any(size <= 0):
        raise argparse.ArgumentTypeError("Print-frame size_mm values must be positive")
    return {
        "origin_ft": origin.tolist(),
        "x_axis": x_axis.tolist(),
        "y_axis": y_axis.tolist(),
        "size_mm": size.tolist(),
    }


def parse_material_color(text: str) -> int:
    """Resolve one material name, alias, or palette hex to its filament index."""
    key = str(text).strip().casefold()
    if key not in BUILDING_COLOR_ALIASES:
        raise argparse.ArgumentTypeError(
            f"Unsupported color {text!r}; choose white/ivory, green, blue, brown/tan, "
            "or a matching configured palette hex"
        )
    return BUILDING_COLOR_ALIASES[key]


def parse_building_colors(text: str) -> list[dict]:
    """Read and normalize a JSON list of per-building material overrides."""
    source = text.strip()
    candidate = None
    if source.startswith("@"):
        candidate = Path(source[1:]).expanduser()
    elif not source.startswith("[") and not source.startswith("{"):
        candidate = Path(source).expanduser()
    if candidate is not None:
        if not candidate.is_file():
            raise argparse.ArgumentTypeError(f"Building-colors file does not exist: {candidate}")
        source = candidate.read_text().strip()
    try:
        payload = json.loads(source)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError(f"Invalid building-colors JSON: {error}") from error
    if isinstance(payload, dict):
        payload = payload.get("buildings")
    if not isinstance(payload, list):
        raise argparse.ArgumentTypeError(
            "Building colors must be a JSON list or an object with a 'buildings' list"
        )
    normalized = []
    for index, raw in enumerate(payload, 1):
        if not isinstance(raw, dict):
            raise argparse.ArgumentTypeError(f"Building color entry {index} must be a JSON object")
        entry = dict(raw)
        if "lat" in entry:
            if "latitude" in entry:
                raise argparse.ArgumentTypeError(
                    f"Building color entry {index} supplies both 'lat' and 'latitude'"
                )
            entry["latitude"] = entry.pop("lat")
        if "lon" in entry or "lng" in entry:
            aliases = [key for key in ["lon", "lng"] if key in entry]
            if "longitude" in entry or len(aliases) > 1:
                raise argparse.ArgumentTypeError(
                    f"Building color entry {index} supplies multiple longitude fields"
                )
            entry["longitude"] = entry.pop(aliases[0])
        color = entry.pop("color", None)
        if not isinstance(color, str) or color.strip().casefold() not in BUILDING_COLOR_ALIASES:
            choices = "white/ivory, green, blue, brown/tan, or a matching configured palette hex"
            raise argparse.ArgumentTypeError(
                f"Building color entry {index} has unsupported color {color!r}; choose {choices}"
            )
        material = BUILDING_COLOR_ALIASES[color.strip().casefold()]
        has_coordinate = "latitude" in entry or "longitude" in entry
        selector_keys = [key for key in ["address", "bin", "doitt_id", "bbl"] if key in entry]
        selector_count = len(selector_keys) + int(has_coordinate)
        if selector_count != 1:
            raise argparse.ArgumentTypeError(
                f"Building color entry {index} must use exactly one selector: address, "
                "latitude+longitude, BIN, DoITT ID, or BBL"
            )
        result = {"material": material, "color": MATERIAL_NAMES[material]}
        if has_coordinate:
            if "latitude" not in entry or "longitude" not in entry:
                raise argparse.ArgumentTypeError(
                    f"Building color entry {index} must supply latitude and longitude together"
                )
            try:
                latitude = float(entry.pop("latitude"))
                longitude = float(entry.pop("longitude"))
            except (TypeError, ValueError) as error:
                raise argparse.ArgumentTypeError(
                    f"Building color entry {index} has invalid latitude/longitude"
                ) from error
            if not (
                NYC_BOUNDS[0] <= longitude <= NYC_BOUNDS[2]
                and NYC_BOUNDS[1] <= latitude <= NYC_BOUNDS[3]
            ):
                raise argparse.ArgumentTypeError(
                    f"Building color entry {index} coordinates are outside the supported NYC bounds"
                )
            result.update(latitude=latitude, longitude=longitude)
        else:
            selector = selector_keys[0]
            value = entry.pop(selector)
            valid_type = isinstance(value, str) if selector == "address" else (
                isinstance(value, (str, int)) and not isinstance(value, bool)
            )
            if not valid_type or not str(value).strip():
                raise argparse.ArgumentTypeError(
                    f"Building color entry {index} has invalid {selector} selector {value!r}"
                )
            value = re.sub(r"\.0$", "", str(value).strip())
            result[selector] = value
        if entry:
            raise argparse.ArgumentTypeError(
                f"Building color entry {index} has unknown fields: {', '.join(sorted(entry))}"
            )
        normalized.append(result)
    return normalized


def snap_up(value: float, step: float) -> float:
    return round(math.ceil((value - 1e-9) / step) * step, 9)


def oriented_polygon_frame(polygon, scale: float, grid_step_mm: float) -> tuple[dict, tuple[float, float]]:
    """Return a north-forward print frame with the polygon's longer axis on model Y."""
    rectangle = polygon.minimum_rotated_rectangle
    corners = shapely.get_coordinates(rectangle.exterior)[:-1]
    if len(corners) != 4:
        raise ValueError("Bounding polygon does not have a usable oriented envelope")
    edges = [corners[(index + 1) % 4] - corners[index] for index in range(4)]
    lengths = np.asarray([np.linalg.norm(edge) for edge in edges])
    long_axis = edges[int(np.argmax(lengths))] / float(lengths.max())
    # Put geographic north at the top when the long axis is mostly north/south;
    # for a mostly east/west polygon, put east at the top for deterministic output.
    primary = 1 if abs(long_axis[1]) >= abs(long_axis[0]) else 0
    if long_axis[primary] < 0:
        long_axis = -long_axis
    y_axis = long_axis
    x_axis = np.asarray([y_axis[1], -y_axis[0]])
    coordinates = shapely.get_coordinates(polygon)
    x_values = coordinates @ x_axis
    y_values = coordinates @ y_axis
    source_width_ft = float(x_values.max() - x_values.min())
    source_length_ft = float(y_values.max() - y_values.min())
    k = FT * 1000 / scale
    width = snap_up(source_width_ft * k, grid_step_mm)
    height = snap_up(source_length_ft * k, grid_step_mm)
    # Grid snapping adds a sub-cell symmetric margin rather than changing X/Y
    # scale independently and distorting the requested geography.
    origin_x = float(x_values.min() - (width / k - source_width_ft) / 2)
    origin_y = float(y_values.min() - (height / k - source_length_ft) / 2)
    origin = x_axis * origin_x + y_axis * origin_y
    frame = {
        "origin_ft": origin.tolist(),
        "x_axis": x_axis.tolist(),
        "y_axis": y_axis.tolist(),
    }
    return frame, (width, height)


def brim_for_placement(width: float, height: float, translation: tuple[float, float]) -> float:
    """Largest configured brim that leaves the requested plate-edge clearance."""
    x, y = translation
    margin = min(x, y, PLATE_MM - x - width, PLATE_MM - y - height)
    return max(0.0, min(MODEL_MAX_BRIM_MM, margin - MODEL_BRIM_GAP_MM - PLATE_EDGE_CLEARANCE_MM))


def model_envelope(
    width: float, height: float, translation: tuple[float, float], brim_width: float
) -> tuple[float, float, float, float]:
    reach = brim_width + MODEL_BRIM_GAP_MM
    x, y = translation
    return x - reach, y - reach, x + width + reach, y + height + reach


def prime_tower_layout(width: float, height: float, nozzle_mm: float = DEFAULT_NOZZLE_MM) -> dict:
    """Center the model and evaluate it against the fixed tower band.

    Bambu Studio defines wipe_tower_x/y as the left-front tower corner. Tower
    depth is purge-dependent, but the model and tower are separated completely
    on X, so their Y ranges cannot collide. If a centered model overlaps the
    tower band, auto mode disables the tower instead of pushing the model to a
    plate edge; explicit ``--prime-tower on`` reports that it cannot fit.
    """
    translation_xy = ((PLATE_MM - width) / 2, (PLATE_MM - height) / 2)
    brim_width = brim_for_placement(width, height, translation_xy)
    envelope = model_envelope(width, height, translation_xy, brim_width)
    tower_left = PRIME_TOWER_POSITION_MM[0] - PRIME_TOWER_BRIM_MM
    tower_right = PRIME_TOWER_POSITION_MM[0] + PRIME_TOWER_WIDTH_MM + PRIME_TOWER_BRIM_MM
    plate_fits = (
        envelope[0] >= PLATE_EDGE_CLEARANCE_MM - 1e-9
        and envelope[1] >= PLATE_EDGE_CLEARANCE_MM - 1e-9
        and envelope[2] <= PLATE_MM - PLATE_EDGE_CLEARANCE_MM + 1e-9
        and envelope[3] <= PLATE_MM - PLATE_EDGE_CLEARANCE_MM + 1e-9
        and tower_left >= PLATE_EDGE_CLEARANCE_MM
        and tower_right <= PLATE_MM - PLATE_EDGE_CLEARANCE_MM
    )
    clearance = tower_left - envelope[2]
    required_clearance = PRIME_TOWER_CLEARANCE_BEADS * float(nozzle_mm)
    fits = plate_fits and clearance >= required_clearance - 1e-9
    return {
        "fits": fits,
        "translation_mm": [translation_xy[0], translation_xy[1], 0.0],
        "model_brim_width_mm": brim_width,
        "model_envelope_mm": list(envelope),
        "tower_x_envelope_mm": [tower_left, tower_right],
        "model_to_tower_clearance_mm": clearance,
        "required_model_to_tower_clearance_mm": required_clearance,
    }


def tower_free_layout(width: float, height: float) -> tuple[list[float], float]:
    translation_xy = ((PLATE_MM - width) / 2, (PLATE_MM - height) / 2)
    return [translation_xy[0], translation_xy[1], 0.0], brim_for_placement(
        width, height, translation_xy
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Generate a detailed four-color Bambu-compatible NYC map 3MF.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    result.add_argument("--latitude", type=float, help="WGS84 center latitude for a rectangular crop")
    result.add_argument("--longitude", type=float, help="WGS84 center longitude for a rectangular crop")
    result.add_argument(
        "--bounding-polygon", "--bounding-box-polygon",
        type=parse_bounding_polygon, metavar="WKT|GEOJSON|PATH",
        help=("Arbitrary WGS84 Polygon crop, supplied as WKT, GeoJSON, a file path, or @file; "
              "mutually exclusive with --latitude/--longitude"),
    )
    result.add_argument(
        "--size-mm", type=parse_size, metavar="WIDTHxHEIGHT",
        help="Printed XY size for a centered rectangular crop; 200x200 when omitted",
    )
    result.add_argument(
        "--print-frame", type=parse_print_frame, metavar="JSON|PATH",
        help=("Polygon mode only: explicit EPSG:2263 origin/axes and printed size emitted by "
              "the multi-chunk planner, keeping adjacent jobs on one manufacturing grid"),
    )
    scale = result.add_mutually_exclusive_group()
    scale.add_argument("--scale", type=float, help="Map scale denominator; 6286.5 when omitted")
    scale.add_argument(
        "--length-mm", type=float,
        help="Set a polygon crop's longer printed side and derive its scale and width",
    )
    result.add_argument(
        "--vertical-exaggeration", type=float, default=1.0,
        help="Multiplier for measured Z relief; printable cap/road clearances stay fixed",
    )
    result.add_argument(
        "--terrain-relief-factor", type=float,
        help=("Optional extra multiplier for LiDAR ground relief only; when omitted, real relief "
              "is enlarged only if needed to reach the minimum printable level budget"),
    )
    result.add_argument(
        "--terrain-origin-m", type=float,
        help=("Shared NAVD88 elevation mapped to minimum terrain height. Multi-chunk jobs must "
              "use one value for every piece; ordinary jobs default to their local minimum"),
    )
    result.add_argument(
        "--minimum-terrain-levels", type=float, default=6.0,
        help="Target slicer-layer count across the robust 5th-95th percentile ground relief",
    )
    result.add_argument("--grid-step-mm", type=float, default=0.125, help="Manufacturing raster spacing")
    result.add_argument(
        "--nozzle-mm", type=float, choices=sorted(PROCESS_PRESETS), default=DEFAULT_NOZZLE_MM,
        help="Installed P2S nozzle size; sets the printable minimum for every drawn feature",
    )
    result.add_argument(
        "--layer-height", type=float, default=0.24,
        help="Installed official P2S process layer height for the selected nozzle",
    )
    result.add_argument(
        "--prime-tower", choices=["auto", "on", "off"], default="auto",
        help="Auto enables the tower when its brim envelope fits beside the model",
    )
    result.add_argument(
        "--building-colors", type=parse_building_colors, metavar="JSON|PATH",
        default=[],
        help=("JSON list (inline, file, or @file) assigning visible buildings to the existing "
              "ivory, green, blue, or tan filament by address, WGS84 point, BIN, DoITT ID, or BBL"),
    )
    result.add_argument(
        "--foundation-color", type=parse_material_color, metavar="COLOR",
        default="ivory",
        help=("Filament for the hidden substrate below every visible surface: ivory (default), "
              "green, blue, or tan. The substrate is most of the model by volume, so this is "
              "the setting that decides which filament the print mostly consumes"),
    )
    result.add_argument("--output", type=Path, help="Final 3MF path; derived from job ID when omitted")
    result.add_argument("--data-dir", type=Path, default=ROOT / "data", help="Shared input-data root")
    result.add_argument(
        "--cache-dir", type=Path,
        help="Dataset cache root (defaults to <data-dir>/cache)",
    )
    result.add_argument(
        "--output-dir", type=Path, default=ROOT / "output",
        help="Root for generated jobs, models, plans, and other disposable output",
    )
    result.add_argument(
        "--lidar-source", choices=["cache", "laz"], default="cache",
        help="Elevation source: cached 0.5 m rasters (default) or raw LAZ tiles",
    )
    result.add_argument(
        "--lidar-cache-dir", type=Path,
        help="NYC 2017 LiDAR cache directory (defaults to <cache-dir>/nyc_lidar_2017)",
    )
    result.add_argument(
        "--project-settings-template", type=Path,
        default=ROOT / "data/bambu/project_settings.json",
        help="Clean Bambu project-settings template",
    )
    result.add_argument(
        "--bambu-studio", type=Path,
        default=Path("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"),
        help="Bambu Studio executable used for profiles and optional offline slicing",
    )
    result.add_argument("--job-id", help="Stable cache name; derived from request when omitted")
    result.add_argument(
        "--source-padding-m", type=float, default=20.0,
        help="Extra source context around the exact crop",
    )
    result.add_argument(
        "--infer-hidden-road-profiles",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Construct inferred lower roads between measured tunnel approaches",
    )
    result.add_argument(
        "--offline", action="store_true",
        help="Never access the network; fail if a selected source or other input is absent",
    )
    result.add_argument("--force", action="store_true", help="Rerun all stages in this job")
    result.add_argument("--no-preview", action="store_true", help="Skip rendering and embedding a PNG preview")
    result.add_argument("--full-validation", action="store_true", help="Also run expensive cross-material boolean checks")
    result.add_argument(
        "--slice", action=argparse.BooleanOptionalAction, default=False,
        help="Run an optional offline Bambu Studio validation slice",
    )
    result.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING"], default="INFO")
    return result


def build_config(args) -> tuple[dict, str, Path]:
    polygon_mode = args.bounding_polygon is not None
    cache_dir = (
        Path(args.cache_dir).resolve() if args.cache_dir is not None
        else (args.data_dir / "cache").resolve()
    )
    lidar_cache_dir = (
        Path(args.lidar_cache_dir).resolve()
        if args.lidar_cache_dir is not None
        else (cache_dir / "nyc_lidar_2017").resolve()
    )
    if not (0.1 <= args.grid_step_mm <= 0.5):
        raise ValueError("Grid step must be between 0.1 and 0.5 mm")
    if polygon_mode:
        if args.latitude is not None or args.longitude is not None:
            raise ValueError("Use either --bounding-polygon or --latitude/--longitude, not both")
        if args.size_mm is not None:
            raise ValueError("--size-mm is derived from a bounding polygon; use --scale or --length-mm")
        if args.print_frame is not None and args.length_mm is not None:
            raise ValueError("--length-mm cannot be combined with --print-frame; supply --scale")
        aoi_wgs = args.bounding_polygon
        if not box(*NYC_BOUNDS).covers(aoi_wgs):
            raise ValueError("Bounding polygon extends outside the supported NYC bounding box")
        aoi_2263 = gpd.GeoSeries([aoi_wgs], crs=4326).to_crs(2263).iloc[0]
        if args.length_mm is not None:
            if not (20 <= args.length_mm <= 250):
                raise ValueError("Polygon length must be between 20 and 250 mm")
            rectangle = aoi_2263.minimum_rotated_rectangle
            corners = shapely.get_coordinates(rectangle.exterior)[:-1]
            edges = [float(np.linalg.norm(corners[(i + 1) % 4] - corners[i])) for i in range(4)]
            source_length_ft = max(edges)
            effective_scale = source_length_ft * FT * 1000 / args.length_mm
        else:
            effective_scale = args.scale if args.scale is not None else 6286.5
        if not (1000 <= effective_scale <= 50000):
            raise ValueError("Scale denominator must be between 1,000 and 50,000")
        if args.print_frame is None:
            frame, (width, height) = oriented_polygon_frame(
                aoi_2263, effective_scale, args.grid_step_mm
            )
        else:
            frame = {key: args.print_frame[key] for key in ["origin_ft", "x_axis", "y_axis"]}
            width, height = args.print_frame["size_mm"]
            frame_origin = np.asarray(frame["origin_ft"], dtype=float)
            x_axis = np.asarray(frame["x_axis"], dtype=float)
            y_axis = np.asarray(frame["y_axis"], dtype=float)
            coordinates = shapely.get_coordinates(aoi_2263)
            relative = coordinates - frame_origin
            local_x = relative @ x_axis
            local_y = relative @ y_axis
            k = FT * 1000 / effective_scale
            tolerance_ft = max(args.grid_step_mm / k * 1e-5, 1e-7)
            if (
                local_x.min() < -tolerance_ft
                or local_y.min() < -tolerance_ft
                or local_x.max() > width / k + tolerance_ft
                or local_y.max() > height / k + tolerance_ft
            ):
                raise ValueError(
                    "Bounding polygon is not covered by the supplied --print-frame"
                )
        frame_origin = np.asarray(frame["origin_ft"])
        x_axis = np.asarray(frame["x_axis"])
        y_axis = np.asarray(frame["y_axis"])
        k = FT * 1000 / effective_scale
        projected_center = frame_origin + x_axis * width / (2 * k) + y_axis * height / (2 * k)
        center_wgs = gpd.GeoSeries([Point(*projected_center)], crs=2263).to_crs(4326).iloc[0]
        args.longitude, args.latitude = center_wgs.x, center_wgs.y
    else:
        if args.print_frame is not None:
            raise ValueError("--print-frame is only valid with --bounding-polygon")
        if (args.latitude is None) != (args.longitude is None):
            raise ValueError("--latitude and --longitude must be supplied together")
        if args.latitude is None:
            raise ValueError("Supply --bounding-polygon or both --latitude and --longitude")
        if args.length_mm is not None:
            raise ValueError("--length-mm is only valid with --bounding-polygon")
        width, height = args.size_mm or (200.0, 200.0)
        effective_scale = args.scale if args.scale is not None else 6286.5
        if not (1000 <= effective_scale <= 50000):
            raise ValueError("Scale denominator must be between 1,000 and 50,000")
        if not (
            NYC_BOUNDS[0] <= args.longitude <= NYC_BOUNDS[2]
            and NYC_BOUNDS[1] <= args.latitude <= NYC_BOUNDS[3]
        ):
            raise ValueError("Coordinates are outside the supported NYC bounding box")
        projected_center = gpd.GeoSeries(
            [Point(args.longitude, args.latitude)], crs=4326
        ).to_crs(2263).iloc[0]
        k = FT * 1000 / effective_scale
        origin = [projected_center.x - width / (2 * k), projected_center.y - height / (2 * k)]
        frame = {"origin_ft": origin, "x_axis": [1.0, 0.0], "y_axis": [0.0, 1.0]}
        aoi_2263 = box(origin[0], origin[1], origin[0] + width / k, origin[1] + height / k)
        aoi_wgs = gpd.GeoSeries([aoi_2263], crs=2263).to_crs(4326).iloc[0]

    if not (20 <= width <= 250 and 20 <= height <= 250):
        if polygon_mode:
            raise ValueError(
                f"Polygon produces a {width:g}x{height:g} mm model; each dimension must be 20-250 mm"
            )
        raise ValueError("Each print dimension must be between 20 and 250 mm")
    if not (0.25 <= args.vertical_exaggeration <= 5.0):
        raise ValueError("Vertical exaggeration must be between 0.25 and 5.0")
    if args.terrain_origin_m is not None and not math.isfinite(args.terrain_origin_m):
        raise ValueError("Terrain origin must be finite")
    if args.terrain_relief_factor is not None and not (0.25 <= args.terrain_relief_factor <= 10.0):
        raise ValueError("Terrain relief factor must be between 0.25 and 10.0")
    if not (0 <= args.minimum_terrain_levels <= 50):
        raise ValueError("Minimum terrain levels must be between 0 and 50")
    if not (0 <= args.source_padding_m <= 500):
        raise ValueError("Source padding must be between 0 and 500 metres")
    source_width_m = width / 1000 * effective_scale + 2 * args.source_padding_m
    source_height_m = height / 1000 * effective_scale + 2 * args.source_padding_m
    diff = (source_width_m / 0.5) * (source_height_m / 0.5) - 30_000_000
    if diff > 0:
        raise ValueError(
            f"The requested size/scale/padding would exceed the 30-million-cell elevation-grid limit by {diff:,.0f}"
        )
    for dimension in [width, height]:
        if abs(dimension / args.grid_step_mm - round(dimension / args.grid_step_mm)) > 1e-6:
            raise ValueError("Width and height must be exact multiples of --grid-step-mm")
    proposed_prime_layout = prime_tower_layout(width, height, args.nozzle_mm)
    prime = args.prime_tower == "on" or (
        args.prime_tower == "auto" and proposed_prime_layout["fits"]
    )
    if args.prime_tower == "on" and not proposed_prime_layout["fits"]:
        envelope = proposed_prime_layout["model_envelope_mm"]
        tower = proposed_prime_layout["tower_x_envelope_mm"]
        raise ValueError(
            "Prime tower does not fit beside this model: "
            f"model brim envelope={envelope}, tower X envelope={tower}, "
            f"clearance={proposed_prime_layout['model_to_tower_clearance_mm']:.3f} mm "
            f"(required {proposed_prime_layout['required_model_to_tower_clearance_mm']:g} mm)"
        )
    if prime:
        translation = proposed_prime_layout["translation_mm"]
        brim_width = proposed_prime_layout["model_brim_width_mm"]
    else:
        translation, brim_width = tower_free_layout(width, height)
    proposed_prime_layout["requested_mode"] = args.prime_tower
    proposed_prime_layout["selected"] = prime
    args.size_mm = (width, height)
    args.scale = effective_scale
    aoi_geojson = json.loads(shapely.to_geojson(aoi_wgs))
    payload = {
        "latitude": args.latitude, "longitude": args.longitude, "size_mm": [width, height],
        "scale": effective_scale, "grid_step_mm": args.grid_step_mm, "layer_height": args.layer_height,
        "aoi_wgs84": aoi_geojson,
        "source_padding_m": args.source_padding_m,
        "vertical_exaggeration": args.vertical_exaggeration,
        "terrain_relief_factor": args.terrain_relief_factor,
        "minimum_terrain_levels": args.minimum_terrain_levels,
        "infer_hidden_road_profiles": args.infer_hidden_road_profiles,
        "lidar_source": args.lidar_source,
        "cache_dir": str(cache_dir),
        "building_color_overrides": args.building_colors,
        "prime_tower": prime, "pipeline_version": PIPELINE_VERSION,
    }
    if args.print_frame is not None:
        payload["frame_epsg2263"] = frame
    if args.terrain_origin_m is not None:
        payload["terrain_origin_m"] = args.terrain_origin_m
    short = hashlib.sha256(canonical(payload).encode()).hexdigest()[:10]
    prefix = "nyc_polygon" if polygon_mode else f"nyc_{args.latitude:.5f}_{args.longitude:.5f}"
    slug = args.job_id or f"{prefix}_{width:g}x{height:g}_{short}"
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", slug)
    output = (args.output or args.output_dir / "models" / f"{slug}.3mf").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.layer_height not in PROCESS_PRESETS[args.nozzle_mm]:
        raise ValueError(
            f"Layer height {args.layer_height:g} mm has no installed P2S process preset for a "
            f"{args.nozzle_mm:g} mm nozzle; available: "
            + ", ".join(f"{height:g}" for height in sorted(PROCESS_PRESETS[args.nozzle_mm])))
    # Every drawn width below is a cartographic choice floored by what this
    # nozzle can lay, so a coarser nozzle widens a symbol rather than dropping
    # it and a finer one never shrinks it below the width the map intends.
    line=lambda mm:printable_width_mm(mm,args.nozzle_mm,beads=DRAWN_LINE_BEADS)
    feature=lambda mm:printable_width_mm(mm,args.nozzle_mm,beads=MINIMUM_FEATURE_BEADS)
    structural_roof=structural_roof_thickness_mm(args.nozzle_mm,args.layer_height)
    color_depth=surface_color_depth_mm(args.layer_height,.24)
    config = {
        "pipeline_version": PIPELINE_VERSION,
        "name": (
            f"NYC polygon map {args.latitude:.5f}, {args.longitude:.5f}"
            if polygon_mode else f"NYC map {args.latitude:.5f}, {args.longitude:.5f}"
        ),
        "center_wgs84": [args.longitude, args.latitude], "size_mm": [width, height],
        "scale_denominator": effective_scale, "grid_step_mm": args.grid_step_mm,
        "aoi_wgs84": aoi_geojson, "frame_epsg2263": frame,
        "source_padding_m": args.source_padding_m,
        "lidar_source": args.lidar_source,
        "lidar_cache_dir": str(lidar_cache_dir),
        "cache_dir": str(cache_dir),
        "vertical_exaggeration": args.vertical_exaggeration,
        "terrain_relief_factor": args.terrain_relief_factor,
        "minimum_terrain_relief_levels": args.minimum_terrain_levels,
        "maximum_terrain_relief_factor": 3.0, "minimum_terrain_source_span_m": 0.5,
        "base_mm": 1.8, "minimum_terrain_mm": 2.4, "minimum_path_width_mm": feature(0.625),
        "path_relief_mm": pavement_pad_relief_mm(args.layer_height),
        "minimum_fixture_width_mm": feature(0.45),
        "minimum_top_peak_diameter_mm": 1.0, "top_peak_minimum_separation_mm": 1.0,
        "top_peak_reinforcement_band_mm": 2.0,
        "top_peak_reinforcement_step_mm": 0.16, "inferred_cooling_height_mm": 0.2,
        "canopy_max_height_m": 36.0, "canopy_minimum_source_height_m": 1.0,
        "canopy_smoothing_m": 1.0, "canopy_maximum_closed_gap_mm": 0.75,
        "canopy_edge_roll_mm": 0.50, "canopy_trail_setback_mm": 0.30,
        "infer_hidden_road_profiles": args.infer_hidden_road_profiles, "minimum_tunnel_clearance_mm": 0.48,
        "minimum_tunnel_cover_mm": structural_roof, "minimum_tunnel_evidence_mm": 0.08,
        "maximum_tunnel_clearance_mm": 1.40, "tunnel_portal_transition_fraction": 0.15,
        "minimum_bridge_clearance_mm": 0.48, "minimum_bridge_evidence_mm": 0.08,
        "minimum_bridge_deck_thickness_mm": structural_roof,
        "maximum_bridge_clearance_mm": 1.40, "minimum_crossing_length_mm": 0.80,
        "ivory_carriageways": True,
        "road_line_width_mm": line(0.875), "major_road_line_width_mm": line(1.00),
        "road_line_relief_mm": DRAWN_LINE_RELIEF_MM,
        "road_surface_match_tolerance_mm": 0.20,
        "road_width_percentile": 30.0,
        "road_width_sample_interval_m": 15.0, "road_maximum_physical_width_m": 60.0,
        "subway_entrances": True, "minimum_entrance_width_mm": feature(0.5),
        "entrance_relief_mm": 0.2,
        "colors": MATERIAL_COLORS,
        "foundation_material": args.foundation_color,
        "building_color_overrides": args.building_colors,
        "plate_translation_mm": translation, "nozzle_mm": args.nozzle_mm, "wall_generator": "arachne",
        "layer_height_mm": args.layer_height,
        "process_preset": PROCESS_PRESETS[args.nozzle_mm][args.layer_height],
        "machine_preset": machine_preset(args.nozzle_mm),
        # The first layer sets the phase of every slicing plane above it, so the
        # meshes are quantized against it and the sliced profile must repeat it.
        "first_layer_height_mm": FIRST_LAYER_HEIGHT_MM,
        "wall_loops": 2, "infill_percent": 15, "bottom_shell_layers": 3,
        "ironing_type": "top", "top_surface_line_width_mm": round(1.05*args.nozzle_mm,10),
        "top_shell_layers": 4, "brim_width_mm": brim_width, "brim_gap_mm": MODEL_BRIM_GAP_MM,
        "minimum_surface_color_depth_mm": 0.24, "surface_color_depth_mm": color_depth,
        "export_snap_denominator": 65536,
        "prime_tower": prime, "prime_tower_width_mm": PRIME_TOWER_WIDTH_MM,
        "prime_tower_brim_width_mm": PRIME_TOWER_BRIM_MM,
        "prime_tower_position_mm": list(PRIME_TOWER_POSITION_MM),
        "prime_tower_layout": proposed_prime_layout,
        "source_notes": (
            f"Generated from cached/downloaded free NYC sources (LiDAR source: {args.lidar_source}); "
            "contains OpenStreetMap data © OpenStreetMap contributors, available under ODbL 1.0; "
            "see ATTRIBUTION.md, the job manifest, and logs."
        ),
    }
    if args.terrain_origin_m is not None:
        config["terrain_origin_m"] = args.terrain_origin_m
    return config, slug, output


def main() -> None:
    args = parser().parse_args()
    try:
        config, slug, output = build_config(args)
        reference = args.project_settings_template.resolve()
        slicer = args.bambu_studio.resolve()
        profiles = slicer.parent.parent / "Resources/profiles/BBL"
        if not reference.exists():
            raise FileNotFoundError(f"Required clean Bambu project-settings template is missing: {reference}")
        if not profiles.exists():
            raise FileNotFoundError(f"Bambu Studio P2S profiles were not found beside {slicer}")
        if args.slice and not slicer.exists():
            raise FileNotFoundError(f"Bambu Studio executable is missing: {slicer}")
        Pipeline(args, config, args.output_dir.resolve() / "jobs" / slug, output, reference, profiles, slicer).run()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        print(f"generate_3mf failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
