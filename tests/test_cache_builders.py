import argparse
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import requests
from shapely.geometry import box


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cache_common import (  # noqa: E402
    CRS,
    TiledGeoParquetWriter,
    finish_manifest,
    read_tiled_geoparquet,
    reusable_manifest,
    start_manifest,
)
from cache_nyc_3d_buildings_2014 import parse_member  # noqa: E402
from _cache_vector_datasets import (  # noqa: E402
    build_buildings_api,
    infer_legacy_api_progress,
)


class TiledGeoParquetTests(unittest.TestCase):
    def test_empty_writer_publishes_an_empty_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = TiledGeoParquetWriter(root, tile_span_ft=10)
            records, outputs = writer.finalize(
                deduplicate_by=["feature_id"], hash_outputs=False
            )
            self.assertEqual(records, [])
            self.assertTrue((root / "catalog.geojson").is_file())
            self.assertEqual(len(gpd.read_file(root / "catalog.geojson")), 0)
            self.assertEqual(outputs[0]["path"], "catalog.geojson")
            self.assertFalse((root / "staging").exists())

    def test_boundary_feature_is_available_without_duplicate_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = gpd.GeoDataFrame(
                {
                    "feature_id": ["crossing", "right"],
                    "source_order": [1, 2],
                    "geometry": [box(8, 1, 12, 3), box(15, 1, 16, 3)],
                },
                crs=CRS,
            )
            writer = TiledGeoParquetWriter(root, tile_span_ft=10)
            self.assertEqual(writer.add(frame), 3)
            records, _ = writer.finalize(
                deduplicate_by=["feature_id"], source_order=["source_order"], hash_outputs=False
            )
            self.assertEqual(len(records), 2)

            result = read_tiled_geoparquet(
                root, (0, 0, 20, 5),
                deduplicate_by=["feature_id"], source_order=["source_order"],
            )
            self.assertEqual(result.feature_id.tolist(), ["crossing", "right"])

            boundary = read_tiled_geoparquet(
                root, (10, 0, 11, 5),
                deduplicate_by=["feature_id"], source_order=["source_order"],
            )
            self.assertEqual(boundary.feature_id.tolist(), ["crossing"])


class ManifestTests(unittest.TestCase):
    def test_manifest_is_only_reusable_after_outputs_are_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configuration = {"version": 1}
            sources = {"source": {"bytes": 4}}
            in_progress = start_manifest(root, "test", configuration, sources)
            self.assertIsNone(reusable_manifest(root, configuration, sources))
            output = root / "value.bin"
            output.write_bytes(b"data")
            finish_manifest(
                root, in_progress,
                [{"path": "value.bin", "bytes": 4}], rows=1,
            )
            self.assertIsNotNone(reusable_manifest(root, configuration, sources))
            output.write_bytes(b"changed")
            self.assertIsNone(reusable_manifest(root, configuration, sources))


