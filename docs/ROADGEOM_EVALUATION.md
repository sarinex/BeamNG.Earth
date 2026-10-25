# Road geometry: verification and A/B procedure

Initial implementation report on 2026-10-25: implemented and automated-test verified; **not verified inside BeamNG.drive at that stage**. The installed BeamNG 0.40.3.0 executable and launcher were tried with an isolated user folder under `build/beamng-roadgeom-eval`. The launcher exited with `0x03000004` before MCP started. Its log is under that folder's `current/beamng-launcher.log`. No visual-quality, game frame-time, load-latency or driving-collision result was claimed. No game process was left running by that initial attempt.

Follow-up on 2026-10-25: the user started the game successfully. Reading its live state through the existing MCP endpoint confirmed `accepted=geos`, `active=geos`, and nonzero Earcut/corner counters while the page incorrectly displayed Legacy. The page read `core._state().roadGeometry`, which does not exist: `core.status()` is a console dump string, and the generator state lives in the separate roadgeom module. A dedicated structured `core.roadGeometryState()` now feeds the settings page, pending status includes the loaded backend, cancellation clears stale text, and unavailable status no longer defaults to an unverified Legacy label. The status reader was refreshed in the running session without replacing world objects or changing backend selection. This verifies live status reporting; visual quality and driving collision remain unverified.

The same session exposed false vertex-budget fallbacks. Native terrain following emits independent triangle vertices, while clipping can retain unused vertices. Backend version 2 shares equivalent position/UV vertices and drops unreferenced vertices before applying the unchanged 60,000-vertex limit. Four cached tiles that previously fell back were rebuilt offline with zero fallbacks: 79,981 → 20,476; 87,035 → 23,917; 75,165 → 20,960; and 85,739 → 23,680 vertices. Automated tests also cover discarded unused inputs, identical position/UV triangle output, and a genuinely oversized mesh that still triggers the guard. The running server must be restarted to load this Python change; the live game was left running.

Follow-up validation: 89 targeted Python checks (88 passed, one normal standalone-LuaJIT skip), eight Lua suites with 1,098 passing assertions, four real cached-tile exports with zero fallback, and successful source ZIP packaging. The installed Lua files were updated; the live status-reader refresh preserved generator selection, queued state and world objects. Live Python workers retain their existing imports until the companion is restarted.

## Implemented operations

GEOS buffers actual ground-road centrelines, repairs polygons, unions their pavement and existing curb-return infill, subtracts material overlap and clips at tile boundaries. Mapped circular/irregular road loops retain central holes. Earcut triangulates pavement, holes, concave sidewalk corners and overlap-clipped slab pieces, with coverage/index/winding checks. Meshes use the existing terrain-following and `.cdae` paths. Sidewalk ramps, tactile pads, height planes, stock curb profiles, fixed bridges/tunnels, grading, floating-origin transforms, friction masks and collision scheduling use existing infrastructure.

Legacy remains the default and its original algorithms remain available. The extension is optional, with dependency-free legacy imports and separately installed native packages. The [integration guide](UPSTREAM_INTEGRATION.md) lists contracts, changed upstream files, cache identity, rollback and upgrade steps.

## Automated checks

The extension tests use the repository's unittest/Lua test helpers. They exercise actual tile/shape export and a spawned Windows worker, rather than only standalone polygons.

- Legacy direct dispatch, explicit/default tile and mesh equality, existing byte-for-byte worker fixtures, server import with Shapely/Earcut blocked.
- T, cross, Y, acute, offset, multiway and different-width/material junctions; circular/irregular loops, concavity, holes, invalid/duplicate vertices and repairs.
- Independently built adjacent tiles, disjoint materials/slabs, nondegenerate upward road triangles, valid indices, graded heights, fixed decks and elevated-road separation.
- Byte-identical shape output across local/spawned worker execution; captured immutable backend settings; separate connection/revision paths; stale worker suppression; inline JSON and pooled shape ownership.
- Repeated Legacy → GEOS → Legacy selection; rejection of missing dependencies/invalid requests; stationarity, pending unsafe/MP switches, queue/collision completion, reconnect and disconnect cleanup in Lua.
- Existing settings persistence tests, core stubbed/robustness, roads, rebase, governor and simulation hold tests.
- Optional installation failure/success, independent requirements stamp, existing interpreter reuse, Python 3.10 syntax/conditional requirements, source ZIP contents and Windows/Linux native packaging command construction.

The 16 targeted Python suites completed 323 tests (three expected platform/tool skips); eight targeted Lua suites passed 1,092 assertions. The full server suite passed after increasing its timeout from 180 s to 600 s (45 tests in about 302 s). The full repository-wide suite was not run.

