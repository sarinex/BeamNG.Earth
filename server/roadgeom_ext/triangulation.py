"""Validated Earcut triangulation of polygonal GEOS output, including interior rings."""
import numpy as np
import shapely
import mapbox_earcut

GRID = 0.001  # anchor ENU metres; 1 mm, applied before overlays, never tile-relative rounding


def polygons(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry] if geometry.area > 1e-9 else []
    return [p for g in getattr(geometry, "geoms", ()) for p in polygons(g)]


def repair(geometry, precision=GRID):
    if not geometry.is_valid:
        geometry = shapely.make_valid(geometry)
    parts = polygons(geometry)
    result = shapely.union_all(parts) if parts else shapely.Polygon()
    return shapely.set_precision(result, precision) if precision else result


def triangulate(geometry):
    """Return XY float64 and CCW indices. Reject bad coverage instead of emitting broken meshes."""
    points, triangles, count = [], [], 0
    for poly in sorted(polygons(repair(geometry, precision=0)), key=lambda p: p.normalize().wkb):
        poly = poly.normalize()
        rings = [np.asarray(r.coords, np.float64)[:-1, :2] for r in [poly.exterior, *poly.interiors]]
        xy = np.ascontiguousarray(np.concatenate(rings))
        if not np.isfinite(xy).all():
            raise ValueError("nonfinite polygon coordinates")
        ends = np.cumsum([len(r) for r in rings], dtype=np.uint32)
        tri = mapbox_earcut.triangulate_float64(xy, ends).astype(np.int64).reshape(-1, 3)
        if not len(tri) or tri.min() < 0 or tri.max() >= len(xy):
            raise ValueError("Earcut returned invalid indices")
        a, b, c = xy[tri[:, 0]], xy[tri[:, 1]], xy[tri[:, 2]]
        cross = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
        keep = np.abs(cross) > 1e-10
        tri, cross = tri[keep], cross[keep]
        tri[cross < 0] = tri[cross < 0, ::-1]
        if abs(np.abs(cross).sum() / 2 - poly.area) > max(1e-6, poly.area * 1e-7):
            raise ValueError("Earcut coverage differs from polygon area")
        # Area alone misses an equal-area triangle over a hole and a gap elsewhere.
        pieces = shapely.polygons(xy[tri])
        if shapely.union_all(pieces).symmetric_difference(poly).area > max(1e-6, poly.area * 1e-7):
            raise ValueError("Earcut crossed an exterior or hole boundary")
        points.append(xy)
        triangles.append(tri + count)
        count += len(xy)
    if not points:
        return np.empty((0, 2)), np.empty((0, 3), dtype=np.int64)
    return np.concatenate(points), np.concatenate(triangles)


def interpolate_edges(poly, xy):
    """Lift repaired boundary points back to the original 3D sidewalk boundary."""
    a, b = np.asarray(poly), np.roll(poly, -1, axis=0)
    d = b[:, :2] - a[:, :2]
    den = np.maximum((d * d).sum(1), 1e-20)
    out = []
    for point in xy:
        f = np.clip(((point - a[:, :2]) * d).sum(1) / den, 0, 1)
        near = a[:, :2] + f[:, None] * d
        i = np.argmin(((near - point) ** 2).sum(1))
        out.append(a[i, 2] + f[i] * (b[i, 2] - a[i, 2]))
    return np.c_[xy, out]


def corner(poly):
    xy, tri = triangulate(shapely.Polygon(np.asarray(poly)[:, :2]))
    return interpolate_edges(poly, xy), tri
