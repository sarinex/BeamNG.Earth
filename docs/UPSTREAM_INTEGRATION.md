# Optional road geometry extension

Implementation base: `f8e7a774042ea53832e570d6474da2af890d91cf` (Update README.md).
Original project: https://github.com/TecnaGamer/BeamNG.Earth.
This checkout's origin is https://github.com/sarinex/BeamNG.Earth; the base SHA is the checkout's implementation base, not a claim that the two remotes currently have identical heads.
Feature branch: `feature/roadgeom-geos`. No upstream push, PR or merge is part of this work.

## Architecture and contracts

The architecture documentation describes earlier versions too. At this base, `roads.ROADS_IN_GROUND` defaults to true: `surface.py` paints carriageways into the ground imagery and friction masks. `roads.build_tile` still builds markings, junction/sidewalk descriptions, bridges and tunnels. `roadmesh.build` drapes render-only meshes over the existing graded 2 m height lattice; `sidewalks.Builder` produces separate drawn and lean collidable meshes. `cdae.py` writes TSShape files. `roads.Tracker` schedules tiles per connection, and the mod creates/deletes their objects through its governor, collision scheduler and pooled-slot reference accounting.

The extension augments those boundaries; it does not replace grading, imagery, material masks, terrain blocks, the exporter or streaming architecture.

| Extension file | Responsibility |
| --- | --- |
| `server/roadgeom_ext/__init__.py` | Dependency-free dispatch, lazy availability checks, logged mesh/sidewalk fallback |
| `server/roadgeom_ext/geos_backend.py` | Buffered ground centrelines, curb-return infill, disjoint material unions, tile intersection, terrain-draped pavement mesh |
| `server/roadgeom_ext/triangulation.py` | Polygon repair, hole-aware Earcut, CCW winding, index/area/coverage checks, sidewalk boundary elevation interpolation |
| `server/roadgeom_ext/sidewalks.py` | Existing curb/ramp profiles; repaired corner triangulation and overlapping slab subtraction, retaining each triangle's height plane and UVs |
| `server/roadgeom_ext/streaming.py` | Per-connection selection, immutable worker requests, epochs, output namespaces, existing Tracker/slot adapter, anchor pruning |
| `mod/.../beamngEarth/roadgeom.lua` | Desired/accepted/loaded state, safe pending switch, progress, stale message gate, diagnostics |
| `mod/.../beamngEarth/roadgeom_safety.lua` | Oriented vehicle-footprint terrain/collision probes and bounded caching of blocked checks |
| `server/requirements-roadgeom-ext.txt` | Separately pinned optional dependencies with Python 3.10 marker |

`surface_chunks` consumes the actual near-road tuples `(fid, Spec, ENU float64 Nx3 centreline, longitude)` already gathered by `roads.build_tile`, plus `junctions.find` corner records (`arc`, `fill`, optional `fills`) and the exact tile box. These tuples contain only `ground_parts`; fixed bridge/tunnel layers are excluded. It emits extension chunks with `geom=(XY, triangles)`, alongside unchanged ordinary chunks. The extension mesh adapter consumes those chunks immediately; they never go on the wire. It emits the existing `(P-origin, T, UV, materials, triangleMaterial)` tuple. `roads.write_tile` writes the existing JSON/shape layout, optionally adding `roadGeometry` diagnostics. A 60,000-vertex conservative GEOS budget falls back to legacy pavement; the general exporter remains unchanged.

Shapely performs buffering, validation/repair, union, tile intersections, material differences, slab intersection/difference and boundary extraction. Earcut triangulates the resulting concave polygons, interior holes and sidewalk corner/slab pieces. Actual source road loops retain their central holes; upstream island curbs remain raised using the stock profile. No circular replacement, invented island, new asset or coordinate-specific geometry rule is used. Ground roads retain the existing graded/drawn elevation sampler; fixed decks and tunnel surfaces retain their original elevations and meshes. Pavement uses stock `road_asphalt` (loaded by the existing West Coast USA material file), concrete/cobble use existing materials, and markings retain their ribbon UVs.

## Upstream hooks to review after an update

