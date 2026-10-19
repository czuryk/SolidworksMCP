# Validation and known limits

[Project overview](../README.md)

## Current candidate

Installed candidate: **6.30.0rc1**, ready for user acceptance. The native qualification
matrix passed on **SOLIDWORKS 2026 SP2.1** on October 12–13, 2026: all 12 fixture
lifecycles at 128/256/512 nodes across straight, curved, loft and lattice families;
16 Boolean/contact/scope cases; and all three full-tower union orders with strict
export, rollback and edit/revert. This qualifies the tested fixtures, not every
possible 512-node program. The candidate is not promoted to a stable release.

The hash-locked Eiffel benchmark passed all 26 exports, retaining 186,152 facets,
normal reads, strict validation and no mesh repair:

| Reader / session | Runs | Median export | Maximum export |
|---|---:|---:|---:|
| compat / fresh | 3 | 278.15 s | 278.86 s |
| compat / repeated | 10 | 233.05 s | 250.88 s |
| compat / after five edit/revert cycles | 10 | 340.70 s | 366.44 s |
| bound / fresh | 3 | 349.48 s | 381.09 s |

The two compat series ran in the same SolidWorks process without a restart. No
native RPC failures occurred in these runs. The slowest after-edit export was
1.32 times the fresh compat median, below the 2-times qualification budget.
Bound showed no measured speed advantage; load conditions differed between series,
so this is not an isolated causal comparison. **compat remains the default**.
STL bytes can vary across fresh sessions or edit/revert due to local triangulation;
strict acceptance passed, but binary determinism is not promised.

Known limits: long build sessions accumulated up to 46.88 GiB of SolidWorks private
memory; save and restart between completed jobs when warned. Never restart inside
active CAD work or a benchmark series. One of the recorded status/metrics pairs
took 1.563 s during STL validation, so the subsecond target was not met universally.
The original RPC-loss cause and a universal degradation fix remain unproven.

## Tests

The repository contains the deterministic unit and regression suite:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Release 6.15.0 adds durable retry/conflict, native rollback, checkpoint retention
and scoped UI-monitor regression tests.
`tests/live_request_receipts.py --output C:/Tests/Receipts.SLDPRT --report C:/Tests/receipts.json --other-document C:/Tests/Other.SLDPRT`
checks native replay across reopen/MCP restart, changed targets/payloads/state,
and the UI-only monitor on the shared owner. Supply an existing separate part
for the document-switch check.
`tests/live_sta_executor.py --output C:/Tests/Executor.SLDPRT --report C:/Tests/executor.json`
checks native H round trips, responsive status/analysis, a delayed Python request,
and reconciliation after restarting the MCP process. It does not hang or kill SolidWorks.
Opt-in live acceptance: `tests/live_shape_program.py`, `tests/live_shape_edit.py`,
`tests/live_shape_clearance.py`, and `tests/live_shape_mcp.py`. Each creation test
requires a new explicit .SLDPRT path. Run only one live CAD test at a time.
These tests do not replace live acceptance
for COM behavior: qualifying another SOLIDWORKS major version should also cover
standard planes, planar faces, existing-sketch activation, view recovery,
features, transactions, vectorization, and export.

The following opt-in tests require an open, unblocked SOLIDWORKS session and
create new test parts. Use absolute output paths that do not already exist:

```powershell
python tests/live_shape_program.py --output C:/Tests/NativeShape.SLDPRT --observe --report C:/Tests/native.json
python tests/live_shape_mcp.py --output C:/Tests/McpShape.SLDPRT --report C:/Tests/mcp.json
```

The first test saves/reopens the B-rep and checks analytical volumes and native
mirror mass/centroids. The second starts a fresh stdio server, tests strict
schemas and writer exclusion, creates another native part and decodes the
inline images. Neither replaces the final short-prompt reconstruction benchmark.

## Publication checks

The code snapshot with optional model handling on October 19, 2026 passed all
**731** unit and regression tests, including 17 optional-weight tests. These
COM-mocked and offline checks complement the recorded
native acceptance runs; they do not replace live validation on another machine.

The localized-plane fix also passed six live MCP lifecycles on SP2.1:
Front/Top/Right with English and Polish plane names, each through extrusion,
exact bounds, native health, save/reopen and STEP. See the
[reproduction and native results](issue-1-standard-planes.md).

To verify documentation without connecting to CAD:

```powershell
python scripts/generate_tool_docs.py --check
python tests/check_skill_docs.py --skill skills/solidworks-mcp
```

## Optional model acceptance

On October 19, 2026, a fresh stdio MCP server on SOLIDWORKS 2026 SP2.1 passed with
an empty line-art directory and an empty Hugging Face cache:

- All 13 default tools were advertised; capabilities reported the missing weights.
- Both line-art and region tracing returned `MODEL_WEIGHTS_MISSING`, including
  with `force_commit`, before CAD work.
- A parametric pedestal was analyzed, created, saved and inspected: one healthy
  native body, **20 × 30 × 20 mm**, volume **6031.857895 mm³**, zero kernel faults.
- A second rejected tracing request preserved the model revision. The test
  closed its own saved part and confirmed that no weights had been downloaded.

The offline installer also verified and copied the real DexiNed/TEED files from
the separate model repository. Both loaded on an RTX 4090 with CUDA 12.4 and
produced finite, nonconstant 128 × 128 probability maps. This is an installation
and inference check, not a new end-to-end native tracing qualification.

[Recorded results](evidence/optional-models-20261019.json).
To repeat these opt-in checks, use new output paths and run only one live CAD
test at a time:

```powershell
python tests/live_optional_models.py --output-dir C:/Tests/OptionalModels
python tests/live_lineart_models.py --model-dir C:/Downloads/SolidworksMCP-models/lineart --report C:/Tests/lineart-inference.json
```

The unit tests additionally cover missing/partial files, corrupt binaries,
unreadable files, LFS pointers, installation recovery, SHA-256 validation and
rejection before CAD queueing. The main source archive excludes `.pt` files;
old Git history is retained, so this change does not purge historical LFS storage
or reset the account's bandwidth usage.
