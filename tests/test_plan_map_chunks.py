import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Polygon, box


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _chunk_geometry as geometry  # noqa: E402
import plan_map_chunks as planner  # noqa: E402


def parsed(*arguments):
    return planner.parser().parse_args(
        ["--bounding-polygon", "POLYGON ((-74.01 40.70, -74.00 40.70, -74.00 40.71, -74.01 40.71, -74.01 40.70))",
         *arguments]
    )


class ArgumentTests(unittest.TestCase):
    def test_defaults_are_a_printable_plate(self):
        resolved = planner.resolve(parsed())
        self.assertEqual(resolved["envelope_mm"], (235.0, 235.0))

    def test_an_envelope_off_the_grid_step_is_rejected(self):
        with self.assertRaises(planner.PlanError) as caught:
            planner.resolve(parsed("--max-chunk-size-mm", "235.03x235"))
        self.assertIn("grid-step", str(caught.exception))

    def test_an_oversized_envelope_is_rejected(self):
        with self.assertRaises(planner.PlanError):
            planner.resolve(parsed("--max-chunk-size-mm", "300x300"))

    def test_a_scale_over_the_elevation_ceiling_names_the_fix(self):
        with self.assertRaises(planner.PlanError) as caught:
            planner.resolve(parsed("--scale", "40000"))
        message = str(caught.exception)
        self.assertIn("elevation", message)
        self.assertIn("--scale", message)

    def test_a_polygon_outside_nyc_is_rejected(self):
        arguments = planner.parser().parse_args([
            "--bounding-polygon",
            "POLYGON ((-80 40, -79 40, -79 41, -80 41, -80 40))",
        ])
        with self.assertRaises(planner.PlanError) as caught:
            planner.resolve(arguments)
        self.assertIn("NYC bounding box", str(caught.exception))

    def test_nonsense_knobs_are_rejected(self):
        for flag, value in (("--max-chunks", "0"), ("--cut-snap-mm", "0"),
                            ("--min-edge-mm", "-1"), ("--cost-resolution-m", "0"),
                            ("--min-chunk-fill", "1.5")):
            with self.subTest(flag=flag):
                with self.assertRaises(planner.PlanError):
                    planner.resolve(parsed(flag, value))


