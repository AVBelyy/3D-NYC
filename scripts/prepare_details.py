"""Subset geometry-contributing detail datasets and derive road symbols."""

import hashlib
import json

import geopandas as gpd
import pandas as pd

from map_common import ANALYSIS, AOI, CACHE_DIR, CFG, K, OUT, PROCESSED, RAW, write_json
from road_symbols import build_road_symbols


SOURCES = {
    "parks_structures": RAW / "nyc_parks_structures/structures.geojson",
    "mta_subway_entrances": RAW / "mta_subway_entrances_2024/subway_entrances.csv",
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    processed = PROCESSED
    cache_paths = {
        "parks_structures": CACHE_DIR / "nyc_parks_structures",
        "mta_subway_entrances": CACHE_DIR / "mta_subway_entrances_2024",
    }
    source_inventory = {}
    source_kinds = {}
    loaded = {}
    for name, cache_path in cache_paths.items():
        manifest_path = cache_path / "manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("status") != "complete" or manifest.get("production_ready") is False:
                raise RuntimeError(f"Cache is not production-ready: {manifest_path}")
            source_inventory[name] = manifest.get("sources", {})
            source_kinds[name] = "cache"
            loaded[name] = cache_path
        else:
            path = SOURCES[name]
            assert path.exists() and path.stat().st_size > 0, path
            source_inventory[name] = {"bytes": path.stat().st_size, "sha256": digest(path)}
            source_kinds[name] = "raw"
            loaded[name] = path

    if source_kinds["parks_structures"] == "cache":
        try:
            structures = gpd.read_parquet(
                loaded["parks_structures"] / "data.parquet", bbox=AOI.bounds
            )
        except ValueError:
            structures = gpd.read_parquet(loaded["parks_structures"] / "data.parquet")
        structures = structures[structures.intersects(AOI)].copy()
    else:
        structures = gpd.read_file(loaded["parks_structures"]).to_crs(2263)
        structures = structures[structures.intersects(AOI)].copy()

    if source_kinds["mta_subway_entrances"] == "cache":
        try:
            entrances = gpd.read_parquet(
                loaded["mta_subway_entrances"] / "data.parquet", bbox=AOI.bounds
            )
        except ValueError:
            entrances = gpd.read_parquet(loaded["mta_subway_entrances"] / "data.parquet")
        entrances = entrances[entrances.within(AOI)].copy()
    else:
        entrances = pd.read_csv(loaded["mta_subway_entrances"])
        entrances = gpd.GeoDataFrame(
            entrances,
            geometry=gpd.points_from_xy(
                entrances.entrance_longitude, entrances.entrance_latitude
            ),
            crs=4326,
        ).to_crs(2263)
        entrances = entrances[entrances.within(AOI)].copy()
    structures.to_parquet(processed / "parks_structures.parquet")
    entrances.to_parquet(processed / "mta_subway_entrances.parquet")

    roadbed = gpd.read_parquet(processed / "planimetrics_ROADBED_aoi.parquet")
    roadbed = roadbed[
        roadbed.intersects(AOI) & roadbed.SUB_FEATURE_CODE.isin([350000, 350010, 350030])
    ].copy()
    osm = gpd.read_parquet(OUT / "osm_detail.parquet")
    routes, road_surfaces, road_report = build_road_symbols(
        osm, roadbed, AOI, float(CFG["scale_denominator"]), CFG
    )
    routes.to_parquet(processed / "road_symbol_routes.parquet")
    road_surfaces.to_parquet(processed / "ivory_road_surface.parquet")

    detail_layers = {}
    for name in ["CURB", "CURB_CUT", "MEDIAN", "PAVEMENT_EDGE", "ROADBED", "SIDEWALK", "SIDEWALK_LINE"]:
        frame = gpd.read_parquet(processed / f"planimetrics_{name}_aoi.parquet")
        detail_layers[name] = int(frame.intersects(AOI).sum())
    report = {
        "aoi_epsg2263_bounds_ft": list(AOI.bounds),
        "source_inventory": source_inventory,
        "source": source_kinds,
        "region": {
            "parks_structures": len(structures),
            "mta_entrances": len(entrances),
            "ivory_road_surface_polygons": len(road_surfaces),
            "ivory_road_surface_area_mm2": float(road_surfaces.area.sum() * K * K),
            "road_symbols": road_report,
        },
        "existing_planimetric_detail_features": detail_layers,
        "cost_usd": 0,
    }
    write_json(ANALYSIS / "details_inventory.json", report)
    write_json(OUT / "detail_inventory.json", report)
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