class CityGmlTests(unittest.TestCase):
    def test_member_cache_keeps_roofs_and_derives_building_record(self):
        document = b"""<?xml version='1.0' encoding='UTF-8'?>
        <core:CityModel xmlns:core='http://www.opengis.net/citygml/2.0'
          xmlns:bldg='http://www.opengis.net/citygml/building/2.0'
          xmlns:gen='http://www.opengis.net/citygml/generics/2.0'
          xmlns:gml='http://www.opengis.net/gml'>
          <bldg:Building gml:id='building-1'>
            <gen:stringAttribute name='DOITT_ID'><gen:value>42</gen:value></gen:stringAttribute>
            <gen:stringAttribute name='BIN'><gen:value>1000042</gen:value></gen:stringAttribute>
            <bldg:boundedBy><bldg:GroundSurface><gml:Polygon>
              <gml:exterior><gml:LinearRing><gml:posList>
                0 0 0 10 0 0 10 10 0 0 10 0 0 0 0
              </gml:posList></gml:LinearRing></gml:exterior>
            </gml:Polygon></bldg:GroundSurface></bldg:boundedBy>
            <bldg:boundedBy><bldg:RoofSurface><gml:Polygon>
              <gml:exterior><gml:LinearRing><gml:posList>
                0 0 10 10 0 10 10 10 10 0 10 10 0 0 10
              </gml:posList></gml:LinearRing></gml:exterior>
            </gml:Polygon></bldg:RoofSurface></bldg:boundedBy>
            <bldg:boundedBy><bldg:WallSurface><gml:Polygon>
              <gml:exterior><gml:LinearRing><gml:posList>
                0 0 0 10 0 0 10 0 10 0 0 10 0 0 0
              </gml:posList></gml:LinearRing></gml:exterior>
            </gml:Polygon></bldg:WallSurface></bldg:boundedBy>
          </bldg:Building>
        </core:CityModel>"""
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "sample.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("sample.gml", document)
            with zipfile.ZipFile(archive_path) as archive:
                member = archive.getinfo("sample.gml")
                buildings, roofs, scanned = parse_member(
                    archive, member, 0, (-1, -1, 20, 20)
                )
        self.assertEqual(scanned, 1)
        self.assertEqual(len(buildings), 1)
        self.assertEqual(len(roofs), 1)
        self.assertEqual(buildings.iloc[0].doitt_id, "42")
        self.assertEqual(buildings.iloc[0].roof_count, 1)
        self.assertEqual(buildings.iloc[0].z_min_ft, 0)
        self.assertEqual(buildings.iloc[0].z_max_ft, 10)
        self.assertEqual(roofs.iloc[0].kind, "roof")
        self.assertTrue(roofs.iloc[0].geometry.has_z)


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _FakeSession:
    def __init__(self, pages, *, fail_offset=None):
        self.pages = pages
        self.fail_offset = fail_offset
        self.requested_offsets = []

    def get(self, _url, *, params, timeout):
        if params.get("returnCountOnly") == "true":
            return _FakeResponse({"count": 5})
        offset = int(params["resultOffset"])
        self.requested_offsets.append(offset)
        if offset == self.fail_offset:
            raise requests.HTTPError("503 Server Error")
        return _FakeResponse({"features": self.pages[offset]})


def _building_feature(object_id, x):
    return {
        "type": "Feature",
        "properties": {"OBJECTID": object_id, "DOITT_ID": object_id, "HEIGHT_ROOF": 20},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[x, 1], [x + 1, 1], [x + 1, 2], [x, 2], [x, 1]]],
        },
    }


class VectorResumeTests(unittest.TestCase):
    def _args(self):
        return argparse.Namespace(
            api_page_size=2,
            api_retries=0,
            api_backoff_seconds=0,
            tile_span_ft=10,
            skip_output_hashes=True,
            force=False,
            limit_building_pages=None,
        )

    def test_api_resumes_at_first_uncommitted_page(self):
        pages = {
            0: [_building_feature(1, 1), _building_feature(2, 3)],
            2: [_building_feature(3, 11), _building_feature(4, 13)],
            4: [_building_feature(5, 15)],
        }
        configuration = {
            "version": 1, "source": "api", "coverage_bounds": [0, 0, 20, 20],
            "tile_span_ft": 10, "api_page_size": 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "buildings"
            first = _FakeSession(pages, fail_offset=2)
            with patch("_cache_vector_datasets.retrying_session", return_value=first):
                with self.assertRaises(requests.HTTPError):
                    build_buildings_api(destination, box(0, 0, 20, 20), self._args(), configuration)
            checkpoint = json.loads((destination / "progress.json").read_text())
            self.assertEqual(checkpoint["next_offset"], 2)
            self.assertEqual(first.requested_offsets, [0, 2])

            second = _FakeSession(pages)
            with patch("_cache_vector_datasets.retrying_session", return_value=second):
                outputs, count = build_buildings_api(
                    destination, box(0, 0, 20, 20), self._args(), configuration
                )
            self.assertEqual(second.requested_offsets, [2, 4])
            self.assertEqual(count, 5)
            self.assertTrue((destination / "manifest.json").is_file())
            self.assertFalse((destination / "progress.json").exists())
            self.assertFalse((destination / "staging").exists())
            self.assertTrue(any(record["path"] == "catalog.geojson" for record in outputs))

    def test_legacy_recovery_discards_only_last_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = TiledGeoParquetWriter(root, tile_span_ft=10)
            for index in range(3):
                writer.add(gpd.GeoDataFrame(
                    {"feature_id": [index], "geometry": [box(1, 1, 2, 2)]}, crs=CRS
                ))
            recovered = infer_legacy_api_progress(root / "staging", 2000)
            self.assertEqual(recovered["next_offset"], 4000)
            self.assertEqual(recovered["next_batch"], 2)
            remaining = sorted(path.name for path in (root / "staging").glob("*/*.parquet"))
            self.assertEqual(remaining, ["part-00000000.parquet", "part-00000001.parquet"])


if __name__ == "__main__":
    unittest.main()
