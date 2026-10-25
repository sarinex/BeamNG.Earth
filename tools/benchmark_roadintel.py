"""Offline Group 4 A/B: actual netconvert, tiles/export, AI graphs and coordinated terrain.

Synthetic fixtures only. Does not access, launch, or automate BeamNG.drive.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "server" / "tests"), str(ROOT / "tools")]
from roadintel_ext.runtime import Dataset, MapView, bind_anchor
from roadintel_ext.sumo import preprocess, executable
from roadintel_fixtures import network, offline_server, close_server, FixtureMap
from test_roads import FRAME, LAT, LON, flat
from benchmark_roadgeom import tile, process_rss
import navgraph
import roads
import roadgeom_ext
import peerwork


def summary(samples):
    return {"p50Ms": statistics.median(samples), "p95Ms": float(np.percentile(samples, 95))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="build/roadintel-benchmark.json")
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 2:
        parser.error("--repeats must be at least 2")
    report = {"kind": "synthetic offline; in-game results NOT YET VALIDATED", "python": sys.version.split()[0],
              "sourceId": peerwork.code_id(), "geometryLibraries": roadgeom_ext.availability()[1], "logicalCPUs": os.cpu_count(),
              "platform": platform.platform(), "netconvert": str(executable()), "repeats": args.repeats,
              "versions": {"eclipse-sumo": "1.28.0", "sumolib": "1.28.0", "osmium": "4.3.2"},
              "fixtures": {}, "scope": "cold extract/SUMO conversion, warm region cache, road tile generation/export, navigation, terrain masks and grading; excludes downloads and engine frame times"}
    with tempfile.TemporaryDirectory() as work:
        root = Path(work)
        for kind, left_hand in [(k, False) for k in ("T", "cross", "offset", "multiway", "ramp", "roundabout", "bridge")] + [("cross", True), ("roundabout", True)]:
            key = kind + ("_left_hand" if left_hand else "")
            fixture = root / key
            fixture.mkdir()
            source = fixture / "fixture.osm.xml"
            f = network(source, kind, left_hand=left_hand)
            start = time.perf_counter()
            path, _ = preprocess(source, fixture / "cache", LAT, LON, left_hand=left_hand)
            cold = (time.perf_counter() - start) * 1000
            hits = []
            for _ in range(args.repeats):
                start = time.perf_counter()
                _, hit = preprocess(source, fixture / "cache", LAT, LON, left_hand=left_hand)
                hits.append((time.perf_counter() - start) * 1000)
                assert hit
            dataset = Dataset(fixture / "cache")
            enriched = MapView(FixtureMap(f), dataset, left_hand).cell(None, layers=("roads",)).layers["roads"]
            row = report["fixtures"][key] = {"preprocessingColdMs": cold, "preprocessingHit": summary(hits),
                                             "cacheBytes": path.stat().st_size, "combinations": {}}
            for intelligence, feat in (("legacy", f), ("sumo", enriched)):
                for geometry in ("legacy", "geos"):
                    out = fixture / f"{intelligence}_{geometry}"
                    out.mkdir()
                    tile(feat, geometry, out, left_hand=left_hand)
                    timings, graphs = [], []
                    for _ in range(args.repeats):
                        measured = tile(feat, geometry, out, left_hand=left_hand)
                        timings.append(measured["totalMs"])
                        start = time.perf_counter()
                        graph = navgraph.build(FRAME, [feat], flat, (120, 120), left_hand=left_hand)
                        graphs.append((time.perf_counter() - start) * 1000)
                    row["combinations"][f"{geometry}+{intelligence}"] = dict(summary(timings),
                        graph=summary(graphs), triangles=measured["triangles"], meshBytes=measured["meshBytes"],
                        graphNodes=len(graph["nodes"]), graphEdges=len(graph["edges"]),
                        intelligence=graph.get("roadIntelligence"),
                        sections=[s.intel.diagnostic() if s and s.intel else None for s in roads.road_specs(feat)],
                        diagnostics=measured["diagnostics"])
            row["regionCache"] = dataset.stats
            print(f'{key:23} convert {cold:7.1f} ms  hit {statistics.median(hits):5.2f} ms  '
                  + ' / '.join(f'{k}: {v["p50Ms"]:.2f} ms' for k, v in row["combinations"].items()), flush=True)
        # A genuinely rebuilt heightmap/mask set for every profile, not a cached terrain lookup.
        source = root / "terrain.osm.xml"
        f = network(source)
        preprocess(source, root / "terrain-cache", LAT, LON)
        dataset = Dataset(root / "terrain-cache")
        server = offline_server(root / "terrain", f)
        report["terrain"] = {}
        try:
            for intelligence in ("legacy", "sumo"):
                timings, stages = [], []
                for _ in range(args.repeats):
                    anchor = server.new_anchor(LAT, LON)
                    anchor.q = 1 / 32
                    bind_anchor(server, anchor, dataset if intelligence == "sumo" else None)
                    server.anchors[anchor.id] = anchor
                    start = time.perf_counter()
                    info = asyncio.run(server.terrain(anchor, 128, 128))
                    timings.append((time.perf_counter() - start) * 1000)
                    stages.append(info["stages"])
                report["terrain"][intelligence] = dict(summary(timings), samples=stages,
                    description="512x512 physics block, 2m samples, fresh anchor, synthetic DEM + landcover, graded roads and .ter/friction-mask writes")
        finally:
            close_server(server)
        report["processRssBytes"] = process_rss()
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    main()
