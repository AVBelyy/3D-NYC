# Data layout

`data/` contains inputs used by `scripts/generate_3mf.py`. Generated models,
jobs, plans, logs, previews, and intermediate files belong in `output/`.

The dataset name `X` is deliberately identical throughout the download/cache
workflow:

```text
scripts/download_X.py  -> data/raw/X/
scripts/cache_X.py     -> data/cache/X/
```

`data/raw/` is transient and ignored by Git. After `cache_X.py` publishes a
complete cache manifest, the corresponding raw directory may be deleted.
`data/cache/` is also ignored because its contents can be rebuilt by the
matching scripts. Rebuilding a live source such as OpenStreetMap or Building
Footprints at a later date can produce a different snapshot, so retain the
cache and its manifest when exact output reproduction matters. The checked-in
inputs are `bambu/project_settings.json` and the reusable crop polygons in
`polygons/`.

## Cache datasets

| X | Source represented by the cache |
| --- | --- |
| `nyc_lidar_2017` | NYC 2017 LiDAR |
| `nyc_3d_buildings_2014` | NYC 2014 3D Building Model |
| `nyc_building_footprints` | NYC Building Footprints |
| `nyc_planimetrics_2022` | NYC 2022 Planimetric Database |
| `nyc_parks_trails` | NYC Parks Trails |
| `nyc_parks_structures` | NYC Parks Structures |
| `mta_subway_entrances_2024` | MTA Subway Entrances and Exits (2024) |
| `nyc_land_cover_2017` | NYC 2017 Land Cover |
| `new_york_osm` | Geofabrik New York OpenStreetMap extract |
| `nyc_geosearch` | Responses cached lazily from NYC Planning GeoSearch |

`nyc_geosearch` is a runtime request cache, so it has no download/cache script
pair. All other cache directories are created by `scripts/cache_X.py`; their
raw source is fetched by `scripts/download_X.py` (the building-footprint cache
streams the official feature service by default and uses its downloaded CSV
only with `--building-source csv`).

The default generator requires the LiDAR cache. For other datasets it prefers a
complete, production-ready cache and can fall back to raw data during an online
run. A bounded non-LiDAR cache is marked non-production-ready and is rejected by
the generator; bounded LiDAR caches are supported when they cover the requested
area and source padding.
