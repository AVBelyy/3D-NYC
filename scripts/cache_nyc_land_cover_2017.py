#!/usr/bin/env python3
"""Convert the NYC land-cover IMG into a resumable native-resolution GeoTIFF."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window

from cache_common import (
    CACHE_FORMAT_VERSION,
    DATA,
    DEFAULT_CACHE_ROOT,
    Progress,
    atomic_json,
    file_signature,
    finish_manifest,
    output_record,
    parse_bounds,
    reusable_manifest,
    start_manifest,
)


PIPELINE_VERSION = 1
ARCHIVE_MEMBER = "Land_Cover/NYC_2017_LiDAR_LandCover.img"


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--source", type=Path, help="Extracted IMG or source ZIP (auto-detected by default)")
    value.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    value.add_argument("--bounds", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
    value.add_argument("--band-rows", type=int, default=256, help="Rows copied per IO band")
    value.add_argument(
        "--checkpoint-bands", type=int, default=4,
        help="Close/flush the GeoTIFF after this many bands before advancing the resume checkpoint",
    )
    value.add_argument("--compression", choices=("zstd", "deflate", "lzw"), default="zstd")
    value.add_argument("--compression-level", type=int, default=9)
    value.add_argument("--hash-source", action="store_true")
    value.add_argument("--skip-output-hashes", action="store_true")
    value.add_argument("--force", action="store_true")
    return value


def resolve_source(value: Path | None) -> tuple[Path, str]:
    if value is not None:
        path = value.resolve()
    else:
        # The small extracted .img header is not usable without its ~98 GB
        # .ige companion.  The ZIP is the canonical compact source and GDAL can
        # stream both members from /vsizip without extracting them first.
        path = (DATA / "raw/nyc_land_cover_2017/Land_Cover.zip").resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    dataset = f"/vsizip/{path}/{ARCHIVE_MEMBER}" if path.suffix.lower() == ".zip" else str(path)
    return path, dataset


def crop_window(source: rasterio.DatasetReader, bounds: tuple[float, float, float, float] | None) -> Window:
    if bounds is None:
        return Window(0, 0, source.width, source.height)
    window = source.window(*bounds).round_offsets().round_lengths()
    return window.intersection(Window(0, 0, source.width, source.height))


def main() -> None:
    args = parser().parse_args()
    if args.band_rows <= 0 or args.checkpoint_bands <= 0:
        raise ValueError("--band-rows and --checkpoint-bands must be positive")
    source_file, dataset = resolve_source(args.source)
    bounds = parse_bounds(args.bounds)
    component_dir = args.cache_root.resolve() / "nyc_land_cover_2017"
    component_dir.mkdir(parents=True, exist_ok=True)
    sources = {"landcover": file_signature(source_file, with_hash=args.hash_source)}
    configuration = {
        "pipeline_version": PIPELINE_VERSION,
        "cache_format_version": CACHE_FORMAT_VERSION,
        "source_member": ARCHIVE_MEMBER if source_file.suffix.lower() == ".zip" else None,
        "coverage_bounds": list(bounds) if bounds else None,
        "band_rows": args.band_rows,
        "checkpoint_bands": args.checkpoint_bands,
        "compression": args.compression,
        "compression_level": args.compression_level,
        "resampling": None,
        "vertical_or_class_quantization": None,
    }
    if not args.force:
        existing = reusable_manifest(component_dir, configuration, sources)
        if existing:
            print(
                f"Land-cover cache is current: {component_dir} "
                f"({existing['output_bytes'] / 1024**3:.2f} GiB)"
            )
            return

    final_path = component_dir / "landcover_native.tif"
    temporary = component_dir / "landcover_native.in_progress.tif"
    progress_path = component_dir / "progress.json"
    if args.force:
        temporary.unlink(missing_ok=True)
        progress_path.unlink(missing_ok=True)
    manifest = start_manifest(component_dir, "nyc_land_cover_2017", configuration, sources)

    with rasterio.Env(GDAL_CACHEMAX=512 * 1024**2), rasterio.open(dataset) as source:
        window = crop_window(source, bounds)
        width, height = int(window.width), int(window.height)
        transform = source.window_transform(window)
        profile = source.profile.copy()
        profile.update(
            driver="GTiff", width=width, height=height, count=1,
            dtype="uint8", transform=transform, crs=source.crs,
            tiled=True, blockxsize=512, blockysize=512,
            compress=args.compression, predictor=2, BIGTIFF="YES", SPARSE_OK="TRUE",
            nodata=0,
        )
        if args.compression == "zstd":
            profile["zstd_level"] = args.compression_level
        elif args.compression == "deflate":
            profile["zlevel"] = args.compression_level

        next_row = 0
        counts: Counter[int] = Counter()
        if temporary.exists() and progress_path.exists() and not args.force:
            checkpoint = json.loads(progress_path.read_text())
            if checkpoint.get("configuration") == configuration and checkpoint.get("sources") == sources:
                next_row = int(checkpoint.get("next_row", 0))
                counts.update({int(key): int(value) for key, value in checkpoint.get("class_counts", {}).items()})
                mode = "r+"
            else:
                temporary.unlink()
                progress_path.unlink()
                mode = "w"
        else:
            if temporary.exists() or progress_path.exists():
                temporary.unlink(missing_ok=True)
                progress_path.unlink(missing_ok=True)
            mode = "w"

        started = time.monotonic()
        progress = Progress("land cover", height, unit="rows")
        progress.update(next_row, detail="resuming" if next_row else "")
        row = next_row
        while row < height:
            checkpoint_end = min(
                row + args.band_rows * args.checkpoint_bands, height
            )
            with rasterio.open(temporary, mode, **(profile if mode == "w" else {})) as target:
                while row < checkpoint_end:
                    rows = min(args.band_rows, checkpoint_end - row)
                    source_window = Window(window.col_off, window.row_off + row, width, rows)
                    array = source.read(1, window=source_window, out_dtype="uint8")
                    target.write(array, 1, window=Window(0, row, width, rows))
                    values, frequencies = np.unique(array, return_counts=True)
                    counts.update({int(value): int(count) for value, count in zip(values, frequencies)})
                    row += rows
                    progress.update(row, detail=f"classes={len(counts)}")
            # Closing the GDAL dataset flushes all compressed blocks. Publish
            # the resume position only after that durable boundary.
            mode = "r+"
            atomic_json(progress_path, {
                "configuration": configuration,
                "sources": sources,
                "next_row": row,
                "class_counts": dict(sorted(counts.items())),
            })
        progress.close(detail=f"classes={len(counts)}")
    os.replace(temporary, final_path)
    progress_path.unlink(missing_ok=True)
    outputs = [output_record(final_path, component_dir, with_hash=not args.skip_output_hashes)]
    completed = finish_manifest(
        component_dir,
        manifest,
        outputs,
        width=width,
        height=height,
        transform=list(transform)[:6],
        crs=str(profile["crs"]),
        dtype="uint8",
        nodata=0,
        class_counts=dict(sorted(counts.items())),
        elapsed_seconds=time.monotonic() - started,
        production_ready=bounds is None,
    )
    print(
        f"Completed land-cover cache: {final_path}\n"
        f"  raster: {width:,} x {height:,} native pixels\n"
        f"  size: {completed['output_bytes'] / 1024**3:.2f} GiB"
    )


if __name__ == "__main__":
    main()