class OrientationTests(unittest.TestCase):
    def setUp(self):
        self.target = box(980000.0, 200000.0, 990000.0, 260000.0)

    def test_north_is_used_verbatim(self):
        chosen = planner.choose_bearing(Path("/nonexistent"), self.target, "north")
        self.assertEqual(chosen["bearing_rad"], 0.0)
        self.assertEqual(chosen["source"], "north")

    def test_an_explicit_bearing_is_used_verbatim(self):
        chosen = planner.choose_bearing(Path("/nonexistent"), self.target, "27.5")
        self.assertAlmostEqual(math.degrees(chosen["bearing_rad"]), 27.5)
        self.assertEqual(chosen["source"], "explicit")

    def test_an_unparseable_bearing_is_rejected(self):
        with self.assertRaises(planner.PlanError):
            planner.choose_bearing(Path("/nonexistent"), self.target, "sideways")

    def test_auto_without_the_osm_cache_names_the_cache_script(self):
        from _chunk_cost import SourceDataError

        with self.assertRaises(SourceDataError) as caught:
            planner.choose_bearing(Path("/nonexistent-cache-root"), self.target, "auto")
        self.assertIn("cache_new_york_osm.py", str(caught.exception))

    def test_the_mode_holds_the_main_grid_when_a_second_grid_competes(self):
        # Manhattan's shape: a dominant grid plus a smaller neighbourhood on a
        # different one. Averaging lands between them, which is what drifts a
        # cut off its street; the mode stays on the grid most cuts will follow.
        angle = np.concatenate([np.full(300, 29.0), np.full(100, 12.0)])
        weight = np.concatenate([np.full(300, 3.0), np.ones(100)])
        peak, coherence = planner.dominant_bearing(angle, weight)
        self.assertAlmostEqual(peak, 29.0, places=1)
        self.assertGreater(coherence, 0.5)
        mean = math.degrees(
            np.angle(np.sum(weight * np.exp(4j * np.radians(angle)))) / 4
        ) % 90.0
        self.assertGreater(abs(mean - 29.0), 0.5)

    def test_a_bearing_near_the_wrap_is_not_split_across_bins(self):
        angle = np.concatenate([np.full(100, 89.6), np.full(100, 0.3)])
        peak, coherence = planner.dominant_bearing(angle, np.ones(200))
        self.assertTrue(min(peak, 90.0 - peak) < 1.0, peak)
        self.assertGreater(coherence, 0.9)

    def test_scattered_directions_report_low_coherence(self):
        angle = np.linspace(0.0, 90.0, 900, endpoint=False)
        _, coherence = planner.dominant_bearing(angle, np.ones(900))
        self.assertLess(coherence, planner.MINIMUM_GRID_COHERENCE)

    def test_the_envelope_bearing_follows_the_long_axis(self):
        # A rectangle twice as tall as it is wide: its long axis points north.
        self.assertAlmostEqual(planner.envelope_bearing(self.target) % math.pi, 0.0, places=6)

    def test_the_frame_origin_sits_at_the_target_corner(self):
        frame = planner.build_frame(self.target, 0.0, 10000.0, 0.125)
        local = frame.to_frame(self.target)
        self.assertAlmostEqual(local.bounds[0], 0.0, places=6)
        self.assertAlmostEqual(local.bounds[1], 0.0, places=6)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.shared = {
            "plan_id": "demo", "scale": 10533.0,
            "grid_step_mm": 0.125, "layer_height": 0.24, "vertical_exaggeration": 1.0,
            "terrain_origin_m": -11.4712, "terrain_relief_factor": 1.0,
            "source_padding_m": 20.0, "prime_tower": "auto",
            "offline": False, "no_preview": False, "model_dir": "output/models",
        }
        self.paths = {"polygon": "output/plans/demo/chunks/demo_A1.geojson",
                      "frame": "output/plans/demo/chunks/demo_A1.frame.json",
                      "model": "output/models/demo_A1.3mf"}

    def test_every_cross_chunk_value_is_pinned_on_the_command_line(self):
        command = planner.command_for({"label": "A1"}, self.shared, self.paths)
        for flag in ("--print-frame", "--scale", "--grid-step-mm", "--layer-height",
                     "--vertical-exaggeration", "--terrain-origin-m",
                     "--terrain-relief-factor", "--source-padding-m"):
            self.assertIn(flag, command)

    def test_the_command_names_a_bare_interpreter(self):
        """A plan is tracked, so it must not record the planner's own python."""
        command = planner.command_for({"label": "A1"}, self.shared, self.paths)
        self.assertEqual(command[0], "python")
        for token in command:
            self.assertFalse(token.startswith("/"), token)

    def test_the_polygon_and_frame_are_passed_as_files(self):
        command = planner.command_for({"label": "A1"}, self.shared, self.paths)
        self.assertIn(f"@{self.paths['polygon']}", command)
        self.assertIn(f"@{self.paths['frame']}", command)

    def test_offline_and_preview_switches_are_forwarded(self):
        self.shared.update(offline=True, no_preview=True)
        command = planner.command_for({"label": "A1"}, self.shared, self.paths)
        self.assertIn("--offline", command)
        self.assertIn("--no-preview", command)

    def test_the_terrain_datum_keeps_enough_precision_to_match_across_plates(self):
        command = planner.command_for({"label": "A1"}, self.shared, self.paths)
        value = command[command.index("--terrain-origin-m") + 1]
        self.assertEqual(float(value), round(self.shared["terrain_origin_m"], 4))


# One manufacturing cell at the scale these fixtures use, and the frame the
# fixture plan was cut in.
CELL_FT = 4.32
A_FRAME = geometry.Frame((981000.0, 200000.0), (0.8, -0.6), (0.6, 0.8), 10000.0, 0.125)


