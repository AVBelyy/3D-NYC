#!/usr/bin/env python3
"""Shared implementation for the dataset-specific download commands."""

import argparse
from pathlib import Path

from _datasets import DATASETS
from download_data import ROOT, download


def download_dataset(name: str, *, raw_dir: Path | None = None):
    url, relative = DATASETS[name]
    return download(url, relative, raw_dir=raw_dir)


def main_for(name: str) -> None:
    parser = argparse.ArgumentParser(
        description=f"Download {name} into the matching data/raw/{name} directory."
    )
    parser.add_argument(
        "--raw-dir", type=Path, default=ROOT / "data/raw",
        help="Raw-data root (the dataset name is appended)",
    )
    args = parser.parse_args()
    download_dataset(name, raw_dir=args.raw_dir.resolve())
