# NYC 2017 LiDAR cache

`scripts/cache_nyc_lidar_2017.py` converts NYC 2017 LAZ point clouds into the
two 0.5 m raster measurements used by the generator: mean class-2 ground
elevation and maximum class 1/2/17/25 elevation. Its default destination is
`data/cache/nyc_lidar_2017/`.

For a bounded area, first download the intersecting source tiles (bounds are
EPSG:2263 feet), then build the cache:

```bash
.venv/bin/python scripts/download_nyc_lidar_2017.py \
  --bounds XMIN YMIN XMAX YMAX

.venv/bin/python scripts/cache_nyc_lidar_2017.py \
  --bounds XMIN YMIN XMAX YMAX \
  --coverage all
```

This follows the normal dataset contract:

```text
scripts/download_nyc_lidar_2017.py -> data/raw/nyc_lidar_2017/
scripts/cache_nyc_lidar_2017.py    -> data/cache/nyc_lidar_2017/
```

For a large build with limited disk space, let the cache builder fetch and
remove each raw LAZ after its last use:

```bash
.venv/bin/python scripts/cache_nyc_lidar_2017.py \
  --coverage all \
  --download-missing \
  --delete-source-after-last-use \
  --workers 1
```

Without streaming deletion, `data/raw/nyc_lidar_2017` can be removed after a
complete manifest is published. The cache is resumable; rerunning the same
command reuses completed tiles. Use `--dry-run` to inspect tile selection and
estimated source volume, and `--overwrite` only when deliberately rebuilding.

The cache layout is:

```text
data/cache/nyc_lidar_2017/
  manifest.json
  catalog.geojson
  tiles/
    x..._y..._ground_m.tif
    x..._y..._upper_surface_m.tif
    x..._y....json
```

To use a nondefault location, pass `--output-dir` to the cache builder and
`--lidar-cache-dir` to `generate_3mf.py`.
