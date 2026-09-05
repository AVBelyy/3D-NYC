import json
import shlex
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import LineString, Polygon, box, mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_chunk_plan import _compare_generated_pair_arbitrary, validate_plan  # noqa: E402


FT = 0.3048006096012192


class PlanFixture:
    def __init__(self, root: Path):
        self.root = root
        self.plan_path = root / "plan.json"
        self.commands_path = root / "commands.sh"
        self.data_dir = root / "data"
        self.output_dir = root / "output"
        self.scale = 1000.0
        self.grid_step = 0.5
        self.step_ft = self.grid_step * self.scale / (FT * 1000.0)
        self.origin = np.asarray([990000.0, 220000.0])
        self.x_axis = np.asarray([1.0, 0.0])
        self.y_axis = np.asarray([0.0, 1.0])
        self.cut_x = [0, 60, 120]
        self.cut_y = [0, 60]
        self.target = box(
            self.origin[0], self.origin[1],
            self.origin[0] + self.cut_x[-1] * self.step_ft,
            self.origin[1] + self.cut_y[-1] * self.step_ft,
        )
        self.target_wgs = gpd.GeoSeries([self.target], crs=2263).to_crs(4326).iloc[0]
        self.shared_options = {
            "--scale": "1000",
            "--terrain-origin-m": "-5",
            "--terrain-relief-factor": "1",
            "--vertical-exaggeration": "1",
            "--minimum-terrain-levels": "6",
            "--grid-step-mm": "0.5",
            "--layer-height": "0.24",
            "--prime-tower": "off",
            "--source-padding-m": "20",
            "--lidar-source": "cache",
            "--lidar-cache-dir": str(root / "lidar"),
            "--cache-dir": str(root / "cache"),
            "--data-dir": str(self.data_dir),
            "--output-dir": str(self.output_dir),
        }
        self.shared_flags = {"--offline": True, "--full-validation": False, "--slice": False}
        self.records = [self._record(0), self._record(1)]
        validator = str(ROOT / "scripts/validate_chunk_plan.py")
        self.static_argv = [sys.executable, validator, "--plan", str(self.plan_path), "--report", str(root / "validation.json")]
        self.post_argv = [
            sys.executable, validator, "--plan", str(self.plan_path), "--check-generated", "--require-generated",
            "--report", str(root / "post_generation_validation.json"),
        ]
        self.plan = {
            "schema_version": 2,
            "plan_id": "synthetic",
            "request": {
                "maximum_chunks": 2,
                "chunk_size_mm": [30.0, 30.0],
                "bounding_polygon_wgs84": mapping(self.target_wgs),
                "target_wkb_hex_epsg2263": shapely.to_wkb(self.target, hex=True),
            },
            "generation": {
                "python": sys.executable,
                "script": str(ROOT / "scripts/generate_3mf.py"),
                "shared_options": self.shared_options,
                "shared_flags": self.shared_flags,
                "static_validation_argv": self.static_argv,
                "post_generation_validation_argv": self.post_argv,
            },
            "layout": {
                "scale_denominator": self.scale,
                "grid_step_mm": self.grid_step,
                "global_origin_ft": self.origin.tolist(),
                "x_axis": self.x_axis.tolist(),
                "y_axis": self.y_axis.tolist(),
                "cut_grid_cells": {"x": self.cut_x, "y": self.cut_y},
                "grid": {"columns": 2, "rows": 1},
            },
            "terrain": {"shared_origin_m_navd88": -5.0, "shared_relief": {"factor": 1.0}},
            "chunk_count": 2,
            "chunks": self.records,
            "files": {"commands": str(self.commands_path)},
        }
        self.write()

    def _cell(self, column: int):
        return box(
            self.origin[0] + self.cut_x[column] * self.step_ft,
            self.origin[1],
            self.origin[0] + self.cut_x[column + 1] * self.step_ft,
            self.origin[1] + self.cut_y[-1] * self.step_ft,
        )

    def _record(self, column: int) -> dict:
        chunk_id = f"synthetic_r01_c{column + 1:02d}"
        polygon_path = self.root / "chunks" / f"{chunk_id}.geojson"
        frame_path = self.root / "frames" / f"{chunk_id}.json"
        polygon_path.parent.mkdir(parents=True, exist_ok=True)
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        projected = self._cell(column)
        wgs = gpd.GeoSeries([projected], crs=2263).to_crs(4326).iloc[0]
        polygon_path.write_text(json.dumps(mapping(wgs)))
        frame = {
            "origin_ft": [self.origin[0] + self.cut_x[column] * self.step_ft, self.origin[1]],
            "x_axis": self.x_axis.tolist(), "y_axis": self.y_axis.tolist(), "size_mm": [30.0, 30.0],
        }
        frame_path.write_text(json.dumps(frame))
        output = self.root / "models" / f"{chunk_id}.3mf"
        argv = [
            sys.executable, str(ROOT / "scripts/generate_3mf.py"),
            "--bounding-polygon", "@" + str(polygon_path),
            "--print-frame", "@" + str(frame_path),
        ]
        for option, value in self.shared_options.items():
            argv.extend([option, value])
        argv.extend([flag for flag, enabled in self.shared_flags.items() if enabled])
        argv.extend(["--job-id", chunk_id, "--output", str(output)])
        return {
            "id": chunk_id, "row": 1, "column": column + 1, "component": 1,
            "area_sq_ft": float(projected.area), "frame": frame,
            "polygon_file": str(polygon_path), "frame_file": str(frame_path),
            "output_3mf": str(output), "argv": argv, "command": shlex.join(argv),
        }

    def write(self):
        self.plan["chunk_count"] = len(self.plan["chunks"])
        self.plan["request"]["maximum_chunks"] = max(
            self.plan["request"]["maximum_chunks"], len(self.plan["chunks"])
        )
        self.plan_path.write_text(json.dumps(self.plan, indent=2))
        text = (
            "#!/bin/sh\nset -eu\n\n" + shlex.join(self.static_argv) + "\n\n"
            + "\n\n".join(record["command"] for record in self.plan["chunks"]) + "\n\n"
            + shlex.join(self.post_argv) + "\n"
        )
        self.commands_path.write_text(text)

    def enable_semantic_mode(self):
        self.plan["schema_version"] = 3
        self.plan["layout"]["partition_mode"] = "semantic_paths"
        for column, record in enumerate(self.plan["chunks"]):
            geometry = self._cell(column)
            record["planned_polygon_wkb_hex_epsg2263"] = shapely.to_wkb(geometry, hex=True)
            record["logical_cell_wkb_hex_epsg2263"] = shapely.to_wkb(geometry, hex=True)
            record["frame_grid_bounds"] = [self.cut_x[column], 0, self.cut_x[column + 1], 60]
        x = self.origin[0] + self.cut_x[1] * self.step_ft
        seam = LineString([(x, self.origin[1]), (x, self.origin[1] + self.cut_y[-1] * self.step_ft)])
        self.plan["seams"] = [{"axis": "x", "index": 1, "segment": 1,
                                "geometry_wkb_hex_epsg2263": shapely.to_wkb(seam, hex=True)}]
        self.write()

    def write_generated(self, record: dict, height: float):
        job = self.output_dir / "jobs" / record["id"]
        (job / "work").mkdir(parents=True, exist_ok=True)
        frame = record["frame"]
        config = {
            "scale_denominator": self.scale, "terrain_origin_m": -5.0,
            "terrain_relief_factor": 1.0, "vertical_exaggeration": 1.0,
            "minimum_terrain_relief_levels": 6.0, "grid_step_mm": self.grid_step,
            "layer_height_mm": 0.24, "source_padding_m": 20.0, "lidar_source": "cache",
            "lidar_cache_dir": str(self.root / "lidar"),
            "cache_dir": str(self.root / "cache"),
            "size_mm": frame["size_mm"],
            "frame_epsg2263": {key: frame[key] for key in ["origin_ft", "x_axis", "y_axis"]},
            "aoi_wgs84": json.loads(Path(record["polygon_file"]).read_text()),
        }
        (job / "config.json").write_text(json.dumps(config))
        (job / "work/field_build_report.json").write_text(json.dumps({
            "vertical_origin_m_navd88": -5.0, "terrain_relief": {"factor": 1.0},
        }))
        shape_ = (60, 60)
        mask = np.ones(shape_, dtype=bool)
        np.savez_compressed(
            job / "work/map_fields.npz",
            height_mm=np.full(shape_, height, dtype=np.float32),
            ground_mm=np.full(shape_, 5.0, dtype=np.float32),
            material=np.zeros(shape_, dtype=np.uint8), aoi_mask=mask,
        )
        metadata = {"schema_version": 1, "argv": record["argv"], "shell_command": record["command"], "working_directory": str(ROOT)}
        (job / "generation_command.json").write_text(json.dumps(metadata))
        model = Path(record["output_3mf"])
        model.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(model, "w") as archive:
            archive.writestr("Metadata/generation_command.json", json.dumps(metadata))