The validation environment had no standalone `luajit` executable. Lua suites were run with Lupa 2.8's LuaJIT 2.1 runtime installed **only under ignored `build/lua-runtime`**, with Python's monotonic wall clock supplied as LuaSocket's `gettime`. The upstream stopwatch falls back to low-resolution `os.clock` without LuaSocket; that produced timing-sensitive collision assertions in both unchanged upstream and modified road suites. Using the monotonic wall-clock dependency gave consistent passes without editing those assertions. Some Python tests that invoke the standalone LuaJIT executable still report their normal skip. Reproduce the Lua runs with `.venv/Scripts/python.exe tools/run_lua_tests.py --runtime-dir build/lua-runtime roadgeom roads settings core_stubbed core_robustness rebase governor simhold`.

Full executable/AppImage builds and a real Python 3.10 interpreter are not available in this environment's prepared toolchain. Command-level packaging and Python 3.10 syntax/requirements checks are automated; binary compatibility must be verified in release CI.

## Offline benchmark

Reproduce:

```text
.venv/Scripts/python.exe tools/benchmark_roadgeom.py --out build/roadgeom-benchmark.json
```

The checked-in [measurement data](roadgeom-benchmark.json) uses Python 3.12.13, Shapely 2.2.0 and Earcut 2.1.0, seven warmed repetitions per backend. Measurements include synthetic tile generation, terrain following, sidewalk/collision meshes and binary shape export. They exclude map downloads, source grading plans, terrain imagery and engine work. The JSON includes Python allocation peaks (excluding native allocations) and process RSS snapshots including native libraries. RSS is a warmed process snapshot, not an isolated per-tile allocation or game memory measurement.

| Synthetic fixture | Legacy p50 / p95 ms | GEOS p50 / p95 ms | Legacy / GEOS road triangles | Legacy / GEOS total mesh bytes |
| --- | ---: | ---: | ---: | ---: |
| T | 17.98 / 18.18 | 56.87 / 57.82 | 792 / 799 | 147474 / 148597 |
| Cross | 26.57 / 27.25 | 108.90 / 109.53 | 964 / 974 | 229656 / 231247 |
| Acute | 11.72 / 12.20 | 35.54 / 38.71 | 666 / 673 | 98024 / 99867 |
| Offset | 28.06 / 28.96 | 129.65 / 130.61 | 990 / 1015 | 262844 / 274167 |
| Six-way | 39.17 / 40.50 | 169.32 / 172.78 | 1572 / 1588 | 365227 / 367754 |
| Irregular roundabout | 20.11 / 21.04 | 99.12 / 100.20 | 690 / 793 | 174761 / 197640 |

At the base commit, Legacy paints base carriageways into the ground; its listed road triangles largely represent markings and gutters. GEOS adds explicit union pavement meshes while leaving that terrain image generation unchanged. These figures therefore compare the actual selectable pipelines, not two equivalent standalone mesh algorithms. GEOS is slower in every fixture, and the roundabout adds more mesh data. No acceptable frame-time overhead has yet been established. Keep Legacy as default; use the real-world tests below to decide whether the quality improvement justifies the cost.

Per-tile JSON exposes `roadGeometry`: backend/version, footprint and mesh times, polygon/hole/Earcut counts, final road vertices/triangles, mesh bytes, invalid inputs and fallbacks/reason. Version 2 also records `inputVertices` and `compactedVertices`. Inlined tile messages include a bounded 256-sample stream summary (mean/p95 geometry time, aggregate fallback/corner counts and outstanding/stale jobs). Inspect `extensions.beamngEarth_core.roadGeometryState()` in the game console. `core.status()` remains its existing console dump string. Detailed fallback errors go to the existing `earthd.roadgeom` logger. Geometry time summaries exclude exports and queue time; use the existing worker debug timing and benchmark for those. The synthetic measurements above were captured on backend version 1; the four cached-tile regressions document the version 2 fix separately.

## Exact in-game procedure