def a_continued_plan(**overrides):
    """A minimal manifest of two square plates side by side, in WGS84."""
    shared = {
        "plan_id": "printed", "scale": 10000.0, "grid_step_mm": 0.125,
        "layer_height": 0.16, "vertical_exaggeration": 1.0, "source_padding_m": 20.0,
        "land_cover_dataset": "nyc_land_cover_2021",
        "terrain_origin_m": -7.25, "terrain_relief_factor": 1.0,
    }
    shared.update(overrides)
    return {
        "frame": {"origin_ft": [981000.0, 200000.0],
                  "x_axis": [0.8, -0.6], "y_axis": [0.6, 0.8]},
        "cut_settings": {"snap_mm": 1.0},
        "shared_generation": shared,
        "chunks": [
            {"label": "A1", "polygon_wgs84": json.loads(shapely.to_geojson(
                box(-74.01, 40.72, -74.00, 40.73)))},
            {"label": "A2", "polygon_wgs84": json.loads(shapely.to_geojson(
                box(-74.01, 40.71, -74.00, 40.72)))},
        ],
    }


class RecordedPathTests(unittest.TestCase):
    """Paths a plan records are repository-relative, whoever planned it."""

    def test_an_absolute_in_repository_path_is_recorded_relative(self):
        self.assertEqual(
            planner.repo_relative_text(str(planner.ROOT / "output" / "models")),
            "output/models",
        )

    def test_the_file_argument_prefix_survives_the_rewrite(self):
        self.assertEqual(
            planner.repo_relative_text(f"@{planner.ROOT}/data/polygons/a.geojson"),
            "@data/polygons/a.geojson",
        )

    def test_text_that_is_already_relative_is_recorded_as_typed(self):
        for text in ("output/models", "@data/polygons/a.geojson", "--scale", "10533"):
            self.assertEqual(planner.repo_relative_text(text), text)

    def test_a_path_outside_the_repository_is_left_alone(self):
        """The rewrite is not free to relocate a genuinely external path."""
        for text in ("/usr/bin/python3", "@/var/tmp/a.geojson"):
            self.assertEqual(planner.repo_relative_text(text), text)


