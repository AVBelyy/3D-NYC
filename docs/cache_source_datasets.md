# Source dataset cache runbook

The generator consumes one directory per source dataset under `data/cache/`.
The same dataset token `X` is used by its two maintenance scripts and paths:

```text
scripts/download_X.py -> data/raw/X/
scripts/cache_X.py    -> data/cache/X/
```

`data/raw/` is only a staging area. After `cache_X.py` finishes and publishes a
complete `data/cache/X/manifest.json`, its raw directory is no longer needed by
normal cached generation. Both raw and cached data are Git-ignored. They can be
rebuilt by the scripts below, although live sources can change between builds.

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
```

The vector cache builders use the NYC 2017 LiDAR catalog to define citywide
coverage, so build `nyc_lidar_2017` first. Planimetrics automatically extracts
its downloaded FileGDB archive. The building-footprint cache is the exception
to the normal pair: it streams the official feature service by default, so its
download command is needed only when building with `--building-source csv`.

LiDAR is large and region-selective. Follow the dedicated
[LiDAR cache guide](cache_nyc_lidar_2017.md).

Non-LiDAR cache manifests record source identity, configuration, output
inventory, and whether the cache is production-ready. Bounded non-LiDAR caches
are deliberately not production-ready and are rejected by the generator. The
LiDAR cache uses a separate manifest/catalog contract and may cover only the
area being generated. `generate_3mf.py` validates the relevant contract before
use.

To store caches elsewhere, pass the same `--cache-root` to every non-LiDAR
cache builder and `--cache-dir` to the generator. Pass `--output-dir` to the
LiDAR cache builder and the resulting dataset directory as
`--lidar-cache-dir` to the generator. These options name different levels:
`--cache-root`/`--cache-dir` is the parent of dataset directories, while the
LiDAR options point at `nyc_lidar_2017` itself.

For a cache-only offline generation run, cache all nine datasets in the table.

`data/cache/nyc_geosearch` is different: the generator fills it lazily with
NYC Planning GeoSearch responses only for address-selected building colors, so
it has no download/cache script pair. An offline address selection succeeds
only if its exact normalized-address response is already cached; coordinate or
identifier selections do not use GeoSearch.