1. Install/start with the prepared environment (`python install.py`, then `python start.py`). Optional packages are installed by default; generation remains Legacy until selected. Source users who want no native optional dependencies can pass `--road-geometry legacy` to both commands.
2. Start BeamNG.drive with its MCP server enabled for automated camera capture (Options → Advanced). Load the BeamNG.Earth level. Use the existing companion port/configuration; no new server or UI is required.
3. In the existing pause-menu BeamNG.Earth settings, find **Road Geometry Generator** next to **Roads**. Select **Legacy (Original)** and wait for **Active road generator** to confirm it. GEOS is the other option, **GEOS + Earcut (Experimental)**. The dropdown is the saved requested choice; the status line is the confirmed loaded state or a pending/error message.
4. Stop on terrain clear of curbs, bridge decks and tunnel structures before switching. While driving, selection remains pending. In single player, the mod holds simulation while it regenerates roads, retires old objects through the governor and updates collision; it restores the previous pause state after loading. In multiplayer it waits until the network session ends. Reconnect/missing-dependency/old-server errors are displayed; do not interpret the requested dropdown value as proof of an active backend.
5. Visit the exact initial locations: Fells Point **39.28188, -76.59114** (`roadgeom_fells`), Harbor Point **39.2801, -76.5890** (`roadgeom_harbor`). These are benchmark fixtures only; no coordinate rules occur in the generator. The two new entries in `docs/benchmarks.json` have fixed camera defaults.
6. Generalize with existing dataset entries: `fells_point` for the nearby street grid/simple junctions, `tokyo_shibuya` for irregular urban junctions, and `punjab_farmland` for rural intersections. The dataset points identify areas, not guaranteed junctions in every source release. Inspect the mapped road geometry first. If a repeatable camera must be added to frame an intersection, add a new view/ID and record it for **both** backends; never move an existing saved view after a baseline exists.
7. With the requested backend confirmed and all queues settled, run the existing benchmark:

   ```text
   .venv/Scripts/python.exe tools/benchmark.py check
   .venv/Scripts/python.exe tools/benchmark.py run --label roadgeom-legacy --places roadgeom_fells,roadgeom_harbor,fells_point,tokyo_shibuya,punjab_farmland
   ```

   Switch in-game to GEOS, wait for active confirmation, then repeat with label `roadgeom-geos`. Run `tools/benchmark.py compare <legacy-run-directory> <geos-run-directory>`. The harness restores weather/camera state and fixes the sun and camera views. It does not select the generator automatically. Verify active status at each location, including after teleports and server reconnects.
8. Capture the same ground/aerial views and record p50/p95/p99/max frame time, tile generation/queue/load latency, mesh size, triangle/vertex counts, collision rebuild milliseconds, process/game memory and fallback reasons. Use `tools/perfmon.py`, existing core/governor status and server debug timing. Run at least three passes on the same machine/settings, compare warm-cache results separately from cold-cache results, and retain raw logs/screenshots.
9. Drive across junctions, curb ramps and entries/exits at street speeds, then approach at high streaming speeds. Check wheel contact/friction and static collision raycasts, not render-only raycasts. Include an elevated crossing, sloping junction, tile seam, teleport and floating-origin rebase. Inspect island holes and curbs in both circular and irregular roundabouts when mapped.
10. Switch back to Legacy, wait for confirmation and repeat the view/drive. Restart and verify saved preference, connect an old server, simulate missing native dependencies, and switch while jobs are in flight. Check stale counters and that pool references return only after deletion.

Decide performance limits from measured baseline variability before accepting a rollout. Reject any new collision hole, cross-level connection, missing island, persistent stale object or incorrect friction alignment. Record worse or ambiguous views as such; more triangles do not establish improvement.

## Known limits and rollback

- Junction discovery, curb radii, sidewalk eligibility, accessibility placement and raised island detection still come from upstream. GEOS repairs/merges their footprints; it cannot infer missing mapped features or cure every topology error.
- Different built materials use deterministic material ordering at overlaps; a richer road-priority policy remains future work. Rough/grass/rock ground roads retain their existing terrain rendering.
- Ground friction masks, terrain grading and imagery are unchanged. New visual curb infill still needs in-game wheel/material checks against those sampled masks.
- The upstream nearby context is 40 m. Very wide roads or unusually large junction complexes outside that context need further seam evaluation; there is no location-specific seam repair.
- Ground road bytes are deterministic in one anchor frame, including across worker processes. Floating-origin loading uses the existing transforms, but regeneration in a different tangent frame may shift sub-millimetre snapping. Engine rebase behavior has not been validated here.
- Mesh/sidewalk operation failures are logged and counted and use controlled legacy fallback; a GEOS-selected tile can therefore contain legacy pieces. Check counters before crediting GEOS with a screenshot. Conservative vertex-budget fallback protects the existing exporter.
- Old switch-revision files for a still-held anchor are kept until anchor/session cleanup. Repeated switching can temporarily grow disk usage.
- Live switching in multiplayer is deliberately deferred. This version is ready for reproducible single-player A/B evaluation, not validated for release as the default.

Rollback: select **Legacy (Original)**, stop at a safe terrain position, and wait for the active line. No restart or manual cache purge is required. If dependencies are unavailable, keep the default Legacy and launch source installs with `--road-geometry legacy`. To revert the code, follow the isolated-commit rollback in the integration guide.
