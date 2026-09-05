import sys
import unittest
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, box


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from road_symbols import (  # noqa: E402
    TRAIL_HIGHWAYS,
    trail_width_mm,
    build_road_symbols,
    classify_highway,
    printable_widths,
    sample_roadbed_widths_m,
)


CFG = {"grid_step_mm": 0.125, "nozzle_mm": 0.4, "road_minimum_bead_ratio": 0.85}


class RoadClassificationTests(unittest.TestCase):
    def test_trail_width_uses_its_own_printable_width(self):
        config={"minimum_path_width_mm":0.5,"scale_denominator":5576.988668344}
        self.assertEqual(trail_width_mm("footway",{},config),0.5)
        self.assertAlmostEqual(trail_width_mm("footway",{"width":"6"},config),
            6000/config["scale_denominator"])

    def test_all_hard_trail_classes_are_tan_only(self):
        for highway in TRAIL_HIGHWAYS:
            with self.subTest(highway=highway):
                classification, _ = classify_highway(
                    highway,
                    {"surface": "asphalt", "name": "Named trail", "lanes": "2", "maxspeed": "20"},
                    1.0,
                )
                self.assertEqual(classification, "trail")

    def test_car_free_road_form_can_still_be_a_carriageway(self):
        classification, _ = classify_highway("pedestrian", {
            "name": "Park Drive", "surface": "asphalt", "lanes": "1",
            "maxspeed": "20 mph", "oneway": "yes", "motor_vehicle": "no",
        }, 0.9)
        self.assertEqual(classification, "carriageway")

    def test_pedestrian_walk_and_plaza_remain_tan(self):
        for tags in [
            {"name": "Garden Walk", "surface": "asphalt"},
            {"name": "Civic Plaza", "surface": "paving_stones", "area": "yes"},
        ]:
            with self.subTest(tags=tags):
                self.assertEqual(classify_highway("pedestrian", tags, 1.0)[0], "trail")

    def test_minor_service_access_is_not_promoted(self):
        for service in ["parking_aisle", "driveway", "alley", "emergency_access"]:
            with self.subTest(service=service):
                result = classify_highway("service", {
                    "service": service, "surface": "asphalt", "lanes": "1", "name": "Access"
                }, 1.0)
                self.assertEqual(result[0], "other")

    def test_print_widths_are_grid_aligned_and_have_two_casings(self):
        for major in [False, True]:
            widths = printable_widths(CFG, major)
            self.assertAlmostEqual(widths.core_mm / 0.125, round(widths.core_mm / 0.125))
            self.assertAlmostEqual(widths.outer_mm / 0.125, round(widths.outer_mm / 0.125))
            self.assertGreaterEqual(widths.core_mm, 0.45)
            self.assertGreaterEqual((widths.outer_mm - widths.core_mm) / 2, 0.34)

    def test_widths_expand_with_scale_and_preserve_core_fraction(self):
        small_scale = printable_widths(CFG, physical_width_m=9.0, scale_denominator=16_621)
        large_scale = printable_widths(CFG, physical_width_m=9.0, scale_denominator=3_144)
        self.assertEqual(small_scale, printable_widths(CFG))
        self.assertGreater(large_scale.outer_mm, small_scale.outer_mm)
        self.assertGreater(large_scale.core_mm, small_scale.core_mm)
        self.assertAlmostEqual(large_scale.core_mm / large_scale.outer_mm, 0.40, delta=0.03)

    def test_cross_section_width_estimator_rejects_intersection_flare(self):
        # Source coordinates are EPSG:2263 US survey feet. A 10 m street has a
        # short 24 m-wide intersection flare in its middle.
        feet_per_m = 1 / 0.3048006096012192
        line = LineString([(0, 0), (120 * feet_per_m, 0)])
        street = box(0, -5 * feet_per_m, 120 * feet_per_m, 5 * feet_per_m)
        flare = box(55 * feet_per_m, -12 * feet_per_m, 65 * feet_per_m, 12 * feet_per_m)
        samples = sample_roadbed_widths_m(line, street.union(flare), interval_m=10)
        self.assertGreaterEqual(len(samples), 8)
        self.assertAlmostEqual(float(np.percentile(samples, 30)), 10.0, delta=0.2)


class RoadGeometryTests(unittest.TestCase):
    def setUp(self):
        self.aoi = box(0, 0, 100, 100)
        self.scale = 10_000

    def frame(self, rows):
        if not rows:
            return gpd.GeoDataFrame({"geometry": gpd.GeoSeries([], crs=2263)}, geometry="geometry", crs=2263)
        return gpd.GeoDataFrame(rows, geometry="geometry", crs=2263)

    def test_core_is_continuous_across_split_road_and_never_follows_trail(self):
        osm = self.frame([
            {"osm_id": 1, "highway": "residential", "tags": '{"highway":"residential","name":"Main"}',
             "geometry": LineString([(10, 50), (50, 50)])},
            {"osm_id": 2, "highway": "residential", "tags": '{"highway":"residential","name":"Main"}',
             "geometry": LineString([(50, 50), (90, 50)])},
            {"osm_id": 3, "highway": "footway", "tags": '{"highway":"footway","name":"Woodland Ramble"}',
             "geometry": LineString([(50, 10), (50, 90)])},
        ])
        roadbed = self.frame([{"geometry": box(8, 46, 92, 54), "SUB_FEATURE_CODE": 350000}])
        routes, outer, core, report = build_road_symbols(osm, roadbed, self.aoi, self.scale, CFG)
        self.assertEqual(report["categorical_trails_eligible"], 0)
        self.assertFalse(bool(routes.loc[routes.osm_id.eq(3), "core_eligible"].iloc[0]))
        core_union = core.geometry.union_all()
        outer_union = outer.geometry.union_all()
        self.assertTrue(core_union.covers(LineString([(10, 50), (90, 50)])))
        self.assertTrue(outer_union.covers(core_union))
        # The trail crosses the road symbol once, but no ivory branch extends
        # north or south along it.
        self.assertFalse(core_union.intersects(LineString([(50, 60), (50, 90)])))

    def test_centerline_fallback_works_without_roadbed(self):
        osm = self.frame([{
            "osm_id": 8, "highway": "primary", "tags": '{"highway":"primary","name":"Broadway"}',
            "geometry": LineString([(10, 20), (90, 20)]),
        }])
        roadbed = self.frame([])
        routes, outer, core, _ = build_road_symbols(osm, roadbed, self.aoi, self.scale, CFG)
        self.assertTrue(bool(routes.core_eligible.iloc[0]))
        self.assertGreater(outer.area.sum(), core.area.sum())
        self.assertTrue(core.geometry.union_all().covers(osm.geometry.iloc[0]))

    def test_bridge_and_tunnel_do_not_leak_into_surface_polygons(self):
        osm = self.frame([
            {"osm_id": 11, "highway": "primary", "tags": '{"highway":"primary","bridge":"yes","layer":"1"}',
             "geometry": LineString([(10, 30), (90, 30)])},
            {"osm_id": 12, "highway": "secondary", "tags": '{"highway":"secondary","tunnel":"yes","layer":"-1"}',
             "geometry": LineString([(10, 70), (90, 70)])},
        ])
        roadbed = self.frame([])
        routes, outer, core, _ = build_road_symbols(osm, roadbed, self.aoi, self.scale, CFG)
        self.assertTrue(routes.core_eligible.all())
        self.assertTrue(routes.bridge.any())
        self.assertTrue(routes.tunnel.any())
        self.assertEqual(len(outer), 0)
        self.assertEqual(len(core), 0)


if __name__ == "__main__":
    unittest.main()
