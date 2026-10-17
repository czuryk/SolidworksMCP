# Localized standard planes: issue #1

[Validation](validation.md) · [Tool reference](tools.md#create_sketch)

`create_sketch` now accepts the existing `Front`, `Top` and `Right` arguments even
when the document's standard planes have localized or user-defined names.

## Cause and fix

[Issue #1](https://github.com/czuryk/SolidworksMCP/issues/1) originally reported
`Could not select Front Plane` and `COM_MEMBER_MISMATCH` on Polish SOLIDWORKS 2026
SP3.2. **piotrostapiuk-ship-it** subsequently
[confirmed the localized-name cause and proposed a working fix](https://github.com/czuryk/SolidworksMCP/issues/1#issuecomment-5639563463).

The legacy tool selected the fixed English name with `SelectByID2`. Its callout
already used the null-dispatch variant, so substituting the existing
`select_by_id2` wrapper alone would leave the name mismatch intact. A false
selection result was then labelled `COM_MEMBER_MISMATCH` by generic legacy error
enrichment, even without a COM exception.

The fix follows the reporter's proposal: enumerate the first three top-level
`RefPlane` features, map them to Front/Top/Right, clear the selection, and call
`IFeature.Select2(False, 0)`. Only a successful selection starts the sketch. The
shape compiler already used positional discovery; both paths now share it.
If traversal is incomplete, only an exact English `RefPlane` feature is accepted
as a fallback. Later custom planes do not displace the standard trio.

Failures return `STANDARD_PLANE_NOT_FOUND` or `PLANE_SELECTION_FAILED` with
`sketch_created: false`, the requested plane, resolved name, lookup and selection
methods, and the original HRESULT if a COM exception occurred. RPC disconnects
still reach the executor's existing recovery handling. Successful sketches retain
verified Normal To and Fit to Screen behavior.

## Reproduction and native acceptance

Tested October 17, 2026 on **SOLIDWORKS 2026 SP2.1**, API revision **34.2.1.2**,
using fresh MCP processes. No service-pack update was needed.

Before the fix, the English Front plane succeeded. Renaming the standard trio to
`Przod_Issue1`, `Gora_Issue1`, `Prawa_Issue1` reproduced the reported selection
failure, legacy error 104 and misleading `COM_MEMBER_MISMATCH`.

After the fix, six new native parts passed the full lifecycle. Each used a closed
100 × 50 mm rectangle and a 20 mm extrusion. Bounds below are in model X/Y/Z:

| Plane names | Plane | Exact size, mm | Body / faces | Save/reopen | STEP |
|---|---|---|---|---|---|
| English | Front | 100 × 50 × 20 | 1 / 6 | Pass | Pass |
| English | Top | 100 × 20 × 50 | 1 / 6 | Pass | Pass |
| English | Right | 20 × 50 × 100 | 1 / 6 | Pass | Pass |
| Polish | Front | 100 × 50 × 20 | 1 / 6 | Pass | Pass |
| Polish | Top | 100 × 20 × 50 | 1 / 6 | Pass | Pass |
| Polish | Right | 20 × 50 × 100 | 1 / 6 | Pass | Pass |

The Polish names were `Płaszczyzna przednia`, `Płaszczyzna górna` and
`Płaszczyzna prawa`. The test read back each name and sketch normal, measured
bounds with `IBody2.GetExtremePoint` to 0.000001 mm, checked zero kernel faults
with `IBody2.Check3`, and repeated the body checks after reopening. All six STEP
files contained a STEP header and `MANIFOLD_SOLID_BREP`.

This tests renamed planes on the installed locale, **not a Polish SP3.2
installation**. The reporter separately documented successful SP3.2 acceptance.
The reporter's `AccessSelections(document, None)` diagnostic mismatch is a
separate concern: this fix does not call that API or claim to repair it.

[Recorded results and artifact hashes](evidence/issue-1-standard-planes.json)
include the before/after facts. CAD binaries and full local call logs are not
included in the repository.

## Repeat the checks

```powershell
python -m unittest discover -s tests -p "test_standard_plane_sketch.py"
python -m unittest discover -s tests -p "test_*.py"
python tests/live_standard_planes.py --output-dir C:/Tests/Issue1-NewRun
```

The live test requires Windows, SOLIDWORKS and a new output directory. Run one CAD
test at a time. It saves a report after each call and closes its own completed
fixtures; on failure it retains the report and affected fixture for inspection.
The ten new unit tests cover all planes, fallback validation, unsuccessful
selection, HRESULT preservation, RPC loss and post-creation view failure.
