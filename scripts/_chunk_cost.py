#!/usr/bin/env python3
"""Evidence for where a printed map may be cut, built from the NYC caches.

A seam between two plates is a vertical plane through the model, so the things
that must not be cut are exactly the things that stand up: buildings, bridges,
elevated structures and mature canopy.  The dominant signal is therefore the
cached LiDAR above-ground height (``upper_surface_m - ground_m``), refined by
Planimetrics polygons that say which flat ground is paved roadway, water, park
or plaza, and by hard keep-outs around structures the generator fits per crop.

Everything here reads only ``data/cache`` and never invents a class legend the
repository does not document.
"""

from __future__ import annotations

import hashlib
import json
import math
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import shapely
from affine import Affine
from rasterio.features import rasterize
from rasterio.warp import Resampling, reproject
from shapely.geometry import Polygon, box

from _chunk_geometry import (
    AXIS_X,
    FT,
    CutChooser,
    CutRequest,
    Frame,
    PlanGeometryError,
    rectilinear_path,
    to_points,
)
from cache_common import read_tiled_geoparquet


COST_SURFACE_VERSION = 1
CRS = 2263
# Price for a cell inside a hard keep-out. Large enough that no reasonable
# corridor prefers it, finite so prefix sums stay well defined.
BLOCKED_COST = 1e12
# A seam longer than this multiple of a feature's own narrow width is running
# along it rather than crossing it.
LENGTHWISE_RATIO = 1.6

# Planimetrics layers this module reads, and what each contributes. Layer names
# are the cached file stems; see data/cache/nyc_planimetrics_2022/manifest.json.
CHEAP_SURFACE_LAYERS = ("ROADBED", "MEDIAN", "PLAZA", "PARKING_LOT")
WATER_LAYER = "HYDROGRAPHY"
PARK_LAYER = "PARK"
STRUCTURE_LAYER = "TRANSPORT_STRUCTURE"
# The only nyc_land_cover_2017 classes with a documented in-repo meaning are 1
# (tree canopy) and 1|2 (vegetation), per build_map_fields.py. Nothing else in
# that raster's legend is relied on here.
VEGETATION_CLASSES = (1, 2)


class SourceDataError(RuntimeError):
    """A required cache is missing, incomplete, or not production-ready."""


@dataclass(frozen=True)
class CostWeights:
    """Knobs for turning evidence into a scalar cut cost."""

    height_weight: float = 4.0
    height_cap_m: float = 40.0
    # Tree height is discounted against building height. The generator models
    # canopy as continuous measured relief rather than discrete objects, so a
    # seam through a tree line is a minor blemish where a seam through a
    # building is a broken object; without this, cuts jog constantly to thread
    # between individual trees in open parkland.
    canopy_height_factor: float = 0.25
    building_weight: float = 300.0
    cheap_surface_factor: float = 0.15
    open_ground_factor: float = 0.4
    open_ground_height_m: float = 2.0
    water_crossing_penalty: float = 60.0
    nodata_cost: float = 40.0
    # Every building footprint is a keep-out, not merely expensive: a seam
    # through a building is the one defect no amount of cost tuning should be
    # able to trade away. keep_out_height_m raises the bar to only taller
    # buildings when a target is too dense to route around all of them.
    keep_out_height_m: float = 0.0
    keep_out_buffer_m: float = 2.0
    straightness: float = 8.0
    centering: float = 0.25

    def key(self) -> dict:
        return {field_name: getattr(self, field_name) for field_name in self.__annotations__}


def cache_signature(cache_dir: Path, name: str, *, required: bool = True) -> dict:
    """Identify a cache by its published manifest, refusing unfinished builds."""
    manifest_path = cache_dir / name / "manifest.json"
    if not manifest_path.is_file():
        if required:
            raise SourceDataError(
                f"Cache {name} is missing its manifest at {manifest_path}. "
                f"Build it with scripts/cache_{name}.py first."
            )
        return {"name": name, "present": False}
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("status") != "complete" or manifest.get("production_ready") is False:
        raise SourceDataError(
            f"Cache {name} is not production-ready ({manifest_path}); rebuild it "
            f"with scripts/cache_{name}.py"
        )
    return {
        "name": name,
        "present": True,
        "cache_format_version": manifest.get("cache_format_version"),
        "completed_at": manifest.get("completed_at"),
        "output_bytes": manifest.get("output_bytes"),
    }


