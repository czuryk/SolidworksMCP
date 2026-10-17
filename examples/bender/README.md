# Bender examples and recorded evidence

[Illustrated case studies](../../docs/bender.md) · [Project overview](../../README.md)

| Example | Source | Recorded server | Scope |
|---|---|---|---|
| Detailed character | [appearance-24-body/source.py](appearance-24-body/source.py) | 6.29.4; read-only recheck on 6.29.5 | 76 program components, 24 native bodies, measured front/side likeness; no export |
| Glue assembly | [printable-9-body/source.py](printable-9-body/source.py) | Run began on 6.27.0; final export on 6.28.0 | 65 program components, nine bodies, parameter cycle, assembly report and validated export |

Both programs use the injected `model` API. They are inputs to
`execute_model_code`, not standalone Python programs. Shape components include
consumed intermediate nodes; their count is different from the final body count.

## Offline verification

From the repository root, with the server dependencies installed:

```powershell
python examples/bender/check_evidence.py
```

This verifies the published artifact hashes, compiles both original programs with
the current interpreter, checks the independent `H` and `Clr` inputs, and checks
the recorded comparison and export results. It does not open SOLIDWORKS or rerun
the historical native tests. Compilation hashes can change with compiler recipe
versions; the archived hashes describe the original run.

## Live reproduction

Use an idle SOLIDWORKS session and new absolute paths for both the part and its
delivery. Keep the two cases in separate directories.

1. Call `get_model_context` and `model_api_lookup(topic="all")`.
2. Read the selected `source.py` into the `code` argument of
   `execute_model_code(mode="analyze", code=..., draft_views=["front", "left"])`.
   Review bounds, component counts and warnings.
3. Build with `execute_model_code(mode="create", code=...,
   output_path="C:/CAD/Bender-new/model.SLDPRT")`. Supply a path that does not
   exist. Collect a `RUNNING` result with `wait_operation` until completion.
4. Use `inspect_model` for native health and exact bounds, then
   `compare_reference(source="native", document_path=..., view="front",
   image_path=..., height_mm=150, tolerance_mm=...)` and repeat for `view="left"`.
   Resolve [references/front.png](references/front.png) and
   [references/side.png](references/side.png) to absolute paths. The appearance
   case used 24 levels and 1.5 mm; the assembly case used 48 levels and 2 mm.
5. For the assembly case, inspect all eight mating pairs, stability and wall
   thickness. Test `parameters={"H":120}` using a fresh `expected_revision`,
   verify height and fixed `Clr=0.25` natively, and revert before final export.
6. Capture final views with `observe_model`. If a delivery is wanted, call
   `export_model` with a new absolute directory and inspect every acceptance
   field. A matching photograph or a passed export alone is not a clearance test.

The exact parameters and nested schemas are in the [tool reference](../../docs/tools.md).
The original [assembly notes](printable-9-body/assembly.md) describe the historical
`delivery/` directory; that binary directory is not distributed here. The JSON
file records describe the archived files rather than downloads in this repository.

## Evidence conventions

Images are unmodified source captures, approved references and native comparison
overlays. [provenance.json](provenance.json) records their source names and SHA-256
hashes. Machine-local absolute paths in JSON are replaced with
`recorded-local-file/<basename>`; comparison references and overlays use relative
paths to shipped files. Measurements, revisions, timings and statuses are retained.
The recheck file is an explicitly labelled extract of the Bender-related results.

The historical test scope is preserved: the 24-body case has no STEP/STL delivery,
and the nine-body delivery manifest does not certify every manufacturing property.
