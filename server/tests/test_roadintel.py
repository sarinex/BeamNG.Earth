"""Offline Group 4 contracts, including real pinned netconvert (no simulator/game)."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from roadintel_ext.model import normalize, turn_lanes
from roadintel_ext.runtime import Dataset, MapView, effective_specs
from roadintel_ext.sumo import preprocess, convert, executable, region
import navgraph
import roads
import surface
from types import SimpleNamespace
from roadintel_fixtures import network, offline_server, close_server
from test_roads import features, FRAME, flat, rules


def spec(width=10):
    return roads.Spec(width, "paved", False, "primary")


class Layout(unittest.TestCase):
    def test_mapped_directional_complement_and_width_conflicts(self):
        s = normalize({"lanes": "5", "lanes:forward": "2", "width": "16"}, spec())
        self.assertEqual((s.forward, s.backward), (2, 3))
        for width in ("unknown", "7;9", "0", "100"):
            with self.subTest(width=width), self.assertRaises(ValueError):
                normalize({"lanes": "2", "width": width}, spec())
        with self.assertRaisesRegex(ValueError, "conflict"):
            normalize({"oneway": "yes", "lanes": "2", "width": "8", "width:lanes": "3|3"}, spec())

    def test_sidewalks_and_asymmetric_parking_follow_feature_reversal(self):
        tags = {"lanes": "2", "width": "9", "sidewalk": "left", "parking:right": "lane", "parking:right:width": "2"}
        a = normalize(tags, spec(), reverse=True)
        self.assertEqual((a.sidewalk_sides, a.parking_left, a.parking_right), ((-1,), 2, 0))
        sp = spec()._replace(intel=a)
        self.assertTrue(roads.has_sidewalks(sp))
        self.assertEqual(roads.walk_sides(sp), (-1,))
        b = normalize(dict(tags, sidewalk="no"), spec())
        self.assertFalse(roads.has_sidewalks(spec()._replace(intel=b)))

    def test_arrows_are_source_gated_stock_ribbons(self):
        from roadintel_ext.arrows import ribbons
        s = normalize({"lanes": "2", "oneway": "yes", "turn:lanes": "left|through;right"}, spec())
        pts = np.array([[0., 0., 0.], [100., 0., 0.]])
        self.assertEqual(ribbons(s, pts), [])
        rows = ribbons(replace(s, arrows_forward=True), pts)
        self.assertGreater(len(rows), 5)
        self.assertTrue(all(row[0]["m"] == "line_white" for row in rows))
        self.assertTrue(all(np.isfinite(row[2]).all() for row in rows))
        self.assertEqual(ribbons(replace(s, arrows_forward=True), pts * .1), [])
    def test_asymmetric_counts_widths_and_markings(self):
        s = normalize({"lanes": "3", "lanes:forward": "2", "lanes:backward": "1", "width": "9"}, spec())
        self.assertEqual((s.forward, s.backward, s.centre), (2, 1, 1.5))
        self.assertEqual(s.divider_offsets, (-1.5,))
        self.assertEqual(s.envelope_width, 9)
        self.assertEqual(s.driving_width, 9)
        road = spec()._replace(width=s.envelope_width, intel=s)
        self.assertEqual(navgraph.driving_width(road), 9)
        marks = roads.extras(road, np.array([[0., 0., 0.], [50., 0., 0.]]), -76)
        self.assertTrue(any(abs(row[2][0, 1] - 1.62) < 1e-6 for row in marks))

    def test_left_hand_and_reverse(self):
        tags = {"lanes:forward": "2", "lanes:backward": "1", "width": "9"}
        a = normalize(tags, spec(), left_hand=True)
        self.assertEqual(a.centre, -1.5)
        b = normalize(tags, spec(), reverse=True)
        self.assertEqual((b.forward, b.backward, b.centre), (1, 2, -1.5))
        for direction, reverse, expected in (("yes", False, 1), ("-1", False, -1), ("yes", True, -1)):
            a = normalize({"lanes": "2", "oneway": direction}, spec(), reverse=reverse)
            self.assertEqual(a.direction, expected)
            self.assertEqual(a.forward + a.backward, 2)
            self.assertIsNone(a.centre)

    def test_turn_lanes_and_lane_widths(self):
        a = normalize({"oneway": "yes", "lanes": "3", "turn:lanes": "left|through|through;right",
                       "width:lanes": "3|3.2|3.4"}, spec())
        self.assertEqual(a.turns_forward[2], ("through", "right"))
        self.assertAlmostEqual(a.envelope_width, 9.6)
        self.assertEqual(a.confidence, "mapped-lane-widths")
        with self.assertRaises(ValueError):
            turn_lanes("left|through", 3)

    def test_parking_shoulders_width_and_missing(self):
        a = normalize({"lanes": "2", "parking:right": "lane", "parking:right:width": "2",
                       "shoulder:left:width": "1", "width": "9"}, spec())
        self.assertEqual((a.parking_right, a.shoulder_left, a.driving_width), (2, 1, 6))
        self.assertEqual((a.drive_left, a.drive_right), (3.5, -2.5))
        self.assertIn("estimated", a.confidence)
        for tags in ({}, {"lanes": "3"}, {"lanes": "2;3"}, {"lanes": "4", "lanes:forward": "3", "lanes:backward": "2"},
                     {"lanes": "3", "lanes:both_ways": "1"}, {"lanes": "2", "access": "no"},
                     {"lanes": "2", "oneway": "reversible"}):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                normalize(tags, spec())

    def test_all_geometry_combinations_and_shared_surface(self):
        f = features([[(20, 100), (240, 100)]], ["primary"])
        original = roads.road_specs(f)[0]
        tagged = replace(f, attrs=dict(f.attrs), rules=dict(f.rules))
        tagged._roadintel = [{"tags": {"lanes": "3", "lanes:forward": "2", "width": "9"},
                              "reverse": False, "provenance": "fixture", "region": {"leftHand": False}}]
        for intelligence, feats in (("legacy", f), ("sumo", tagged)):
            expected = original.width if intelligence == "legacy" else 9
            for geometry in ("legacy", "geos"):
                chunks = roads.build_tile(FRAME, 0, 0, [feats], flat, backend=geometry)
                self.assertTrue(chunks)
                self.assertEqual(roads.road_specs(feats)[0].width, expected)
            paint = surface._road_segments(FRAME, 0, 0, 256, 256, 256, [feats])
            self.assertTrue(np.allclose(paint[1], expected / 2))
        self.assertIsNone(roads.road_specs(f)[0].intel)
        self.assertEqual(roads.road_specs(f)[0].width, original.width)


class NativeSUMO(unittest.TestCase):
    def test_pbf_full_tag_input(self):
        import osmium
        with tempfile.TemporaryDirectory() as work:
            xml, pbf, cache = Path(work) / "fixture.osm.xml", Path(work) / "fixture.osm.pbf", Path(work) / "cache"
            f = network(xml)
            xml.write_text(xml.read_text().replace('k="width" v="9.9"', 'k="width" v="9.9" /><tag k="cycleway:right" v="separate"'))
            with osmium.SimpleWriter(str(pbf)) as writer:
                class Copy(osmium.SimpleHandler):
                    def node(self, n): writer.add_node(n)
                    def way(self, w): writer.add_way(w)
                    def relation(self, r): writer.add_relation(r)
                Copy().apply_file(str(xml))
            path, _ = preprocess(pbf, cache, 39.2834, -76.5934)
            data = json.loads(path.read_text())
            self.assertEqual(data["ways"]["10"]["tags"]["cycleway:right"], "separate")
            self.assertEqual(data["diagnostics"]["restrictionRelations"], 1)
    def test_bounded_subprocess_is_killed_on_memory_limit(self):
        from roadintel_ext.sumo import _run_bounded
        from unittest.mock import Mock
        process = Mock(pid=123)
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryFile() as log:
            with patch("roadintel_ext.sumo.subprocess.Popen", return_value=process), patch("roadintel_ext.sumo._rss", return_value=3*1024**3):
                with self.assertRaisesRegex(ValueError, "memory budget"):
                    _run_bounded(["netconvert", "--output-file", str(Path(work) / "n.xml")], stdout=log, stderr=-2, timeout=90)
        process.kill.assert_called_once()
        process.wait.assert_called_once()

    def test_incomplete_relation_and_invalid_network_report_failure(self):
        from roadintel_ext.sumo import read_network
        with tempfile.TemporaryDirectory() as work:
            source, cache = Path(work) / "fixture.osm.xml", Path(work) / "cache"
            f = network(source)
            source.write_text(source.read_text().replace('type="way" ref="21" role="to"', 'type="way" ref="999" role="to"'))
            preprocess(source, cache, 39.2834, -76.5934)
            ds = Dataset(cache)
            f._roadintel = [ds.match(f, i) for i in range(len(f))]
            g = navgraph.build(FRAME, [f], flat, (120, 120))
            self.assertIn("incomplete", g["roadIntelligence"]["fallbackJunctions"]["c2"])
            invalid = Path(work) / "invalid.net.xml"
            invalid.write_text("<net>")
            import xml.sax
            import xml.etree.ElementTree as ET
            with self.assertRaises((xml.sax.SAXParseException, ET.ParseError, ValueError, RuntimeError)):
                read_network(invalid, {})


class Installation(unittest.TestCase):
    def test_pins_source_package_and_legacy_import_independence(self):
        import ast
        import importlib.util
        root = Path(__file__).resolve().parents[2]
        pins = (root / "server/requirements-roadintel-ext.txt").read_text()
        for package in ("eclipse-sumo==1.28.0", "sumolib==1.28.0", "osmium==4.3.2"):
            self.assertIn(package, pins)
        for path in (root / "server/roadintel_ext").glob("*.py"):
            ast.parse(path.read_text(), feature_version=(3, 10))
        result = subprocess.run([sys.executable, "-c", "import roads,sys; assert 'sumo' not in sys.modules; assert 'sumolib' not in sys.modules; assert 'osmium' not in sys.modules"],
                                cwd=root / "server", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        module_spec = importlib.util.spec_from_file_location("roadintel_package", root / "tools/package.py")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        files = module.collect()
        for name in ("server/roadintel_ext/runtime.py", "server/requirements-roadintel-ext.txt", "tools/prepare_roadintel.py", "docs/ROADINTEL_EVALUATION.md"):
            self.assertIn(name, files)


class NativeNetworkContracts(unittest.TestCase):
    def test_cache_update_pins_old_snapshot_and_corruption_falls_back(self):
        with tempfile.TemporaryDirectory() as work:
            source, cache = Path(work) / "fixture.osm.xml", Path(work) / "cache"
            f = network(source)
            first, _ = preprocess(source, cache, 39.2834, -76.5934)
            old = Dataset(cache)
            before = old.match(f, 0)
            source.write_text(source.read_text().replace('v="9.9"', 'v="10.2"'))
            second, _ = preprocess(source, cache, 39.2834, -76.5934)
            self.assertNotEqual(first, second)
            new = Dataset(cache)
            self.assertNotEqual(old.identity, new.identity)
            self.assertEqual(old.match(f, 0)["tags"]["width"], "9.9")
            self.assertEqual(new.match(f, 0)["tags"]["width"], "10.2")
            # SHA verification happens before parsing a fresh payload.
            second.write_text(second.read_text() + " ")
            broken = Dataset(cache)
            view = MapView(SimpleNamespace(cell=lambda *a, **kw: SimpleNamespace(layers={"roads": f})), broken)
            clone = view.cell(None).layers["roads"]
            self.assertIn("checksum", clone._roadintel[0]["error"])
            self.assertEqual(roads.road_specs(clone)[0].width, roads.road_specs(f)[0].width)

    def test_stable_junction_ids_after_frame_rebase_and_duplicate_cells(self):
        import geodesy
        with tempfile.TemporaryDirectory() as work:
            source, cache = Path(work) / "fixture.osm.xml", Path(work) / "cache"
            f = network(source)
            preprocess(source, cache, 39.2834, -76.5934)
            data = Dataset(cache)
            f._roadintel = [data.match(f, i) for i in range(len(f))]
            a = navgraph.build(FRAME, [f], flat, (0, 0))
            other = geodesy.EnuFrame(39.284, -76.594)
            b = navgraph.build(other, [f, f], flat, (0, 0))
            self.assertEqual({n for n in a["nodes"] if n.startswith("ri")}, {n for n in b["nodes"] if n.startswith("ri")})

    def test_ambiguous_spatial_matching_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            source, cache = Path(work) / "fixture.osm.xml", Path(work) / "cache"
            f = network(source)
            preprocess(source, cache, 39.2834, -76.5934)
            f.lonlat[:, 1] += .001
            with self.assertRaisesRegex(ValueError, "does not match"):
                Dataset(cache).match(f, 0)

    def test_conditional_restrictions_explicitly_fall_back(self):
        with tempfile.TemporaryDirectory() as work:
            source, cache = Path(work) / "fixture.osm.xml", Path(work) / "cache"
            f = network(source)
            source.write_text(source.read_text().replace('</relation>', '<tag k="restriction:conditional" v="no_left_turn @ (Mo-Fr 08:00-16:00)" /></relation>'))
            preprocess(source, cache, 39.2834, -76.5934)
            data = Dataset(cache)
            f._roadintel = [data.match(f, i) for i in range(len(f))]
            g = navgraph.build(FRAME, [f], flat, (0, 0))
            self.assertIn("conditional", g["roadIntelligence"]["fallbackJunctions"]["c2"])
            self.assertIn("c2", g["nodes"])
    def test_real_import_restriction_cache_and_graph(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "fixture.osm.xml"
            feats = network(source)
            output, hit = preprocess(source, Path(work) / "cache", 39.2834, -76.5934)
            self.assertFalse(hit)
            data = json.loads(output.read_text())
            self.assertEqual(data["diagnostics"]["restrictionRelations"], 1)
            self.assertFalse(any(c["from"].startswith("10") and c["to"].startswith("21") for c in data["connections"]))
            self.assertTrue(preprocess(source, Path(work) / "cache", 39.2834, -76.5934)[1])
            dataset = Dataset(Path(work) / "cache")
            base = SimpleNamespace(cell=lambda *a, **kw: SimpleNamespace(layers={"roads": feats}))
            view = MapView(base, dataset)
            f = view.cell(None).layers["roads"]
            self.assertTrue(all("error" not in r for r in f._roadintel), f._roadintel)
            graph = navgraph.build(FRAME, [f], flat, (120, 120))
            self.assertIn("c2", graph["roadIntelligence"]["sumoJunctions"], graph["roadIntelligence"])
            self.assertGreater(graph["roadIntelligence"]["laneConnections"], 0)
            self.assertNotIn("c2", graph["nodes"])
            self.assertTrue(all(e[2] == 1 for e in graph["edges"] if any(n.startswith("ri") for n in e[:2])))
            self.assertEqual(graph, navgraph.build(FRAME, [f, f], flat, (120, 120)))
            city = navgraph.build(FRAME, [f], flat, (120, 120), urban_fn=lambda la, lo: np.ones(np.shape(la), bool))
            self.assertTrue(city["signals"], "urban fixture exercises existing stop/signal controllers")
            self.assertIn("c2", city["roadIntelligence"]["sumoJunctions"])
            for signal in city["signals"]:
                self.assertIn(signal["controlNode"], city["nodes"])
                self.assertTrue(all(app[0] in city["nodes"] for app in signal["app"]))
            self.assertGreater(dataset.stats["hits"], 0)
            self.assertIsNone(roads.road_specs(feats)[0].intel)

    def test_complex_networks_roundabout_lht_and_grade_separation(self):
        for kind in ("T", "offset", "multiway", "ramp", "roundabout", "bridge"):
            for left_hand in (False, True):
                with self.subTest(kind=kind, left_hand=left_hand), tempfile.TemporaryDirectory() as work:
                    source = Path(work) / "fixture.osm.xml"
                    f = network(source, kind, restriction=False, left_hand=left_hand)
                    path, _ = preprocess(source, Path(work) / "cache", 39.2834, -76.5934, left_hand=left_hand)
                    data = json.loads(path.read_text())
                    f._roadintel = [Dataset(Path(work) / "cache").match(f, i, left_hand) for i in range(len(f))]
                    graph = navgraph.build(FRAME, [f], flat, (120, 120), left_hand=left_hand)
                    self.assertTrue(graph["edges"])
                    self.assertGreater(len(graph.get("roadIntelligence", {}).get("sumoJunctions", [])), 0)
                    if kind == "bridge":
                        self.assertFalse(any(data["edges"][c["from"]]["way"] in ("10", "11") and
                                             data["edges"][c["to"]]["way"] in ("20", "21") for c in data["connections"]))
                    if kind == "roundabout":
                        ring_edges = [e for e in data["edges"].values() if e["way"] == "30"]
                        self.assertTrue(ring_edges)
                        self.assertTrue(all(s.direction == 1 for s in [roads.road_specs(f)[-1].intel]))

    def test_missing_executable_timeout_invalid_output_and_bad_cache(self):
        with patch("roadintel_ext.sumo.executable", side_effect=FileNotFoundError("missing")):
            with self.assertRaises(FileNotFoundError):
                convert("missing.osm", "missing.net.xml")
        with patch("roadintel_ext.sumo.executable", return_value=Path("netconvert")):
            with self.assertRaisesRegex(ValueError, "timed out"):
                convert("x", "y", runner=lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("netconvert", 1)))
            with self.assertRaisesRegex(ValueError, "failed"):
                convert("x", "missing.xml", runner=lambda *a, **k: SimpleNamespace(returncode=1))
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaises(ValueError):
                Dataset(work)
            Path(work, "region_1.ref").write_text("../outside.json")
            with self.assertRaises(ValueError):
                Dataset(work)


class CoordinatedTerrain(unittest.TestCase):
    def test_server_rejects_old_legacy_anchor_during_rollback(self):
        from earthd import Connection
        selection = SimpleNamespace(awaiting_anchor=True, dataset=None, epoch=4)
        old = SimpleNamespace(road_epoch=0)
        conn = SimpleNamespace(active=3, teleport_pending=False, road_geometry=selection,
                               s=SimpleNamespace(anchors={1: old}))
        Connection.commit_anchor(conn, 1)
        self.assertTrue(selection.awaiting_anchor)
        self.assertEqual(conn.active, 3)

    def test_retry_cannot_remove_server_terrain_barrier(self):
        from roadgeom_ext.streaming import Selection
        selected = Selection()
        selected.awaiting_anchor = True
        sent = []
        conn = SimpleNamespace(roads=selected.tracker(), send=sent.append)
        selected.select(conn, {"version": 1, "backend": "legacy", "speed": 0})
        self.assertTrue(sent[-1]["ok"])
        self.assertTrue(sent[-1]["terrainRebuild"])
        self.assertTrue(selected.awaiting_anchor)
        selected.select(conn, {"version": 1, "backend": "legacy", "speed": 0, "rebuildTerrain": "false"})
        self.assertFalse(sent[-1]["ok"])

    def test_fresh_anchor_rebuilds_grading_masks_meshes_and_graph_without_mutating_legacy(self):
        import asyncio
        from PIL import Image
        from roadintel_ext.runtime import bind_anchor, server_for
        from test_roads import LAT, LON
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            source = root / "fixture.osm.xml"
            f = network(source)
            source.write_text(source.read_text().replace('k="lanes" v="3"', 'k="lanes" v="4"')
                              .replace('k="lanes:forward" v="2"', 'k="lanes:forward" v="3"')
                              .replace('v="9.9"', 'v="13.2"'))
            preprocess(source, root / "cache", LAT, LON)
            dataset = Dataset(root / "cache")
            server = offline_server(root, f)
            try:
                legacy, intelligent = server.new_anchor(LAT, LON), server.new_anchor(LAT, LON)
                legacy.q = intelligent.q = 1 / 32
                bind_anchor(server, intelligent, dataset)
                server.anchors.update({legacy.id: legacy, intelligent.id: intelligent})
                first = asyncio.run(server.terrain(legacy, 128, 128))
                second = asyncio.run(server.terrain(intelligent, 128, 128))
                self.assertNotEqual(first["ter"], second["ter"])
                self.assertIsNot(legacy.grader, intelligent.grader)
                self.assertIsNot(legacy.chunks, intelligent.chunks)
                self.assertIsNone(intelligent.grader.shared)
                profile = server_for(server, intelligent)
                self.assertEqual(profile.block_pools, [])
                self.assertIsNone(profile.road_pool)
                self.assertIsNone(profile.far_pool)
                self.assertIs(server_for(profile, legacy), server)
                original = roads.road_specs(f)[0]
                enriched = roads.road_specs(profile.mapdata.cell(None, layers=("roads",)).layers["roads"])[0]
                self.assertNotEqual(original.width, enriched.width)
                pavement = surface.ROAD_MATERIAL["paved"]
                a = np.array(Image.open(Path(server.out_dir, first["layers"][pavement])))
                b = np.array(Image.open(Path(server.out_dir, second["layers"][pavement])))
                self.assertFalse(np.array_equal(a, b), "friction/paint masks must rebuild at enriched widths")
                for backend in ("legacy", "geos"):
                    path, count = server.build_roads_tile(intelligent.id, 0, 0, backend=backend, namespace=backend)
                    self.assertGreater(count, 0)
                    data = json.loads(Path(server.out_dir, backend, "roads_2_0_0.json").read_text())
                    self.assertIn("roadIntelligence", data["roadGeometry"])
                graph_path, n, m = server.build_navgraph(intelligent.id, 120, 120, 1000001)
                graph = json.loads(Path(server.out_dir, "navgraph_2_1000001.json").read_text())
                self.assertGreater(n, 0)
                self.assertTrue(graph["roadIntelligence"]["sumoJunctions"])
                self.assertIs(asyncio.run(server.terrain(legacy, 128, 128)), first)
                self.assertIsNone(roads.road_specs(f)[0].intel)
            finally:
                close_server(server)


if __name__ == "__main__":
    unittest.main()