class ContinuedPlanTests(unittest.TestCase):
    """Joining a plan whose plates are already printed."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "plan.json"

    def write(self, plan):
        self.path.write_text(json.dumps(plan))
        return self.path

    def args(self, *arguments):
        return parsed("--scale", "10000", "--layer-height", "0.16", *arguments)

    def test_the_lattice_is_inherited_rather_than_derived_from_the_new_target(self):
        plan = a_continued_plan()
        anchor = geometry.Frame(
            tuple(plan["frame"]["origin_ft"]), tuple(plan["frame"]["x_axis"]),
            tuple(plan["frame"]["y_axis"]), 10000.0, 0.125,
        )
        elsewhere = box(1000000.0, 260000.0, 1002000.0, 262000.0)
        frame = planner.build_frame(elsewhere, 0.0, 10000.0, 0.125, anchor=anchor)
        self.assertEqual(frame.origin_ft, anchor.origin_ft)
        self.assertEqual(frame.x_axis, anchor.x_axis)
        self.assertEqual(frame.y_axis, anchor.y_axis)

    def test_a_setting_the_two_plans_disagree_on_names_the_value_to_pass(self):
        path = self.write(a_continued_plan(layer_height=0.24))
        with self.assertRaises(planner.PlanError) as caught:
            planner.load_continued_plan(path, self.args("--continue-plan", str(path)))
        self.assertIn("--layer-height 0.24", str(caught.exception))

    def test_matching_settings_are_accepted(self):
        path = self.write(a_continued_plan())
        plan = planner.load_continued_plan(path, self.args("--continue-plan", str(path)))
        self.assertEqual(plan["shared_generation"]["plan_id"], "printed")

    def test_fitting_the_scale_would_change_the_map_and_is_refused(self):
        path = self.write(a_continued_plan())
        with self.assertRaises(planner.PlanError) as caught:
            planner.load_continued_plan(
                path, self.args("--continue-plan", str(path), "--fit-scale")
            )
        self.assertIn("--fit-scale", str(caught.exception))

    def test_a_file_that_is_not_a_manifest_says_so(self):
        path = self.write({"hello": "world"})
        with self.assertRaises(planner.PlanError) as caught:
            planner.load_continued_plan(path, self.args("--continue-plan", str(path)))
        self.assertIn("plan manifest", str(caught.exception))

    def test_whole_plates_are_reported_as_replaced_or_met(self):
        contact = planner.continued_contact(
            a_continued_plan(), box(-74.01, 40.71, -74.00, 40.72), sliver_ft=CELL_FT
        )
        self.assertEqual(contact["replaces"], ["A2"])
        self.assertEqual(contact["meets"], ["A1"])

    def test_a_target_built_from_plates_carries_their_own_outline(self):
        plan = a_continued_plan()
        target = planner.plates_target(plan, ["A1", "A2"], frame=A_FRAME, sliver_ft=CELL_FT)
        self.assertEqual(target.geom_type, "Polygon")
        self.assertEqual(len(target.interiors), 0)
        self.assertAlmostEqual(target.bounds[1], 40.71)
        self.assertAlmostEqual(target.bounds[3], 40.73)
        # The join it leaves behind is the printed plates' own boundary.
        self.assertEqual(
            planner.continued_contact(plan, target, sliver_ft=CELL_FT)["replaces"],
            ["A1", "A2"],
        )

    def test_an_outline_off_its_lattice_by_a_projection_hair_is_put_back(self):
        """A straight cut edge comes back from WGS84 with its ends apart.

        Left alone, a new cut drawn to the same lattice position runs
        straight while the outline slants, and the wedge between them is a
        sliver the solid modelling turns into a spike.
        """
        frame = A_FRAME
        step = round(1.0 / frame.grid_step_mm) * frame.cell_ft
        edge = 40 * step
        bent = Polygon([(0.0, 0.0), (edge, 0.0), (edge + 0.011, 30 * step), (0.0, 30 * step)])
        fixed = planner.on_lattice(bent, {"cut_settings": {"snap_mm": 1.0}}, frame)
        corners = sorted(round(x, 6) for x, _ in np.asarray(fixed.exterior.coords))
        self.assertEqual(corners.count(round(edge, 6)), 2, "the edge is still bent")
        self.assertAlmostEqual(fixed.area, bent.area, delta=abs(0.011 * 30 * step))

    def test_a_plan_that_records_no_lattice_is_left_alone(self):
        bent = Polygon([(0.0, 0.0), (100.0, 0.0), (100.011, 50.0), (0.0, 50.0)])
        self.assertEqual(planner.on_lattice(bent, {}, A_FRAME), bent)

    def test_naming_a_plate_the_plan_does_not_have_lists_the_ones_it_does(self):
        with self.assertRaises(planner.PlanError) as caught:
            planner.plates_target(a_continued_plan(), ["A1", "Z9"], frame=A_FRAME, sliver_ft=CELL_FT)
        self.assertIn("Z9", str(caught.exception))
        self.assertIn("A1, A2", str(caught.exception))

    def test_plates_that_do_not_touch_cannot_be_one_target(self):
        plan = a_continued_plan()
        plan["chunks"][1]["polygon_wgs84"] = json.loads(shapely.to_geojson(
            box(-73.98, 40.71, -73.97, 40.72)))
        with self.assertRaises(planner.PlanError) as caught:
            planner.plates_target(plan, ["A1", "A2"], frame=A_FRAME, sliver_ft=CELL_FT)
        self.assertIn("connected", str(caught.exception))

    def test_a_target_is_named_exactly_one_way(self):
        with self.assertRaises(planner.PlanError) as caught:
            planner.resolve(parsed("--replace-plates", "A1"))
        self.assertIn("exactly one", str(caught.exception))
        with self.assertRaises(planner.PlanError) as caught:
            planner.resolve(planner.parser().parse_args([]))
        self.assertIn("exactly one", str(caught.exception))

    def test_replacing_plates_needs_the_plan_they_belong_to(self):
        arguments = planner.parser().parse_args(["--replace-plates", "A1,A2"])
        with self.assertRaises(planner.PlanError) as caught:
            planner.resolve(arguments)
        self.assertIn("--continue-plan", str(caught.exception))

    def test_rings_that_only_graze_a_plate_are_not_a_partial_cover(self):
        """A union's own boundary crosses its neighbour's ring by nanometres."""
        plan = a_continued_plan()
        grazing = box(-74.01, 40.71, -74.00, 40.72 + 1e-12)
        self.assertEqual(
            planner.continued_contact(plan, grazing, sliver_ft=CELL_FT)["replaces"], ["A2"]
        )

    def test_a_target_cutting_through_a_printed_plate_is_refused(self):
        with self.assertRaises(planner.PlanError) as caught:
            planner.continued_contact(
                a_continued_plan(), box(-74.01, 40.71, -74.00, 40.725), sliver_ft=CELL_FT
            )
        self.assertIn("A1", str(caught.exception))


class SummaryTests(unittest.TestCase):
    def test_quality_is_weighted_by_seam_length(self):
        seams = [
            {"length_ft": 100.0, "building_fraction": 1.0},
            {"length_ft": 300.0, "building_fraction": 0.0},
        ]
        summary = planner.summarise([], seams, 10000.0)
        self.assertAlmostEqual(summary["building_fraction"], 0.25)

    def test_crossings_are_listed_per_feature_not_pooled_by_name(self):
        seams = [
            {"length_ft": 100.0, "between": ["A1", "A2"],
             "crosses": [{"kind": "bridge", "name": "unnamed", "length_ft": 60.0},
                         {"kind": "bridge", "name": "unnamed", "length_ft": 30.0}]},
        ]
        summary = planner.summarise([], seams, 10000.0)
        self.assertEqual(len(summary["keep_out_crossings"]), 2)
        self.assertAlmostEqual(summary["keep_out_length_ft"], 90.0)
        self.assertEqual(summary["keep_out_crossings"][0]["between"], ["A1", "A2"])

    def test_crossing_lengths_are_converted_to_printed_millimetres(self):
        seams = [{"length_ft": 100.0, "between": ["A1", "A2"],
                  "crosses": [{"kind": "bridge", "name": "x", "length_ft": 1000.0}]}]
        summary = planner.summarise([], seams, 10000.0)
        self.assertAlmostEqual(summary["longest_crossing_mm"], 1000.0 * geometry.FT * 1000 / 10000)

    def test_joint_fit_is_summarised_per_printed_metre_of_seam(self):
        seams = [
            {"length_ft": 1000.0, "sides": 1, "corners": 0, "shortest_side_ft": 1000.0,
             "between": ["A1", "A2"]},
            {"length_ft": 1000.0, "sides": 5, "corners": 4, "shortest_side_ft": 40.0,
             "between": ["A2", "A3"]},
        ]
        joints = planner.summarise([], seams, 10000.0)["joints"]
        metres = 2000.0 * geometry.FT * 1000 / 10000 / 1000
        self.assertEqual((joints["sides"], joints["corners"]), (6, 4))
        self.assertAlmostEqual(joints["corners_per_metre"], 4 / metres)
        self.assertAlmostEqual(joints["shortest_side_mm"], 40.0 * geometry.FT * 1000 / 10000)
        self.assertEqual(joints["worst_joint"], ["A2", "A3"])

    def test_no_seams_yields_no_weighted_values(self):
        summary = planner.summarise([], [], 10000.0)
        self.assertIsNone(summary["building_fraction"])
        self.assertEqual(summary["keep_out_crossings"], [])

    def test_quality_is_measured_on_final_seams_not_search_cuts(self):
        """Compaction can absorb an early cut, so cuts are not seams."""
        import numpy as np
        from test_chunk_cost import a_surface

        surface = a_surface(np.ones((40, 40)),
                            layers={"building_core": np.zeros((40, 40), dtype=bool)})
        seams = [{"chunks": [0, 1], "length_ft": 50.0,
                  "geometry": shapely.LineString([(10.0, 10.0), (10.0, 300.0)])}]
        measured = planner.measure_seams(surface, seams, ["A1", "A2"])
        self.assertEqual(measured[0]["between"], ["A1", "A2"])
        self.assertIn("blocked_samples", measured[0])
        self.assertIn("building_fraction", measured[0])


class PlanOutputTests(unittest.TestCase):
    """End-to-end writing with straight cuts, so no dataset is required."""

    def setUp(self):
        self.frame = planner.build_frame(
            box(980000.0, 200000.0, 990000.0, 230000.0), 0.0, 10000.0, 0.125
        )
        self.target_ft = self.frame.to_frame(box(980000.0, 200000.0, 990000.0, 230000.0))
        limits = (self.frame.feet(235.0), self.frame.feet(235.0))
        self.polygons = geometry.partition(
            self.target_ft, limits_ft=limits, deviation_ft=0.0, sample_step_ft=50.0
        ).polygons
        self.labels = geometry.grid_labels(self.frame, self.polygons, limits)
        self.shared = {
            "plan_id": "demo", "scale": 10000.0,
            "grid_step_mm": 0.125, "layer_height": 0.24, "vertical_exaggeration": 1.0,
            "terrain_origin_m": -1.5, "terrain_relief_factor": 1.0,
            "source_padding_m": 20.0, "prime_tower": "auto", "offline": True,
            "no_preview": True, "model_dir": "output/models",
        }

    def test_chunk_records_carry_a_covering_print_frame(self):
        records = planner.chunk_records(self.frame, self.polygons, self.labels, self.shared)
        self.assertEqual(len(records), len(self.polygons))
        for record in records:
            payload = record["print_frame"]
            self.assertEqual(sorted(payload), ["origin_ft", "size_mm", "x_axis", "y_axis"])
            self.assertTrue(all(side > 0 for side in payload["size_mm"]))
            self.assertGreater(record["area_km2"], 0)

    def test_written_polygons_round_trip_through_geojson_exactly(self):
        records = planner.chunk_records(self.frame, self.polygons, self.labels, self.shared)
        for record in records:
            text = shapely.to_geojson(record["polygon_wgs84"])
            restored = shapely.from_geojson(text)
            self.assertEqual(
                shapely.get_coordinates(restored).tolist(),
                shapely.get_coordinates(record["polygon_wgs84"]).tolist(),
            )

    def test_the_plan_directory_holds_everything_needed_to_build(self):
        records = planner.chunk_records(self.frame, self.polygons, self.labels, self.shared)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = planner.write_plan(root, {"planner_version": 1}, records, self.shared)
            self.assertTrue((root / "plan.json").is_file())
            self.assertTrue((root / "commands.sh").is_file())
            self.assertTrue((root / "chunks.geojson").is_file())
            self.assertEqual(len(manifest["chunks"]), len(records))
            for record in records:
                stem = f"demo_{record['label']}"
                self.assertTrue((root / "chunks" / f"{stem}.geojson").is_file())
                self.assertTrue((root / "chunks" / f"{stem}.frame.json").is_file())
            script = (root / "commands.sh").read_text()
            self.assertIn("generate_3mf.py", script)
            self.assertEqual(script.count("--print-frame"), len(records))
            self.assertTrue(script.startswith("#!/bin/sh"))

    def test_the_written_plan_carries_no_path_from_the_planning_machine(self):
        """plan.json and commands.sh are tracked; neither may name this checkout."""
        records = planner.chunk_records(self.frame, self.polygons, self.labels, self.shared)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planner.write_plan(root, {"planner_version": 1}, records, self.shared)
            for name in ("plan.json", "commands.sh"):
                self.assertNotIn(str(planner.ROOT), (root / name).read_text(), name)

    def test_the_written_frame_files_satisfy_the_generator_parser(self):
        from generate_3mf import parse_print_frame

        records = planner.chunk_records(self.frame, self.polygons, self.labels, self.shared)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planner.write_plan(root, {"planner_version": 1}, records, self.shared)
            for record in records:
                path = root / "chunks" / f"demo_{record['label']}.frame.json"
                self.assertEqual(parse_print_frame(str(path)), record["print_frame"])

    def test_the_written_polygons_satisfy_the_generator_parser(self):
        from generate_3mf import parse_bounding_polygon

        records = planner.chunk_records(self.frame, self.polygons, self.labels, self.shared)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            planner.write_plan(root, {"planner_version": 1}, records, self.shared)
            for record in records:
                path = root / "chunks" / f"demo_{record['label']}.geojson"
                self.assertEqual(parse_bounding_polygon(f"@{path}").geom_type, "Polygon")

    def test_assembled_size_reports_the_whole_map(self):
        width, height = planner.assembled_size_mm(self.frame, self.target_ft)
        self.assertAlmostEqual(width, self.frame.mm(10000.0), places=6)
        self.assertAlmostEqual(height, self.frame.mm(30000.0), places=6)


if __name__ == "__main__":
    unittest.main()
