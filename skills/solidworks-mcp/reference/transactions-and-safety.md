# Transactions, budgets, dimensions, and UI safety

Use this reference for expert compound mutations and raw API recovery.
`execute_model_code` already owns checkpoints and rollback; do not wrap it in
another transaction or repeat this expert preflight for ordinary shape work.
The objective is one semantic MCP
call, one bounded transaction, one verification phase, and either a complete
commit or a complete rollback.

## Contents

1. [Common mutation envelope](#common-mutation-envelope)
2. [Unsaved-document guard](#unsaved-document-guard)
3. [Budgets and stop-loss](#budgets-and-stop-loss)
4. [Run a transaction](#run-a-transaction)
5. [Execute a CAD plan](#execute-a-cad-plan)
6. [Create a parametric sketch](#create-a-parametric-sketch)
7. [Analyze degrees of freedom](#analyze-degrees-of-freedom)
8. [Batch dimensions without Modify dialogs](#batch-dimensions-without-modify-dialogs)
9. [UI watchdog and recovery](#ui-watchdog-and-recovery)
10. [Limitations and known issues](#limitations-and-known-issues)

## Common mutation envelope

Raw `execute_python` is not a transaction. Since 6.18.0 its failures retain
stdout/stderr/traceback and classify Python errors separately from COM HRESULTs.
Read `data.error.details.script_state`: `PARTIAL_EFFECTS_POSSIBLE` means the
script started; it is not proof of rollback. No post-failure CAD probe or
automatic retry runs. `COM_OBJECT_DISCONNECTED` may mean a stale object after
rebuild, not a crashed SolidWorks process. Reacquire current context before
deciding recovery. A script longer than one call answers `RUNNING` and is
collected with `wait_operation`; only a really unknown outcome follows the
quarantine protocol below.

High-level mutating tools accept some or all of this envelope. Automatic
recovery is always capped at one retry; leave it disabled unless the failure is
classified and the retry operation is idempotent:

```json
{
  "save_path": "D:\\parts\\stage-01.SLDPRT",
  "allow_unsaved_document": false,
  "idempotency_key": "stage-01-v3",
  "document_path": "D:\\parts\\stage-01.SLDPRT",
  "expected_revision": "<fresh context revision>",
  "budget": {
    "max_elapsed_sec": 90,
    "max_rebuilds_per_sketch": 2,
    "max_solver_time_per_sketch_sec": 15,
    "max_rollbacks_per_component": 2,
    "max_elapsed_without_saved_body_sec": 120,
    "warn_only": false
  },
  "ui_guard": {"enabled": true},
  "recovery": {"auto_recover": false}
}
```

Read `get_capabilities` for the exact fields accepted by the running version.
Public legacy requests with idempotency_key (and execute_cad_plan.plan_id)
require the active saved part document_path and fresh expected_revision.
The receipt binds document, starting revision and payload; reusing a key for
changed intent is a conflict. Identical retries return historical results only
while native post-state matches. Unkeyed legacy calls have no retry guarantee.
Shape create/edit/revert derives keys automatically. A repeat runs again when
nothing would be duplicated: a create that left no file, or a completed request
whose document is back at the exact saved state it started from (after
`mode="revert"`). An identical edit that failed is answered from its receipt
with "Not executed again"; a new key makes it a new attempt. get_request_status
is COM-free; never bypass a RUNNING/unknown receipt by guessing a new key.

## Unsaved-document guard

The first mutation of an unsaved Part is rejected with `DOCUMENT_UNSAVED`
unless one of these is explicit:

- `save_path`: absolute path for the durable document.
- `allow_unsaved_document=true`: intentional disposable work.

Do not set the override merely to make a call pass. A successful unsaved
session is not a recoverable artifact.

## Budgets and stop-loss

Supported budget controls include:

- `max_elapsed_sec`: overall server/transaction deadline.
- `max_rebuilds_per_sketch`: prevents repeated global rebuild loops.
- `max_solver_time_per_sketch_sec`: bounds constraint solving.
- `max_rollbacks_per_component`: prevents endless retry cycles.
- `max_elapsed_without_saved_body_sec`: demands material saved progress.
- `warn_only`: report a breach without aborting; keep false for acceptance.

A budget failure returns `BUDGET_EXCEEDED`, runtime counters, the failed stage,
and rollback status. It can still spend a few seconds collecting a diagnostic
snapshot and proving restoration.

## Run a transaction

```json
{
  "name": "base-body",
  "operations": [
    {
      "op": "create_parametric_sketch",
      "args": {
        "name": "Base_Profile",
        "plane": "Front",
        "unit": "mm",
        "entities": [{"id": "profile", "type": "circle", "center": [0, 0], "radius": 20}],
        "constraints": [{"type": "fixed", "entities": ["profile"]}],
        "dimensions": [],
        "validation": {"require_closed": true, "closed_contours": 1}
      }
    },
    {
      "op": "advanced_extrude",
      "args": {
        "sketch_name": "Base_Profile",
        "depth": 20,
        "unit": "mm",
        "feature_name": "Base_Boss",
        "expected_bbox": {"min": [-50, -25, 0], "max": [50, 25, 20]}
      }
    }
  ],
  "checkpoint": {"mode": "save_copy", "keep_last_n": 1},
  "save_policy": {"save_after_success": true, "save_checkpoint": true},
  "invariants": {
    "expected_new_features": ["Base_Profile", "Base_Boss"],
    "solid_body_count": {"min": 1},
    "no_modal_dialog": true
  },
  "budget": {"max_elapsed_sec": 90, "max_rebuilds_per_sketch": 2},
  "idempotency_key": "base-body-v1",
  "document_path": "D:\\parts\\stage-01.SLDPRT",
  "expected_revision": "<fresh context revision>"
}
```

The operation whitelist is returned by `get_capabilities`; it contains native
CAD operations only. Plans are limited to 100 operations. Raw Python and
recursive transactions are not accepted.

Available invariant shapes include:

- `expected_new_features`
- `required_bodies`
- `solid_body_count` as an integer or `{min,max}`
- `required_files`
- `no_modal_dialog`
- `sketch_status`

Checkpoint modes are `save_copy` and `none`. Supply `path` when a particular
location is required and `keep_last_n` for retention. A checkpoint save-copy
must not change the active document path. For a clean saved part the checkpoint
preserves exact native file bytes. Rollback restores those verified bytes to the
original path and reopens that path; matching feature/body names are not proof.
Dirty manual state must be saved first. No checkpoint means restoration is
unproven, not successful. Retention touches only matching owned manifests.

## Execute a CAD plan

Use `execute_cad_plan` when later steps depend on earlier outputs:

```json
{
  "plan_id": "plan-profile-boss-v1",
  "document_path": "D:\\parts\\stage-01.SLDPRT",
  "expected_revision": "<fresh context revision>",
  "operations": [
    {
      "op": "create_parametric_sketch",
      "args": {
        "name": "Plan_Profile",
        "plane": "Front",
        "entities": [{"id": "profile", "type": "circle", "center": [0, 0], "radius": 10}],
        "constraints": [{"type": "fixed", "entities": ["profile"]}],
        "validation": {"require_closed": true, "closed_contours": 1}
      }
    },
    {
      "op": "advanced_extrude",
      "when": {"step_success": 0},
      "args": {"sketch_name": "$steps.0.data.sketch_name", "depth": 12, "unit": "mm"}
    }
  ],
  "invariants": {"solid_body_count": {"min": 1}, "no_modal_dialog": true},
  "transaction": {"checkpoint_before": true, "rollback_on_failure": true}
}
```

References use zero-based `$steps.<index>...`. The `when` object is deliberately
limited to the keys `step_success`, `body_exists`, and `feature_exists`;
arbitrary code is not a condition language. `step_success` contains an earlier
zero-based step index. The entire plan shares rollback and invariants, and
`plan_id` is also its idempotency key.

## Create a parametric sketch

```json
{
  "name": "Bracket_Profile",
  "plane": "Front",
  "unit": "mm",
  "entities": [
    {"id": "bottom", "type": "line", "start": [-20, -10], "end": [20, -10]},
    {"id": "right", "type": "line", "start": [20, -10], "end": [20, 10]},
    {"id": "top", "type": "line", "start": [20, 10], "end": [-20, 10]},
    {"id": "left", "type": "line", "start": [-20, 10], "end": [-20, -10]}
  ],
  "constraints": [
    {"type": "horizontal", "entities": ["bottom"]},
    {"type": "horizontal", "entities": ["top"]},
    {"type": "vertical", "entities": ["left"]},
    {"type": "vertical", "entities": ["right"]},
    {"type": "coincident", "entities": ["bottom.end", "right.start"]},
    {"type": "coincident", "entities": ["right.end", "top.start"]},
    {"type": "coincident", "entities": ["top.end", "left.start"]},
    {"type": "coincident", "entities": ["left.end", "bottom.start"]},
    {"type": "fixed", "entities": ["bottom.start"]}
  ],
  "dimensions": [
    {"id": "width", "type": "length", "entity": "bottom", "value": 40, "text_position": [0, -15]},
    {"id": "height", "type": "length", "entity": "right", "value": 20, "text_position": [25, 0]}
  ],
  "equations": [],
  "solve": {"target": "fully_defined"},
  "validation": {"require_closed": true, "closed_contours": 1, "max_entities": 4},
  "transaction": {"rollback_on_failure": true},
  "idempotency_key": "bracket-profile-v1",
  "document_path": "D:\\parts\\stage-01.SLDPRT",
  "expected_revision": "<fresh context revision>"
}
```

Build entities, constraints, dimensions, and equations in one request. The
server orders constraints defensively and minimizes rebuilds. Valid output
modes include fully defined production geometry and explicitly acknowledged
reference/construction geometry; use the capability schema for exact names.

## Analyze degrees of freedom

Call `analyze_sketch_dof` after the batch solve when a sketch remains
under-defined. Use its entity-level free coordinates, connected components,
and recommendations. Do not add arbitrary dimensions one by one: that is slow
and frequently creates a minimal conflict that is harder to diagnose.

When the status is over-defined, use the returned conflict set as a diagnostic;
remove or revise the smallest conflicting relation group inside a new
transaction.

## Batch dimensions without Modify dialogs

Use `add_dimensions_batch` for many dimensions:

```json
{
  "sketch_name": "Dimension_Test",
  "unit": "mm",
  "dimensions": [
    {"id": "D_width", "type": "length", "entity": "bottom", "value": 20, "text_position": [0, -15]},
    {"id": "D_height", "type": "length", "entity": "right", "value": 12.3, "text_position": [25, 0]}
  ],
  "rebuild": "once",
  "suppress_modify_dialog": true,
  "rollback_on_failure": true,
  "guard_policy": "operation_scoped",
  "ui_guard": {"enabled": true}
}
```

The server temporarily suppresses the SolidWorks input-on-create preference,
creates all dimensions, rebuilds once, and restores the exact previous user
preference in `finally`. Verify returned dimension values and driven/driving
state. Preference restoration is an acceptance invariant, not cleanup trivia.

## UI watchdog and recovery

In 6.13, raw Python jobs use the shared STA executor and can attach a
process-isolated Win32-only monitor. Example ui_guard for a supported tracked
Modify-prone call:

```json
{
  "process_isolated": true,
  "auto_resolve_known": ["dimension_modify"],
  "expected_dialog_text": "D1@Dimension_Test",
  "caused_by": {"com_method": "AddHorizontalDimension2"}
}
```

The script must invoke that operation with injected com_get. The server assigns
the request identity, publishes a marker only while the call is active, waits
for clean monitor readiness, and disarms on return/cancellation/owner exit.
The monitor can accept at most one known matching dialog. A pre-existing,
unknown, wrong-text or untracked dialog is never automatically accepted.
Do not use process_isolated=false. Native dimension preference guards remain
the preferred route; the monitor does not add another CAD writer.

**Long operations (6.25.0).** A call waits at most `SOLIDWORKS_MCP_SYNC_WAIT_SEC`
(default 45 s); `execution_timeout_sec` only shortens that wait. A longer
operation returns `status: RUNNING` with `operation_id`; `wait_operation`
collects the original result. Expiry or client cancellation of the wait no
longer records `OUTCOME_UNKNOWN`, and a finished operation never needs
`reconcile_session`. The paragraphs below apply only to a really unknown
outcome: dead owner process, killed native client, watchdog abandonment, or a
failed mutation with an unconfirmed rollback (`ROLLBACK_UNCONFIRMED`).

`execution_timeout_sec` only shortens the wait of one call; it is not native
cancellation or a rollback guarantee. A request that never left the queue is
answered `CAD_SESSION_BUSY` with the running `operation_id` and was not
executed. Use COM-free `get_executor_status`: `busy: true` is ordinary running
work, `quarantined: true` an unknown outcome that blocks CAD reads too.

For a quarantined session pass the journal operation_id to `reconcile_session`.
It inspects the current native document and reports the actual revision; it does
not replay or validate the original design intent. If the COM owner died
(`owner_alive: false`) or a native capture client was killed, a verified
SolidWorks process restart is required first: `expert_tools` →
`restart_solidworks`. It lists open documents, refuses while any is unsaved
unless `discard_unsaved=true`, and returns the previous and new process ids.
An MCP restart alone cannot establish that native work has stopped. Every
mutation records its checkpoint path, source hash and starting revision in the
session journal.

## Limitations and known issues

- Owner-drawn SolidWorks controls may expose little text. Classification then
  depends on operation context plus causal identity; weak evidence is not
  auto-resolved.
- The serial owner is a Windows STA thread. An isolated native capture child
  runs under its parent lease; killing that child leaves the outcome unknown.
- Screenshot capture adds recovery latency; sub-second recovery is normal and
  preferable to losing the evidence.
- A force-killed worker may not execute session-restoration code. Always verify
  the preference, selection, active sketch, and saved document after recovery.
- `operation_scoped` guards cover the active job, not arbitrary future UI.
- An external crash or OS/power loss can still interrupt a checkpoint write.
- Native receipts do not cover arbitrary external side effects of raw code or
  exports, and do not prove design or print acceptance.
