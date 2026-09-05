# Source dataset cache runbook

The generator consumes one directory per source dataset under `data/cache/`.
The same dataset token `X` is used by its two maintenance scripts and paths:

```text
scripts/download_X.py -> data/raw/X/
scripts/cache_X.py    -> data/cache/X/
```

`data/raw/` is only a staging area. Delete `data/raw/X` after `cache_X.py`
finishes and publishes `data/cache/X/manifest.json`. Both raw and cached data
are Git-ignored; they can be restored from the scripts below.

| X | Download | Build cache |
| --- | --- | --- |
| `nyc_3d_buildings_2014` | `download_nyc_3d_buildings_2014.py` | `cache_nyc_3d_buildings_2014.py` |
| `nyc_building_footprints` | `download_nyc_building_footprints.py` | `cache_nyc_building_footprints.py` |
| `nyc_planimetrics_2022` | `download_nyc_planimetrics_2022.py` | `cache_nyc_planimetrics_2022.py` |
| `nyc_parks_trails` | `download_nyc_parks_trails.py` | `cache_nyc_parks_trails.py` |
| `nyc_parks_structures` | `download_nyc_parks_structures.py` | `cache_nyc_parks_structures.py` |
| `mta_subway_entrances_2024` | `download_mta_subway_entrances_2024.py` | `cache_mta_subway_entrances_2024.py` |
| `nyc_land_cover_2017` | `download_nyc_land_cover_2017.py` | `cache_nyc_land_cover_2017.py` |
| `new_york_osm` | `download_new_york_osm.py` | `cache_new_york_osm.py` |
| `nyc_lidar_2017` | `download_nyc_lidar_2017.py` | `cache_nyc_lidar_2017.py` |

Run a pair from the repository root, for example:

```bash
.venv/bin/python scripts/download_new_york_osm.py
.venv/bin/python scripts/cache_new_york_osm.py
rm -rf data/raw/new_york_osm
```

The vector cache builders use the NYC 2017 LiDAR catalog to define citywide
coverage, so build `nyc_lidar_2017` first. Planimetrics automatically extracts
its downloaded FileGDB archive. The building-footprint cache streams the
official feature service by default; pass `--building-source csv` to consume
the file produced by its download script instead.

LiDAR is large and region-selective. Follow the dedicated
[LiDAR cache guide](cache_nyc_lidar_2017.md).

Every complete cache has a manifest with its source identity, configuration,
output inventory, and production-readiness marker. `generate_3mf.py` validates
these manifests before use. To store caches elsewhere, pass the same
`--cache-root` to the non-LiDAR cache builders and `--cache-dir` to the
generator. LiDAR accepts its dataset directory through `--output-dir` while
the generator accepts it through `--lidar-cache-dir`.

`data/cache/nyc_geosearch` is different: the generator fills it lazily with
NYC Planning GeoSearch responses, so it has no download/cache script pair.
