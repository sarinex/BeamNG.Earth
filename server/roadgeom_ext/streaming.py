"""Per-connection backend epochs, immutable worker inputs and namespaced shape files."""
from functools import partial
import hashlib
import glob
import json
import math
import os
import secrets
from types import SimpleNamespace

import roads

from . import VERSION, availability, validate


def anchor_files(out_dir, aid):
    """Only extension-owned files; called when upstream has established that an anchor is no longer held."""
    return glob.glob(os.path.join(out_dir, "roadgeom_*", f"*_{aid}_*"))


class Tracker(roads.Tracker):
    def __init__(self, backend, epoch, namespace):
        super().__init__()
        self.backend, self.epoch, self.namespace = backend, epoch, namespace
        self.closed = False
        self.stale = 0
        self.samples = []
        self.diagnostics = {"fallbacks": 0, "geosCorners": 0, "earcutTriangles": 0, "invalid": 0}

    def close(self):
        self.closed = True
        for fut in self.futs.values():
            fut.cancel()
        super().close()

    def _built(self, conn, aid, key, fut):
        if self.closed:
            self.stale += 1
            return
        tracker = self

        class ServerView:
            out_dir = os.path.join(conn.s.out_dir, tracker.namespace)

            def pool_shapes(self, ignored, files):
                pool = getattr(conn.s, "pool_shapes", None)
                return pool(conn, {k: f"{tracker.namespace}/{v}" for k, v in files.items()}) if pool else {}

        def send(msg):
            msg.update(roadBackend=self.backend, roadEpoch=self.epoch)
            data = msg.get("data", {})
            diagnostic = data.get("roadGeometry", {})
            for name in self.diagnostics:
                self.diagnostics[name] += diagnostic.get(name, 0)
            if diagnostic:
                self.samples = (self.samples + [diagnostic.get("footprintMs", 0) + diagnostic.get("meshMs", 0)])[-256:]
                diagnostic["stream"] = dict(self.diagnostics, stale=self.stale, outstanding=len(self.building),
                    averageMs=sum(self.samples) / len(self.samples), p95Ms=sorted(self.samples)[int((len(self.samples)-1)*.95)])
            conn.send(msg)

        proxy = SimpleNamespace(s=ServerView(), send=send)
        super()._built(proxy, aid, key, fut)

    def on_pose(self, conn, *args, **kwargs):
        if self.closed:
            return
        tracker = self

        def send(msg):
            msg.update(roadBackend=tracker.backend, roadEpoch=tracker.epoch)
            conn.send(msg)

        class View:
            def __getattr__(self, key):
                return getattr(conn, key)
        view = View()
        view.send = send
        super().on_pose(view, *args, **kwargs)


class Selection:
    def __init__(self, server=None):
        self.backend, self.epoch = "legacy", 0
        self.intelligence, self.dataset, self.awaiting_anchor = "legacy", None, False
        self.nav_required = bool(getattr(server, "traffic", False))
        self.token = secrets.token_hex(8)
        # Session + anchor + immutable parameters are the generated-file identity; raw map caches stay upstream's.
        config = {k: getattr(server, k, None) for k in
                  ("sidewalks", "near_visual", "buildings", "grading")}
        config["road_widths"] = getattr(server, "road_widths", None) is not None
        config["version"] = VERSION
        import peerwork
        config["source"] = peerwork.code_id()
        self.config = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
        self.namespace = self._namespace()

    def _namespace(self):
        return f"roadgeom_{self.token}_{self.epoch}_{self.backend}_{self.intelligence}_v{VERSION}_{self.config}"

    def capability(self):
        return {"version": 1, "backends": ["legacy", "geos"], "active": self.backend, "epoch": self.epoch,
                "requestIds": True, "intelligence": self.intelligence, "intelligenceBackends": ["legacy", "sumo"],
                "coordinatedTerrain": True, "navEpochs": True, "navRequired": self.nav_required}

    def tracker(self):
        return Tracker(self.backend, self.epoch, self.namespace)

    def builder(self, server):
        # A partial captures values NOW, never reads a mutable backend from a worker thread/process.
        return partial(server.build_roads_tile, backend=self.backend, namespace=self.namespace)

    def select(self, conn, msg):
        error = None
        requested = msg.get("backend")
        intelligence = msg.get("intelligence", self.intelligence)
        dataset = self.dataset
        terrain_rebuild = intelligence != self.intelligence or bool(msg.get("rebuildTerrain"))
        request_id = msg.get("requestId")
        valid_id = isinstance(request_id, int) and not isinstance(request_id, bool) and 0 < request_id < 2**53
        try:
            validate(requested)
            if intelligence not in ("legacy", "sumo"):
                raise ValueError("road intelligence backend must be legacy or sumo")
            if intelligence == "sumo" and (terrain_rebuild or dataset is None):
                from roadintel_ext.runtime import Dataset
                dataset = Dataset(os.environ.get("BNGE_ROADINTEL_DIR", ""))
            elif intelligence == "legacy":
                dataset = None
            if request_id is not None and not valid_id:
                raise ValueError("invalid road geometry requestId")
            if msg.get("version") != 1:
                raise ValueError("unsupported road geometry protocol version")
            if requested == "geos":
                available, reason = availability()
                if not available:
                    raise ValueError(reason)
            # The client also checks collision support and holds single-player simulation during replacement.
            speed = msg.get("speed")
            if not isinstance(speed, (float, int)) or not math.isfinite(speed) or speed > 0.25 or speed < 0:
                raise ValueError("road geometry switch requires a stationary vehicle")
        except (ValueError, TypeError) as exc:
            error = str(exc)
        if error is None:
            conn.roads.close()
            self.backend, self.epoch = requested, self.epoch + 1
            self.intelligence, self.dataset = intelligence, dataset
            self.awaiting_anchor = terrain_rebuild
            self.namespace = self._namespace()
            conn.roads = self.tracker()
            if hasattr(conn, "nav"):
                from roadintel_ext.navigation import Tracker as NavTracker
                conn.nav.close()
                conn.nav = NavTracker(self.backend, self.epoch)
        response = {"type": "road_geometry_state", "version": 1, "backend": self.backend,
                    "epoch": self.epoch, "ok": error is None, "error": error or "",
                    "intelligence": self.intelligence, "terrainRebuild": terrain_rebuild and error is None}
        if valid_id:
            response["requestId"] = request_id
        conn.send(response)
