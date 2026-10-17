# Installation and configuration

[Project overview](../README.md) · [Companion skill](../skills/solidworks-mcp/SKILL.md)

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

Start SOLIDWORKS and reconnect the MCP client. Begin ordinary modeling with
`get_model_context`. For environment diagnostics, discover and call
`get_capabilities` or `get_environment_status` through `expert_tools`.

### Stable compatibility identifiers

`SolidworksMCP` is the public project and MCP handshake name. The Python package
remains `solidworks_mcp`, the configured tool namespace remains `solidworks`,
and existing artifact schemas retain their `solidworks-mcp/...` identifiers.
Those internal names are compatibility contracts and are intentionally not
renamed.

## Safety rules

- Supply an absolute `save_path` before the first mutation of an unsaved
  document, or explicitly opt into disposable work with
  `allow_unsaved_document=true`.
- Treat `success` as execution evidence. Verify topology, body count, bounding
  boxes, volume, tolerances, and a readable independent artifact.
- Use `scope_bodies` and `expected_merge_bodies` for multibody cuts and bosses.
- Do not auto-confirm unknown dialogs. Raw-code watchdog recovery requires an
  active tracked call and exact expected text; prefer native dimension guards.
- Native MCP tool coordinates use the declared user unit, normally millimetres.
  Raw SOLIDWORKS COM coordinates use metres.
- Do not invoke a blind `Ctrl+8` after sketch operations. Version 6.5.32 already
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

## Install the companion skill

The repository ships [skills/solidworks-mcp](../skills/solidworks-mcp/SKILL.md),
including its ten linked reference files. It teaches the agent the draft/build/edit/
verify/export workflow, native checks, long-operation handling and recovery.
The skill complements the configured MCP server; copying it does not install or
start the server.

For Codex, copy the whole folder into the configured skills directory. From the
repository root, the following PowerShell commands use `CODEX_HOME` when set and
otherwise the default user directory. They stop if a skill already exists so a
local customized copy can be reviewed before replacement.

```powershell
$skillRoot = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME 'skills' } else { Join-Path $HOME '.codex/skills' }
$skillTarget = Join-Path $skillRoot 'solidworks-mcp'
if (Test-Path -LiteralPath $skillTarget) { throw 'Review the existing skill before replacing it.' }
New-Item -ItemType Directory -Force -Path $skillRoot | Out-Null
Copy-Item -LiteralPath .\skills\solidworks-mcp -Destination $skillTarget -Recurse
```

Keep the `reference/` directory beside `SKILL.md`. In other skill-aware clients,
use the client's supported skill directory and preserve this folder structure.
For clients without skill discovery, supply the main skill as modeling guidance
and let the agent read the linked references when needed.

To check the bundled skill against this server's current tool and constructor names:

```powershell
python tests/check_skill_docs.py --skill skills/solidworks-mcp
```