# --------------------------------------------------------------------------
# Raster grid in frame coordinates
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameGrid:
    """A north-up-in-frame raster covering one plan's target polygon."""

    frame: Frame
    origin_ft: tuple[float, float]
    resolution_ft: float
    width: int
    height: int

    @classmethod
    def covering(cls, frame: Frame, target_ft: Polygon, resolution_m: float, margin_m: float):
        resolution_ft = resolution_m / FT
        minx, miny, maxx, maxy = target_ft.buffer(margin_m / FT).bounds
        minx = math.floor(minx / resolution_ft) * resolution_ft
        miny = math.floor(miny / resolution_ft) * resolution_ft
        width = max(1, int(math.ceil((maxx - minx) / resolution_ft)))
        height = max(1, int(math.ceil((maxy - miny) / resolution_ft)))
        return cls(frame, (minx, miny), resolution_ft, width, height)

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    @property
    def transform(self) -> Affine:
        """EPSG:2263 transform with row 0 at the frame-local top edge."""
        x = np.asarray(self.frame.x_axis)
        y = np.asarray(self.frame.y_axis)
        top_left = (
            np.asarray(self.frame.origin_ft)
            + x * self.origin_ft[0]
            + y * (self.origin_ft[1] + self.height * self.resolution_ft)
        )
        step = self.resolution_ft
        return Affine(x[0] * step, -y[0] * step, top_left[0],
                      x[1] * step, -y[1] * step, top_left[1])

    @property
    def world_bounds(self) -> tuple[float, float, float, float]:
        corners = box(
            self.origin_ft[0], self.origin_ft[1],
            self.origin_ft[0] + self.width * self.resolution_ft,
            self.origin_ft[1] + self.height * self.resolution_ft,
        )
        return self.frame.to_world(corners).bounds

    def frame_box(self) -> Polygon:
        return box(
            self.origin_ft[0], self.origin_ft[1],
            self.origin_ft[0] + self.width * self.resolution_ft,
            self.origin_ft[1] + self.height * self.resolution_ft,
        )

    def columns(self, x_ft: np.ndarray) -> np.ndarray:
        return np.floor((np.asarray(x_ft) - self.origin_ft[0]) / self.resolution_ft).astype(int)

    def rows(self, y_ft: np.ndarray) -> np.ndarray:
        offset = (np.asarray(y_ft) - self.origin_ft[1]) / self.resolution_ft
        return (self.height - 1 - np.floor(offset)).astype(int)

    def x_at(self, columns: np.ndarray) -> np.ndarray:
        return self.origin_ft[0] + (np.asarray(columns) + 0.5) * self.resolution_ft

    def y_at(self, rows: np.ndarray) -> np.ndarray:
        return self.origin_ft[1] + (self.height - 0.5 - np.asarray(rows)) * self.resolution_ft


# --------------------------------------------------------------------------
# Layer construction
# --------------------------------------------------------------------------


def _burn(grid: FrameGrid, geometries: Iterable, *, all_touched: bool = True) -> np.ndarray:
    """Rasterize EPSG:2263 geometry onto the frame grid.

    ``grid.transform`` already carries the frame rotation, so shapes are burnt
    in world coordinates and rasterio inverts the rotation for us.
    """
    shapes = [
        (geometry, 1) for geometry in geometries
        if geometry is not None and not geometry.is_empty
    ]
    if not shapes:
        return np.zeros(grid.shape, dtype=bool)
    return rasterize(
        shapes, out_shape=grid.shape, transform=grid.transform,
        fill=0, dtype="uint8", all_touched=all_touched,
    ).astype(bool)


def _read_planimetrics(cache_dir: Path, layer: str, bounds) -> gpd.GeoDataFrame:
    path = cache_dir / "nyc_planimetrics_2022" / f"{layer}.parquet"
    if not path.is_file():
        raise SourceDataError(f"Planimetrics layer {layer} is missing at {path}")
    return gpd.read_parquet(path, bbox=tuple(bounds))


def _lidar_mosaics(cache_dir: Path, grid: FrameGrid) -> dict[str, np.ndarray]:
    """Mosaic cached ground and upper-surface rasters onto the frame grid.

    The upper surface is max-pooled and the ground is both mean-pooled (for
    height) and min-pooled (for the shared terrain datum, which must be a true
    lower bound of what the generator will sample at full resolution).
    """
    root = cache_dir / "nyc_lidar_2017"
    catalog_path = root / "catalog.geojson"
    if not catalog_path.is_file():
        raise SourceDataError(
            f"LiDAR catalog is missing at {catalog_path}; build it with "
            "scripts/cache_nyc_lidar_2017.py"
        )
    catalog = gpd.read_file(catalog_path)
    catalog = catalog.set_crs(4326) if catalog.crs is None else catalog
    catalog = catalog.to_crs(CRS)
    area = grid.frame.to_world(grid.frame_box())
    selected = catalog[catalog.intersects(area)]
    if selected.empty:
        raise SourceDataError(
            "The requested area has no cached NYC 2017 LiDAR coverage; extend the "
            "LiDAR cache before planning here"
        )
    plans = (
        ("upper_max", "upper", Resampling.max, np.fmax),
        ("ground_mean", "ground", Resampling.average, None),
        ("ground_min", "ground", Resampling.min, np.fmin),
    )
    result = {name: np.full(grid.shape, np.nan, np.float32) for name, _, _, _ in plans}
    transform = grid.transform
    for path in selected["ground"].tolist() + selected["upper"].tolist():
        if not (root / path).is_file():
            raise SourceDataError(f"LiDAR cache tile is missing: {root / path}")
    for name, column, resampling, combine in plans:
        target = result[name]
        for relative in selected[column]:
            with rasterio.open(root / relative) as source:
                patch = np.full(grid.shape, np.nan, np.float32)
                reproject(
                    rasterio.band(source, 1), patch,
                    src_transform=source.transform, src_crs=source.crs,
                    dst_transform=transform, dst_crs=CRS,
                    resampling=resampling, dst_nodata=np.nan,
                )
            valid = np.isfinite(patch)
            if combine is None:
                target[valid] = patch[valid]
            else:
                target[valid] = combine(target[valid], patch[valid])
    return result


