"""Optional geometry backend contracts; same unittest runner as the upstream tests."""
import os
import subprocess
import sys
import unittest
import json
import tempfile
import threading
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import Future
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import roadgeom_ext
import roadmesh
import roads
from test_roads import FRAME, LAT, LON, features, flat
from roadgeom_ext.streaming import Selection


def init_worker_fixture():
    import blockworker
    from test_blockworker import FakeMapData, FakeDem
    f = features([[(-300, 100), (300, 100)], [(120, -100), (120, 300)]], ['secondary', 'residential'])
    md = FakeMapData(f, roads.cells_of_tile(FRAME, 0, 0)[0])
    blockworker._s = SimpleNamespace(md=md, dem=FakeDem(), widths=None, surface=None, airports=None,
                                    sea=None, lock=threading.Lock(), bridges={},
                                    anchor=lambda aid, lat, lon: (None, None))


class Legacy(unittest.TestCase):
    def test_default_tile_and_mesh_equal_explicit_legacy(self):
        f = features([[(0, 20), (256, 20)], [(120, 0), (120, 250)]], ['secondary', 'residential'])
        default = roads.build_tile(FRAME, 0, 0, [f], flat)
        explicit = roads.build_tile(FRAME, 0, 0, [f], flat, backend='legacy')
        self.assertEqual(default, explicit)
        expected = roadmesh.build(FRAME, default, flat, (0, 0, 0))
        actual = roadgeom_ext.build_mesh('legacy', FRAME, explicit, flat, (0, 0, 0))
        for a, b in zip(expected, actual):
            np.testing.assert_equal(a, b)
    def test_dispatch_is_original_call(self):
        with mock.patch.object(roadmesh, "build", return_value=object()) as build:
            result = roadgeom_ext.build_mesh("legacy", "frame", [], "elev", (0, 0, 0), follow=True)
            self.assertIs(result, build.return_value)
            build.assert_called_once_with("frame", [], "elev", (0, 0, 0), follow=True)

    def test_import_without_optional_dependencies(self):
        code = '''
import sys, importlib.abc
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, *args):
        if name.split('.')[0] in ('shapely', 'mapbox_earcut'):
            raise ImportError('deliberately absent')
sys.meta_path.insert(0, Block())
import roadgeom_ext, roadmesh, earthd, blockworker
assert roadgeom_ext.build_mesh('legacy', None, [], None, (0,0,0)) is None
assert not roadgeom_ext.availability()[0]
'''
        subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(roadmesh.__file__), check=True)

    def test_validation(self):
        for name in roadgeom_ext.BACKENDS:
            self.assertEqual(roadgeom_ext.validate(name), name)
        with self.assertRaises(ValueError):
            roadgeom_ext.validate("auto")


