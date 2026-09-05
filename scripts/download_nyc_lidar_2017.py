#!/usr/bin/env python3
"""Download NYC 2017 LiDAR tiles for an EPSG:2263 bounding box."""

import argparse
import json
from pathlib import Path

import geopandas as gpd
import requests

from cache_nyc_lidar_2017 import DEFAULT_INDEX, DEFAULT_LAZ
from download_data import download

SERVICE = (
    "https://services6.arcgis.com/yG5s3afENB5iO9fj/arcgis/rest/services/"
    "NYC_2017_LiDAR_TopoBathymetric_LAS_Tile_Grid_Index/FeatureServer"
)


def download_index(path: Path) -> gpd.GeoDataFrame:
    if path.is_file():
        return gpd.read_file(path)
    metadata = requests.get(SERVICE, params={"f": "json"}, timeout=60).json()
    layer = SERVICE + "/" + str(metadata["layers"][0]["id"])
    features, offset = [], 0
    while True:
        response = requests.get(layer + "/query", params={
            "where": "1=1", "outFields": "*", "f": "geojson", "outSR": 4326,
            "resultOffset": offset, "resultRecordCount": 1000, "orderByFields": "OBJECTID",
        }, timeout=90)
        response.raise_for_status()
        batch = response.json().get("features", [])
        features.extend(batch)
        offset += len(batch)
        if len(batch) < 1000:
            break
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return gpd.read_file(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bounds", type=float, nargs=4, required=True, metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
    parser.add_argument("--tile-index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--tile-dir", type=Path, default=DEFAULT_LAZ)
    args = parser.parse_args()
    index = download_index(args.tile_index.resolve()).to_crs(2263)
    xmin, ymin, xmax, ymax = args.bounds
    selected = index.cx[xmin:xmax, ymin:ymax]
    if selected.empty:
        raise SystemExit("No NYC 2017 LiDAR tiles intersect those EPSG:2263 bounds")
    for number, row in enumerate(selected.itertuples(), 1):
        tile_id = str(row.LAS_ID)
        print(f"Tile {number}/{len(selected)}: {tile_id}", flush=True)
        download(str(row.azure_url), f"tiles/{tile_id}.laz", raw_dir=args.tile_dir.parent)


if __name__ == "__main__":
    main()
