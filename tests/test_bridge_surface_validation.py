"""Crossing-field validation must judge the routes the map actually draws.

The surface symbolizer refuses to draw categorically excluded ways such as
driveways and parking aisles, so the field above one is ordinary terrain
rather than a severed route.  These tests build a minimal work directory --
one tunnel, one route crossing it -- and drive the real script, so the
selection rule cannot drift from ``build_map_fields.py``.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from road_symbols import drawn_route_classification  # noqa: E402

# One model millimetre per source foot keeps the fixture readable.
SCALE = 304.8006096012192
SIZE_MM = 10.0
STEP_MM = 0.5
CELLS = int(SIZE_MM / STEP_MM)
TUNNEL_ID = 900001
ROUTE_ID = 900002
BRIDGE_ID = 900003


class DrawnRouteClassificationTests(unittest.TestCase):
    class Route:
        def __init__(self, classification):
            self.classification = classification

    def test_the_symbolizer_decision_wins_when_it_recorded_one(self):
        for classification in ("carriageway", "trail", "other"):
            self.assertEqual(
                drawn_route_classification(self.Route(classification), "service"),
                classification)

    def test_a_way_the_symbolizer_never_saw_falls_back_to_its_class(self):
        self.assertEqual(drawn_route_classification(None, "footway"), "trail")
        self.assertEqual(drawn_route_classification(None, "STEPS"), "trail")
        self.assertEqual(drawn_route_classification(None, "service"), "other")
        self.assertEqual(drawn_route_classification(None, None), "other")


class CrossingFieldValidationTests(unittest.TestCase):
    """Run the real validator over a synthetic tunnel and one crossing route."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.processed = self.root / "processed"
        self.work.mkdir(parents=True)
        self.processed.mkdir(parents=True)
        (self.root / "config.json").write_text(json.dumps({
            "size_mm": [SIZE_MM, SIZE_MM], "grid_step_mm": STEP_MM,
            "scale_denominator": SCALE, "center_wgs84": [-73.97, 40.78],
            "frame_epsg2263": {"origin_ft": [0.0, 0.0],
                               "x_axis": [1.0, 0.0], "y_axis": [0.0, 1.0]},
        }))
        # A tunnel running east-west, crossed at its midpoint by one route.
        gpd.GeoDataFrame(
            {"osm_id": [TUNNEL_ID], "width_mm": [0.625], "name": ["Test Tunnel"],
             "geometry": [LineString([(2, 5), (8, 5)])]},
            geometry="geometry", crs=2263).to_parquet(self.work / "tunnels.parquet")

    def write_route(self, highway, tags, classification):
        gpd.GeoDataFrame(
            {"osm_type": ["way"], "osm_id": [ROUTE_ID], "tags": [json.dumps(tags)],
             "highway": [highway], "name": [None],
             "geometry": [LineString([(5, 2), (5, 8)])]},
            geometry="geometry", crs=2263).to_parquet(self.work / "osm_detail.parquet")
        gpd.GeoDataFrame(
            {"osm_id": [ROUTE_ID], "highway": [highway],
             "classification": [classification],
             "classification_reason": ["fixture"],
             "ivory_eligible": [classification == "carriageway"],
             "geometry": [LineString([(5, 2), (5, 8)])]},
            geometry="geometry",
            crs=2263).to_parquet(self.processed / "road_symbol_routes.parquet")

    def write_fields(self, road_corridor):
        """Green everywhere, optionally with an ivory corridor along the route."""
        material = np.full((CELLS, CELLS), 1, dtype=np.uint8)
        if road_corridor:
            material[:, int(5 / STEP_MM)] = 0
        np.savez(self.work / "map_fields.npz", material=material,
                 height_mm=np.full((CELLS, CELLS), 2.0, dtype=np.float32))

    def validate(self):
        report = self.root / "report.json"
        environment = {**os.environ,
                       "MAP_CONFIG": str(self.root / "config.json"),
                       "MAP_WORK_DIR": str(self.work),
                       "NYC_PROCESSED_DIR": str(self.processed),
                       "NYC_OUTPUT_DIR": str(self.root / "output")}
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "validate_bridge_surfaces.py"),
             "--fields-only", "--report", str(report)],
            cwd=ROOT, env=environment, capture_output=True, text=True)
        self.assertTrue(report.exists(), completed.stdout + completed.stderr)
        return completed.returncode, json.loads(report.read_text())

    def test_an_undrawn_driveway_over_a_tunnel_is_not_judged(self):
        # The manhattan_2m_C10 failure: a private driveway crossing the
        # Queens-Midtown Tunnel. The map draws nothing there, so the green
        # park field above it is correct and must not fail the build.
        self.write_route("service", {"highway": "service", "service": "driveway",
                                     "access": "private", "surface": "concrete"}, "other")
        self.write_fields(road_corridor=False)
        code, report = self.validate()
        self.assertEqual(code, 0)
        self.assertEqual(report["result"], "passed")
        self.assertEqual(report["checked_segments"], 0)
        self.assertEqual(report["undrawn_route_tunnel_pairs_skipped"], 1)

    def test_a_drawn_carriageway_severed_over_a_tunnel_still_fails(self):
        # The symmetric case: the same geometry the map does draw. A green
        # field above it is a real gap in the road and must be rejected.
        self.write_route("residential", {"highway": "residential"}, "carriageway")
        self.write_fields(road_corridor=False)
        code, report = self.validate()
        self.assertEqual(code, 1)
        self.assertEqual(report["result"], "failed")
        self.assertEqual(report["checked_segments"], 1)
        self.assertEqual(report["undrawn_route_tunnel_pairs_skipped"], 0)
        self.assertTrue(report["bridges"][0]["non_road_field_samples"])
        self.assertEqual(report["bridges"][0]["classification"], "carriageway")

    def test_a_drawn_carriageway_carried_over_a_tunnel_passes(self):
        self.write_route("residential", {"highway": "residential"}, "carriageway")
        self.write_fields(road_corridor=True)
        code, report = self.validate()
        self.assertEqual(code, 0)
        self.assertEqual(report["result"], "passed")
        self.assertEqual(report["checked_segments"], 1)
        self.assertFalse(report["bridges"][0]["non_road_field_samples"])

    def test_a_drawn_trail_over_a_tunnel_is_still_judged(self):
        # Trails are drawn tan, so they carry the same continuity obligation.
        self.write_route("footway", {"highway": "footway"}, "trail")
        self.write_fields(road_corridor=False)
        code, report = self.validate()
        self.assertEqual(code, 1)
        self.assertEqual(report["result"], "failed")
        self.assertEqual(report["bridges"][0]["classification"], "trail")