@unittest.skipUnless(roadgeom_ext.availability()[0], 'optional GEOS dependencies absent')
class Geometry(unittest.TestCase):
    def test_real_worker_spawn_uses_backend_and_matches_local_shape_bytes(self):
        import blockworker
        with mock.patch.object(blockworker, '_s', None):
            init_worker_fixture()
            with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                args = (1, LAT, LON, 15, 0, 0, a, '/temp/x', True, .025)
                blockworker.roads_tile(*args, backend='geos')
                with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context('spawn'),
                                         initializer=init_worker_fixture) as pool:
                    pool.submit(blockworker.roads_tile, *args[:6], b, *args[7:], backend='geos').result(timeout=30)
                for name in os.listdir(a):
                    if name.endswith('.cdae'):
                        with open(os.path.join(a, name), 'rb') as f, open(os.path.join(b, name), 'rb') as g:
                            self.assertEqual(f.read(), g.read(), name)
                with open(os.path.join(a, 'roads_1_0_0.json')) as f:
                    data = json.load(f)
                self.assertGreater(data['roadGeometry']['earcutTriangles'], 0)
                self.assertEqual(data['roadGeometry'].get('fallbacks', 0), 0)

    def assert_mesh(self, mesh):
        self.assertIsNotNone(mesh)
        p, t, uv, mats, m = mesh
        self.assertTrue(np.isfinite(p).all())
        self.assertTrue(np.isfinite(uv).all())
        self.assertGreater(len(t), 0)
        self.assertGreaterEqual(t.min(), 0)
        self.assertLess(t.max(), len(p))
        normals = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
        self.assertTrue((normals[:, 2] > 1e-10).all())
        return p, t

    def region(self, chunks):
        import shapely
        return shapely.union_all([shapely.Polygon(xy[tri]) for c in chunks if 'geom' in c
                                  for xy, ts in [c['geom']] for tri in ts])

    def build(self, lines, **kw):
        f = features(lines, ['residential'] * len(lines), **kw)
        walks, stats = [], {}
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, lambda la, lo: np.ones(np.shape(la), bool),
                                  walks=walks, backend='geos', diagnostics=stats)
        self.assertEqual(stats.get('fallbacks', 0), 0, stats)
        self.assertGreater(stats.get('earcutTriangles', 0), 0, stats)
        mesh = roadgeom_ext.build_mesh('geos', FRAME, chunks, flat, (0, 0, 0), clip=roads.tile_bbox(0, 0), follow=True)
        self.assert_mesh(mesh)
        return chunks, walks, mesh, stats

    def test_junction_matrix_and_real_mesh_export(self):
        cases = {
            'T': [[(10, 100), (240, 100)], [(120, 100), (120, 240)]],
            'cross': [[(10, 100), (240, 100)], [(120, 10), (120, 240)]],
            'Y': [[(10, 100), (120, 100)], [(120, 100), (240, 220)], [(120, 100), (230, 10)]],
            'acute': [[(10, 100), (240, 100)], [(120, 100), (240, 120)]],
            'offset': [[(10, 100), (240, 100)], [(100, 10), (100, 100)], [(120, 100), (120, 240)]],
            'multiway': [[(120, 120), (120 + 100 * np.cos(a), 120 + 100 * np.sin(a))]
                         for a in np.linspace(0, 2 * np.pi, 7)[:-1]],
        }
        for name, lines in cases.items():
            with self.subTest(name=name):
                chunks, walks, mesh, stats = self.build(lines)
                import cdae
                with tempfile.TemporaryDirectory() as d:
                    path, _ = roadgeom_ext.write_tile(d, '/vfs', 1, 0, 0, chunks, walks, mesh,
                                                      backend='geos', diagnostics=stats)
                    with open(os.path.join(d, os.path.basename(path))) as f:
                        data = json.load(f)
                    self.assertIn('road', data)
                    self.assertIn('walkcol', data)
                    self.assertEqual(stats.get('fallbacks', 0), 0, stats)
                    self.assertTrue(cdae.read(os.path.join(d, 'road_1_0_0.cdae')))

    def test_roundabouts_keep_island_and_irregular_shape(self):
        import shapely
        for rx, ry in [(45, 45), (60, 28)]:
            angles = np.linspace(0, 2 * np.pi, 33)
            ring = [(120 + rx * np.cos(a), 120 + ry * np.sin(a)) for a in angles]
            chunks, _, _, stats = self.build([ring, [(120 + rx, 120), (240, 120)]])
            region = self.region(chunks)
            self.assertFalse(region.covers(shapely.Point(120, 120)))
            self.assertGreater(stats['holes'], 0)
            self.assertTrue(region.covers(shapely.Point(225, 120)))

    def test_connected_source_nodes_preserve_corner_ramps(self):
        cases = [
            [[(120,120),(300,120)], [(120,120),(120,300)], [(120,120),(-60,120)], [(120,120),(120,-60)]],
            [[(120,120),(120,300)], [(120,120),(-60,120)], [(120,120),(127,113)],
             [(127,113),(127,-60)], [(127,113),(300,113)]],
        ]
        for lines in cases:
            chunks, walks, mesh, stats = self.build(lines)
            corners = [w['corner'] for w in walks if 'corner' in w]
            self.assertEqual(len(corners), 4)
            self.assertGreater(sum(len(c.get('ramps', [])) for c in corners), 0)
            with tempfile.TemporaryDirectory() as tmp:
                roadgeom_ext.write_tile(tmp, '/test', 1, 0, 0, chunks, walks, mesh,
                                        backend='geos', diagnostics=stats)
                self.assertEqual(stats.get('fallbacks', 0), 0, stats)
                self.assertTrue(os.path.isfile(os.path.join(tmp, 'walkcol_1_0_0.cdae')))

    def test_concave_holes_invalid_and_duplicate_points(self):
        import shapely
        from roadgeom_ext.triangulation import triangulate, repair
        shapes = [shapely.Polygon([(0, 0), (8, 0), (8, 2), (2, 2), (2, 8), (0, 8)]),
                  shapely.Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], [[(2, 2), (8, 2), (8, 8), (2, 8)]]),
                  shapely.Polygon([(0, 0), (0, 0), (10, 10), (0, 10), (10, 0)]),
                  shapely.Polygon([(0, 0), (1e-8, 0), (10, 0), (10, 10), (0, 10)])]
        for shape in shapes:
            shape = repair(shape)
            p, t = triangulate(shape)
            covered = shapely.union_all(shapely.polygons(p[t]))
            self.assertLess(covered.symmetric_difference(shape).area, 1e-6)

    def test_adjacent_tiles_independent_and_deterministic(self):
        import shapely
        f = features([[(-100, 90), (700, 170)], [(256, 60), (256, 230)]], ['residential'] * 2)
        regions, meshes = [], []
        for tx in (0, 1, 0):
            chunks = roads.build_tile(FRAME, tx, 0, [f], flat, walks=[], backend='geos')
            regions.append(self.region(chunks))
            meshes.append(roadgeom_ext.build_mesh('geos', FRAME, chunks, flat, (0, 0, 0),
                                                 clip=roads.tile_bbox(tx, 0), follow=True))
        self.assertEqual(regions[0].wkb, regions[2].wkb)
        for a, b in zip(meshes[0], meshes[2]):
            np.testing.assert_equal(a, b)
        seam = shapely.LineString([(256, 0), (256, 256)])
        self.assertLess(regions[0].intersection(seam).symmetric_difference(regions[1].intersection(seam)).length, 1e-6)
        self.assertLess(regions[0].intersection(regions[1]).area, 1e-8)

    def test_grade_and_fixed_deck_are_preserved(self):
        from test_roadmesh import slope
        f = features([[(-10, 100), (270, 100)]], ['residential'])
        chunks = roads.build_tile(FRAME, 0, 0, [f], slope, walks=[], backend='geos')
        p, t = self.assert_mesh(roadgeom_ext.build_mesh('geos', FRAME, chunks, slope, (0, 0, 0), follow=True))
        self.assertGreater(np.ptp(p[:, 2]), 10)
        fixed = {'m': 'road_asphalt_2lane', 'n': [[20, 20, 50], [200, 20, 50]], 'w': 5, 'fixed': True, 'deck': True}
        mesh = roadgeom_ext.build_mesh('geos', FRAME, [fixed], flat, (0, 0, 0), follow=True)
        np.testing.assert_allclose(mesh[0][:, 2], 50 + roadmesh.DECK_SKIN)

    def test_elevated_road_does_not_join_ground(self):
        import shapely
        f = features([[(20, 100), (240, 100)], [(120, 20), (120, 240)]], ['residential'] * 2,
                     levels=[(1, 1)], flags=[(1, 'is_bridge')])
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, walks=[], backend='geos')
        region = self.region(chunks)
        self.assertFalse(region.covers(shapely.Point(120, 180)))

    def test_material_regions_do_not_overlap(self):
        f = features([[(10, 100), (240, 100)], [(120, 10), (120, 240)]], ['residential'] * 2,
                     surfaces=[(1, 'concrete')], widths=[(0, 6), (1, 16)])
        chunks = roads.build_tile(FRAME, 0, 0, [f], flat, walks=[], backend='geos')
        regions = [self.region([c]) for c in chunks if 'geom' in c]
        self.assertGreater(len(regions), 1)
        self.assertLess(regions[0].intersection(regions[1]).area, 1e-8)

    def test_geometry_failure_falls_back_with_reason(self):
        from roadgeom_ext import geos_backend
        stats = {}
        chunks = [{'m': 'line_white', 'w': .1, 'n': [[0, 0, 0], [10, 0, 0]]}]
        with mock.patch.object(geos_backend, 'repair', side_effect=ValueError('fixture failure')):
            spec = roads.Spec(6, 'paved', True, 'residential')
            result = geos_backend.surface_chunks([('a', spec, np.array([[0, 0, 0], [10, 0, 0]]), 0)],
                                                 chunks, [], (0, 0, 256, 256), stats)
        self.assertIs(result, chunks)
        self.assertEqual(stats['fallbacks'], 1)
        self.assertIn('fixture failure', stats['reason'])

    def test_empty_mesh_fallback_never_serializes_extension_arrays(self):
        chunks, _, _, _ = self.build([[(-10,100),(270,100)]])
        stats = {}
        with mock.patch('roadgeom_ext.geos_backend.build_mesh', return_value=None):
            roadgeom_ext.build_mesh('geos', FRAME, chunks, flat, (0,0,0), diagnostics=stats)
        self.assertEqual(stats['fallbacks'], 1)
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = roadgeom_ext.write_tile(tmp, '/test', 1, 0, 0, chunks, backend='geos')
            with open(os.path.join(tmp, os.path.basename(path))) as f:
                data = json.load(f)
            self.assertFalse(any('geom' in c for c in data['chunks']))

    def test_sidewalk_overlap_union_preserves_plane_uv_and_collision(self):
        from roadgeom_ext.sidewalks import disjoint_slabs
        import shapely
        p = np.array([[0,0,1], [10,0,2], [0,10,1], [2,0,1.2], [12,0,2.2], [2,10,1.2]], float)
        t = np.array([[0,1,2], [3,4,5]])
        uv = p[:, :2] / 10
        result = disjoint_slabs(p, t, uv, np.array([1,1]))
        pieces = shapely.polygons(result[0][result[1], :2])
        self.assertAlmostEqual(shapely.area(pieces).sum(), shapely.union_all(pieces).area, places=7)
        expected = shapely.union_all(shapely.polygons(p[t, :2]))
        self.assertLess(expected.symmetric_difference(shapely.union_all(pieces)).area, 1e-7)
        np.testing.assert_allclose(result[0][:,2], 1 + .1 * result[0][:,0])
        np.testing.assert_allclose(result[2], result[0][:,:2] / 10)


