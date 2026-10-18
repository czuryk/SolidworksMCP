# Installation and configuration

[Project overview](../README.md) · [Companion skill](../skills/solidworks-mcp/SKILL.md)

## Requirements

- Windows 10 or Windows 11.
- SOLIDWORKS 2025 or 2026, installed and licensed.
- Python 3.10 or newer; Python 3.11 is the acceptance-tested runtime.
- Deep vectorization additionally requires model weights and an NVIDIA CUDA
  environment. Ordinary CAD tools work without the weights.

The server drives the interactive SOLIDWORKS process. It is not a headless CAD
kernel, and an unknown modal dialog must be inspected by a human.

## Clone and install

Use **Code → Download ZIP** on GitHub and extract it, or clone normally.
The main checkout does not require Git LFS:

```powershell
git clone https://github.com/czuryk/SolidworksMCP.git
Set-Location SolidworksMCP
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

## Optional vectorization models

**Install weights only if you need image tracing.** Startup, parametric modeling,
editing, reference comparisons, inspection and export work without them. The
server does not download weights automatically or silently switch tracing backends.

### Line-art tracing: DexiNed + TEED

The `stroke_centerlines`, `stroke_edges` and `all_visible_edges` modes use two
verified TorchScript files from the separate
[SolidworksMCP-models repository](https://github.com/czuryk/SolidworksMCP-models):
`dexined_biped_v2.pt` and `teed_biped_5.pt` (about 142 MB together).

Download that repository with Git LFS, independently of the server:

```powershell
git lfs install
git clone https://github.com/czuryk/SolidworksMCP-models.git C:/Downloads/SolidworksMCP-models
git -C C:/Downloads/SolidworksMCP-models lfs pull
```

From the main SolidworksMCP project folder, install the downloaded files:

```powershell
python scripts/install_vectorization_models.py --from-dir C:/Downloads/SolidworksMCP-models
```

The installer verifies both SHA-256 hashes before replacing any existing files,
then copies them to `solidworks_mcp/models/lineart/`. It makes no network requests.
Original upstream training checkpoints are not interchangeable with these exports.
[Source and license notices](../solidworks_mcp/models/lineart/LICENSES.md).

Alternatively, keep the files in the separate repository and set
`SOLIDWORKS_MCP_LINEART_MODELS_DIR` in your MCP launch environment to its absolute
`lineart` directory. For example, add this beside `command` and `args`:

```json
"env": {
  "SOLIDWORKS_MCP_LINEART_MODELS_DIR": "C:\\Downloads\\SolidworksMCP-models\\lineart"
}
```

Reconnect MCP after changing its launch environment. Existing local weights in
the original server directory remain supported. When updating an older Git
checkout, Git may remove the formerly tracked weights; back them up first or
reinstall from the separate models repository.

The **main project ZIP works without LFS**. A source ZIP of the **models repository**
contains only pointer files unless its GitHub archive setting includes LFS objects.
The installer detects these pointers and asks for `git lfs pull` in that repository.
LFS bandwidth is shared across the owner's account: separating repositories
isolates a blocked model download from the main checkout but does not add quota.

### Region tracing: SAM 2.1 + ViTMatte

The default region backend (`outer_silhouette`, `silhouette_with_holes`,
`all_region_boundaries`, `guided_components`) uses different models from the
local Hugging Face cache. Install these only when you need region tracing:

```powershell
hf download facebook/sam2.1-hiera-large
hf download hustvl/vitmatte-base-composition-1k
```

These models also require CUDA and the compatible PyTorch/Transformers packages
from `requirements.txt`. The explicit `classical` backend does not use model
weights. It is diagnostic-only and requires an explicit
`commit.acknowledge_classical_backend=true` acknowledgement for CAD commit;
its supported modes and quality checks still apply.

### Check availability

Call `expert_tools` with `action="call"`, `name="get_capabilities"`. Inspect
`deep_vectorization.line_art` for per-file status, verified hashes, model directory
and CUDA readiness; `deep_vectorization.models_cached` reports region model caches.
The cache probe checks snapshot presence; actual loading also checks compatibility.

| Result | Meaning and action |
|---|---|
| `weights_status: ready` | Both line-art files have the expected hashes; `available` also requires CUDA. |
| `MODEL_WEIGHTS_MISSING` | A required file or region model cache is missing. The error lists it and links installation instructions. |
| `MODEL_WEIGHTS_INVALID` | A line-art file is corrupt, unreadable or an LFS pointer, or cached region models cannot load. Follow the reported recovery steps. |
| `CAPABILITY_UNAVAILABLE` | Required runtime support, such as CUDA, is unavailable. Read the error details. |

Missing line-art files and missing region snapshots are rejected before an
`image_to_sketch` request enters the CAD queue, including `force_commit`. No
sketch is created. Other tools remain usable. After installing files, check
capabilities again and retry the tracing request.

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
