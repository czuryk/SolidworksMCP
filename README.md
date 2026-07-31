# SolidworksMCP

**SolidworksMCP** is a production-oriented Model Context Protocol server for
controlling a live SOLIDWORKS session through the Windows COM API.

Current release: **6.5.31**. The release was acceptance-tested with
**SOLIDWORKS 2026 SP2.1** and retains compatibility guards for SOLIDWORKS 2025.

## Highlights

- Native sketch, feature, body, reference-geometry, view, probe, and export
  operations.
- Transactional CAD plans with checkpoints, invariants, idempotency, budgets,
  and rollback.
- Parametric sketches with named entities, constraints, dimensions, equations,
  persistent IDs, and degree-of-freedom analysis.
- Multibody tools with explicit merge scope, clearance, interference, bounding
  box, and volume verification.
- Deterministic raster-to-sketch pipelines for silhouettes and line art,
  including GPU-backed segmentation, topology preservation, primitive fitting,
  and reverse-raster quality gates.
- Native sketch/body export and calibrated geometric comparison instead of
  screenshot-only acceptance.
- Verified active-sketch Normal To and Fit to Screen. The server reads the
  resulting camera matrix and pixel occupancy instead of trusting a UI command.
- Structured diagnostics, modal-dialog preflight, a narrowly scoped Modify
  watchdog, and finite recovery policies.

## Requirements

- Windows 10 or Windows 11.
- SOLIDWORKS 2025 or 2026, installed and licensed.
- Python 3.10 or newer; Python 3.11 is the acceptance-tested runtime.
- Git LFS for the packaged line-art checkpoints.
- An NVIDIA CUDA environment is strongly recommended for the deep
  vectorization modes. `get_capabilities` reports the actual backend state.

The server drives the interactive SOLIDWORKS process. It is not a headless CAD
kernel, and an unknown modal dialog must be inspected by a human.

## Clone and install

Install Git LFS before cloning, or pull the model objects afterwards:

```powershell
git lfs install
git clone https://github.com/czuryk/SolidworksMCP.git
Set-Location SolidworksMCP
git lfs pull
```

Create an isolated Python environment and install dependencies:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

For CUDA vectorization, install the PyTorch build appropriate for the machine's
CUDA driver before or together with `requirements.txt`.

## MCP configuration

Point the MCP client at the repository entry point using the virtual
environment's Python executable:

```json
{
  "mcpServers": {
    "SolidworksMCP": {
      "command": "C:\\path\\to\\SolidworksMCP\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\path\\to\\SolidworksMCP\\solidworks_mcp_server.py"
      ]
    }
  }
}
```

Start SOLIDWORKS, restart or reconnect the MCP client, then call
`get_capabilities` and `get_environment_status` before a substantial modeling
operation.

### Stable compatibility identifiers

`SolidworksMCP` is the public project and MCP handshake name. The Python package
remains `solidworks_mcp`, the configured tool namespace remains `solidworks`,
and existing artifact schemas retain their `solidworks-mcp/...` identifiers.
Those internal names are compatibility contracts and are intentionally not
renamed.

## Operation families

The server exposes both small native operations and higher-level verified
workflows. Important families include:

- documents and environment: create/open/save/close, capabilities, session
  metrics, modal-state inspection, recovery;
- sketches: standard planes, planar faces, exact model-coordinate contours,
  primitive drawing, parametric sketches, dimensions, constraints, equations;
- features: verified extrude/cut, revolve, sweep, fillet, chamfer, shell,
  mirrors, patterns, reference axes and planes;
- bodies: visibility, naming, appearance, volume, clearance, interference,
  multibody insert workflows;
- verification: ray/section probes, sketch topology, native geometry export,
  sketch-to-sketch and calibrated image comparisons, body silhouettes;
- vectorization: selectable silhouette/region/line-art modes, deep segmentation,
  controllable approximation, CAD commit, and reverse-raster verification;
- orchestration: transactions, declarative CAD plans, semantic primitives,
  model-graph synchronization, atomic export bundles.

Use MCP tool discovery for the authoritative live schema. Tool descriptions and
`get_capabilities` are versioned with the server.

## Safety rules

- Supply an absolute `save_path` before the first mutation of an unsaved
  document, or explicitly opt into disposable work with
  `allow_unsaved_document=true`.
- Treat `success` as execution evidence. Verify topology, body count, bounding
  boxes, volume, tolerances, and a readable independent artifact.
- Use `scope_bodies` and `expected_merge_bodies` for multibody cuts and bosses.
- Do not auto-confirm unknown dialogs. Only the recognized, operation-scoped
  dimension Modify dialog may be handled by the watchdog.
- Native MCP tool coordinates use the declared user unit, normally millimetres.
  Raw SOLIDWORKS COM coordinates use metres.
- Do not invoke a blind `Ctrl+8` after sketch operations. Version 6.5.31 already
  applies and verifies the stable side/up orientation and working-geometry fit.

## Vectorization models

The deterministic line-art checkpoints live under
`solidworks_mcp/models/lineart/` and are stored with Git LFS:

- `dexined_biped_v2.pt`
- `teed_biped_5.pt`

Their source and license notices are documented in
`solidworks_mcp/models/lineart/LICENSES.md`. A clone containing small LFS pointer
files instead of the binary objects is incomplete; run `git lfs pull` before
starting the server.

SOLIDWORKS Autotrace/Picture to Sketch is an interactive PropertyManager feature
and has no supported public API entry point. This server therefore uses its own
deterministic segmentation and line-art pipelines.

## Configuration

Default runtime settings are stored in `solidworks_mcp/config.json`. Keep
machine-specific or sensitive overrides in `config.local.json`; that file is
ignored by Git.

Notable defaults include:

- automatic SOLIDWORKS executable and template discovery;
- millimetres as the user unit;
- operation-scoped dimension dialog guard;
- bounded transaction/runtime limits;
- automatic checkpoint directory management;
- INFO-level file logging.

Relative `log_file` values are written under
`%LOCALAPPDATA%\SolidworksMCP\`, not into the source checkout. An absolute path
in `config.json` remains supported.

## Tests

The repository contains the deterministic unit and regression suite:

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Release 6.5.31 passes **155 tests**. These tests do not replace live acceptance
for COM behavior: qualifying another SOLIDWORKS major version should also cover
standard planes, planar faces, existing-sketch activation, view recovery,
features, transactions, vectorization, and export.

## Project layout

```text
SolidworksMCP/
|-- solidworks_mcp_server.py       MCP stdio entry point
|-- requirements.txt
|-- LICENSE
|-- solidworks_mcp/
|   |-- server.py                  MCP schemas and dispatch
|   |-- tool_registry.py           high-level tool metadata
|   |-- vector_worker.py           isolated scientific worker
|   |-- config.py / config.json
|   |-- constants.py
|   |-- automation/                SOLIDWORKS COM implementation
|   |-- models/lineart/            Git-LFS checkpoints and licenses
|   `-- utils/
`-- tests/
    `-- test_v6.py                 unit and regression suite
```

Runtime logs, caches, local client settings, checkpoints, generated reports,
acceptance artifacts, and manual probes are intentionally excluded from the
repository.

## License

The server is distributed under the MIT License; see `LICENSE`. Packaged model
notices are retained separately in `solidworks_mcp/models/lineart/LICENSES.md`.

Original project: [alisamsam/Solidworks-MCP](https://github.com/alisamsam/Solidworks-MCP).
