# 07 — Memory Graph Viewer

> **Honest-framing reminder:** no unicorns. This is a read-only local visualization of the
> Memory graph — not a new store, not a query engine. It renders whatever `MemoryStore` holds.
>
> This slice follows `_TEMPLATE.md`. Architecture-level interfaces and cross-cutting rules are
> fixed in `00-north-star.md` — referenced, not restated. This file adds implementation depth
> for the viewer only.

## Purpose

A local-first, offline web app that visualizes EDITH's Memory graph (Kuzu) as a force-directed
cloud, so the owner can see what EDITH knows at a glance. Ships a one-command launcher
(`python -m edith.viewer`) that reads the **live** Memory DB, or a self-contained `--demo` that
seeds a realistic sample graph so the visualization is immediately dense and meaningful.

## Scope

**In:** `MemoryStore.graph_snapshot()` (force-graph JSON export of all node/edge tables);
a stdlib threaded HTTP server bound to 127.0.0.1 serving `/graph` + static page; a vendored
`force-graph` (2D canvas) frontend with degree-sized, type-colored nodes, pan/zoom, a zoom
control cluster, and a node-detail panel; a `--demo` seeder.

**Out:** editing memory from the UI (read-only); auth (loopback-only, single-user); remote
access (127.0.0.1 only); repo ingestion that populates the live graph (Slice 2 / ingestion —
this viewer just renders whatever is there); vector/semantic overlays.

## Interface to edithd

- **Inputs:** none — the viewer opens the Kuzu DB directly (read path). It does not consume
  bus events or the Control API.
- **Outputs:** none published. Serves HTTP locally only.
- **Note:** Kuzu is single-writer. The live path is best-effort — if `edithd` holds the write
  lock, stop it first (or use `--demo`, which uses an isolated temp DB and never conflicts).
- **Live DB filename is established here, not yet written by anything.** `edithd` currently
  receives `memory` as an injected dependency and no code persists the live graph to a canonical
  on-disk name. The viewer picks `EDITH_DATA_DIR/memory.kuzu`; because `MemoryStore(path)`
  *creates* a DB if absent, non-demo mode today opens a fresh empty store (renders nothing) until
  Slice 2 repo-ingestion writes to this path. `--demo` is the current showcase.

## Data model

Reuses the store schema. `graph_snapshot()` is schema-introspective (walks `SHOW_TABLES`), so
schema growth renders with no viewer change. Output shape:

```
{"nodes": [{"id", "type", "label", "degree", <raw props>}],
 "links": [{"source", "target", "type"}]}
```

`label` is the per-type display prop (Project→name, Repo→path, PR→title, Person→name,
Fact→text), falling back to `id`. `degree` is computed in Python from link incidence.

Schema was extended additively for the demo/PR-review domain: added `PR` node
(`title, number, state`); added `authored_by` and `reviewed_by` REL tables (PR→Person);
extended `owns` with Repo→PR and `relates_to` with Fact→PR.

## Dependencies

- **Other slices:** Slice 1 Memory store (`edith/memory/store.py`) — reused as-is.
- **Libraries:** **zero new runtime deps.** Server is stdlib `http.server`
  (`ThreadingHTTPServer` + `SimpleHTTPRequestHandler`). Frontend uses `force-graph` (vasturiano,
  UMD build **vendored** at `static/vendor/force-graph.min.js`, pinned v1.49.5) — no CDN at
  runtime, no npm, no build step.

## Tech choices

- **stdlib server, 127.0.0.1 bind:** smallest viable footprint, no framework, loopback-only per
  the local-first / single-user posture.
- **`force-graph` 2D canvas:** handles a dense cloud smoothly and matches the reference
  aesthetic (dark bg, pale nodes, thin translucent links) with minimal code.
- **`serve()` split from CLI:** the server is browser-free and testable over real HTTP; the only
  `webbrowser.open` lives in `__main__`.

## Autonomy & secrets notes

- **Autonomy gate:** read-only; no ASK/AUTO actions. It never writes memory.
- **Secrets:** renders only what `MemoryStore` persisted, which is already run through the
  never-persist redaction filter on write (north-star §6.1). Demo content is generic/plausible —
  no real secrets or tokens.

## How to run

```
# Dense sample graph, no setup — opens the browser automatically:
python -m edith.viewer --demo

# Live memory (reads EDITH_DATA_DIR/memory.kuzu; stop edithd first if it's running):
python -m edith.viewer
python -m edith.viewer --port 9000 --data-dir /path/to/data
```

## Verification / testing

- `tests/test_graph_snapshot.py` — shape, node/link counts, id integrity, computed degree
  (TDD, RED-first against a store with no `graph_snapshot`).
- `tests/test_viewer_server.py` — real HTTP on an ephemeral 127.0.0.1 port: `/graph` JSON shape,
  loopback bind, index page (RED-first against a missing `edith.viewer` module).
- `tests/test_viewer_demo_seed.py` — dense sample (≥120 nodes, all five types, valid links).

## Open questions

- Live-DB read while `edithd` runs: current stance is "stop the daemon or use `--demo`." A
  read-only Kuzu open mode could remove the constraint — deferred until it bites.

---

## Completion Record — Memory Viewer — 2026-07-07

- **What shipped:** `MemoryStore.graph_snapshot()` + `edith/viewer/` (stdlib server, vendored
  force-graph frontend, `--demo` seeder, `python -m edith.viewer` launcher).
- **How it works:** launcher opens Kuzu (live or temp/demo) → threaded stdlib server on
  127.0.0.1 serves `/graph` (snapshot JSON) + static page → force-graph renders the cloud.
- **Key decisions:** schema-introspective snapshot (future-proof); additive schema extension for
  PR + authored_by/reviewed_by; zero new runtime deps (stdlib + vendored JS).
- **Deviations from spec:** none material. Live path documented as best-effort due to Kuzu
  single-writer lock.
- **Files created / changed:** see BUILD_LOG.md.
- **Verification / tests run + results:** 70 passed, 1 skipped; ruff clean; pyright 0 errors;
  live curl of `/graph` returned 158-node JSON; `--demo` seeds ~120-160 nodes.
- **Follow-ups / known gaps:** repo-ingestion (Slice 2) will populate the live graph for real;
  read-only Kuzu open to allow viewing while edithd runs.