def _land_cover_vegetation(cache_dir: Path, grid: FrameGrid) -> np.ndarray | None:
    """Majority-resample the documented vegetation classes, or return None."""
    path = cache_dir / "nyc_land_cover_2017" / "landcover_native.tif"
    if not path.is_file():
        return None
    classes = np.zeros(grid.shape, np.uint8)
    with rasterio.open(path) as source:
        reproject(
            rasterio.band(source, 1), classes,
            src_transform=source.transform, src_crs=source.crs,
            dst_transform=grid.transform, dst_crs=CRS,
            resampling=Resampling.mode, src_nodata=0, dst_nodata=0,
        )
    return np.isin(classes, VEGETATION_CLASSES)


# build_map_fields.py:219,242 selects crossings from OSM highway LineStrings
# only: a tunnel is tunnel in {yes, building_passage} and a bridge is any
# bridge tag other than "no". Matching that exactly keeps the keep-out aligned
# with the geometry the generator actually fits per crop, and in particular
# leaves deep railway=subway lines under the avenues cuttable.
TUNNEL_TAGS = ("yes", "building_passage")
CROSSING_HALF_WIDTH_M = 12.0


def _osm_crossings(cache_dir: Path, grid: FrameGrid, buffer_ft: float) -> gpd.GeoDataFrame:
    """Road bridges and road tunnels, the crossings the generator fits per crop."""
    root = cache_dir / "new_york_osm"
    osm = read_tiled_geoparquet(
        root, tuple(grid.world_bounds),
        deduplicate_by=["source_order"], source_order=["source_order"],
    )
    empty = gpd.GeoDataFrame(
        {"kind": [], "name": []}, geometry=gpd.GeoSeries([], crs=CRS), crs=CRS
    )
    if osm.empty or "highway" not in osm:
        return empty
    roads = osm["highway"].notna().to_numpy()
    kinds = np.full(len(osm), "", dtype=object)
    if "tunnel" in osm:
        kinds = np.where(osm["tunnel"].isin(TUNNEL_TAGS).to_numpy(), "tunnel", kinds)
    if "bridge" in osm:
        bridge = osm["bridge"]
        kinds = np.where((bridge.notna() & (bridge != "no")).to_numpy(), "bridge", kinds)
    selected = osm[roads & (kinds != "")]
    if selected.empty:
        return empty
    half_width = CROSSING_HALF_WIDTH_M / FT + buffer_ft
    names = selected["name"] if "name" in selected else pd.Series([None] * len(selected))
    return gpd.GeoDataFrame(
        {
            "kind": kinds[roads & (kinds != "")],
            "name": names.fillna("unnamed road crossing").to_numpy(),
        },
        geometry=[geometry.buffer(half_width) for geometry in selected.geometry.values],
        crs=CRS,
    )


