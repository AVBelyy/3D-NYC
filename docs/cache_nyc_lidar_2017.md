# NYC 2017 LiDAR cache

`scripts/cache_nyc_lidar_2017.py` converts NYC 2017 LAZ point clouds into the
two 0.5 m raster measurements used by the generator: mean class-2 ground
elevation and maximum class 1/2/17/25 elevation. Its default destination is
`data/cache/nyc_lidar_2017/`.

For a bounded area, first download the directly intersecting source tiles
(bounds are EPSG:2263 US survey feet), then build the cache. Keep
`--download-missing` on the cache command: fixed raster chunks and the 10 m
ground-fill halo can require neighboring LAZ files just outside the requested
bounds.

```bash
.venv/bin/python scripts/download_nyc_lidar_2017.py \
  --bounds XMIN YMIN XMAX YMAX

.venv/bin/python scripts/cache_nyc_lidar_2017.py \
  --bounds XMIN YMIN XMAX YMAX \
  --coverage all \
  --download-missing
```

This follows the normal dataset contract:

```text
scripts/download_nyc_lidar_2017.py -> data/raw/nyc_lidar_2017/
scripts/cache_nyc_lidar_2017.py    -> data/cache/nyc_lidar_2017/
```

The download command also creates `data/raw/nyc_lidar_2017/index.geojson`, which
the cache builder requires. For a large build with limited disk space, let the
cache builder fetch and remove each raw LAZ after its last use:

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
estimated source volume. `--coverage available` uses only LAZ files already on
disk and is useful for development, but it does not guarantee complete source
coverage at a bounded cache edge. Use `--overwrite` only when deliberately
rebuilding.

The cache layout is:

```text
data/cache/nyc_lidar_2017/
  manifest.json
  catalog.geojson
  tiles/
    x+NNNNN_y+NNNNN_ground_m.tif
    x+NNNNN_y+NNNNN_upper_surface_m.tif
    x+NNNNN_y+NNNNN.json
```

To use a nondefault location, pass `--output-dir` to the cache builder and
`--lidar-cache-dir` to `generate_3mf.py`.

The rasters preserve the generator's canonical measurements, not the original
point returns: mean class-2 ground elevation and maximum class 1/2/17/25
elevation in 0.5 m cells. A raster-cache run and a raw-LAZ run over a rotated
job grid are therefore not expected to be bit-identical.