class Switching(unittest.TestCase):
    def connection(self):
        self.messages = []
        self.selection = Selection()
        return SimpleNamespace(roads=self.selection.tracker(), send=self.messages.append,
                               s=SimpleNamespace(out_dir='unused'))

    def select(self, conn, name):
        self.selection.select(conn, {'version': 1, 'backend': name, 'speed': 0})

    def test_repeated_switch_and_stale_workers(self):
        conn = self.connection()
        for backend in ['geos', 'legacy', 'geos', 'legacy']:
            old = conn.roads
            old.aid = 1
            old.building.add((0, 0))
            f = Future()
            with mock.patch('roadgeom_ext.streaming.availability', return_value=(True, 'ok')):
                self.select(conn, backend)
            f.set_result(('/old/roads_1_0_0.json', 2))
            old._built(conn, 1, (0, 0), f)
            self.assertEqual(old.stale, 1)
            self.assertTrue(self.messages[-1]['ok'])
            self.assertEqual(self.messages[-1]['backend'], backend)
            self.assertIn(backend, self.selection.namespace)
        self.assertEqual(self.selection.epoch, 4)

    def test_missing_dependency_rejected_without_state_change(self):
        conn = self.connection()
        with mock.patch('roadgeom_ext.streaming.availability', return_value=(False, 'missing earcut')):
            self.select(conn, 'geos')
        self.assertFalse(self.messages[-1]['ok'])
        self.assertEqual(self.selection.backend, 'legacy')
        self.assertIn('missing earcut', self.messages[-1]['error'])

    def test_connections_and_captured_jobs_are_independent(self):
        conn = self.connection()
        other = Selection()
        server = SimpleNamespace(build_roads_tile=mock.Mock())
        before = self.selection.builder(server)
        with mock.patch('roadgeom_ext.streaming.availability', return_value=(True, 'ok')):
            self.select(conn, 'geos')
        before(1, 0, 0)
        self.assertEqual(server.build_roads_tile.call_args.kwargs['backend'], 'legacy')
        self.assertEqual(other.backend, 'legacy')
        self.assertNotEqual(other.namespace, self.selection.namespace)

    def test_invalid_and_moving_requests(self):
        conn = self.connection()
        for backend, speed in [('bad', 0), ('legacy', 30), ('legacy', float('nan'))]:
            self.selection.select(conn, {'version': 1, 'backend': backend, 'speed': speed})
            self.assertFalse(self.messages[-1]['ok'])
        self.assertEqual(self.selection.epoch, 0)

    def test_namespaced_files_are_inlined_and_pooled_with_real_connection(self):
        conn = self.connection()
        with tempfile.TemporaryDirectory() as root:
            conn.s.out_dir = root
            conn.s.pool_shapes = mock.Mock(return_value={'road': ['/pool/slot_001.cdae', 8]})
            folder = os.path.join(root, self.selection.namespace)
            os.makedirs(folder)
            with open(os.path.join(folder, 'roads_1_0_0.json'), 'w') as f:
                json.dump({'chunks': [], 'roadGeometry': {'earcutTriangles': 2}}, f)
            conn.roads.aid = 1
            fut = Future(); fut.set_result(('/vfs/' + self.selection.namespace + '/roads_1_0_0.json', 1))
            conn.roads._built(conn, 1, (0,0), fut)
            self.assertIn('data', self.messages[-1])
            self.assertEqual(self.messages[-1]['roadEpoch'], 0)
            self.assertEqual(self.messages[-1]['slots']['road'][1], 8)
            owner, files = conn.s.pool_shapes.call_args.args
            self.assertIs(owner, conn)
            self.assertTrue(files['road'].startswith(self.selection.namespace + '/'))


if __name__ == "__main__":
    unittest.main()
