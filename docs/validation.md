# Validation and known limits

[Project overview](../README.md) · [Bender case studies](bender.md)

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
`tests/live_request_receipts.py --output C:/Tests/Receipts.SLDPRT --report C:/Tests/receipts.json`
checks native replay across reopen/MCP restart, changed targets/payloads/state,
and the UI-only monitor on the shared owner.
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

The code snapshot published on October 17, 2026 passed all **704** unit and
regression tests. These COM-mocked and offline checks complement the recorded
native acceptance runs; they do not replace live validation on another machine.

The Bender case studies record earlier versions and identify their exact scope.
The 24-body case did not export. The nine-body case has a validated historical
export and separate assembly checks. Neither is presented as a new 6.30.0rc1 run.

To verify documentation and distributed evidence without connecting to CAD:

```powershell
python scripts/generate_tool_docs.py --check
python tests/check_skill_docs.py --skill skills/solidworks-mcp
python examples/bender/check_evidence.py
```