| Modified upstream file | Localized hook / reason |
| --- | --- |
| `server/roads.py` | Optional backend/diagnostics on `build_tile` and `write_tile`; one final footprint adapter; optional sidewalk builder; tile metrics |
| `server/sidewalks.py` | Optional `Builder.triangulator` for corner slabs, propagated to the lean mesh; default retains the original fan/ear clipping |
| `server/earthd.py` | `Server.build_roads_tile` forwards backend/output namespace to both worker paths; `Connection` owns a Selection, handles additive messages and schedules captured builds; `prune` includes extension files |
| `server/blockworker.py` | `roads_tile` forwards explicit backend to build/mesh/write adapters; no mutable backend process flag |
| `server/peerwork.py` | `source_id` includes the extension package so packaged/distributed code identity reflects it |
| `mod/.../core.lua` | Additive capability/state/progress/tile routing, per-frame switch state update, mission/unload hold cleanup, `roadGeometrySafe()` terrain/rebase gates and the structured `roadGeometryState()` settings accessor (`status()` remains a console string) |
| `mod/.../roads.lua` | Governed retirement, stale-slot release, settled checks and delegation to vehicle-footprint terrain support checks; loaded deck/tunnel clearance retained, existing streaming spawn buffers and slot/collision lifecycle remain in charge |
| `mod/.../settings.lua` | Persisted `roadGeometryBackend` enum, legacy default, status and application hook |
| `mod/.../EarthSettings.vue` | Generator selector next to Roads and a distinct actual-active/pending line |
| `install.py`, `start.py` | Separate optional requirements installation/stamp; failure leaves legacy available |
| `tools/build_app.py`, `.github/workflows/release.yml` | Native library collection and dependency installation on both release platforms |
| `tools/package.py` | Include extension documentation in the source distribution; existing server tree collection already includes modules/requirements |
| `server/tests/test_protocol_agreement.py` | Additive versioned message fields, extension Lua send/read discovery; switching exercised by extension tests |
| `mod/tests/test_settings.lua`, `mod/tests/test_core_stubbed.lua` | Regression coverage for the structured generator accessor and its actual settings-page path |
| `docs/benchmarks.json` | Two new repeatable evaluation locations; existing place coordinates/cameras untouched |

New extension-owned validation files: `server/tests/test_roadgeom_ext.py`, `server/tests/test_roadgeom_install.py`, `mod/tests/test_roadgeom.lua`, `mod/tests/test_roadgeom_safety.lua`, `tools/benchmark_roadgeom.py`, `tools/run_lua_tests.py`, this document, `ROADGEOM_EVALUATION.md`, and `roadgeom-benchmark.json`.

## Selection, cache and lifecycle policy

The default is **legacy**, including old settings files. Both `legacy` and `geos` are explicit request values. No new library is imported for a legacy connection.

The base wire protocol stays at 4. Additive `hello.roadGeometry` advertises extension version 1 and server selection/epoch. Client `road_geometry {version:1, backend, speed, requestId?}` is validated and answered with `road_geometry_state {version, backend, epoch, ok, error, requestId?}`. The optional `requestIds: true` capability advertises echoed request IDs so delayed acknowledgements cannot replace a newer recovery request. Old clients may omit IDs. With an older extension server that does not advertise IDs, a client serializes requests after an acknowledgement timeout, waiting for the old response or a reconnect rather than guessing which request a response belongs to. Road tile/drop messages carry `roadBackend` and `roadEpoch`. Old mods ignore the extra fields. An old server has no capability: the new mod reports unsupported and restores a saved GEOS preference to legacy. Missing native dependencies reject GEOS with an installation diagnostic and preserve the previous valid selection.

The client distinguishes the persisted desired selection, the server-accepted generator and the loaded active generator. It waits for a stationary car on terrain clear of collidable support, then holds single-player simulation while retirement, streaming and collision updates run under the existing governor. Acceptance clears the loaded `active` value until road progress completes and the local mesh/delete/collision queues settle. A 120 s timeout enters an explicit `incomplete` state, retains the known server selection and last successfully loaded backend, and releases only this operation's hold. It does not pretend the previous generator has been restored. Selecting either generator, including the previous Legacy value or the same GEOS value, requests a fresh acknowledged epoch to roll back or retry. A rollback already selected during loading remains queued. Late tiles cannot complete an abandoned transition, and late acknowledgements never retire geometry without a new stationary hold. Failed recovery remains incomplete; reconnect also forces a fresh rebuild when recovering. Switching in multiplayer remains pending until leaving the session; it never freezes other network players. Disconnect, rejection, timeout, mission end and unload release a hold owned by this operation. This is experimental; complete engine validation remains required.

Each connection/revision has its own generated-output subdirectory: connection token, epoch, backend, extension version and a hash of code identity plus server geometry parameters. Anchor IDs and tile indices remain in filenames; anchor-scoped height quantum/handedness/width/elevation inputs remain the upstream inputs. Source/map/DEM releases use the upstream raw-data caches; generated geometry has **no persistent cross-session cache**. Session identity and a fresh connection token prevent reuse across source updates or reconnects. Threads capture backend/namespace before dispatch; worker processes receive explicit arguments. Closed trackers suppress late announcements and every revision writes different files. The existing `pool_shapes` adapter resolves those files and records actual connection holds; pooled paths are reused only after the existing serial/reference acknowledgements release them. Stale client messages explicitly return their unused slots.