@dataclass
class CostSurface(CutChooser):
    """Scalar cut cost plus the evidence layers used to explain a plan."""

    grid: FrameGrid
    cost: np.ndarray
    keep_out: np.ndarray
    height_m: np.ndarray
    ground_m: np.ndarray
    ground_min_m: np.ndarray
    layers: dict[str, np.ndarray]
    weights: CostWeights
    sources: dict = field(default_factory=dict)
    # Named keep-out features in EPSG:2263, so a report can say which bridge a
    # seam crosses rather than only that it crossed something.
    keep_out_features: gpd.GeoDataFrame | None = None
    # "staircase" follows the street grid with right-angle jogs; "angled" makes
    # each seam one straight line whose angle and position are optimized.
    style: str = "staircase"
    _index: shapely.STRtree | None = field(default=None, repr=False)

    # -- CutChooser -------------------------------------------------------

    def _level_step(self, request: CutRequest) -> float:
        """Across spacing: a whole number of snap steps, at least one cell."""
        snap = max(request.snap_ft, 1e-9)
        return snap * max(1, int(math.ceil(self.grid.resolution_ft / snap)))

    def _band_indices(self, request: CutRequest) -> tuple[np.ndarray, np.ndarray]:
        """Along samples, and the snap-lattice levels the cut may sit on.

        Levels are absolute lattice positions rather than offsets from the
        nominal, so the chosen cut already sits on the manufacturing grid and
        no later snapping can nudge it outside the deviation allowance.
        """
        level = self._level_step(request)
        low = math.ceil((request.v_nominal - request.deviation_ft) / level - 1e-9) * level
        high = math.floor((request.v_nominal + request.deviation_ft) / level + 1e-9) * level
        count = int(round((high - low) / level)) + 1
        return request.samples, low + np.arange(max(count, 1)) * level

    def _sample(self, array: np.ndarray, axis: int, u: np.ndarray, v: np.ndarray, fill):
        """Nearest-cell lookup for the (along, across) grid of a cut band."""
        along, across = np.meshgrid(u, v, indexing="ij")
        x, y = (across, along) if axis == AXIS_X else (along, across)
        rows, columns = self.grid.rows(y), self.grid.columns(x)
        inside = (
            (rows >= 0) & (rows < self.grid.height)
            & (columns >= 0) & (columns < self.grid.width)
        )
        out = np.full(rows.shape, fill, dtype=array.dtype if array.dtype != bool else bool)
        out[inside] = array[rows[inside], columns[inside]]
        return out, inside

    def choose(self, request: CutRequest) -> np.ndarray:
        level_ft = self._level_step(request)
        straight = np.asarray([[request.u_start, request.v_nominal],
                               [request.u_end, request.v_nominal]])
        if request.deviation_ft < level_ft:
            return straight
        u, v = self._band_indices(request)
        if len(v) < 2:
            return np.asarray([[request.u_start, v[0]], [request.u_end, v[0]]])
        band, inside = self._sample(self.cost, request.axis, u, v, np.float32(np.inf))
        band = np.where(inside, band.astype(float), np.inf)
        # Price a jog against the band's mean cost, not its cheap tail. A band
        # running down a street is almost all cheap pavement, so a low
        # percentile is near zero and makes jogging effectively free: the
        # search then jogs constantly to shave negligible amounts, which is
        # what turns a straight seam into a staircase. The mean reflects what a
        # jog can actually save. Keep-outs are unaffected either way, since no
        # jog price approaches their cost.
        finite = band[np.isfinite(band)]
        reference = max(float(finite.mean()) if finite.size else 1.0, 1e-3)
        if request.region is not None:
            # Only the in-region part of a cut becomes a seam, so terrain
            # outside it must neither attract nor repel the search.
            along, across = np.meshgrid(u, v, indexing="ij")
            x, y = (across, along) if request.axis == AXIS_X else (along, across)
            band = np.where(shapely.contains_xy(request.region, x, y), band, 0.0)
        nominal_index = int(np.argmin(np.abs(v - request.v_nominal)))
        centering = self.weights.centering * reference

        if self.style == "axis":
            # One straight line parallel to the frame axis. In a frame aligned
            # to the street grid that means the whole seam runs down a single
            # street, which is both the cleanest seam and the flattest one.
            priced = np.where(np.isfinite(band), band, BLOCKED_COST)
            score = priced.sum(axis=0) + centering * np.abs(
                np.arange(len(v)) - nominal_index
            ) * band.shape[0]
            level = float(v[int(np.argmin(score))])
            return np.asarray([[request.u_start, level], [request.u_end, level]])

        along_step = float(u[1] - u[0]) if len(u) > 1 else request.step_ft
        minimum_run = max(1, int(round(request.min_run_ft / max(along_step, 1e-9))))
        levels = staircase_path(
            band,
            minimum_run=minimum_run,
            nominal_index=nominal_index,
            # Priced in the same units as a run: a jog must save at least
            # `straightness` runs' worth of cheap-surface cost to be worth it.
            jog_penalty=self.weights.straightness * reference * minimum_run,
            centering_penalty=centering,
            min_jog_levels=max(1, int(round(request.min_jog_ft / level_ft))),
        )
        path_u, path_v = rectilinear_path(
            u, v[levels], snap_ft=level_ft, min_run_ft=0.0, min_jog_ft=0.0
        )
        points = np.column_stack([path_u, path_v])
        if self.style == "angled":
            points = self.straighten(request.axis, points, min_side_ft=request.min_run_ft)
        return points

    def straighten(self, axis: int, points: np.ndarray, *, min_side_ft: float) -> np.ndarray:
        """Replace right-angle jogs with the fewest straight segments that stay clear.

        The street-following search gives a path that provably avoids every
        keep-out, but as a run of right angles.  This collapses those into
        straight segments at arbitrary angles wherever a segment can be drawn
        without touching a building or structure — long diagonals across a park
        or a shoreline, right angles where a dense block leaves no other route.
        Bends land on the original vertices, which sit at street intersections.

        The objective is lexicographic: fewest sides first, then fewest sides
        under ``min_side_ft``, then cost.  Side count and stubby sides are what
        make a chunk awkward to place on a wall, so they outrank a marginally
        cheaper route rather than competing with it on one blended scale.
        """
        frame_points = np.asarray(to_points(axis, points[:, 0], points[:, 1]), dtype=float)
        count = len(frame_points)
        if count < 3:
            return points
        unreachable = (math.inf, math.inf, math.inf)
        best: list[tuple[float, float, float]] = [unreachable] * count
        back = [0] * count
        best[0] = (0.0, 0.0, 0.0)
        for target in range(1, count):
            for source in range(target - 1, -1, -1):
                if not math.isfinite(best[source][0]):
                    continue
                clear, cost, length = self._segment_quality(
                    frame_points[source], frame_points[target]
                )
                if not clear:
                    continue
                candidate = (
                    best[source][0] + 1.0,
                    best[source][1] + (1.0 if length < min_side_ft else 0.0),
                    best[source][2] + cost,
                )
                if candidate < best[target]:
                    best[target], back[target] = candidate, source
        if not math.isfinite(best[-1][0]):
            return points
        order = [count - 1]
        while order[-1] != 0:
            order.append(back[order[-1]])
        return points[np.asarray(sorted(order))]

    def _segment_quality(self, start, end) -> tuple[bool, float, float]:
        """Whether a straight frame-local segment is keep-out free, its cost and length."""
        line = shapely.LineString([start, end])
        rows, columns = self._seam_samples(line)
        if not len(rows):
            return True, 0.0, float(line.length)
        if self.keep_out[rows, columns].any():
            return False, math.inf, float(line.length)
        priced = self.cost[rows, columns]
        return (True,
                float(np.where(np.isfinite(priced), priced, BLOCKED_COST).sum()),
                float(line.length))

    def _seam_samples(self, seam) -> tuple[np.ndarray, np.ndarray]:
        """Densify a frame-local seam onto the raster and return cell indices."""
        if seam is None or seam.is_empty or seam.length <= 0:
            return np.empty(0, int), np.empty(0, int)
        dense = shapely.get_coordinates(seam.segmentize(self.grid.resolution_ft))
        rows = self.grid.rows(dense[:, 1])
        columns = self.grid.columns(dense[:, 0])
        inside = (
            (rows >= 0) & (rows < self.grid.height)
            & (columns >= 0) & (columns < self.grid.width)
        )
        return rows[inside], columns[inside]

    def blocked(self, seam) -> int:
        rows, columns = self._seam_samples(seam)
        if not len(rows):
            return 0
        return int(self.keep_out[rows, columns].sum())

    def describe(self, seam) -> dict:
        rows, columns = self._seam_samples(seam)
        if not len(rows):
            return {}
        height = self.height_m[rows, columns]
        finite = height[np.isfinite(height)]
        report = {
            "samples": int(len(rows)),
            "mean_cost": float(np.mean(self.cost[rows, columns])),
            "nodata_fraction": float(np.mean(~np.isfinite(height))),
        }
        if finite.size:
            report["mean_above_ground_m"] = float(np.mean(finite))
            report["p95_above_ground_m"] = float(np.percentile(finite, 95))
        for name, key in (("building_core", "building"), ("structure", "structure"),
                          ("cheap_surface", "cheap_surface"), ("water", "water")):
            if name in self.layers:
                report[f"{key}_fraction"] = float(np.mean(self.layers[name][rows, columns]))
        report["blocked_fraction"] = float(np.mean(self.keep_out[rows, columns]))
        crossings = self.crossings(seam)
        if crossings:
            report["crosses"] = crossings
        return report

    def crossings(self, seam) -> list[dict]:
        """Name the keep-out features a seam runs through, longest first.

        Each feature is reported separately rather than pooled by name: many
        bridges are unnamed in the sources, and summing them under one label
        would turn a dozen short, unavoidable perpendicular crossings into one
        alarming total.
        """
        features = self.keep_out_features
        if features is None or features.empty or seam is None or seam.is_empty:
            return []
        world = self.grid.frame.to_world(seam)
        if self._index is None:
            self._index = shapely.STRtree(features.geometry.to_numpy())
        found = []
        for position in self._index.query(world):
            row = features.iloc[int(position)]
            overlap = float(world.intersection(row.geometry).length)
            if overlap <= self.grid.resolution_ft:
                continue
            width = _narrow_width(row.geometry)
            found.append({
                "feature": int(position),
                "kind": str(row["kind"]), "name": str(row["name"]),
                "length_ft": round(overlap, 1),
                "feature_width_ft": round(width, 1),
                # A seam has to cross an elevated road somewhere, and crossing
                # it square costs about its width. Running much further than
                # that means the seam is traveling along the structure, which
                # is the defect worth failing on.
                "along_feature": bool(overlap > LENGTHWISE_RATIO * width),
            })
        return sorted(found, key=lambda item: -item["length_ft"])

    # -- terrain ----------------------------------------------------------

    def terrain_statistics(self, target_ft: Polygon, *, origin_margin_m: float) -> dict:
        """Whole-area terrain values every chunk must be generated with.

        ``generate_3mf`` defaults both the vertical datum and the relief factor
        to crop-local statistics, which would step the terrain at every seam.
        These are the values the planner pins instead.
        """
        inside = _burn(self.grid, [self.grid.frame.to_world(target_ft)], all_touched=False)
        if not inside.any():
            raise SourceDataError("The target polygon does not cover any cost-surface cells")
        ground_min = self.ground_min_m[inside & np.isfinite(self.ground_min_m)]
        ground = self.ground_m[inside & np.isfinite(self.ground_m)]
        if not ground_min.size or not ground.size:
            raise SourceDataError("No LiDAR ground elevations fall inside the target polygon")
        dry = inside & np.isfinite(self.ground_m) & ~self.layers.get(
            "water", np.zeros(self.grid.shape, dtype=bool)
        )
        relief_values = self.ground_m[dry] if dry.any() else ground
        low, high = np.percentile(relief_values, [5, 95])
        return {
            "terrain_origin_m": float(ground_min.min() - origin_margin_m),
            "observed_minimum_m": float(ground_min.min()),
            "origin_margin_m": float(origin_margin_m),
            "relief_p5_m": float(low),
            "relief_p95_m": float(high),
            "relief_span_m": float(max(0.0, high - low)),
            "relief_sample_cells": int(relief_values.size),
        }


