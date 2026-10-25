"""Ground pavement unions; bridge/tunnel surfaces and markings retain upstream contracts."""
import logging
import time

import numpy as np
import shapely

from .triangulation import repair, triangulate

log = logging.getLogger("earthd.roadgeom")


def surface_chunks(roads_near, chunks, corners, box, stats):
    """Full-context ground centrelines + upstream curb-return infill -> disjoint polygon chunks.

    roads_near contains only roads.ground_parts: grade-separated bridges/tunnels never participate.
    Actual mapped ring buffers retain islands; no convex hulls or circular approximations.
    """
    import roads
    groups = {}
    t0 = time.perf_counter()
    try:
        for fid, spec, pts, _ in sorted(roads_near, key=lambda r: (r[0], tuple(r[2][0]))):
            if roads.terrain_surface(spec) or spec.kind not in roads.BUILT:
                continue
            look = roads.look_of(spec)
            # The stock tileable road material avoids repeating a decal's transparent shoulders across a junction.
            key = ("road_asphalt" if look["m"] == "road_asphalt_2lane" else look["m"], look["tl"])
            xy = np.round(np.asarray(pts)[:, :2], 3)
            xy = xy[np.r_[True, np.any(np.diff(xy, axis=0) != 0, axis=1)]]
            if len(xy) < 2 or not np.isfinite(xy).all() or spec.width <= 0:
                stats["invalid"] = stats.get("invalid", 0) + 1
                continue
            # Flat caps preserve mapped end positions. Round joins bound acute mitres without spikes.
            poly = shapely.LineString(xy).buffer(spec.width / 2, cap_style="flat", join_style="round", quad_segs=8)
            groups.setdefault(key, []).append(repair(poly))
        for c in corners:
            for fill in ([c["fill"]] if c.get("fill") is not None else []) + c.get("fills", []):
                look = roads._corner_look(c["arc"][len(c["arc"]) // 2], roads_near)
                material = "road_asphalt" if look["m"] == "road_asphalt_2lane" else look["m"]
                groups.setdefault((material, look["tl"]), []).append(repair(shapely.Polygon(np.asarray(fill)[:, :2])))
        occupied = shapely.Polygon()
        generated = []
        for (material, repeat), parts in sorted(groups.items()):
            union = repair(shapely.union_all(parts))
            # Snap inputs, then use double precision for the final overlay. Rounding an overlay's new
            # intersections back onto a diagonal boundary can introduce a thin overlapping sliver.
            region = shapely.set_precision(union, 0).intersection(shapely.box(*box)).difference(occupied)
            xy, tri = triangulate(region)
            occupied = occupied.union(region)
            if len(tri):
                generated.append({"m": material, "tl": repeat, "n": [], "w": 0,
                                  "geom": (xy, tri)})
                stats["polygons"] = stats.get("polygons", 0) + len(shapely.get_parts(region))
                stats["holes"] = stats.get("holes", 0) + sum(len(p.interiors) for p in shapely.get_parts(region))
                stats["earcutTriangles"] = stats.get("earcutTriangles", 0) + len(tri)
        # Only replace ground base pavement, preserving fixed decks, tubes, gutters and markings.
        materials = {key[0] for key in groups}
        if "road_asphalt" in materials:
            materials.add("road_asphalt_2lane")
        kept = [c for c in chunks if c.get("fixed") or c.get("deck") or c.get("tunnel")
                or c["m"] not in materials]
        stats["geosCorners"] = len(corners)
        stats["backend"] = "geos"
        stats["footprintMs"] = round((time.perf_counter() - t0) * 1000, 3)
        if generated:
            generated[0]["_legacy"] = chunks  # same list, for a controlled per-tile mesh fallback
        return kept + generated
    except Exception as exc:
        stats["fallbacks"] = stats.get("fallbacks", 0) + 1
        stats["reason"] = f"footprint: {type(exc).__name__}: {exc}"
        log.warning("GEOS pavement fallback: %s", stats["reason"], exc_info=True)
        return chunks


def build_mesh(frame, chunks, elev, origin, lift=0.0, clip=None, follow=False, q=None, drawn=None):
    import roadmesh
    regular = [c for c in chunks if "geom" not in c]
    mesh = roadmesh.build(frame, regular, elev, origin, lift, clip, follow, q, drawn)
    parts = [] if mesh is None else [mesh]
    for c in chunks:
        if "geom" not in c:
            continue
        xy, tri = c["geom"]
        uv = xy / np.array([8.0, float(c.get("tl") or 10)])
        if follow:
            grid, i0, j0 = roadmesh._height_grid(frame, drawn or elev, xy, q)
            p, uv, tri, _ = roadmesh.follow_ground(xy, uv, tri, grid, i0, j0)
            p[:, 2] += lift
        else:
            la, lo, _ = frame.enu_to_geodetic(xy[:, 0], xy[:, 1], 0.0)
            z = frame.geodetic_to_enu(la, lo, elev(la, lo))[2]
            p = np.c_[xy, z + lift]
        if len(tri):
            parts.append((p - np.asarray(origin), tri, uv, [c["m"]], np.zeros(len(tri), dtype=int)))
    return merge(parts)


def merge(parts):
    if not parts:
        return None
    ps, ts, us, ms, materials, count = [], [], [], [], [], 0
    for p, t, uv, mats, m in parts:
        mapping = []
        for name in mats:
            if name not in materials:
                materials.append(name)
            mapping.append(materials.index(name))
        ps.append(p); ts.append(t + count); us.append(uv); ms.append(np.asarray(mapping)[m])
        count += len(p)
    if count > 60000:
        raise ValueError("GEOS road tile exceeds conservative 60,000 vertex budget")
    return np.concatenate(ps), np.concatenate(ts), np.concatenate(us), materials, np.concatenate(ms)