class ChunkPlanValidationTests(unittest.TestCase):
    def test_curved_generated_seam_is_sampled_in_overlapping_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seam = [(60, 0), (65, 20), (55, 40), (60, 60)]
            left_geometry = Polygon([(0, 0), *seam, (0, 60)])
            right_geometry = Polygon([(60, 0), (120, 0), (120, 60), (60, 60), *reversed(seam[1:-1])])

            def generated(name, x_offset, width, geometry):
                job = root / name
                (job / "work").mkdir(parents=True)
                xx, yy = np.meshgrid(
                    np.arange(x_offset, x_offset + width) + 0.5,
                    np.arange(60)[::-1] + 0.5,
                )
                mask = shapely.intersects_xy(geometry, xx, yy)
                np.savez_compressed(
                    job / "work/map_fields.npz",
                    height_mm=np.full(mask.shape, 10.0, dtype=np.float32),
                    ground_mm=np.full(mask.shape, 5.0, dtype=np.float32),
                    material=np.zeros(mask.shape, dtype=np.uint8), aoi_mask=mask,
                )
                return {"job_dir": job}

            left = generated("left", 0, 70, left_geometry)
            right = generated("right", 50, 70, right_geometry)
            common = {"x_axis": np.asarray([1.0, 0.0]), "y_axis": np.asarray([0.0, 1.0])}
            left_frame = {
                **common, "origin": np.asarray([0.0, 0.0]),
                "offset_cells": np.asarray([0, 0]), "shape_cells": np.asarray([70, 60]),
            }
            right_frame = {
                **common, "origin": np.asarray([50.0, 0.0]),
                "offset_cells": np.asarray([50, 0]), "shape_cells": np.asarray([70, 60]),
            }
            comparison = _compare_generated_pair_arbitrary(
                left, right, left_frame, right_frame,
                left_geometry, right_geometry, step_ft=1.0,
            )
            self.assertIsNotNone(comparison)
            axis, ground, height, material, samples = comparison
            self.assertEqual(axis, "free_form")
            self.assertGreater(samples, 10)
            self.assertAlmostEqual(float(ground.max()), 0.0)
            self.assertAlmostEqual(float(height.max()), 0.0)
            self.assertTrue(material.all())

    def test_valid_static_plan_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            report = validate_plan(fixture.plan_path)
            self.assertEqual(report["result"], "passed", report["issues"])

    def test_semantic_partition_topology_is_independently_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            fixture.enable_semantic_mode()
            report = validate_plan(fixture.plan_path)
            self.assertEqual(report["result"], "passed", report["issues"])
            bad_x = fixture.origin[0] + (fixture.cut_x[1] + 2) * fixture.step_ft
            bad = LineString([
                (bad_x, fixture.origin[1]),
                (bad_x, fixture.origin[1] + fixture.cut_y[-1] * fixture.step_ft),
            ])
            fixture.plan["seams"][0]["geometry_wkb_hex_epsg2263"] = shapely.to_wkb(bad, hex=True)
            fixture.write()
            report = validate_plan(fixture.plan_path)
            codes = {issue["code"] for issue in report["issues"]}
            self.assertTrue({"BROKEN_INTERNAL_SEAM", "UNPLANNED_INTERNAL_SEAM"} <= codes)

    def test_semantic_frame_bounds_mismatch_is_descriptive(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            fixture.enable_semantic_mode()
            fixture.plan["chunks"][0]["frame_grid_bounds"][2] -= 1
            fixture.write()
            report = validate_plan(fixture.plan_path)
            self.assertIn("FRAME_GRID_BOUNDS_MISMATCH", {issue["code"] for issue in report["issues"]})

    def test_wrong_frame_span_has_descriptive_error(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            record = fixture.plan["chunks"][0]
            record["frame"]["size_mm"][0] = 29.0
            Path(record["frame_file"]).write_text(json.dumps(record["frame"]))
            fixture.write()
            report = validate_plan(fixture.plan_path)
            self.assertIn("FRAME_SIZE_WRONG_GRID_CELL", {issue["code"] for issue in report["issues"]})

    def test_gap_is_recomputed_from_emitted_polygon(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            record = fixture.plan["chunks"][0]
            shortened = box(
                fixture.origin[0], fixture.origin[1],
                fixture.origin[0] + fixture.cut_x[1] * fixture.step_ft - 1.0,
                fixture.origin[1] + fixture.cut_y[-1] * fixture.step_ft,
            )
            wgs = gpd.GeoSeries([shortened], crs=2263).to_crs(4326).iloc[0]
            Path(record["polygon_file"]).write_text(json.dumps(mapping(wgs)))
            fixture.write()
            report = validate_plan(fixture.plan_path)
            self.assertIn("COVERAGE_GAP", {issue["code"] for issue in report["issues"]})

    def test_overlap_is_recomputed_from_emitted_polygons(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            record = fixture.plan["chunks"][0]
            enlarged = box(
                fixture.origin[0], fixture.origin[1],
                fixture.origin[0] + fixture.cut_x[1] * fixture.step_ft + 1.0,
                fixture.origin[1] + fixture.cut_y[-1] * fixture.step_ft,
            )
            wgs = gpd.GeoSeries([enlarged], crs=2263).to_crs(4326).iloc[0]
            Path(record["polygon_file"]).write_text(json.dumps(mapping(wgs)))
            fixture.write()
            report = validate_plan(fixture.plan_path)
            self.assertIn("CHUNK_INTERIOR_OVERLAP", {issue["code"] for issue in report["issues"]})

    def test_authoritative_shared_command_option_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            record = fixture.plan["chunks"][1]
            index = record["argv"].index("--vertical-exaggeration") + 1
            record["argv"][index] = "2"
            record["command"] = shlex.join(record["argv"])
            fixture.write()
            report = validate_plan(fixture.plan_path)
            self.assertIn("COMMAND_SHARED_OPTION_MISMATCH", {issue["code"] for issue in report["issues"]})

    def test_subcell_polygon_has_descriptive_error(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            record = fixture.plan["chunks"][0]
            subcell = box(
                fixture.origin[0] + 0.05, fixture.origin[1] + 0.05,
                fixture.origin[0] + 0.15, fixture.origin[1] + 0.15,
            )
            wgs = gpd.GeoSeries([subcell], crs=2263).to_crs(4326).iloc[0]
            Path(record["polygon_file"]).write_text(json.dumps(mapping(wgs)))
            record["area_sq_ft"] = float(subcell.area)
            fixture.write()
            report = validate_plan(fixture.plan_path)
            self.assertIn(
                "CHUNK_HAS_NO_MANUFACTURING_CELLS",
                {issue["code"] for issue in report["issues"]},
            )

    def test_generated_height_jump_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            fixture.write_generated(fixture.plan["chunks"][0], 10.0)
            fixture.write_generated(fixture.plan["chunks"][1], 10.5)
            report = validate_plan(fixture.plan_path, check_generated=True, require_generated=True)
            self.assertIn("GENERATED_HEIGHT_SEAM_DISCONTINUITY", {issue["code"] for issue in report["issues"]})


if __name__ == "__main__":
    unittest.main()
