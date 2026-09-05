#!/usr/bin/env python3
"""Build data/cache/nyc_planimetrics_2022."""

import zipfile
from pathlib import Path

from _cache_vector_datasets import DATA, main_for


def extract_download() -> None:
    raw = DATA / "raw/nyc_planimetrics_2022"
    geodatabase = raw / "Planimetric_2022.gdb"
    archive = raw / "Planimetric_2022.gdb.zip"
    if geodatabase.is_dir() or not archive.is_file():
        return
    with zipfile.ZipFile(archive) as source:
        source.extractall(raw)


if __name__ == "__main__":
    extract_download()
    main_for("nyc_planimetrics_2022")
