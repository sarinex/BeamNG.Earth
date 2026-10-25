"""Preprocess one deterministic region from a local OSM PBF/XML extract; never starts SUMO simulation."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from roadintel_ext.sumo import preprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("extract", type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--lat", required=True, type=float)
    p.add_argument("--lon", required=True, type=float)
    p.add_argument("--left-hand", action="store_true")
    p.add_argument("--timeout", type=float, default=90)
    args = p.parse_args()
    if not -85 <= args.lat <= 85 or not -180 <= args.lon < 180 or not 1 <= args.timeout <= 300:
        p.error("coordinates or timeout outside supported limits")
    start = time.perf_counter()
    with ThreadPoolExecutor(1, thread_name_prefix="roadintel-offline") as pool:
        job = pool.submit(preprocess, args.extract, args.out, args.lat, args.lon,
                          left_hand=args.left_hand, timeout=args.timeout)
        path, hit = job.result()
    print(f"{'cache hit' if hit else 'prepared'} {path} ({time.perf_counter()-start:.3f}s)")


if __name__ == "__main__":
    main()
