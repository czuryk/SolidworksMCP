"""Generate the public tool reference from the server's schemas without CAD calls."""

import argparse
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from solidworks_mcp import __version__, server
from solidworks_mcp.tool_lifecycle import DEPRECATION_META_KEY


def cell(value):
    return html.escape(str(value), quote=False).replace("|", "\\|").replace("\n", " ")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def value_type(schema):
    if "type" in schema:
        value = schema["type"]
        return " / ".join(value) if isinstance(value, list) else value
    for union in ("oneOf", "anyOf"):
        if union in schema:
            return " / ".join(value_type(item) for item in schema[union])
    return "See schema"


def render():
    catalog = server._all_tools() + [server._expert_tool()]
    names = [tool.name for tool in catalog]
    if len(names) != len(set(names)):
        raise ValueError("Tool names must be unique")
    core_names = server.SHAPE_TOOL_NAMES | server.CORE_SESSION_TOOLS | {"expert_tools"}
    core = [tool for tool in catalog if tool.name in core_names]
    expert = sorted((tool for tool in catalog if tool.name not in core_names), key=lambda tool: tool.name)
    deprecated = [tool for tool in catalog if DEPRECATION_META_KEY in (tool.meta or {})]
    lines = [
        "# Tool reference", "",
        f"Generated from SolidworksMCP **{__version__}**: **{len(catalog)} tools**, "
        f"**{len(core)} in the default profile**, **{len(expert)} expert operations**, "
        f"including **{len(deprecated)} deprecated compatibility calls**.", "",
        "[Project overview](../README.md) · [Companion skill](../skills/solidworks-mcp/SKILL.md) · "
        "[Complete JSON schemas](tool-schemas.json)", "",
        "Descriptions, defaults and input constraints below come from the server code. "
        "Required means required by the JSON schema; descriptions can impose additional "
        "conditions, such as a saved document and a fresh revision for an edit. "
        "Nested structures, unions and complete validation rules are in `tool-schemas.json`, "
        "under `tools.<tool_name>.inputSchema`.", "",
        "## Calling tools", "",
        "Call a default-profile tool directly. For an expert operation, discover its name, "
        "read its schema, and pass its arguments through `expert_tools`:", "",
        "```json", '{"action": "list", "query": "color"}',
        '{"action": "describe", "name": "set_body_color"}', "```", "",
        "Then call `expert_tools` with `action=\"call\"`, `name` and an `arguments` object "
        "matching the described schema. Search is a case-insensitive literal substring, "
        "not a multi-keyword query. `SOLIDWORKS_MCP_TOOL_PROFILE=expert` exposes the full "
        "catalog directly. Existing direct names remain callable for compatibility.", "",
        "A long call can return `RUNNING`. Collect it with `wait_operation`; do not submit "
        "the mutation again. `get_executor_status` is available while CAD is busy. "
        "Use `reconcile_session` only for an unknown, quarantined outcome.", "",
        "## Default profile", "", "| Tool | Purpose |", "|---|---|",
    ]
    for tool in core:
        lines.append(f"| [`{tool.name}`](#{tool.name}) | {cell(tool.description)} |")
    lines += ["", "## Expert index", "", "| Tool | Lifecycle |", "|---|---|"]
    for tool in expert:
        lifecycle = (tool.meta or {}).get(DEPRECATION_META_KEY)
        status = "Deprecated; use " + ", ".join(f"`{name}`" for name in lifecycle["replacements"]) if lifecycle else "Supported"
        lines.append(f"| [`{tool.name}`](#{tool.name}) | {status} |")
    for heading, group in (("Default tool details", core), ("Expert tool details", expert)):
        lines += ["", f"## {heading}", ""]
        for tool in group:
            lines += [f"### {tool.name}", "", html.escape(tool.description, quote=False), ""]
            lifecycle = (tool.meta or {}).get(DEPRECATION_META_KEY)
            if lifecycle:
                lines += ["**Migration:**", "", "```json", json.dumps(lifecycle, indent=2, ensure_ascii=False), "```", ""]
            properties = tool.inputSchema.get("properties", {})
            required = set(tool.inputSchema.get("required", []))
            if properties:
                lines += ["| Input | Type | Required | Default | Description and constraints |", "|---|---|---|---|---|"]
                for name, schema in properties.items():
                    details = [schema.get("description", "")]
                    details += [f"{key}: `{json_text(schema[key])}`" for key in
                                ("enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems", "pattern", "uniqueItems") if key in schema]
                    if any(key in schema for key in ("items", "properties", "oneOf", "anyOf", "allOf", "$ref")):
                        details.append("Nested definition: see complete JSON schema.")
                    default = "`" + json_text(schema["default"]) + "`" if "default" in schema else "—"
                    lines.append(f"| `{name}` | {cell(value_type(schema))} | {'Yes' if name in required else 'No'} | {cell(default)} | {cell(' '.join(filter(None, details)))} |")
                lines += [""]
            else:
                lines += ["No named inputs in the schema.", ""]
    lines += ["## Maintaining this reference", "", "```powershell", "python scripts/generate_tool_docs.py", "python scripts/generate_tool_docs.py --check", "```", "", "Generation only reads tool definitions. It does not connect to or modify SOLIDWORKS.", ""]
    schemas = {"server_version": __version__, "default_profile": [tool.name for tool in core],
               "tools": {tool.name: tool.model_dump(exclude_none=True, by_alias=True) for tool in sorted(catalog, key=lambda tool: tool.name)}}
    return {ROOT / "docs/tools.md": "\n".join(lines),
            ROOT / "docs/tool-schemas.json": json.dumps(schemas, indent=2, ensure_ascii=False) + "\n"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if committed documentation differs from current schemas")
    args = parser.parse_args()
    stale = []
    for path, content in render().items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    if stale:
        print("Stale tool documentation: " + ", ".join(stale))
        return 1
    print("Tool documentation " + ("is current." if args.check else "generated."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