Input XY is rounded to 1 mm on the common anchor ENU lattice before robust overlays. Final material differences use float64 without resnapping intersection points (resnapping diagonals caused measurable thin overlaps). Polygons and features have stable ordering, including holes and triangles. Adjacent tiles use full feature geometry/nearby context before exact clipping. Mesh bytes are deterministic for identical inputs/frame, independent of worker order, proven across a spawned process. A rebase applies the existing rigid transform; regeneration in a new tangent frame can change millimetre rounding. Arbitrary-frame bit identity is not promised.

Backend implementation version 2 compacts the terrain-following result with the existing road builder's position/UV sharing precision and removes unused vertices before enforcing its 60,000 referenced-vertex budget. This avoids falling back merely because native tessellation temporarily duplicates vertices. A genuinely oversized exported mesh still falls back. The version participates in output namespace identity; this is independent of the additive wire extension version, which remains 1.

Old-anchor extension files enter the existing throttled prune queue only after upstream's held-anchor test permits it. Previous switch revisions within a still-held anchor remain on disk until anchor/session cleanup, protecting direct-path shape references; repeated switching can temporarily increase disk use. Pooled slots retain their existing lifecycle.

## Dependencies and packaging

Verified prepared environment: Python 3.12.13, Shapely 2.2.0, Mapbox Earcut 2.1.0. Upstream `server/requirements.txt` stays unchanged. Shapely 2.1.2 is selected below Python 3.11, Shapely 2.2.0 on 3.11+, and Earcut 2.1.0 on both. Python 3.10 syntax is tested; an actual Python 3.10 runtime/build remains untested here.

`python install.py` and `python start.py` install/check optional packages by default so the in-game selector works on a clean source installation. This installs capability; it does **not** change the legacy generator default. `--road-geometry legacy` skips optional dependency installation. The extension has a separate `.venv/.roadgeom-requirements.sha256` stamp; pip/import failure warns and continues with legacy. The existing virtual environment is reused. An installation is also repairable with `.venv/Scripts/python.exe -m pip install -r server/requirements-roadgeom-ext.txt`.

Release CI installs both requirements files. `tools/build_app.py` defaults to bundling GEOS/Earcut with `--collect-all` plus distribution metadata, including GEOS DLL/shared libraries and the Earcut compiled module. `--road-geometry legacy` creates a dependency-free variant. Source ZIP collection includes the extension and requirements automatically. Packaging commands are tested for Windows/Linux, but executable/AppImage builds must still be smoke-tested on their target systems. `.venv`, `build` and installed packages are ignored by Git.

## Bringing in the next release

Run these from the fork, with a clean worktree and a backup branch:

```text
git remote add upstream https://github.com/TecnaGamer/BeamNG.Earth.git
git fetch upstream --tags
git branch backup/roadgeom-before-update
git merge upstream/<release-branch-or-tag>
```

Add the remote only if it does not already exist. Rebase the feature commits instead of merge if that is the fork's established policy; do not rewrite published history casually. Resolve normal Git conflicts, starting with the hook table above. Review `git diff --name-only --diff-filter=U`, the changed upstream function signatures and `git diff <previous-base>..upstream/<release>` for road contracts. Update adapters for any changes to ground-part separation, corner records, mesh tuples, material names, slot acknowledgements, source hashing, settings or worker arguments. Do not patch by line number or copy the upstream generator into the extension.

Run the prepared interpreter:

```text
.venv/Scripts/python.exe tools/run_tests.py test_roads test_roadmesh test_blockworker test_sidewalk_collision test_grading test_islands test_cdae test_protocol test_protocol_agreement test_peerwork test_install test_roadgeom_ext test_roadgeom_install --timeout 600
luajit mod/tests/test_roadgeom.lua
luajit mod/tests/test_settings.lua
luajit mod/tests/test_roads.lua
luajit mod/tests/test_core_stubbed.lua
luajit mod/tests/test_core_robustness.lua
.venv/Scripts/python.exe tools/benchmark_roadgeom.py --out build/roadgeom-after-update.json
```

Run the other existing Lua tests and source/native packaging checks too. Follow `ROADGEOM_EVALUATION.md` for engine A/B and driving tests. Keep legacy as default until the comparison is accepted. Record the new implementation base, conflicts, fallback counts and regressions here after resolving them.

## Disable or remove

