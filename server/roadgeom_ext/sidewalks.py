"""Retain stock curb/ramp profiles and remove overlapping slab triangles with GEOS/Earcut."""
import logging
import numpy as np
import shapely
import sidewalks

from .triangulation import corner, triangulate

log = logging.getLogger("earthd.roadgeom")


class Builder(sidewalks.Builder):
    def __init__(self, origin, collision=False):
        super().__init__(origin, collision=collision, triangulator=self._corner)

    @staticmethod
    def _corner(poly):
        try:
            return corner(poly)
        except Exception:
            # Abort this shape to the tile writer's controlled legacy retry; never publish a partial curb.
            log.warning("GEOS sidewalk triangulation failed", exc_info=True)
            raise

    def arrays(self):
        return disjoint_slabs(*super().arrays())

    def write_collision(self, path):
        # Same slab clipping on the lean mesh, while its curb face and ramp heights remain upstream's.
        original = self.col.arrays
        arrays = disjoint_slabs(*original())
        import cdae
        return cdae.write(path, arrays[0], arrays[1], uvs=arrays[2], material=sidewalks.MATERIALS[0],
                          collision_only=True)


def disjoint_slabs(P, T, UV, M):
    """Clip overlap in XY, retaining each source triangle's 3D plane and UVs (including ramps)."""
    a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    normals = np.cross(b - a, c - a)
    # Upward slab faces only. Lean mesh has material zero, so select by its normal instead.
    top = normals[:, 2] > np.linalg.norm(normals[:, :2], axis=1) * 2
    ids = np.flatnonzero(top)
    if not len(ids):
        return P, T, UV, M
    polys = shapely.polygons(P[T[ids], :2])
    tree = shapely.STRtree(polys)
    pairs = tree.query(polys, predicate="intersects")
    pairs = pairs[:, pairs[1] < pairs[0]]
    overlap = shapely.intersection(polys[pairs[0]], polys[pairs[1]])
    pairs = pairs[:, shapely.area(overlap) > 1e-8]
    candidates = {}
    for i, j in pairs.T:
        candidates.setdefault(int(i), []).append(int(j))
    new_p, new_t, new_uv, new_m, removed = [], [], [], [], set()
    count = len(P)
    for i in sorted(candidates):
        source_id = ids[i]
        # Shared edges need no overlay. Exclude surfaces at different heights.
        near = []
        for j in sorted(candidates[i]):
            overlap = polys[i].intersection(polys[j])
            if overlap.area <= 1e-8:
                continue
            xy = np.asarray(overlap.representative_point().coords)[0]
            def at(t):
                src = P[T[t]]
                f = np.linalg.solve(np.c_[src[:, :2], np.ones(3)].T, np.r_[xy, 1])
                return f @ src[:, 2]
            if abs(at(source_id) - at(ids[j])) < 0.05:
                near.append(polys[j])
        if not near:
            continue
        remaining = polys[i].difference(shapely.union_all(near))
        xy, tri = triangulate(remaining)
        removed.add(source_id)
        if not len(tri):
            continue
        src = P[T[source_id]]
        weights = np.linalg.solve(np.c_[src[:, :2], np.ones(3)].T, np.c_[xy, np.ones(len(xy))].T).T
        new_p.append(np.c_[xy, weights @ src[:, 2]])
        new_uv.append(weights @ UV[T[source_id]])
        new_t.append(tri + count)
        new_m.append(np.full(len(tri), M[source_id]))
        count += len(xy)
    keep = np.array([i not in removed for i in range(len(T))])
    if not new_p:
        return P, T[keep], UV, M[keep]
    return (np.concatenate([P] + new_p), np.concatenate([T[keep]] + new_t),
            np.concatenate([UV] + new_uv), np.concatenate([M[keep]] + new_m))
