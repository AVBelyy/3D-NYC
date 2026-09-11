#!/usr/bin/env bash
# Download every raw source dataset into data/raw/.
#
# Wraps the per-dataset scripts/download_X.py commands described in
# docs/cache_source_datasets.md, then scripts/cache_all.sh turns the downloads
# into the caches the generator reads. Downloads are resumable and record a
# manifest, so a rerun skips whatever already arrived complete.
#
# There is no area to choose. Every source here is citywide, and LiDAR, the one
# dataset selected by area, contributes only its tile index: the LAZ tiles are
# large, so cache_all.sh streams exactly the ones its raster chunks need.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"

# Datasets downloaded by a plain scripts/download_X.py call, in the order of the
# runbook table. Building footprints is deliberately absent: its cache is built
# by streaming the official feature service and never reads a download.
# LAND_COVER is a placeholder that --land-cover-dataset substitutes below, so
# only the survey being fetched changes and the order stays the runbook's.
DATASETS=(
  nyc_3d_buildings_2014
  nyc_planimetrics_2022
  nyc_parks_trails
  nyc_parks_structures
  mta_subway_entrances_2024
  LAND_COVER
  new_york_osm
)

usage() {
  cat <<'USAGE'
Usage: scripts/download_all.sh [options]

Download every raw source dataset into data/raw/, then run
scripts/cache_all.sh to build the caches scripts/generate_3mf.py reads.

Options:
  --land-cover-dataset NAME
        Which land-cover survey to fetch: nyc_land_cover_2021 (default, a
        1.6 GB GeoTIFF) or nyc_land_cover_2017 (a much larger archive). Pass
        the same name to scripts/cache_all.sh.
  -h, --help
        Show this help.

Environment:
  PYTHON  Interpreter used for the dataset scripts (default .venv/bin/python).

Two datasets are deliberately left out, both covered by
docs/cache_source_datasets.md: the building-footprints CSV, because that cache
is built by streaming the official feature service, and the LiDAR tiles,
because cache_all.sh fetches those as it builds. Only the LiDAR tile index is
taken here.
USAGE
}

die() {
  printf 'download_all.sh: %s\n' "$1" >&2
  exit 1
}

run() {
  printf '\n==> %s\n' "$*" >&2
  "$@"
}

land_cover_dataset=nyc_land_cover_2021

while [[ $# -gt 0 ]]; do
  case "$1" in
    --land-cover-dataset)
      [[ $# -ge 2 ]] || die '--land-cover-dataset needs a value'
      case $2 in
        nyc_land_cover_2021|nyc_land_cover_2017) land_cover_dataset=$2 ;;
        *) die "unknown --land-cover-dataset $2" ;;
      esac
      shift 2
      continue
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

[[ -x "$PYTHON" ]] ||
  die "no interpreter at $PYTHON; create .venv as README.md describes or set PYTHON"

cd "$ROOT"

DATASETS=("${DATASETS[@]/LAND_COVER/$land_cover_dataset}")

for dataset in "${DATASETS[@]}"; do
  run "$PYTHON" "scripts/download_$dataset.py"
done

# The 2021 rasters are the default LiDAR source and are fetched whole: about
# 39 GB, ten files, no per-area selection to make.  The 2017 collection stays
# available; only its tile index is fetched here, because its LAZ are selected
# by area and streamed by the cache builder.
run "$PYTHON" scripts/download_nyc_lidar_2021.py
run "$PYTHON" scripts/download_nyc_lidar_2017.py --index-only

printf '\nDownloaded raw sources under data/raw/. Next: scripts/cache_all.sh\n' >&2