Select **Legacy (Original)** in-game and wait for the active line to confirm completion. For source installations without the optional packages, use `--road-geometry legacy` on install/start. Missing packages never prevent legacy import/start. To remove the extension code, revert the isolated feature commits (UI/protocol, backend hooks, installer/package integration and documentation), keeping any unrelated fork changes. Do not delete legacy algorithms or remove only the package while leaving imports/hooks behind. No manual cache deletion is needed for normal backend switching.

Library contracts: [Shapely precision policy](https://shapely.readthedocs.io/en/stable/reference/shapely.set_precision.html), [Shapely polygon repair](https://shapely.readthedocs.io/en/2.1.2/reference/shapely.make_valid.html), [Earcut Python ring endpoints](https://github.com/skogler/mapbox_earcut_python).

## Optional Group 4 road intelligence

Group 4 starts from the user's fixed Group 1 head `e3b1dab`, without resetting its switching fixes. Details, installation, source/cache contracts, bounds and limits are in [ROADINTEL.md](ROADINTEL.md). The single pending engine checklist is [ROADINTEL_EVALUATION.md](ROADINTEL_EVALUATION.md). No upstream push or merge is included. The original and Group 1 requirements files stay unchanged; SUMO/PyOsmium are pinned separately in `server/requirements-roadintel-ext.txt`.

Extension ownership: `server/roadintel_ext/{model,runtime,sumo,navigation,arrows}.py`, `tools/prepare_roadintel.py`, `tools/benchmark_roadintel.py`, `server/tests/{test_roadintel,roadintel_fixtures}.py`, and `mod/tests/test_roadintel.lua`. `model` normalizes supported cross-sections, `runtime` supplies immutable feature/anchor views, `sumo` performs bounded offline native import, `navigation` projects verified SUMO turns onto the existing custom graph, and `arrows` emits supported mapped glyphs from the existing stock white-line material.

Additional upstream hooks to review after merging TecnaGamer updates:

| File | Hook / contract |
| --- | --- |
| `roads.py` | Trailing optional `Spec.intel`; final ground/bridge/tunnel spec adapter; enriched markings and sidewalk sides; per-feature diagnostics; avoid inferred bulb-outs on mapped sections |
| `navgraph.py` | Enriched driving width, direction/counts and driving-center offset; passenger access guard; retain edge→source correspondence for the final movement adapter |
| `parking.py` | Do not place a car on an enriched side without a dedicated parking width |
| `earthd.py` | One-line anchor-view decorators on terrain/provisional builds, grading, road/nav/far/building/patch producers, base photos and related warm/elevation entry points; bind dataset before first terrain; no provisional partial model for SUMO anchors; wait for a matching replacement anchor before new roads/AI |
| `roadgeom_ext/streaming.py` | Extend the existing Selection with independent intelligence, dataset snapshot and terrain barrier; advertise additive capability/fields; reset both trackers under the same epoch |
| `peerwork.py` | Include extension Python files in source identity; experimental anchors use upstream thread builders rather than unmodified/distributed process workers |
| Lua `roadgeom.lua` | Existing state machine now tracks both dimensions; current terrain and applied AI are completion barriers; late terrain cannot commit after hold/epoch expiry |
| Lua `core.lua` | Request same-origin fresh anchor, import it under the existing hold/rebase path, retire old road/AI state, gate nav messages and wait for far/road/AI settlement |
| Lua `navgraph.lua` | Current graph settlement accessor, retirement and retained diagnostics; `controlNode` resolves existing signal controller references after portal splitting |
| Lua `settings.lua` / `EarthSettings.vue` | Independent persisted `roadIntelligenceBackend`, Legacy default, requested/accepted/loaded/incomplete status |
| `tools/package.py` | Include preparation CLI, optional requirements (via server tree), documentation and benchmark report in source ZIP; frozen SUMO bundling is deliberately not added |

Keep the original update procedure above. After resolving conflicts, compare `Spec` defaults, feature immutability, surface/grading data access, the anchor and ChunkCache lifetime, bridge/tunnel elevation sampling, signal-control graph references, stale worker suppression and pooled shape acknowledgements. Ensure a new producer cannot bypass `server_for(anchor)` and accidentally paint Legacy widths into a SUMO anchor. Bump the road-intelligence format version whenever normalized/import policy changes; do not re-use old cache pointers under a new policy.

Run the full Python/Lua suites, the Group 4 synthetic benchmark, and source packaging. The Lua test runner now supports its existing scoping/sky suites on Windows/Lupa too. The existing CLI default golden test ignores the additive capability only after asserting both actual defaults are Legacy; the mocked Linux launcher test preserves Windows drive letters when parsing container volumes. These are test portability/fixture changes, not launcher behavior changes. Follow the single manual checklist before claiming any engine result. To disable the feature, choose Legacy intelligence and wait for the complete coordinated terrain rollback; geometry can remain independently Legacy or GEOS.