def staircase_path(
    band: np.ndarray,
    *,
    minimum_run: int,
    nominal_index: int,
    jog_penalty: float,
    centering_penalty: float,
    min_jog_levels: int,
) -> np.ndarray:
    """Cheapest street-following staircase through a cut band.

    ``band`` is cost sampled at (along-step, across-level).  The cut holds one
    level for at least ``minimum_run`` along-steps and may then jog
    perpendicular to a level at least ``min_jog_levels`` away, paying
    ``jog_penalty`` plus the real cost of the cells the jog crosses.  Charging
    the crossing is what keeps a jog on a cross-street instead of driving it
    through the buildings between two streets.

    A jog may start at *any* along-step rather than on a fixed pitch, which
    matters: forcing jogs onto a fixed pitch is what puts them mid-block.  The
    minimum run is enforced by a lock-in state instead, so the result is
    regular by construction and no later straightening can push the cut back
    onto something it deliberately avoided.

    Blocked cells carry a large finite price rather than infinity, so a band
    with no clear corridor still yields the least-bad cut instead of failing;
    the caller re-measures the finished seam and reports what it crosses.
    """
    band = np.asarray(band, dtype=float)
    if band.ndim != 2 or band.shape[0] < 1 or band.shape[1] < 1:
        raise PlanGeometryError("A cut band must be a non-empty 2-D array")
    priced = np.where(np.isfinite(band), band, BLOCKED_COST)
    steps, levels = priced.shape
    run = min(max(1, int(minimum_run)), steps)
    grid = np.arange(levels)
    delta = np.abs(grid[:, None] - grid[None, :])
    low = np.minimum(grid[:, None], grid[None, :])
    high = np.maximum(grid[:, None], grid[None, :])
    jog_fixed = np.where(delta >= max(1, min_jog_levels), jog_penalty, np.inf)
    centering = centering_penalty * np.abs(grid - nominal_index)
    step_cost = priced + centering

    # A state is either free to jog, or locked for k more steps at its level.
    free = _State.empty(levels)
    locked = [_State.empty(levels) for _ in range(run)]
    start = _State(step_cost[0].copy(), np.zeros(levels, np.int32),
                   np.full(levels, -1, np.int32))
    if run == 1:
        free = start
    else:
        locked[run - 1] = start
    history_start = np.zeros((steps, levels), np.int32)
    history_origin = np.full((steps, levels), -1, np.int32)
    history_start[0], history_origin[0] = free.start, free.origin

    for step in range(1, steps):
        released = locked.pop(0)
        locked.append(_State.empty(levels))
        free = free.combine(released)
        walk = np.concatenate([[0.0], np.cumsum(priced[step])])
        crossing = jog_fixed + (walk[high + 1] - walk[low])
        candidates = free.cost[:, None] + crossing
        source = np.argmin(candidates, axis=0)
        locked[run - 1] = _State(
            candidates[source, grid],
            np.full(levels, step, np.int32),
            source.astype(np.int32),
        )
        free = free.advance(step_cost[step])
        for state in locked:
            state.add(step_cost[step])
        history_start[step], history_origin[step] = free.start, free.origin

    level = int(np.argmin(free.cost))
    path = np.empty(steps, dtype=int)
    cursor = steps - 1
    while cursor >= 0:
        begin = int(history_start[cursor][level])
        path[begin:cursor + 1] = level
        origin = int(history_origin[cursor][level])
        if origin < 0 or begin == 0:
            path[:begin] = level
            break
        cursor, level = begin - 1, origin
    return path


