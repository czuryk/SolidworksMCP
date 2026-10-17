# Shape-model delivery

Use `export_model(document_path, expected_revision, directory)` with a fresh
revision and a **new absolute directory**; `replace=true` atomically replaces a
directory that holds only an earlier `export_model` delivery (any other existing
directory is refused). Bodies are written as `stl/<BodyName>.stl`, so file names
do not change when a rebuild reorders the bodies. The source must be saved, clean,
rebuilt and have all 1–64 solid bodies visible. Optional `expected_bodies` guards
the exact body-name set. The tool preserves source bytes and publishes native
SLDPRT, STEP, one STL per body and a manifest only after structural acceptance.
Default tessellation deviation is 0.02 mm; choose a different value for a concrete
requirement, not as a response to every visual discrepancy. On a nine-body model
the export takes 100-115 s: the call answers `RUNNING` and `wait_operation`
returns the result.

## What acceptance means

Export checks native kernel/feature health and true bounds, native file structure,
STEP units/body count, and STL closed oriented topology/bounds/volume. Read the
result and manifest acceptance fields. Successful export is not proof of:

- resemblance to references or all dimensions meeting the task;
- native parameter cycling after save/reopen;
- uniform mating gaps, wall thickness, mesh self-intersections or print-process
  readiness;
- Unity skeleton, weights or animation suitability.

Check only requirements relevant to the deliverable. `inspect_model` with
`precision="delivery"` supplies detailed native facts without screenshots;
`body_pairs` answers targeted fit questions. Each clearance criterion is
true/false/null. The global minimum distance does not establish a uniform local
clearance, and a positive surface distance can coexist with solid containment.

## Failed STL validation

Strict raw tessellation is the default (`mesh_cleanup="none"`). A failure preserves
the source and does not publish a partial delivery. Optional
`diagnostics_directory` must also be a new absolute directory; it retains the raw
rejected mesh and bounded facet diagnostics separately from delivery.

For sub-resolution tessellation seams, explicit `mesh_cleanup="bounded"` offers
limited welding and local winding correction followed by the unchanged strict
validator on the written float32 STL:

- maximum vertex displacement 0.00006 mm;
- at most 10 collapsed facets and 64 winding corrections, with tighter
  count-relative limits;
- no hole filling, duplicate-facet deletion, general remeshing or native repair;
- cleanup rejects unresolved open/nonmanifold edges and multiple shells.
  Already-valid raw hollow meshes pass unchanged.

The delivery retains raw STL hashes and a facet/displacement audit. Files marked
`raw_stl_diagnostic` are evidence, not printable deliverables; use final `kind=stl`
entries. Do not repeatedly retry with weaker validation or claim geometric
repair. Read live `model_api_lookup(topic="delivery")` for the current contract.

## Expert formats and destinations

`export_bundle` remains available for selected bodies, explicit per-format paths
and intentional overwrite workflows. `export_file` supports additional native
formats. Their contracts differ from `export_model`; do not treat them as a way
to bypass a rejected shape delivery. See
[verification-and-export.md](verification-and-export.md) only for those workflows
or a requested calibrated comparison.