class TaggedBridgeMaterialTests(unittest.TestCase):
    """A carriageway deck shared with a footway must still print as a road.

    Reproduces the manhattan_2m_A5 Riverside Drive Viaduct: an ivory-eligible
    bridge whose surveyed deck was repainted tan by the sidewalk mapped beside
    it.  Only the tan direction is a defect -- a trail ribbon inside a wider
    roadway legitimately samples the ivory the carriageway owns.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.processed = self.root / "processed"
        self.work.mkdir(parents=True)
        self.processed.mkdir(parents=True)
        (self.root / "config.json").write_text(json.dumps({
            "size_mm": [SIZE_MM, SIZE_MM], "grid_step_mm": STEP_MM,
            "scale_denominator": SCALE, "center_wgs84": [-73.97, 40.78],
            "frame_epsg2263": {"origin_ft": [0.0, 0.0],
                               "x_axis": [1.0, 0.0], "y_axis": [0.0, 1.0]},
        }))

    def write_bridge(self, ivory_eligible):
        gpd.GeoDataFrame(
            {"osm_id": [BRIDGE_ID], "width_mm": [0.625], "name": ["Test Viaduct"],
             "ivory_eligible": [ivory_eligible],
             "geometry": [LineString([(2, 5), (8, 5)])]},
            geometry="geometry", crs=2263).to_parquet(self.work / "bridges.parquet")

    def write_fields(self, deck_material):
        material = np.full((CELLS, CELLS), 1, dtype=np.uint8)
        material[int(5 / STEP_MM), :] = deck_material
        np.savez(self.work / "map_fields.npz", material=material,
                 height_mm=np.full((CELLS, CELLS), 2.0, dtype=np.float32))

    def validate(self):
        report = self.root / "report.json"
        environment = {**os.environ,
                       "MAP_CONFIG": str(self.root / "config.json"),
                       "MAP_WORK_DIR": str(self.work),
                       "NYC_PROCESSED_DIR": str(self.processed),
                       "NYC_OUTPUT_DIR": str(self.root / "output")}
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "validate_bridge_surfaces.py"),
             "--fields-only", "--report", str(report)],
            cwd=ROOT, env=environment, capture_output=True, text=True)
        self.assertTrue(report.exists(), completed.stdout + completed.stderr)
        return completed.returncode, json.loads(report.read_text())

    def test_a_carriageway_deck_repainted_tan_is_rejected(self):
        self.write_bridge(ivory_eligible=True)
        self.write_fields(deck_material=3)
        code, report = self.validate()
        self.assertEqual(code, 1)
        self.assertEqual(report["result"], "failed")
        self.assertTrue(report["bridges"][0]["carriageway_tan_field_samples"])
        # Tan is still a road colour, so continuity alone never saw this.
        self.assertFalse(report["bridges"][0]["non_road_field_samples"])

    def test_a_carriageway_deck_left_ivory_passes(self):
        self.write_bridge(ivory_eligible=True)
        self.write_fields(deck_material=0)
        code, report = self.validate()
        self.assertEqual(code, 0)
        self.assertEqual(report["result"], "passed")
        self.assertEqual(report["bridges"][0]["carriageway_tan_field_samples"], 0)

    def test_a_trail_deck_is_not_required_to_be_tan(self):
        # A sidewalk ribbon inside the roadway it flanks reads ivory by design.
        self.write_bridge(ivory_eligible=False)
        self.write_fields(deck_material=0)
        code, report = self.validate()
        self.assertEqual(code, 0)
        self.assertEqual(report["result"], "passed")
        self.assertEqual(report["bridges"][0]["carriageway_tan_field_samples"], 0)


if __name__ == "__main__":
    unittest.main()
