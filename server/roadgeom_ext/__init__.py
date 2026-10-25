"""Optional road geometry extension. Importing this package needs no GEOS libraries."""

VERSION = "2"
BACKENDS = ("legacy", "geos")


def validate(name):
    if name not in BACKENDS:
        raise ValueError("road geometry backend must be legacy or geos")
    return name


def _legacy_chunks(chunks):
    original = next((c["_legacy"] for c in chunks if "_legacy" in c), None)
    if original is None:
        return [c for c in chunks if "geom" not in c]
    ids = {id(c) for c in original}
    return original + [c for c in chunks if "geom" not in c and id(c) not in ids]


def availability():
    """Load native libraries only when GEOS is explicitly requested."""
    try:
        import shapely
        import mapbox_earcut
        return True, f"Shapely {shapely.__version__}, Earcut {mapbox_earcut.__version__}"
    except Exception as exc:
        return False, (f"GEOS unavailable: {exc}. Install server/requirements-roadgeom-ext.txt "
                       "with the server's Python, or select Legacy.")


def build_mesh(backend, *args, diagnostics=None, **kwargs):
    import roadmesh
    validate(backend)
    if backend == "legacy":
        return roadmesh.build(*args, **kwargs)
    from .geos_backend import build_mesh as build
    import time
    t0 = time.perf_counter()
    try:
        result = build(*args, diagnostics=diagnostics, **kwargs)
        if result is None and any("geom" in c for c in args[1]):
            raise ValueError("terrain following produced no GEOS triangles")
    except Exception as exc:
        import logging
        logging.getLogger("earthd.roadgeom").warning("GEOS mesh fallback: %s", exc, exc_info=True)
        if diagnostics is not None:
            diagnostics["fallbacks"] = diagnostics.get("fallbacks", 0) + 1
            diagnostics["reason"] = f"mesh: {type(exc).__name__}: {exc}"
        if not any("_legacy" in c for c in args[1]):
            raise
        original = _legacy_chunks(args[1])
        result = roadmesh.build(args[0], original, *args[2:], **kwargs)
    if diagnostics is not None:
        diagnostics["meshMs"] = round((time.perf_counter() - t0) * 1000, 3)
        diagnostics["vertices"] = len(result[0]) if result else 0
        diagnostics["triangles"] = len(result[1]) if result else 0
        diagnostics["version"] = VERSION
    return result


def sidewalk_builder(backend, origin, collision=True):
    import sidewalks
    validate(backend)
    if backend == "legacy":
        return sidewalks.Builder(origin, collision=collision)
    from .sidewalks import Builder
    return Builder(origin, collision=collision)


def write_tile(*args, backend="legacy", diagnostics=None, **kwargs):
    import logging
    import roads
    # If even the controlled legacy mesh fallback is empty, use upstream's JSON/decal contract.
    # Extension numpy chunks are an in-process mesh input and must never reach JSON.
    if backend == "geos" and len(args) > 5:
        mesh = args[7] if len(args) > 7 else kwargs.get("road_mesh")
        if mesh is None:
            args = args[:5] + (_legacy_chunks(args[5]),) + args[6:]
    try:
        return roads.write_tile(*args, backend=backend, diagnostics=diagnostics, **kwargs)
    except Exception as exc:
        if backend == "legacy":
            raise
        logging.getLogger("earthd.roadgeom").warning("GEOS sidewalk fallback: %s", exc, exc_info=True)
        if diagnostics is not None:
            diagnostics["fallbacks"] = diagnostics.get("fallbacks", 0) + 1
            diagnostics["reason"] = f"sidewalk: {type(exc).__name__}: {exc}"
        return roads.write_tile(*args, backend="legacy", diagnostics=diagnostics, **kwargs)
