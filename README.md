# SolidworksMCP

## Major Update

From a short description and reference images to **native, editable SOLIDWORKS
models**. This update brings a new modeling workflow, a companion agent skill,
and measured checks from the first draft to the final delivery.

### 1. Build complete parametric models

Describe the model in a compact Python shape program. Preview its proportions
before building, then create named solids, native features and equations in
SOLIDWORKS. Keep dimensions as parameters so the saved model can evolve.

### 2. Edit with confidence

Change parameters or patch just the intended part of the program. Failed edits
restore the saved model; revert takes you back to an earlier state. Long
operations report progress, and completed results survive an MCP reconnect.

### 3. Deliver a result you can check

Compare the built model with reference images, measure fits and inspect native
body health. Export SLDPRT, STEP and per-body STL with validation results and
file hashes. See exactly what passed and what still needs checking.

[Get started](docs/setup.md) · [Install the skill](docs/setup.md#install-the-companion-skill) ·
[Modeling guide](docs/modeling-guide.md) · [All tools](docs/tools.md)

## Built with SolidworksMCP

![Bender built as native SOLIDWORKS solids](docs/assets/bender/appearance-hero.png)

**Bender, 150 mm.** A compact program builds the visor, convex eyes, curved teeth,
access door, limbs and hands as native solids. Front and side comparisons measure
the result against the approved references.

| Detailed character | Glue assembly |
|---|---|
| 24 healthy native bodies | Nine separate bodies with locating joints |
| Every sampled silhouette level within 1.5 mm | Recorded height change preserves the 0.25 mm joint gaps |
| Native views and comparison overlays | Validated SLDPRT, STEP and nine STL exports |

[See the pictures and measured results](docs/bender.md) ·
[Try the original programs](examples/bender/README.md)

## Start modeling

[Install the server](docs/setup.md) and the included
[solidworks-mcp skill](skills/solidworks-mcp/SKILL.md), then connect your MCP client
to SOLIDWORKS. The skill guides the agent through drafting, building, editing,
verification and export.

Start with `get_model_context` and `model_api_lookup(topic="all")`. A first shape
program can be as small as:

```python
Height = model.parameter("Height", 80)
model.revolve("Pedestal", [(0, 20), (Height / 4, 20), (Height, 8)])
```

Pass it to `execute_model_code` in `analyze` mode to preview, then `create` with a
new absolute `.SLDPRT` path to build. [Continue with the modeling guide](docs/modeling-guide.md).

## Explore

| You want to… | Open |
|---|---|
| Set up the server and companion skill | [Installation](docs/setup.md) |
| Learn the modeling and editing workflow | [Modeling guide](docs/modeling-guide.md) |
| Find any tool, its parameters or its replacement | [Complete tool reference](docs/tools.md) |
| Inspect Bender images, tests and source programs | [Case studies](docs/bender.md) |
| Read test coverage and remaining limits | [Validation](docs/validation.md) |
| Follow the release history | [Changelog](CHANGELOG.md) |

<details>
<summary>Version, compatibility and test scope</summary>

Current candidate: **6.30.0rc1**. Requires Windows and an installed, licensed
SOLIDWORKS session. The documented native fixtures were qualified on SOLIDWORKS
2026 SP2.1; Python 3.11 was tested. SOLIDWORKS 2025 compatibility guards remain,
but the new recipes have not been qualified on that version.

The published code snapshot passed **704 unit and regression tests**. The default
catalog has 13 tools, with 84 more expert operations available through discovery.
The full schemas are [versioned alongside the documentation](docs/tool-schemas.json).

The Bender examples are recorded October 5–7 runs on 6.27.0–6.29.5, not a fresh
6.30.0rc1 native acceptance run. The detailed character run cancelled export;
the glue-assembly run has a validated export and separate assembly checks.
Images, source and records are included; CAD binaries remain external.

Long CAD sessions can accumulate substantial memory. Export validation does not
certify every manufacturing property. [Recorded evidence and limitations](docs/validation.md).

</details>

<details>
<summary>Developer checks</summary>

```powershell
python -m unittest discover -s tests -p "test_*.py"
python scripts/generate_tool_docs.py --check
python tests/check_skill_docs.py --skill skills/solidworks-mcp
python examples/bender/check_evidence.py
```

The bundled line-art checkpoints use Git LFS. Documentation and Bender images
use ordinary Git.

</details>

## License

[MIT](LICENSE). Packaged model notices: [LICENSES.md](solidworks_mcp/models/lineart/LICENSES.md).
