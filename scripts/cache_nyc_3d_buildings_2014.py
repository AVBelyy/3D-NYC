#!/usr/bin/env python3
"""Precompute NYC CityGML building footprints and roof polygons by archive member."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import zipfile
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from lxml import etree
from shapely.geometry import Polygon, box

from cache_common import (
    CACHE_FORMAT_VERSION,
    CRS,
    DATA,
    DEFAULT_CACHE_ROOT,
    DEFAULT_COVERAGE,
    Progress,
    atomic_geoparquet,
    atomic_json,
    file_signature,
    finish_manifest,
    load_coverage,
    output_record,
    parse_bounds,
    reusable_manifest,
    spatial_sort,
    start_manifest,
)


PIPELINE_VERSION = 1
GML_NAMESPACE = "http://www.opengis.net/gml"
BUILDING_COLUMNS = (
    "gml_id", "doitt_id", "bin", "roof_count", "roof_levels",
    "z_min_ft", "z_max_ft", "source_member_order", "source_building_order",
)
ROOF_COLUMNS = (
    "gml_id", "doitt_id", "bin", "kind", "z_min_ft", "z_max_ft",
    "source_member_order", "source_building_order", "source_surface_order",
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--source", type=Path, default=DATA / "raw/nyc_3d_buildings_2014/DA_WISE_GML.zip")
    value.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    value.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    value.add_argument("--bounds", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
    value.add_argument("--hash-source", action="store_true")
    value.add_argument("--skip-output-hashes", action="store_true")
    value.add_argument("--force", action="store_true")
    value.add_argument("--limit-members", type=int, help=argparse.SUPPRESS)
    return value


def coordinates(element) -> list[np.ndarray]:
    result = []
    for position in element.iter(f"{{{GML_NAMESPACE}}}posList"):
        if not position.text:
            continue
        values = np.fromstring(position.text, sep=" ")
        if values.size >= 9 and values.size % 3 == 0:
            result.append(values.reshape(-1, 3))
    return result


def empty_frame(columns: tuple[str, ...]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {column: pd.Series(dtype="object") for column in columns},
        geometry=gpd.GeoSeries([], crs=CRS), crs=CRS,
    )


def member_slug(name: str) -> str:
    stem = Path(name).stem.replace(" ", "_")
    digest = hashlib.sha1(name.encode()).hexdigest()[:8]
    return f"{stem}-{digest}"


def valid_sidecar(
    sidecar: Path,
    component_dir: Path,
    configuration: dict[str, Any],
    sources: dict[str, Any],
    member: zipfile.ZipInfo,
) -> dict[str, Any] | None:
    if not sidecar.exists():
        return None
    try:
        value = json.loads(sidecar.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    expected_member = {"name": member.filename, "crc": member.CRC, "bytes": member.file_size}
    if value.get("configuration") != configuration or value.get("sources") != sources or value.get("member") != expected_member:
        return None
    for output in value.get("outputs", []):
        path = component_dir / output["path"]
        if not path.is_file() or path.stat().st_size != output["bytes"]:
            return None
    return value


def parse_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    member_order: int,
    coverage_bounds: tuple[float, float, float, float],
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, int]:
    xmin, ymin, xmax, ymax = coverage_bounds
    buildings: list[dict[str, Any]] = []
    roofs_output: list[dict[str, Any]] = []
    scanned = 0
    progress = Progress(f"GML {member_order + 1}", unit="buildings")
    with archive.open(member) as source:
        iterator = etree.iterparse(source, events=("end",), tag="{*}Building", huge_tree=True)
        for _, element in iterator:
            scanned += 1
            namespace = etree.QName(element).namespace
            attrs = {
                attribute.get("name"): attribute.findtext("{*}value")
                for attribute in element.findall("{*}stringAttribute")
            }
            roofs = element.findall(f".//{{{namespace}}}RoofSurface")
            grounds = element.findall(f".//{{{namespace}}}GroundSurface")
            walls = element.findall(f".//{{{namespace}}}WallSurface")
            ground_coordinates = [array for ground in grounds for array in coordinates(ground)]
            near = False
            if ground_coordinates:
                combined = np.concatenate(ground_coordinates)
                near = (
                    combined[:, 0].max() >= xmin and combined[:, 0].min() <= xmax
                    and combined[:, 1].max() >= ymin and combined[:, 1].min() <= ymax
                )
            if near:
                gml_id = element.get(f"{{{GML_NAMESPACE}}}id")
                roof_z: list[float] = []
                all_z: list[float] = []
                footprints = []
                surface_order = 0
                for kind, objects in (("roof", roofs), ("ground", grounds), ("wall", walls)):
                    for obj in objects:
                        for polygon in obj.iter(f"{{{GML_NAMESPACE}}}Polygon"):
                            exterior = polygon.find(
                                f".//{{{GML_NAMESPACE}}}exterior//{{{GML_NAMESPACE}}}posList"
                            )
                            if exterior is None or not exterior.text:
                                continue
                            values = np.fromstring(exterior.text, sep=" ")
                            if values.size < 9 or values.size % 3:
                                continue
                            outer = values.reshape(-1, 3)
                            holes = []
                            for position in polygon.findall(
                                f".//{{{GML_NAMESPACE}}}interior//{{{GML_NAMESPACE}}}posList"
                            ):
                                values = np.fromstring(position.text or "", sep=" ")
                                if values.size >= 9 and values.size % 3 == 0:
                                    holes.append(values.reshape(-1, 3))
                            all_z.extend(outer[:, 2].tolist())
                            if kind == "roof":
                                roof_z.extend(outer[:, 2].tolist())
                                roofs_output.append({
                                    "gml_id": gml_id,
                                    "doitt_id": attrs.get("DOITT_ID"),
                                    "bin": attrs.get("BIN"),
                                    "kind": "roof",
                                    "z_min_ft": float(outer[:, 2].min()),
                                    "z_max_ft": float(outer[:, 2].max()),
                                    "source_member_order": member_order,
                                    "source_building_order": scanned,
                                    "source_surface_order": surface_order,
                                    "geometry": Polygon(outer, holes),
                                })
                            elif kind == "ground":
                                footprint = Polygon(outer[:, :2], [hole[:, :2] for hole in holes])
                                footprints.append(shapely.make_valid(footprint))
                            surface_order += 1
                if footprints and all_z:
                    buildings.append({
                        "gml_id": gml_id,
                        "doitt_id": attrs.get("DOITT_ID"),
                        "bin": attrs.get("BIN"),
                        "roof_count": len(roofs),
                        "roof_levels": len(set(np.round(roof_z, 1))),
                        "z_min_ft": min(all_z),
                        "z_max_ft": max(all_z),
                        "source_member_order": member_order,
                        "source_building_order": scanned,
                        "geometry": shapely.union_all(footprints),
                    })
            element.clear()
            while element.getprevious() is not None:
                del element.getparent()[0]
            progress.update(scanned, detail=f"kept={len(buildings):,} roofs={len(roofs_output):,}")
    progress.close(detail=f"kept={len(buildings):,} roofs={len(roofs_output):,}")
    building_frame = (
        gpd.GeoDataFrame(buildings, geometry="geometry", crs=CRS)
        if buildings else empty_frame(BUILDING_COLUMNS)
    )
    roof_frame = (
        gpd.GeoDataFrame(roofs_output, geometry="geometry", crs=CRS)
        if roofs_output else empty_frame(ROOF_COLUMNS)
    )
    return spatial_sort(building_frame), spatial_sort(roof_frame), scanned


def main() -> None:
    args = parser().parse_args()
    source = args.source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    bounds = parse_bounds(args.bounds)
    coverage = load_coverage(args.coverage.resolve(), bounds)
    coverage_bounds = tuple(map(float, coverage.bounds))
    component_dir = args.cache_root.resolve() / "nyc_3d_buildings_2014"
    members_dir = component_dir / "members"
    members_dir.mkdir(parents=True, exist_ok=True)
    source_signature = file_signature(source, with_hash=args.hash_source)
    sources = {"citygml_zip": source_signature}
    if bounds is None:
        sources["coverage_catalog"] = file_signature(args.coverage.resolve())
    configuration = {
        "pipeline_version": PIPELINE_VERSION,
        "cache_format_version": CACHE_FORMAT_VERSION,
        "crs": CRS,
        "coverage_bounds": list(coverage_bounds),
        "coverage_catalog": None if bounds else str(args.coverage.resolve()),
        "coverage_mode": "catalog_envelope",
        "retained_surfaces": ["roof"],
        "building_footprint_source": "GroundSurface",
        "limit_members": args.limit_members,
    }
    if not args.force:
        existing = reusable_manifest(component_dir, configuration, sources)
        if existing:
            print(
                f"CityGML cache is current: {component_dir} "
                f"({existing['buildings']:,} buildings, {existing['roofs']:,} roofs)"
            )
            return
    manifest = start_manifest(component_dir, "nyc_3d_buildings_2014", configuration, sources)
    started = time.monotonic()
    outputs: list[dict[str, Any]] = []
    catalog_rows = []
    total_buildings = total_roofs = total_scanned = 0

    with zipfile.ZipFile(source) as archive:
        members = sorted(
            (member for member in archive.infolist() if member.filename.lower().endswith(".gml")),
            key=lambda member: member.filename,
        )
        if args.limit_members:
            members = members[: args.limit_members]
        overall = Progress("GML members", len(members), unit="members")
        for member_order, member in enumerate(members):
            slug = member_slug(member.filename)
            sidecar = members_dir / f"{slug}.json"
            cached = None if args.force else valid_sidecar(
                sidecar, component_dir, configuration, sources, member
            )
            if cached:
                record = cached
            else:
                buildings, roofs, scanned = parse_member(
                    archive, member, member_order, coverage_bounds
                )
                building_path = members_dir / f"{slug}-buildings.parquet"
                roof_path = members_dir / f"{slug}-roofs.parquet"
                atomic_geoparquet(buildings, building_path)
                atomic_geoparquet(roofs, roof_path)
                member_outputs = [
                    output_record(building_path, component_dir, with_hash=not args.skip_output_hashes),
                    output_record(roof_path, component_dir, with_hash=not args.skip_output_hashes),
                ]
                geometries = [frame for frame in (buildings, roofs) if not frame.empty]
                if geometries:
                    all_bounds = np.vstack([frame.total_bounds for frame in geometries])
                    member_bounds = [
                        float(all_bounds[:, 0].min()), float(all_bounds[:, 1].min()),
                        float(all_bounds[:, 2].max()), float(all_bounds[:, 3].max()),
                    ]
                else:
                    member_bounds = list(coverage_bounds)
                record = {
                    "configuration": configuration,
                    "sources": sources,
                    "member": {"name": member.filename, "crc": member.CRC, "bytes": member.file_size},
                    "outputs": member_outputs,
                    "building_path": member_outputs[0]["path"],
                    "roof_path": member_outputs[1]["path"],
                    "buildings": len(buildings), "roofs": len(roofs), "scanned": scanned,
                    "bounds": member_bounds,
                }
                atomic_json(sidecar, record)
            outputs.extend(record["outputs"])
            outputs.append(output_record(sidecar, component_dir, with_hash=not args.skip_output_hashes))
            total_buildings += record["buildings"]
            total_roofs += record["roofs"]
            total_scanned += record["scanned"]
            catalog_rows.append({
                "member": member.filename,
                "building_path": record["building_path"],
                "roof_path": record["roof_path"],
                "buildings": record["buildings"], "roofs": record["roofs"],
                "geometry": box(*record["bounds"]),
            })
            overall.update(member_order + 1, detail=f"buildings={total_buildings:,} roofs={total_roofs:,}")
        overall.close(detail=f"buildings={total_buildings:,} roofs={total_roofs:,}")

    catalog = (
        gpd.GeoDataFrame(catalog_rows, geometry="geometry", crs=CRS)
        if catalog_rows else gpd.GeoDataFrame(
            {"member": [], "building_path": [], "roof_path": [], "buildings": [], "roofs": []},
            geometry=gpd.GeoSeries([], crs=CRS), crs=CRS,
        )
    )
    catalog_path = component_dir / "catalog.geojson"
    temporary = component_dir / "catalog.in_progress.geojson"
    catalog.to_file(temporary, driver="GeoJSON")
    os.replace(temporary, catalog_path)
    outputs.append(output_record(catalog_path, component_dir, with_hash=not args.skip_output_hashes))
    completed = finish_manifest(
        component_dir,
        manifest,
        outputs,
        members=len(catalog_rows),
        scanned_buildings=total_scanned,
        buildings=total_buildings,
        roofs=total_roofs,
        elapsed_seconds=time.monotonic() - started,
        production_ready=args.limit_members is None and bounds is None,
    )
    print(
        f"Completed CityGML cache: {component_dir}\n"
        f"  buildings: {total_buildings:,}\n"
        f"  roof polygons: {total_roofs:,}\n"
        f"  size: {completed['output_bytes'] / 1024**3:.2f} GiB"
    )


if __name__ == "__main__":
    main()
