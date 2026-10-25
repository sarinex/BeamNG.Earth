#!/usr/bin/env python3
"""Offline synthetic tile A/B, including existing mesh export. No downloads or game required.

Run with the prepared Python: .venv/Scripts/python.exe tools/benchmark_roadgeom.py --out build/roadgeom-benchmark.json
Uses the repository's deterministic road fixtures (source checkout, not the player ZIP).
"""
import argparse
import json
import os
import platform
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tracemalloc

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'server'), str(ROOT / 'server' / 'tests')]
import roads
import roadgeom_ext
from test_roads import FRAME, features, flat


def process_rss():
    """Process working set, including native libraries (snapshot, not per-tile allocation)."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('peak', 'working', 'peakPaged', 'paged',
                                                    'peakNonpaged', 'nonpaged', 'pagefile', 'peakPagefile')]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        current = ctypes.windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE
        memory = ctypes.windll.psapi.GetProcessMemoryInfo
        memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        memory.restype = wintypes.BOOL
        if memory(current(), ctypes.byref(counters), counters.cb):
            return counters.working
    elif sys.platform.startswith('linux'):
        return int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    return None


def fixtures():
    angles = np.linspace(0, 2 * np.pi, 33)
    return {
        'T': [[(120,100),(-30,100)], [(120,100),(290,100)], [(120,100),(120,280)]],
        'cross': [[(120,120),(300,120)], [(120,120),(120,300)], [(120,120),(-60,120)], [(120,120),(120,-60)]],
        'acute': [[(120,100),(-30,100)], [(120,100),(290,100)], [(120,100),(290,130)]],
        'offset': [[(120,120),(120,300)], [(120,120),(-60,120)], [(120,120),(127,113)],
                   [(127,113),(127,-60)], [(127,113),(300,113)]],
        'multiway': [[(120, 120), (120 + 180 * np.cos(a), 120 + 180 * np.sin(a))]
                     for a in np.linspace(0, 2 * np.pi, 7)[:-1]],
        'irregular_roundabout': [[(120 + 60 * np.cos(a), 120 + 28 * np.sin(a)) for a in angles],
                                 [(180, 120), (280, 120)]],
    }


def tile(feats, backend, out):
    start = time.perf_counter()
    walks, diagnostics = [], {} if backend == 'geos' else None
    chunks = roads.build_tile(FRAME, 0, 0, [feats], flat, lambda la, lo: np.ones(np.shape(la), bool),
                              walks=walks, backend=backend, diagnostics=diagnostics)
    mesh = roadgeom_ext.build_mesh(backend, FRAME, chunks, flat, (0, 0, 0),
                                   clip=roads.tile_bbox(0, 0), follow=True, q=1/32, diagnostics=diagnostics)
    roadgeom_ext.write_tile(out, '/temp/benchmark', 1, 0, 0, chunks, walks, mesh,
                            backend=backend, diagnostics=diagnostics)
    elapsed = (time.perf_counter() - start) * 1000
    return {'totalMs': elapsed, 'vertices': len(mesh[0]) if mesh else 0,
            'triangles': len(mesh[1]) if mesh else 0,
            'meshBytes': sum(p.stat().st_size for p in Path(out).glob('*.cdae')),
            'diagnostics': diagnostics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', default='build/roadgeom-benchmark.json')
    parser.add_argument('--repeats', type=int, default=7)
    args = parser.parse_args()
    if args.repeats < 2:
        parser.error('--repeats must be at least 2')
    ready, reason = roadgeom_ext.availability()
    if not ready:
        parser.error(reason)
    result = {'kind': 'synthetic offline, not in-game', 'python': sys.version.split()[0], 'libraries': reason,
              'platform': platform.platform(), 'logicalCPUs': os.cpu_count(),
              'version': roadgeom_ext.VERSION, 'repeats': args.repeats, 'roadsPaintedInGround': roads.ROADS_IN_GROUND,
              'scope': 'road tile generation + terrain following + sidewalk/collision and cdae export; excludes terrain imagery, downloads and game loading',
              'fixtures': {}}
    for name, lines in fixtures().items():
        feat = features(lines, ['residential'] * len(lines))
        result['fixtures'][name] = {}
        for backend in roadgeom_ext.BACKENDS:
            with tempfile.TemporaryDirectory() as out:
                tile(feat, backend, out)  # warm dependencies/native code
                samples = [tile(feat, backend, out) for _ in range(args.repeats)]
                tracemalloc.start()
                tile(feat, backend, out)
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            times = sorted(row['totalMs'] for row in samples)
            row = samples[-1]
            row.update(p50Ms=statistics.median(times), p95Ms=float(np.percentile(times, 95)), pythonPeakBytes=peak,
                       processRssBytes=process_rss())
            del row['totalMs']
            result['fixtures'][name][backend] = row
            print(f'{name:24} {backend:6} p50 {row["p50Ms"]:7.2f} ms  p95 {row["p95Ms"]:7.2f} ms  '
                  f'{row["triangles"]:5} road tris  {row["meshBytes"]:8} bytes', flush=True)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