@dataclass
class _State:
    """One dynamic-programming frontier: cost plus current-segment provenance."""

    cost: np.ndarray
    start: np.ndarray
    origin: np.ndarray

    @classmethod
    def empty(cls, levels: int) -> "_State":
        return cls(np.full(levels, np.inf), np.zeros(levels, np.int32),
                   np.full(levels, -1, np.int32))

    def combine(self, other: "_State") -> "_State":
        take = other.cost < self.cost
        return _State(
            np.where(take, other.cost, self.cost),
            np.where(take, other.start, self.start),
            np.where(take, other.origin, self.origin),
        )

    def advance(self, step_cost: np.ndarray) -> "_State":
        return _State(self.cost + step_cost, self.start, self.origin)

    def add(self, step_cost: np.ndarray) -> None:
        self.cost = self.cost + step_cost


# --------------------------------------------------------------------------
# Assembly and caching
# --------------------------------------------------------------------------


def surface_key(frame: Frame, target_ft: Polygon, resolution_m: float,
                weights: CostWeights, sources: dict) -> str:
    # Deliberately excludes scale and grid step: the raster depends only on the
    # frame's origin, axes and sampling, so one surface serves every scale a
    # --fit-scale search tries.
    payload = json.dumps({
        "version": COST_SURFACE_VERSION,
        "origin_ft": list(frame.origin_ft),
        "x_axis": list(frame.x_axis),
        "y_axis": list(frame.y_axis),
        "resolution_m": resolution_m,
        "weights": weights.key(),
        "sources": sources,
        "target": shapely.to_wkb(shapely.set_precision(target_ft, 1e-6)).hex(),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def build_cost_surface(
    frame: Frame,
    target_ft: Polygon,
    *,
    cache_dir: Path,
    resolution_m: float = 4.0,
    margin_m: float = 60.0,
    weights: CostWeights | None = None,
    planner_cache_dir: Path | None = None,
    use_land_cover: bool = True,
    log=None,
) -> CostSurface:
    """Rasterize every cut-cost input onto one frame-aligned grid."""
    weights = weights or CostWeights()
    grid = FrameGrid.covering(frame, target_ft, resolution_m, margin_m)
    sources = {
        name: cache_signature(cache_dir, name)
        for name in ("nyc_lidar_2017", "nyc_planimetrics_2022",
                     "nyc_building_footprints", "new_york_osm")
    }
    if use_land_cover:
        sources["nyc_land_cover_2017"] = cache_signature(
            cache_dir, "nyc_land_cover_2017", required=False
        )
    key = surface_key(frame, target_ft, resolution_m, weights, sources)
    cached = _load_surface(planner_cache_dir, key, grid, weights, sources)
    if cached is not None:
        if log:
            log.info("cost_surface_cached", key=key, shape=list(grid.shape))
        return cached

    if log:
        log.info("cost_surface_building", key=key, shape=list(grid.shape),
                 resolution_m=resolution_m)
    world = grid.world_bounds
    elevation = _lidar_mosaics(cache_dir, grid)
    upper, ground, ground_min = (
        elevation["upper_max"], elevation["ground_mean"], elevation["ground_min"]
    )
    height = upper - ground
    observed = np.isfinite(height)
    height_positive = np.where(observed, np.clip(height, 0.0, None), np.nan)

    buffer_ft = weights.keep_out_buffer_m / FT
    layers: dict[str, np.ndarray] = {}

    footprints = read_tiled_geoparquet(
        cache_dir / "nyc_building_footprints", world,
        deduplicate_by=["doitt_id", "bin"], source_order=["source_order"],
    )
    layers["building"] = _burn(grid, footprints.geometry.values)
    # Reporting uses center-burnt footprints so it measures the same thing the
    # keep-out constrains. The all-touched layer above dilates every footprint
    # by up to a cell and would score a seam running cleanly down a street as
    # if it clipped the buildings on both sides.
    layers["building_core"] = _burn(grid, footprints.geometry.values, all_touched=False)
    tall = footprints
    if "height_roof" in footprints and weights.keep_out_height_m > 0:
        heights_m = pd.to_numeric(footprints["height_roof"], errors="coerce") * FT
        tall = footprints[heights_m.fillna(0.0) >= weights.keep_out_height_m]
    tall_shapes = [g.buffer(buffer_ft) for g in tall.geometry.values]
    # Keep-outs are burnt by cell center, not by touch: at this sampling an
    # all-touched footprint grows by up to a whole cell and can close the very
    # street corridor a seam needs. The buffer supplies the clearance instead.
    layers["tall_building"] = _burn(grid, tall_shapes, all_touched=False)

    structures = _read_planimetrics(cache_dir, STRUCTURE_LAYER, world)
    structure_shapes = [g.buffer(buffer_ft) for g in structures.geometry.values]
    layers["structure"] = _burn(grid, structure_shapes, all_touched=False)
    cheap = []
    for layer in CHEAP_SURFACE_LAYERS:
        cheap.extend(_read_planimetrics(cache_dir, layer, world).geometry.values)
    layers["cheap_surface"] = _burn(grid, cheap)
    water = _read_planimetrics(cache_dir, WATER_LAYER, world)
    layers["water"] = _burn(grid, water.geometry.values)
    parks = _read_planimetrics(cache_dir, PARK_LAYER, world)
    layers["park"] = _burn(grid, parks.geometry.values)
    crossings = _osm_crossings(cache_dir, grid, buffer_ft)
    layers["osm_structure"] = _burn(grid, crossings.geometry.values)
    vegetation = _land_cover_vegetation(cache_dir, grid) if use_land_cover else None
    if vegetation is not None:
        layers["vegetation"] = vegetation

    keep_out = layers["structure"] | layers["osm_structure"] | layers["tall_building"]
    named = gpd.GeoDataFrame(
        {
            "kind": (["transport structure"] * len(structure_shapes)
                     + crossings["kind"].tolist()
                     + ["building"] * len(tall_shapes)),
            "name": (
                _names(structures, "NAME", "transport structure")
                + crossings["name"].tolist()
                + _names(tall, "name", "building")
            ),
        },
        geometry=structure_shapes + list(crossings.geometry.values) + tall_shapes,
        crs=CRS,
    )

    low = np.nan_to_num(height_positive, nan=0.0)
    greenery = (layers["park"] | layers.get("vegetation", np.zeros(grid.shape, dtype=bool))) \
        & ~layers["building"]
    height_term = weights.height_weight * np.minimum(low, weights.height_cap_m)
    height_term = np.where(greenery, height_term * weights.canopy_height_factor, height_term)
    cost = 1.0 + height_term
    cost += weights.building_weight * layers["building"]
    open_ground = greenery & (low < weights.open_ground_height_m)
    cost = np.where(layers["cheap_surface"], cost * weights.cheap_surface_factor, cost)
    cost = np.where(open_ground, cost * weights.open_ground_factor, cost)
    # Crossing a bounded water body risks a stepped water surface, because the
    # generator fits each body's level from whatever falls inside one crop
    # (build_map_fields.py:141-158). Running along a shoreline is unaffected.
    cost = np.where(layers["water"], cost + weights.water_crossing_penalty, cost)
    cost = np.where(observed, cost, weights.nodata_cost)
    cost = np.where(keep_out, np.inf, cost).astype(np.float32)

    surface = CostSurface(
        grid=grid, cost=cost, keep_out=keep_out,
        height_m=height_positive.astype(np.float32),
        ground_m=ground.astype(np.float32),
        ground_min_m=ground_min.astype(np.float32),
        layers=layers, weights=weights,
        sources={"datasets": sources, "resolution_m": resolution_m,
                 "observed_fraction": float(observed.mean()),
                 "keep_out_fraction": float(keep_out.mean()), "key": key},
        keep_out_features=named,
    )
    _store_surface(planner_cache_dir, key, surface)
    return surface


def _narrow_width(geometry) -> float:
    """Short side of a feature's oriented envelope: how far across it is."""
    with warnings.catch_warnings():
        # GEOS divides by a zero slope for an axis-aligned rectangle.
        warnings.filterwarnings("ignore", "divide by zero", RuntimeWarning)
        warnings.filterwarnings("ignore", "invalid value", RuntimeWarning)
        rectangle = geometry.minimum_rotated_rectangle
    corners = shapely.get_coordinates(rectangle.exterior)
    if len(corners) < 3:
        return 0.0
    sides = np.hypot(*np.diff(corners, axis=0).T)
    return float(sides.min()) if sides.size else 0.0


def _names(frame: gpd.GeoDataFrame, column: str, fallback: str) -> list[str]:
    """Feature names where the publisher supplied one, else a generic label."""
    if column not in frame:
        return [fallback] * len(frame)
    values = frame[column].astype("string")
    values = values.where(values.notna() & (values.str.strip() != "") & (values != "unset"))
    return values.fillna(fallback).tolist()


def _surface_path(planner_cache_dir: Path | None, key: str) -> Path | None:
    if planner_cache_dir is None:
        return None
    return planner_cache_dir / f"cost_{key}.npz"


def _load_surface(planner_cache_dir, key, grid, weights, sources) -> CostSurface | None:
    path = _surface_path(planner_cache_dir, key)
    features_path = path.with_name(f"cost_{key}_keepouts.parquet") if path else None
    if path is None or not path.is_file() or not features_path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            names = [name for name in data.files if name.startswith("layer_")]
            layers = {name[len("layer_"):]: data[name] for name in names}
            meta = json.loads(path.with_suffix(".json").read_text())
            return CostSurface(
                grid=grid, cost=data["cost"], keep_out=data["keep_out"],
                height_m=data["height_m"], ground_m=data["ground_m"],
                ground_min_m=data["ground_min_m"], layers=layers,
                weights=weights, sources=meta,
                keep_out_features=gpd.read_parquet(features_path),
            )
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def _store_surface(planner_cache_dir, key, surface: CostSurface) -> None:
    path = _surface_path(planner_cache_dir, key)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp.npz")
    np.savez_compressed(
        temporary,
        cost=surface.cost, keep_out=surface.keep_out, height_m=surface.height_m,
        ground_m=surface.ground_m, ground_min_m=surface.ground_min_m,
        **{f"layer_{name}": value for name, value in surface.layers.items()},
    )
    path.with_suffix(".json").write_text(json.dumps(surface.sources, indent=2, sort_keys=True))
    if surface.keep_out_features is not None:
        surface.keep_out_features.to_parquet(
            path.with_name(f"cost_{path.stem[len('cost_'):]}_keepouts.parquet"),
            compression="zstd", write_covering_bbox=True, index=False,
        )
    # Publish the array bundle last so a resumed run never loads a surface
    # whose named keep-outs are missing.
    temporary.replace(path)
